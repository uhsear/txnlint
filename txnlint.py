#!/usr/bin/env python
"""Find the T-SQL transaction mistakes that only show when a script is read across its GO batches.

A bare DELETE commits by itself, the INSERT that refills the table fails, and
the table stays empty with no error. SET XACT_ABORT ON sits at the top and
protects nothing, because no transaction is open. A CATCH block writes its log
row before the ROLLBACK, and the write fails or is undone. A guard that
THROWs stops its own batch, and the next GO sends the batch after it anyway.

Linters that read one statement at a time cannot see these, because each
statement is fine. The mistake is in how the statements sit between BEGIN,
COMMIT, ROLLBACK, CATCH and GO. Other T-SQL linters exist and the README names
them. Some follow BEGIN TRAN or XACT_ABORT across GO. None of the ones read
has the four rules below. This is one standard-library file that has them, and
it says which line is wrong.

    python txnlint.py --self-test
    python txnlint.py deploy.sql
    python txnlint.py sql_folder/
    python txnlint.py sql_folder/ --rules R1,R2
    python txnlint.py deploy.sql --unc-host backupsrv --unc-host filesrv
    python txnlint.py sql_folder/ --out findings.csv --apply

R1 a DELETE or TRUNCATE outside a transaction, then an INSERT into the same table.
R2 SET XACT_ABORT ON, with two or more writes outside any transaction.
R3 a write in a CATCH block before the ROLLBACK that follows it.
R5 a THROW or RAISERROR guard, then a write that runs although the guard fired:
   a write in a later GO batch, or after a RAISERROR with no RETURN.
R6 a UNC path in a string with the wrong number of leading backslashes for a
   host you named, or a UNC path to a host you did not name. It runs only when
   you name hosts with --unc-host, because a lost backslash cannot be told
   from a real path without that list.
E0 a quote or comment that is never closed. The rest of the file is unread.

Nothing is written without --apply. Exit codes: 0 no finding, 1 at least one
finding, 2 a file or folder could not be read or the report could not be
written, 3 no finding but some code was not read (EXEC of a string that may
hold a write, sp_executesql, :r), 64 usage error.
"""

from __future__ import print_function

import argparse
import collections
import contextlib
import csv
import importlib.util
import io
import os
import re
import shutil
import sys
import tempfile

DEFAULT_RULES = ("R1", "R2", "R3", "R5")
ALL_RULES = ("R1", "R2", "R3", "R5", "R6")
PIN = "  <-- pinned defect"

Tok = collections.namedtuple("Tok", "kind val u line batch")
Ev = collections.namedtuple("Ev", "kind line batch name extra ctx")
Finding = collections.namedtuple("Finding", "path line batch rule message")

_EOF = Tok("e", "", "", 0, 0)

# A GO line holds nothing but GO, an optional count and an optional comment.
_GO_RE = re.compile(r"[ \t]*GO(?:[ \t]+[0-9]+)?[ \t]*(?:--[^\n]*|/\*[^\n]*?\*/[ \t]*)?(?=\n|\Z)",
                    re.IGNORECASE)
# A sqlcmd command starts a line with a colon and a letter. "::" does not match.
_CMD_RE = re.compile(r"[ \t]*:[ \t]*([A-Za-z]+)([^\n]*)")
# A sqlcmd scripting variable such as $(Schema) is part of a name: Microsoft's own
# example writes SELECT x.$(ColumnName), so the tool must read $(Schema).Lookup as a table.
_VAR = r"\$\([^\s()'\"]+\)"
_WORD_RE = re.compile(r"(?:[@#]{0,2}[^\W\d]|" + _VAR + r")(?:" + _VAR + r"|[\w$@#])*")
_NUM_RE = re.compile(r"[0-9][\w.]*")
# A backslash run, then a host name, then another backslash. The look-behind
# keeps the match at the start of a path, so C:\host and mid-word runs do not count.
_UNC_RE = re.compile(r"(?<![A-Za-z0-9_.$:\\-])(\\*)([A-Za-z0-9_][A-Za-z0-9_.$-]*)(?=\\)")
_HOST_OK = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.$-]*$")

# A write verb that follows one of these words or marks is not a statement.
# ON is not here: SET NOCOUNT ON and SET XACT_ABORT ON end in ON, and a write may follow
# them with no semicolon. Only ON DELETE and ON UPDATE of a foreign key are tested apart.
_NOT_AFTER = frozenset(["FOR", "AFTER", "OF", "THEN", "GRANT", "DENY",
                        "REVOKE", "WITH", ",", "("])
_REF_ACTION = frozenset(["CASCADE", "NO", "SET"])
# These words start a statement, so an IF condition cannot run past one at the top level.
_STMT_WORDS = frozenset(["SELECT", "SET", "DECLARE", "EXEC", "EXECUTE", "PRINT", "RETURN",
                         "COMMIT", "ROLLBACK", "THROW", "RAISERROR", "INSERT", "UPDATE",
                         "DELETE", "MERGE", "TRUNCATE", "BEGIN", "END", "GOTO"])
_MODULES = frozenset(["PROC", "PROCEDURE", "FUNCTION", "TRIGGER"])
_WRITES = frozenset(["INSERT", "UPDATE", "DELETE", "MERGE", "TRUNCATE"])
_TRAN_WORDS = ("TRAN", "TRANSACTION")
_TRAN_BEGIN = _TRAN_WORDS + ("DISTRIBUTED",)
# These words end a DELETE or UPDATE that has no semicolon, when we look for its alias.
_STOP = frozenset(["IF", "WHILE", "BEGIN", "END", "DECLARE", "EXEC", "EXECUTE", "PRINT",
                   "RETURN", "COMMIT", "ROLLBACK", "THROW", "RAISERROR", "SET"])
_NOT_ALIAS = frozenset(["WHERE", "ON", "JOIN", "INNER", "LEFT", "RIGHT", "FULL", "CROSS",
                        "OUTER", "WITH", "SET", "GROUP", "ORDER", "OPTION", "UNION",
                        "OUTPUT", "USING"])
_VERB_FORM = {"DELETE": "DELETE FROM", "TRUNCATE": "TRUNCATE TABLE"}


def safe(text):
    """ASCII-only text, so a console that cannot print a name does not crash the run."""
    return str(text).encode("ascii", "backslashreplace").decode("ascii")


# ----------------------------------------------------------------- the lexer

def tokenize(text):
    """Return (tokens, problems). Comments are dropped and every GO starts a new batch.

    A problem is (line, batch, message). The lexer stops at the first one,
    because after an unclosed quote nothing that follows can be trusted.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    toks, problems = [], []
    i, n, line, batch = 0, len(text), 1, 1
    bol = True
    while i < n:
        if bol:
            bol = False
            m = _GO_RE.match(text, i)
            if m:
                batch += 1
                i = m.end()
                continue
            m = _CMD_RE.match(text, i)
            if m:
                words = m.group(2).lower().split()
                if m.group(1).lower() == "on" and words[:1] == ["error"] and \
                        words[1:2] in (["exit"], ["ignore"]):
                    toks.append(Tok("c", words[1], words[1].upper(), line, batch))
                elif m.group(1).lower() == "r":
                    # :r pulls in another file. The tool does not read it, and says so.
                    toks.append(Tok("x", "r", "R", line, batch))
                i = m.end()
                continue
        c = text[i]
        if c == "\n":
            line += 1
            i += 1
            bol = True
        elif c in " \t\f\v":
            i += 1
        elif text.startswith("--", i):
            j = text.find("\n", i)
            i = n if j < 0 else j
        elif text.startswith("/*", i):
            j, depth = i, 0
            while j < n:
                if text.startswith("/*", j):
                    depth += 1
                    j += 2
                elif text.startswith("*/", j):
                    depth -= 1
                    j += 2
                    if depth == 0:
                        break
                else:
                    j += 1
            if depth:
                problems.append((line, batch, "a block comment is never closed, so the rest "
                                 "of the file cannot be read"))
                break
            line += text.count("\n", i, j)
            i = j
        elif c in "'[\"":
            close = "]" if c == "[" else c
            j, k, parts = i + 1, -1, []
            while True:
                k = text.find(close, j)
                if k < 0:
                    break
                parts.append(text[j:k])
                if text.startswith(close + close, k):
                    parts.append(close)
                    j = k + 2
                else:
                    j = k + 1
                    break
            if k < 0:
                problems.append((line, batch, "a quote opened here is never closed, so the "
                                 "rest of the file cannot be read"))
                break
            toks.append(Tok("s" if c == "'" else "i", "".join(parts), "", line, batch))
            line += text.count("\n", i, j)
            i = j
        else:
            m = _WORD_RE.match(text, i)
            if m:
                w = m.group()
                i = m.end()
                # N'text' is a literal with a prefix, not a word.
                if not (w in ("N", "n") and text.startswith("'", i)):
                    toks.append(Tok("w", w, w.upper(), line, batch))
                continue
            m = _NUM_RE.match(text, i)
            if m:
                toks.append(Tok("n", m.group(), m.group().upper(), line, batch))
                i = m.end()
            else:
                toks.append(Tok("p", c, c, line, batch))
                i += 1
    return toks, problems


# ------------------------------------------------------------- event scanner

def is_p(t, ch):
    return t.kind == "p" and t.val == ch


def at(toks, j):
    """The token at j, or an end-of-input token."""
    return toks[j] if 0 <= j < len(toks) else _EOF


def paren_args(toks, j):
    """toks[j] is "(". Return (argument token lists, index after the matching ")")."""
    depth, args, cur = 0, [], []
    n = len(toks)
    while j < n:
        t = toks[j]
        j += 1
        if t.kind == "p" and t.val == "(":
            depth += 1
            if depth == 1:
                continue
        elif t.kind == "p" and t.val == ")":
            depth -= 1
            if depth == 0:
                args.append(cur)
                return args, j
        elif t.kind == "p" and t.val == "," and depth == 1:
            args.append(cur)
            cur = []
            continue
        cur.append(t)
    args.append(cur)
    return args, j


def read_name(toks, j):
    """A dotted object name starting at j. Return (lower-case name, next index)."""
    parts = []
    while at(toks, j).kind in ("w", "i"):
        parts.append(toks[j].val.lower())
        j += 1
        dots = 0
        while at(toks, j).kind == "p" and toks[j].val == ".":
            dots += 1
            j += 1
        if not dots:
            break
        parts.extend([""] * (dots - 1))
    return ".".join(parts), j


def write_target(toks, k):
    """The table a write verb at k names, or "" when the name cannot be read."""
    j = k + 1
    if toks[k].u == "TRUNCATE":
        j += 1
    elif at(toks, j).u == "TOP":
        j += 1
        if is_p(at(toks, j), "("):
            j = paren_args(toks, j)[1]
    if at(toks, j).u in ("FROM", "INTO"):
        j += 1
    name, j = read_name(toks, j)
    follow = at(toks, j).u
    if name and "." not in name and not is_scratch(name) and (
            (toks[k].u == "DELETE" and follow == "FROM")
            or (toks[k].u == "UPDATE" and follow == "SET")):
        # The SET that follows an UPDATE target is part of that statement, not a new one.
        name = resolve_alias(toks, k, j + 1 if follow == "SET" else j, name)
    return name


def resolve_alias(toks, k, j, alias):
    """The table behind DELETE x FROM dbo.t AS x or UPDATE x SET ... FROM dbo.t x.

    Looks through the rest of the same statement. Returns the alias itself when
    no FROM or JOIN gives it a table, for example when it names a derived table.
    """
    while j < len(toks):
        t = toks[j]
        if (t.batch != toks[k].batch or t.u in _STOP or is_p(t, ";")
                or (t.u in _WRITES and is_statement_start(toks, j))):
            break
        if t.u in ("FROM", "JOIN"):
            name, j2 = read_name(toks, j + 1)
            if at(toks, j2).u == "AS":
                j2 += 1
            a = at(toks, j2)
            if name and a.kind == "w" and a.u not in _NOT_ALIAS and a.val.lower() == alias:
                return name
        j += 1
    return alias


def is_statement_start(toks, k):
    """True when the write verb at k begins a statement, not a clause of another."""
    t, prev, nxt = toks[k], at(toks, k - 1), at(toks, k + 1)
    if prev.kind in ("w", "p") and prev.u in _NOT_AFTER:
        return False
    # ON DELETE CASCADE, ON UPDATE NO ACTION and ON DELETE SET NULL are foreign key clauses.
    if prev.u == "ON" and t.u in ("DELETE", "UPDATE") and nxt.u in _REF_ACTION:
        return False
    if t.u == "UPDATE":
        return not is_p(nxt, "(") and nxt.u != "STATISTICS"
    if t.u == "MERGE":
        return nxt.u != "JOIN"
    if t.u == "TRUNCATE":
        return nxt.u == "TABLE"
    return True


def raise_is_guard(toks, k):
    """RAISERROR is a guard unless its severity is a literal below 11."""
    if not is_p(at(toks, k + 1), "("):
        return True
    args = paren_args(toks, k + 1)[0]
    sev = args[1] if len(args) > 1 else []
    # isdecimal, not isdigit: a superscript digit passes isdigit and then fails int().
    if len(sev) != 1 or sev[0].kind != "n" or not sev[0].val.isdecimal():
        return True
    return int(sev[0].val) >= 11


def governed_by_if(toks, k):
    """True when the statement at k is the body of an IF, ELSE or WHILE with no BEGIN.

    Also true when k is a BEGIN that opens the body of one. The search runs back
    to the start of the statement and skips parenthesised conditions.
    """
    depth, j = 0, k - 1
    while j >= 0 and toks[j].batch == toks[k].batch:
        t = toks[j]
        if is_p(t, ")"):
            depth += 1
        elif is_p(t, "("):
            depth = max(0, depth - 1)
        elif depth == 0:
            if is_p(t, ";"):
                return False
            if t.kind == "w":
                if t.u in ("IF", "ELSE", "WHILE"):
                    return True
                # UPDATE(col) in a trigger is a function that an IF may test.
                if t.u in _STMT_WORDS and not (t.u == "UPDATE" and is_p(at(toks, j + 1), "(")):
                    return False
        j -= 1
    return False


def leaves_batch(toks, k):
    """True when RAISERROR at k is followed by an unconditional RETURN or GOTO before any write.

    RAISERROR does not end a batch, so without this the statements after it still run.
    The search stays inside the block that holds the RAISERROR. A RETURN that is the
    body of an IF does not count, because it runs only when the condition is true.
    """
    depth = 0
    for j in range(k + 1, len(toks)):
        t = toks[j]
        if t.batch != toks[k].batch:
            return False
        if t.u == "CASE" or (t.u == "BEGIN" and at(toks, j + 1).u not in _TRAN_BEGIN):
            depth += 1
        elif t.u == "END":
            depth -= 1
            if depth < 0:
                return False
        elif depth == 0 and t.u in ("RETURN", "GOTO") and not governed_by_if(toks, j):
            return True
        elif t.u in _WRITES and is_statement_start(toks, j):
            return False
    return False


_WRITE_WORD = re.compile(r"\b(?:INSERT|UPDATE|DELETE|MERGE|TRUNCATE)\b", re.IGNORECASE)


def unread(toks):
    """Places where code that may hold a write is not read: (line, batch, message).

    EXEC of a string that holds a write verb or is not a plain literal, sp_executesql
    with such a string, and :r. EXEC of a procedure is not listed: its body is a
    different script. A string that holds no write verb is harmless to R1, R2, R3 and R5.
    """
    out = []
    for k, t in enumerate(toks):
        body = []
        if t.kind == "x":
            out.append((t.line, t.batch, ":r includes a file that is not read"))
        elif t.u in ("EXEC", "EXECUTE") and is_p(at(toks, k + 1), "("):
            body = [x for arg in paren_args(toks, k + 1)[0] for x in arg]
        elif t.u == "SP_EXECUTESQL":
            body = [at(toks, k + 1)]
        if any(x.kind in ("w", "i") or (x.kind == "s" and _WRITE_WORD.search(x.val))
               for x in body):
            out.append((t.line, t.batch, "%s runs a string that is not read, and it may "
                        "hold a write" % t.u))
    return out


def scan(toks):
    """Turn tokens into the events the rules read, in file order."""
    evs, stack = [], []
    for k, t in enumerate(toks):
        if t.kind == "c":
            evs.append(Ev("sqlcmd", t.line, t.batch, "", t.val == "exit", ""))
            continue
        if t.kind != "w":
            continue
        ctx = ""
        for s in reversed(stack):
            if s in ("try", "catch"):
                ctx = s
                break

        def add(kind, name="", extra=None):
            evs.append(Ev(kind, t.line, t.batch, name, extra, ctx))

        nxt = at(toks, k + 1)
        if t.u == "BEGIN":
            if nxt.u == "TRY":
                stack.append("try")
                add("try")
            elif nxt.u == "CATCH":
                stack.append("catch")
                add("catch")
            elif nxt.u in _TRAN_BEGIN:
                add("begin")
            elif governed_by_if(toks, k):
                stack.append("cblock")
            else:
                stack.append("block")
        elif t.u == "CASE":
            stack.append("case")
        elif t.u == "END":
            if nxt.u in ("TRY", "CATCH"):
                want = nxt.u.lower()
                while stack and stack.pop() != want:
                    pass
                add("end" + want)
            elif stack:
                stack.pop()
        elif t.u == "COMMIT":
            add("commit")
        elif t.u == "ROLLBACK":
            # extra is True when the ROLLBACK is conditional: the early-exit idiom
            # IF cond BEGIN ROLLBACK; RETURN; END must not end the transaction for the
            # statements after it, because they are reached only when cond is false.
            add("rollback", at(toks, k + 2).val.lower() if nxt.u in _TRAN_WORDS else "",
                "cblock" in stack or governed_by_if(toks, k))
        elif t.u == "SAVE" and nxt.u in _TRAN_WORDS:
            add("save", at(toks, k + 2).val.lower())
        elif t.u == "XACT_STATE":
            add("xstate")
        elif t.u == "THROW":
            add("guard", "THROW", False)
        elif t.u == "RAISERROR":
            if raise_is_guard(toks, k):
                # extra is True when the statements after the RAISERROR still run.
                add("guard", "RAISERROR", not leaves_batch(toks, k))
        elif t.u == "SET":
            if nxt.u in ("XACT_ABORT", "IMPLICIT_TRANSACTIONS") and \
                    at(toks, k + 2).u in ("ON", "OFF"):
                add("xact" if nxt.u == "XACT_ABORT" else "implicit", "", toks[k + 2].u == "ON")
        elif t.u in ("CREATE", "ALTER"):
            j = k + 1
            if t.u == "CREATE" and at(toks, j).u == "OR" and at(toks, j + 1).u == "ALTER":
                j += 2
            if not (t.u == "ALTER" and at(toks, k - 1).u == "OR") and at(toks, j).u in _MODULES:
                add("module")
        elif t.u in _WRITES and is_statement_start(toks, k):
            add("write", write_target(toks, k), t.u)
    # ROLLBACK TRAN name undoes only part of a transaction when name is a savepoint.
    saves = set()
    for i, e in enumerate(evs):
        if e.kind == "save":
            saves.add(e.name)
        elif e.kind == "rollback" and e.name in saves:
            evs[i] = e._replace(kind="partial")
    return evs


def units(evs):
    """Group events. A batch that creates a module is its own unit.

    Every other batch joins one script unit, because a transaction and the
    XACT_ABORT setting last across GO but a stored procedure body is its own scope.
    """
    mod_batches = set(e.batch for e in evs if e.kind == "module")
    script, mods = [], {}
    for e in evs:
        if e.batch in mod_batches:
            mods.setdefault(e.batch, []).append(e)
        else:
            script.append(e)
    out = [("script", script)] if script else []
    for b in sorted(mods):
        out.append(("module", mods[b]))
    return out


# --------------------------------------------------------------------- rules

def is_scratch(name):
    """A temp table or table variable. Losing its rows loses nothing that lasts."""
    return name.startswith(("#", "@"))


def same_table(a, b):
    """True when the shorter dotted name is the tail of the longer one."""
    pa, pb = a.split("."), b.split(".")
    m = min(len(pa), len(pb))
    return pa[len(pa) - m:] == pb[len(pb) - m:]


def walk(evs):
    """Yield (event, in_tran) for each event, in order.

    in_tran is the state before the event: an explicit BEGIN TRANSACTION is open,
    or SET IMPLICIT_TRANSACTIONS ON makes the next write open one.
    """
    depth, implicit = 0, False
    for e in evs:
        yield e, depth > 0 or implicit
        if e.kind == "begin":
            depth += 1
        elif e.kind == "commit":
            depth = max(0, depth - 1)
        elif e.kind == "rollback" and not e.extra:
            depth = 0
        elif e.kind == "implicit":
            implicit = e.extra


def rule_r1(evs):
    """A DELETE or TRUNCATE outside a transaction, then an INSERT into the same table."""
    exposed, inserts, out = [], [], []
    for pos, (e, in_tran) in enumerate(walk(evs)):
        if e.kind != "write" or not e.name:
            continue
        if e.extra in ("DELETE", "TRUNCATE") and not in_tran and not is_scratch(e.name):
            exposed.append((pos, e))
        elif e.extra == "INSERT":
            inserts.append((pos, e))
    for pos, d in exposed:
        for ipos, ins in inserts:
            if ipos > pos and same_table(d.name, ins.name):
                where = "" if ins.batch == d.batch else " (batch %d)" % ins.batch
                out.append((d.line, d.batch,
                            "%s %s runs outside a transaction and commits by itself. "
                            "The INSERT at line %d%s can fail or be killed, and the removed "
                            "rows stay removed. Put both statements in one BEGIN TRANSACTION "
                            "and COMMIT." % (_VERB_FORM[d.extra], safe(d.name), ins.line, where)))
                break
    return out


def rule_r2(evs):
    """SET XACT_ABORT ON, with two or more writes outside any transaction."""
    abort, hits, first = None, [], None
    for e, in_tran in walk(evs):
        if e.kind == "xact":
            abort = (e.line, e.batch) if e.extra else None
        elif e.kind == "write" and not in_tran and abort is not None \
                and not is_scratch(e.name):
            if not hits:
                first = abort
            hits.append(e)
    if len(hits) < 2:
        return []
    lines = ", ".join(str(h.line) for h in hits[:5])
    return [(first[0], first[1],
             "SET XACT_ABORT ON rolls back an open transaction, and none is open here. "
             "%d writes (lines %s%s) run outside a transaction, so each commits by itself. "
             "Put them between BEGIN TRANSACTION and COMMIT."
             % (len(hits), lines, ", ..." if len(hits) > 5 else ""))]


def rule_r3(evs):
    """A write in a CATCH block before the ROLLBACK that follows it.

    A CATCH that tests XACT_STATE before it writes is assumed to branch on the
    answer, so its later writes are not reported.
    """
    tries, closed, catches, out = [], False, [], []
    for e, in_tran in walk(evs):
        if e.kind == "begin":
            tries[:] = [True] * len(tries)
        elif e.kind == "try":
            tries.append(in_tran)
        elif e.kind == "endtry":
            closed = tries.pop() if tries else False
        elif e.kind == "catch":
            catches.append({"tran": closed, "rolled": False, "checked": False, "pending": []})
        elif e.kind == "endcatch":
            if catches:
                catches.pop()
        elif e.kind == "xstate":
            if catches:
                catches[-1]["checked"] = True
        elif e.kind == "rollback":
            if catches and not catches[-1]["rolled"]:
                catches[-1]["rolled"] = True
                for w in catches[-1]["pending"]:
                    out.append((w.line, w.batch,
                                "A write (%s) to %s in the CATCH block runs before the "
                                "ROLLBACK at line %d. An uncommittable transaction cannot "
                                "run a write, and a write that does run is undone by the "
                                "ROLLBACK. Put ROLLBACK first, or log to a table variable."
                                % (w.extra, safe(w.name), e.line)))
        elif e.kind == "write":
            if in_tran:
                tries[:] = [True] * len(tries)
            if catches:
                c = catches[-1]
                if c["tran"] and not c["checked"] and not e.name.startswith("@"):
                    c["pending"].append(e)
    return out


def rule_r5(evs):
    """A THROW or RAISERROR guard, then a write that runs although the guard fired.

    THROW ends its own batch, so only a write in a later batch is reported. RAISERROR
    ends nothing, so a write after it in the same batch is reported too, unless a
    RETURN or GOTO follows the RAISERROR first (the guard event says so in extra).
    A guard is reported once, at the first write it fails to stop.
    """
    pending, exit_on, out = [], False, []
    for e in evs:
        if e.kind == "sqlcmd":
            exit_on = e.extra
        elif e.kind == "guard" and e.ctx != "try" and not exit_on:
            pending.append(e)
        elif e.kind == "write" and not is_scratch(e.name):
            keep = []
            for g in pending:
                target = "%s on %s" % (e.extra, safe(e.name or "?"))
                if g.batch < e.batch:
                    fix = ("move the check into the batch that writes"
                           if g.name == "THROW" else
                           "move the check into the batch that writes and follow RAISERROR "
                           "with RETURN, or use THROW")
                    out.append((g.line, g.batch,
                                "%s at line %d reports an error, but the next GO sends batch "
                                "%d (line %d, %s) anyway. Run the script with sqlcmd -b "
                                "or put :on error exit above it, or %s."
                                % (g.name, g.line, e.batch, e.line, target, fix)))
                elif g.extra:
                    out.append((g.line, g.batch,
                                "RAISERROR at line %d reports an error but does not stop its "
                                "batch, so line %d (%s) runs anyway. Follow the RAISERROR with "
                                "RETURN, or use THROW." % (g.line, e.line, target)))
                else:
                    keep.append(g)
            pending = keep
    return out


def rule_r6(toks, hosts):
    """A UNC path with the wrong number of leading backslashes, or an unlisted host."""
    out = []
    for t in toks:
        if t.kind != "s":
            continue
        for m in _UNC_RE.finditer(t.val):
            count, host = len(m.group(1)), m.group(2).lower()
            if host in hosts and count not in (0, 2):
                out.append((t.line, t.batch,
                            "a path to host %s starts with %d backslash(es). A UNC path "
                            "starts with exactly 2." % (safe(host), count)))
            elif host not in hosts and count == 2:
                out.append((t.line, t.batch,
                            "a UNC path names host %s, which is not on the --unc-host list."
                            % safe(host)))
    return out


_RULE_FUNCS = (("R1", rule_r1), ("R2", rule_r2), ("R3", rule_r3), ("R5", rule_r5))


def lint_text(text, path="<text>", rules=DEFAULT_RULES, hosts=()):
    """Every finding in one script, sorted by line. Pure: no file, no output."""
    return analyse(text, path, rules, hosts)[0]


def analyse(text, path="<text>", rules=DEFAULT_RULES, hosts=()):
    """(findings, notes) for one script. A note marks code that was not read. Pure."""
    toks, problems = tokenize(text)
    out = [Finding(path, ln, b, "E0", msg) for ln, b, msg in problems]
    evs = scan(toks)
    for _, uevs in units(evs):
        for rid, fn in _RULE_FUNCS:
            if rid in rules:
                out += [Finding(path, ln, b, rid, msg) for ln, b, msg in fn(uevs)]
    if "R6" in rules:
        out += [Finding(path, ln, b, "R6", msg)
                for ln, b, msg in rule_r6(toks, frozenset(hosts))]
    # R6 reads every string literal itself, so a note matters only to the other rules.
    notes = [Finding(path, ln, b, "note", msg) for ln, b, msg in unread(toks)]         if set(rules) & set(DEFAULT_RULES) else []
    return sorted(out, key=lambda f: (f.line, f.rule, f.message)), notes


def gate(findings, notes=()):
    """The exit code: 1 for a finding, 3 for no finding but code that was not read, else 0."""
    return 1 if findings else (3 if notes else 0)


def parse_rules(text, have_hosts):
    """The rule ids to run. R6 is on when hosts are named, and it needs them."""
    if text is None:
        return DEFAULT_RULES + (("R6",) if have_hosts else ())
    rules = tuple(sorted(set(r.strip().upper() for r in text.split(",") if r.strip())))
    unknown = [r for r in rules if r not in ALL_RULES]
    if not rules or unknown:
        raise ValueError("unknown rule %s. The rules are %s."
                         % (safe(", ".join(unknown) or "(none)"), ", ".join(ALL_RULES)))
    if "R6" in rules and not have_hosts:
        raise ValueError("R6 needs at least one --unc-host, because a lost backslash "
                         "cannot be told from a real path without the host list.")
    return rules


def parse_hosts(values):
    """The host allowlist as a set of lower-case names. Refuses anything that is not a bare name."""
    hosts = set()
    for v in values or ():
        if not _HOST_OK.match(v.strip()):
            raise ValueError("--unc-host takes a bare host name such as backupsrv, "
                             "not %r" % safe(v))
        hosts.add(v.strip().lower())
    return hosts


# ------------------------------------------------------------------- file IO

def decode_sql(raw):
    """SQL text from bytes. A UTF-16 byte order mark decides; otherwise UTF-8, then cp1252."""
    if raw[:2] in (b"\xff\xfe", b"\xfe\xff"):
        return raw.decode("utf-16")
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return raw.decode("cp1252", errors="replace")


def read_sql(path):
    with open(path, "rb") as fh:
        return decode_sql(fh.read())


def _raise(exc):
    raise exc


def collect(paths):
    """The files to read: each file as given, each folder walked for *.sql in sorted order.

    A folder that cannot be listed raises OSError. Skipping it would report a clean run
    for a script that was never read.
    """
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, dirs, names in os.walk(p, onerror=_raise):
                dirs.sort()
                files += [os.path.join(root, nm) for nm in sorted(names)
                          if nm.lower().endswith(".sql")]
        else:
            files.append(p)
    return files


def describe(findings, nfiles, notes=()):
    lines = ["%s:%d: %s %s" % (safe(f.path), f.line, f.rule, f.message) for f in findings]
    lines += ["%s:%d: note: %s" % (safe(f.path), f.line, f.message) for f in notes]
    lines.append("%d file(s) read, %d finding(s)." % (nfiles, len(findings)))
    if notes:
        lines.append("%d place(s) hold code that was not read. A clean result does not cover them."
                     % len(notes))
    return lines


def write_findings(path, findings):
    with open(path, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["path", "line", "batch", "rule", "message"])
        for f in findings:
            w.writerow([safe(f.path), f.line, f.batch, f.rule, f.message])


# ------------------------------------------------------------------ self-test

def _run_checks(check, raises, tmp):
    BS = chr(92)

    def S(*lines):
        return "\n".join(lines)

    def lint(sql, rules=DEFAULT_RULES, hosts=()):
        return [(f.rule, f.line) for f in lint_text(sql, "t.sql", rules, hosts)]

    def writes(sql):
        return [(e.extra, e.name) for e in scan(tokenize(sql)[0]) if e.kind == "write"]

    def kinds(sql):
        return [e.kind for e in scan(tokenize(sql)[0])]

    def batches(sql):
        return [t.batch for t in tokenize(sql)[0] if t.kind == "w"]

    def unc(count, host, rest="share"):
        return BS * count + host + BS + rest

    def q(text):
        return "'" + text + "'"

    def run_cli(argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(argv)
        return code, out.getvalue(), err.getvalue()

    def put(name, text, raw=None):
        path = os.path.join(tmp, name)
        folder = os.path.dirname(path)
        if not os.path.isdir(folder):
            os.makedirs(folder)
        with open(path, "wb") as fh:
            fh.write(raw if raw is not None else text.encode("utf-8"))
        return path

    def slurp(path):
        with open(path, "rb") as fh:
            return fh.read().decode("utf-8")

    PAIR = S("DELETE FROM dbo.Lookup;",
             "INSERT INTO dbo.Lookup (a) SELECT a FROM dbo.Staging;")

    # ---- GO splits batches, and only a GO that stands alone on its line
    check(batches(S("SELECT 1", "GO", "SELECT 2", "go 3", "SELECT 3")) == [1, 2, 3],
          "GO, go and GO with a count each end a batch")
    check(batches(S("SELECT 1", "GO -- done", "SELECT 2", "GO /* c */", "SELECT 3",
                    "   GO   ", "SELECT 4")) == [1, 2, 3, 4],
          "a comment on the GO line and spaces around GO do not stop it")
    toks = tokenize(S("SELECT 'a", "GO", "b'", "FROM t"))[0]
    check({t.batch for t in toks} == {1} and toks[1].kind == "s" and toks[1].val == "a\nGO\nb",
          "a GO line inside a multi-line string is text, not a batch break" + PIN)
    check(batches(S("/*", "GO", "*/", "SELECT 1")) == [1],
          "a GO line inside a block comment is text, not a batch break" + PIN)
    check(batches(S("GOTO done", "SELECT 1 GO")) == [1, 1, 1, 1],
          "GOTO and a GO at the end of a statement line are words, not batch breaks")
    check(lint(PAIR.replace("\n", "\r\n")) == lint(PAIR) == [("R1", 1)],
          "CRLF line endings give the same findings and line numbers")
    check(lint(S("/* a", "b */", "'x", "y'", "DELETE FROM t;", "INSERT INTO t SELECT 1;"))
          == [("R1", 5)],
          "line numbers stay right after a multi-line comment and string")
    check(len(writes("/* a /* b */ DELETE FROM t */ DELETE FROM u")) == 1,
          "block comments nest, as T-SQL block comments do")
    check(tokenize("'it''s'")[0][0].val == "it's" and tokenize("[a]]b]")[0][0].val == "a]b"
          and tokenize('"x""y"')[0][0].val == 'x"y',
          "doubled quotes inside a string, bracket name and double-quoted name are unescaped")
    check([t.kind for t in tokenize("N'x' n'y'")[0]] == ["s", "s"],
          "the N prefix of a Unicode literal is not a word")
    check([t.kind for t in tokenize("SELECT'a' N n 'b'")[0]] == ["w", "s", "w", "w", "s"],
          "only an N glued to a quote is a prefix: a word glued to a quote and a lone N stay words")
    pa = tokenize("(a, f(b, c)) (d)")[0]
    check([[t.val for t in arg] for arg in paren_args(pa, 0)[0]]
          == [["a"], ["f", "(", "b", ",", "c", ")"]] and paren_args(pa, 0)[1] == 10
          and [[t.val for t in arg] for arg in paren_args(pa[:3], 0)[0]] == [["a"], []]
          and paren_args(pa[:3], 0)[1] == 3,
          "paren_args splits at top-level commas only, and an unclosed bracket ends at the end")
    check([t.kind for t in tokenize("@@ROWCOUNT #tmp ##g 12.5 ;")[0]] == ["w", "w", "w", "n", "p"],
          "variables, temp names, numbers and punctuation are tokenised")
    check([t.kind for t in tokenize("[delete] ")[0]] == ["i"] and writes("[delete] t") == [],
          "a bracketed name that looks like a keyword is a name, not a verb")

    # ---- an unclosed quote or comment fails closed
    for text, what in (("SELECT 'abc", "string"), ("SELECT [abc", "bracket name"),
                       ('SELECT "abc', "double-quoted name"),
                       ("SELECT 1\n/* never closed", "block comment")):
        found = lint_text(text)
        check([f.rule for f in found] == ["E0"] and found[0].line == (2 if "/*" in text else 1),
              "an unclosed %s is an E0 finding on its opening line, not a silent pass" % what)
    check(batches("DELETE FROM t;\nSELECT 'x") == [1, 1, 1, 1],
          "tokens before an unclosed quote are kept")

    # ---- sqlcmd commands
    check([(t.kind, t.val) for t in tokenize(S(":on error exit", ":ON ERROR IGNORE"))[0]]
          == [("c", "exit"), ("c", "ignore")],
          ":on error exit and :on error ignore are read, in any case")
    check(tokenize(S(":on error", ":on error maybe", ":setvar a 1", ":reset"))[0] == [],
          "other sqlcmd commands, and an :on error with no valid action, are skipped")
    check([(t.kind, t.val) for t in tokenize(S(":r other.sql", "  :R x.sql"))[0]]
          == [("x", "r"), ("x", "r")],
          ":r is read as an include in any case, and no other command starting with r is")
    check([(t.kind, t.u) for t in tokenize("$(Schema).Lookup x$(a)y $(b)")[0]]
          == [("w", "$(SCHEMA)"), ("p", "."), ("w", "LOOKUP"), ("w", "X$(A)Y"), ("w", "$(B)")]
          and [t.kind for t in tokenize("$ (a) $(a b)")[0]]
          == ["p", "p", "w", "p", "p", "p", "w", "w", "p"],
          "a sqlcmd $(variable) is part of a name, and a dollar sign alone is not" + PIN)
    check([t.kind for t in tokenize("::fn_x()")[0]][:2] == ["p", "p"],
          "a double colon is punctuation, not a sqlcmd command")

    # ---- which write verbs are statements
    check(writes("DELETE FROM dbo.Lookup") == [("DELETE", "dbo.lookup")]
          and writes("DELETE TOP (10) FROM [dbo].[Lookup]") == [("DELETE", "dbo.lookup")]
          and writes("INSERT dbo.Lookup SELECT 1") == [("INSERT", "dbo.lookup")]
          and writes("INSERT INTO db..t (a) SELECT 1") == [("INSERT", "db..t")]
          and writes("UPDATE TOP (5) t SET a = 1") == [("UPDATE", "t")]
          and writes("TRUNCATE TABLE dbo.t") == [("TRUNCATE", "dbo.t")],
          "each write verb names its table, with or without TOP, FROM, INTO and schema")
    check(writes("DELETE (SELECT 1)") == [("DELETE", "")]
          and writes("DELETE TOP 5 FROM t") == [("DELETE", "")],
          "an unreadable target is empty")
    check(writes("DELETE x FROM dbo.t AS x") == [("DELETE", "dbo.t")]
          and writes("DELETE b FROM dbo.Helper b JOIN #w w ON w.id = b.id")
          == [("DELETE", "dbo.helper")]
          and writes("DELETE w FROM dbo.A a JOIN dbo.W w ON a.id = w.id") == [("DELETE", "dbo.w")]
          and writes("UPDATE l SET s = 1 FROM @log l") == [("UPDATE", "@log")],
          "a DELETE or UPDATE through an alias names the table behind the alias" + PIN)
    check(writes("DELETE x FROM (SELECT 1 AS a) y") == [("DELETE", "x")]
          and writes("DELETE x FROM dbo.T LEFT JOIN dbo.U ON 1 = 1") == [("DELETE", "x")]
          and writes("DELETE x FROM dbo.A a; SELECT 1 FROM dbo.B x") == [("DELETE", "x")]
          and writes("DELETE x FROM dbo.A a\nGO\nSELECT 1 FROM dbo.B x") == [("DELETE", "x")]
          and writes("DELETE x FROM dbo.A a IF 1 = 1 SELECT 1 FROM dbo.B x")
          == [("DELETE", "x")]
          and writes("DELETE x FROM dbo.A a INSERT INTO dbo.C SELECT 1 FROM dbo.B x")
          == [("DELETE", "x"), ("INSERT", "dbo.c")],
          "an alias with no table behind it stays the alias, and the search ends with the statement")
    check(writes("DELETE FROM t WHERE 1 IN (SELECT 1 FROM dbo.x t)") == [("DELETE", "t")],
          "a DELETE with no alias keeps its own name, even when a subquery reuses that name")
    check(writes(S("MERGE INTO t USING s ON (t.id = s.id)",
                   "WHEN MATCHED AND s.x = 1 THEN DELETE",
                   "WHEN MATCHED THEN UPDATE SET a = 1",
                   "WHEN NOT MATCHED THEN INSERT (a) VALUES (1);")) == [("MERGE", "t")],
          "the UPDATE, DELETE and INSERT inside a MERGE are clauses, not statements")
    check(writes(S("CREATE TABLE c (id int REFERENCES p(id) ON DELETE CASCADE ON UPDATE CASCADE)",
                   "CREATE TRIGGER trg ON t AFTER INSERT, UPDATE, DELETE AS IF UPDATE(a) PRINT 1",
                   "CREATE TRIGGER trg2 ON t INSTEAD OF DELETE AS PRINT 2",
                   "UPDATE STATISTICS dbo.t",
                   "GRANT INSERT, DELETE ON t TO u",
                   "DECLARE c CURSOR FOR SELECT a FROM t FOR UPDATE OF a",
                   "SELECT * FROM t OPTION (MERGE JOIN)",
                   "SELECT * FROM a INNER MERGE JOIN b ON 1 = 1",
                   "TRUNCATE",
                   "PRINT 'DELETE FROM t' -- INSERT INTO t")) == [],
          "verbs inside DDL, grants, hints, strings and comments are not writes")
    check(writes(S("SET NOCOUNT ON", "DELETE FROM t", "SET XACT_ABORT ON", "UPDATE t SET a = 1",
                   "SET IDENTITY_INSERT dbo.t ON", "INSERT INTO dbo.t (a) SELECT 1"))
          == [("DELETE", "t"), ("UPDATE", "t"), ("INSERT", "dbo.t")],
          "a write straight after SET ... ON, with no semicolon, is still a statement" + PIN)
    check(writes(S("CREATE TABLE c (a int REFERENCES p(a) ON DELETE SET NULL ON UPDATE NO ACTION,",
                   "b int REFERENCES p(b) ON DELETE SET DEFAULT ON UPDATE CASCADE)")) == [],
          "every ON DELETE and ON UPDATE action of a foreign key is a clause, not a statement")
    check(writes("IF @x = 1 DELETE FROM t") == [("DELETE", "t")]
          and len(writes("SELECT 1 DELETE FROM t INSERT INTO t SELECT 1")) == 2
          and writes("DELETE FROM t OUTPUT DELETED.a INTO @log") == [("DELETE", "t")],
          "a write after IF or after a statement with no semicolon is still found")
    check(kinds("CREATE OR ALTER PROCEDURE p AS SELECT 1").count("module") == 1
          and kinds("ALTER FUNCTION f() RETURNS int AS BEGIN RETURN 1 END").count("module") == 1
          and kinds("CREATE TRIGGER t ON x AFTER INSERT AS SELECT 1").count("module") == 1
          and "module" not in kinds("ALTER TABLE t ADD c int; CREATE INDEX i ON t (c)"),
          "CREATE OR ALTER counts once, and tables and indexes are not modules")

    # ---- R1: a destructive statement that commits alone, then an insert
    check(lint(PAIR) == [("R1", 1)],
          "a bare DELETE then an INSERT into the same table is R1")
    check(lint(S("TRUNCATE TABLE dbo.Lookup;", "INSERT INTO dbo.Lookup SELECT 1;"))
          == [("R1", 1)],
          "a bare TRUNCATE then an INSERT is R1")
    check(lint(S("BEGIN TRANSACTION;", PAIR, "COMMIT;")) == [],
          "the same pair inside BEGIN TRANSACTION and COMMIT is clean")
    check(lint(S("SET XACT_ABORT ON;", PAIR)) == [("R2", 1), ("R1", 2)],
          "SET XACT_ABORT ON above the bare pair does not clear R1")
    check(lint(S("DELETE FROM dbo.A;", "INSERT INTO dbo.B SELECT 1;")) == []
          and lint(S("DELETE FROM dbo.Lookup;")) == []
          and lint(S("INSERT INTO dbo.Lookup SELECT 1;", "DELETE FROM dbo.Lookup;")) == [],
          "different tables, a DELETE alone, and an INSERT before the DELETE are clean")
    check(lint(S("DELETE FROM #work;", "INSERT INTO #work SELECT 1;")) == []
          and lint(S("DELETE FROM @work;", "INSERT INTO @work SELECT 1;")) == [],
          "a temp table or table variable is scratch, so losing its rows is not R1")
    check(lint(S("DELETE FROM dbo.Lookup;", "INSERT INTO [dbo].[LOOKUP] SELECT 1;")) == [("R1", 1)]
          and lint(S("DELETE FROM Lookup;", "INSERT INTO dbo.Lookup SELECT 1;")) == [("R1", 1)]
          and lint(S("DELETE FROM dbo.Lookup;", "INSERT INTO other.Lookup SELECT 1;")) == [],
          "table names match on their tail, ignoring case and brackets, and not across schemas")
    found = lint_text(S(PAIR.split("\n")[0], "GO", PAIR.split("\n")[1], "GO"))
    check([(f.rule, f.line, f.batch) for f in found] == [("R1", 1, 1)]
          and "(batch 2)" in found[0].message,
          "a DELETE in one batch and its INSERT in the next is R1, and names the batch")
    check(lint(S("BEGIN TRANSACTION;", "GO", "DELETE FROM dbo.Lookup;", "GO",
                 "INSERT INTO dbo.Lookup SELECT 1;", "GO", "COMMIT;")) == [],
          "a transaction opened before GO is still open after it")
    check(lint(S("BEGIN TRAN;", "SELECT 1;", "ROLLBACK;", PAIR)) == [("R1", 4)],
          "after ROLLBACK the depth is zero again")
    check(lint(S("COMMIT;", PAIR)) == [("R1", 2)]
          and lint(S("COMMIT;", "BEGIN TRAN;", PAIR, "COMMIT;")) == [],
          "a stray COMMIT never takes the depth below zero" + PIN)
    check(lint(S("BEGIN TRAN;", "BEGIN TRAN;", "COMMIT;", PAIR, "COMMIT;")) == [],
          "one COMMIT of two nested BEGIN TRAN leaves the pair inside a transaction")
    check(lint(S("CREATE PROCEDURE dbo.Reload AS", "BEGIN", PAIR, "END")) == [("R1", 3)]
          and lint(S("CREATE PROCEDURE dbo.Reload AS", "BEGIN", "BEGIN TRY", "BEGIN TRAN;",
                     PAIR, "COMMIT;", "END TRY", "BEGIN CATCH", "ROLLBACK;", "END CATCH",
                     "END")) == [],
          "a procedure body is read as its own unit, with and without a transaction")
    check(lint(S("DELETE (SELECT 1);", "INSERT INTO t SELECT 1;")) == []
          and lint(S("MERGE INTO t USING s ON 1 = 1 WHEN MATCHED THEN DELETE;",
                     "INSERT INTO t SELECT 1;")) == [],
          "an unreadable target and a MERGE are not reported as R1")
    check(lint(S("DELETE b FROM dbo.Helper b JOIN #w w ON w.id = b.id;",
                 "INSERT INTO dbo.Helper (id) SELECT id FROM #w;")) == [("R1", 1)],
          "R1 sees a DELETE written through an alias" + PIN)
    check(lint(S("SET IMPLICIT_TRANSACTIONS ON;", PAIR)) == []
          and lint(S("SET IMPLICIT_TRANSACTIONS ON;", "SET IMPLICIT_TRANSACTIONS OFF;", PAIR))
          == [("R1", 3)],
          "with implicit transactions on, the pair is inside a transaction until they go off"
          + PIN)
    check(lint(S("BEGIN TRAN;", "SAVE TRAN sp1;", "UPDATE dbo.A SET x = 1;", "ROLLBACK TRAN sp1;",
                 PAIR, "COMMIT;")) == []
          and lint(S("BEGIN TRAN t1;", "ROLLBACK TRAN t1;", PAIR)) == [("R1", 3)]
          and lint(S("BEGIN TRAN;", "SAVE TRANSACTION @sp;", "ROLLBACK TRANSACTION @sp;",
                     PAIR)) == [],
          "ROLLBACK to a savepoint leaves the transaction open, and ROLLBACK by its name ends it"
          + PIN)
    bare = S("DELETE FROM dbo.Lookup", "INSERT INTO dbo.Lookup (a) SELECT a FROM dbo.Staging")
    check(lint(S("SET NOCOUNT ON", bare)) == [("R1", 2)]
          and lint(S("SET ANSI_NULLS ON", "GO", bare)) == [("R1", 3)]
          and lint(S("SET NOCOUNT ON", "BEGIN TRAN", bare, "COMMIT")) == [],
          "a bare pair straight after SET ... ON with no semicolon is R1, not a silent pass" + PIN)
    early = S("BEGIN TRANSACTION;", "IF @x = 0 BEGIN", "  ROLLBACK;", "  RETURN;", "END", PAIR,
              "COMMIT;")
    check(lint(early) == []
          and lint(S("CREATE PROCEDURE dbo.P AS", "BEGIN", "SET XACT_ABORT ON;", "BEGIN TRAN;",
                     "IF @x = 0 BEGIN ROLLBACK; RETURN; END", PAIR, "COMMIT;", "END")) == [],
          "a ROLLBACK in an early-exit branch does not end the transaction for the code after it"
          + PIN)
    check(lint(S("BEGIN TRAN;", "IF @@ERROR <> 0 ROLLBACK TRAN", PAIR)) == []
          and lint(S("BEGIN TRAN;", "IF @x = 1 PRINT 1", "ELSE", "ROLLBACK;", PAIR)) == []
          and lint(S("BEGIN TRAN;", "IF (SELECT 1) = 1 ROLLBACK;", PAIR)) == []
          and lint(S("BEGIN TRAN;", "IF UPDATE(a) ROLLBACK;", PAIR)) == []
          and lint(S("BEGIN TRAN;", "WHILE @i < 3 BEGIN", "SET @i = @i + 1;", "ROLLBACK;",
                     "END", PAIR)) == [],
          "a ROLLBACK under IF, ELSE or WHILE, with or without BEGIN, is conditional")
    check(lint(S("ROLLBACK;", PAIR)) == [("R1", 2)]
          and lint(S("IF @x = 1", "GO", "ROLLBACK;", PAIR)) == [("R1", 4)]
          and lint(S("BEGIN TRAN;", "UPDATE dbo.A SET x = 1 ROLLBACK;", PAIR)) == [("R1", 3)]
          and lint(S("BEGIN TRAN;", "IF @x = 1 SET @y = 2 ROLLBACK;", PAIR)) == [("R1", 3)]
          and lint(S("CREATE PROCEDURE dbo.P AS", "BEGIN", "BEGIN TRAN;", "ROLLBACK;", PAIR,
                     "END")) == [("R1", 5)],
          "a ROLLBACK that nothing makes conditional still ends the transaction, "
          "even inside a procedure body")
    check(lint(S("BEGIN TRAN;", "IF (SELECT 1 WHERE 1 = (SELECT 2)) = 1 ROLLBACK;", PAIR)) == []
          and lint(S("BEGIN TRAN;", "IF @x = 1 SELECT (1) ROLLBACK;", PAIR)) == [("R1", 3)]
          and lint(S("BEGIN DISTRIBUTED TRAN x;", "ROLLBACK TRAN x;", PAIR)) == [("R1", 3)],
          "a nested condition is skipped whole, and only the body of an IF is conditional")
    check(governed_by_if(tokenize("ROLLBACK; IF x = 1")[0], 0) is False
          and governed_by_if(tokenize("IF x = 1 ROLLBACK")[0], 4) is True
          and governed_by_if(tokenize(S("IF x = 1", "GO", "ROLLBACK"))[0], 4) is False,
          "the search for an IF stops at the start of the file and at a batch break")
    check(lint(S("DELETE TOP 5 FROM t;", "INSERT TOP 5 INTO t SELECT 1;")) == [],
          "two unreadable targets are not taken to be the same table")
    check(lint(S("DELETE FROM t;", "INSERT INTO t SELECT 1;", "INSERT INTO t SELECT 2;"))
          == [("R1", 1)],
          "one DELETE followed by two INSERTs into its table is reported once")
    check(lint(S(":setvar Schema ref", "DELETE FROM $(Schema).Lookup;",
                 "INSERT INTO $(Schema).Lookup (a) SELECT a FROM dbo.Staging;")) == [("R1", 2)]
          and lint(S("DELETE FROM $(Db).dbo.Lookup;", "INSERT INTO $(Db).dbo.Lookup SELECT 1;"))
          == [("R1", 1)]
          and lint(S("DELETE FROM $(A).Lookup;", "INSERT INTO $(B).Lookup SELECT 1;")) == [],
          "a table named through a sqlcmd variable is read: the same variable is R1, two are not"
          + PIN)

    # ---- R2: XACT_ABORT ON with writes that run outside any transaction
    two = S("UPDATE dbo.A SET x = 1;", "UPDATE dbo.B SET y = 1;")
    check(lint(S("SET XACT_ABORT ON;", two)) == [("R2", 1)],
          "SET XACT_ABORT ON with two bare writes is R2")
    check(lint(S("SET XACT_ABORT ON;", "BEGIN TRANSACTION;", two, "COMMIT;")) == [],
          "XACT_ABORT ON with the writes inside a transaction is clean")
    check(lint(S("SET XACT_ABORT ON;", "UPDATE dbo.A SET x = 1;")) == []
          and lint(S("SET XACT_ABORT ON;", "SET XACT_ABORT OFF;", two)) == []
          and lint(two) == [],
          "one bare write, XACT_ABORT turned off again, or never set, is clean")
    found = lint_text(S("SET XACT_ABORT ON;", "GO", two))
    check([(f.rule, f.line, f.batch) for f in found] == [("R2", 1, 1)],
          "XACT_ABORT set in one batch still applies to the writes in the next" + PIN)
    check(lint(S("SET XACT_ABORT ON", "GO", "UPDATE dbo.A SET x = 1", "UPDATE dbo.B SET y = 1"))
          == [("R2", 1)],
          "R2 counts writes that follow SET XACT_ABORT ON with no semicolon" + PIN)
    check(lint(S("CREATE PROCEDURE dbo.P AS", "SET XACT_ABORT ON;", two)) == [("R2", 2)]
          and lint(S("SET XACT_ABORT ON;", "GO", "CREATE PROCEDURE dbo.P AS", two)) == [],
          "inside a procedure only its own SET counts, because it is restored on return")
    check(lint(S("SET XACT_ABORT ON;", "UPDATE #a SET x = 1;", "UPDATE @b SET y = 1;")) == []
          and lint(S("SET XACT_ABORT;", two)) == [] and lint(S("SET NOCOUNT ON;", two)) == [],
          "scratch writes do not count, and SET without a valid XACT_ABORT value is ignored")
    check(lint(S("SET XACT_ABORT ON;", "BEGIN TRAN;", "UPDATE dbo.A SET x = 1;", "COMMIT;", two))
          == [("R2", 1)],
          "writes outside the transaction count even when another part of the script has one")
    check(lint(S("SET IMPLICIT_TRANSACTIONS ON;", "SET XACT_ABORT ON;", two)) == [],
          "with implicit transactions on, XACT_ABORT has a transaction to act on")
    check(lint(S("SET XACT_ABORT ON;", "UPDATE dbo.A SET x = 1;", "SET XACT_ABORT OFF;",
                 "SET XACT_ABORT ON;", "UPDATE dbo.B SET y = 1;")) == [("R2", 1)],
          "R2 points at the SET that was in force at the first write")
    five = S("SET XACT_ABORT ON;", *["UPDATE dbo.T%d SET x = 1;" % i for i in range(5)])
    check("5 writes (lines 2, 3, 4, 5, 6) run" in lint_text(five)[0].message,
          "R2 lists five lines in full, with no ...")
    many = S("SET XACT_ABORT ON;", *["UPDATE dbo.T%d SET x = 1;" % i for i in range(6)])
    check("6 writes (lines 2, 3, 4, 5, 6, ...)" in lint_text(many)[0].message,
          "R2 lists the first five lines and then ...")

    # ---- R3: a write in CATCH before the ROLLBACK
    catch_log = S("SET XACT_ABORT ON;",
                  "BEGIN TRY",
                  "  BEGIN TRANSACTION;",
                  "  UPDATE dbo.A SET x = 1;",
                  "  COMMIT;",
                  "END TRY",
                  "BEGIN CATCH",
                  "  INSERT INTO dbo.RunLog (msg) VALUES (ERROR_MESSAGE());",
                  "  IF @@TRANCOUNT > 0 ROLLBACK;",
                  "  THROW;",
                  "END CATCH")
    check(lint(catch_log) == [("R3", 8)],
          "a log INSERT in CATCH before the ROLLBACK is R3")
    swapped = catch_log.split("\n")
    swapped[7], swapped[8] = swapped[8], swapped[7]
    check(lint("\n".join(swapped)) == [],
          "the same CATCH with the ROLLBACK first is clean")
    no_tran = catch_log.replace("  BEGIN TRANSACTION;", "  SELECT 1;").replace(
        "  COMMIT;", "  SELECT 2;").replace("SET XACT_ABORT ON;", "SET NOCOUNT ON;")
    check(lint(catch_log.replace("dbo.RunLog", "@log")) == [] and lint(no_tran) == [],
          "a table variable survives ROLLBACK, and a TRY with no transaction has none to doom")
    check(lint(catch_log.replace("IF @@TRANCOUNT > 0 ROLLBACK;",
                                 "IF XACT_STATE() = 1 COMMIT;")) == [],
          "a CATCH that commits and never rolls back is not R3" + PIN)
    check(lint(S("BEGIN TRANSACTION;", "BEGIN TRY", "UPDATE dbo.A SET x = 1;", "END TRY",
                 "BEGIN CATCH", "INSERT INTO dbo.RunLog VALUES (1);", "ROLLBACK;",
                 "END CATCH")) == [("R3", 6)]
          and lint(S("BEGIN TRANSACTION;", "COMMIT;", "BEGIN TRY", "UPDATE dbo.A SET x = 1;",
                     "END TRY", "BEGIN CATCH", "INSERT INTO dbo.RunLog VALUES (1);",
                     "ROLLBACK;", "END CATCH")) == [],
          "a transaction open before the TRY counts, and one committed before it does not")
    check(lint(S("BEGIN TRY", "BEGIN TRY", "BEGIN TRANSACTION;", "END TRY",
                 "BEGIN CATCH", "ROLLBACK;", "END CATCH", "END TRY",
                 "BEGIN CATCH", "INSERT INTO dbo.RunLog VALUES (1);", "ROLLBACK;",
                 "END CATCH")) == [("R3", 10)],
          "a transaction begun in a nested TRY counts for the outer CATCH too")
    check(lint(S("BEGIN TRANSACTION;", "BEGIN TRY", "UPDATE dbo.A SET x = 1;", "END TRY",
                 "BEGIN CATCH", "IF 1 = 1", "BEGIN",
                 "  UPDATE dbo.RunLog SET s = CASE WHEN 1 = 1 THEN 'a' ELSE 'b' END;",
                 "END", "ROLLBACK;", "END CATCH", "UPDATE dbo.Z SET q = 1;")) == [("R3", 8)],
          "BEGIN, END and CASE inside a CATCH do not end it early or late")
    check(lint(S("BEGIN TRANSACTION;", "BEGIN TRY", "SELECT 1;", "END TRY", "BEGIN CATCH",
                 "UPDATE dbo.A SET x = 1;", "DELETE FROM dbo.B;", "ROLLBACK;", "ROLLBACK;",
                 "END CATCH")) == [("R3", 6), ("R3", 7)],
          "every write before the ROLLBACK is reported once, and a second ROLLBACK adds none")
    check(lint(S("BEGIN TRANSACTION;", "BEGIN TRY", "BEGIN", "UPDATE dbo.A SET x = 1;", "END TRY",
                 "BEGIN CATCH", "INSERT INTO dbo.RunLog VALUES (1);", "ROLLBACK;",
                 "END CATCH")) == [("R3", 7)],
          "a BEGIN that is never closed inside a TRY does not hide the CATCH after it")
    branchy = S("BEGIN TRANSACTION;", "BEGIN TRY", "UPDATE dbo.A SET x = 1;", "END TRY",
                "BEGIN CATCH", "IF XACT_STATE() = 1", "BEGIN",
                "  INSERT INTO dbo.RunLog VALUES (1);", "  COMMIT;", "END",
                "ELSE IF XACT_STATE() = -1", "  ROLLBACK;", "END CATCH")
    unchecked = branchy.replace("IF XACT_STATE() = 1", "IF 1 = 1").replace(
        "IF XACT_STATE() = -1", "IF 1 = 2")
    check(lint(branchy) == [] and lint(unchecked) == [("R3", 8)],
          "a CATCH that tests XACT_STATE before it writes is not R3, and one that does not is"
          + PIN)
    check(lint(S("BEGIN TRANSACTION;", "BEGIN TRY", "UPDATE dbo.A SET x = 1;", "END TRY",
                 "BEGIN CATCH", "INSERT INTO dbo.RunLog VALUES (1);",
                 "IF XACT_STATE() <> 0 ROLLBACK;", "END CATCH")) == [("R3", 6)],
          "a write before the XACT_STATE test is still R3")
    check(lint(S("SELECT XACT_STATE();", "BEGIN TRY", "SELECT 1;", "END TRY", "BEGIN CATCH",
                 "ROLLBACK;", "END CATCH")) == [],
          "XACT_STATE outside a CATCH changes nothing")
    check(lint(S("SET IMPLICIT_TRANSACTIONS ON;", "BEGIN TRY", "UPDATE dbo.A SET x = 1;",
                 "END TRY", "BEGIN CATCH", "INSERT INTO dbo.RunLog VALUES (1);", "ROLLBACK;",
                 "END CATCH")) == [("R3", 6)],
          "with implicit transactions on, a TRY that writes has a transaction to doom")
    check(lint(S("BEGIN TRAN;", "SAVE TRAN sp;", "BEGIN TRY", "UPDATE dbo.A SET x = 1;",
                 "END TRY", "BEGIN CATCH", "ROLLBACK TRAN sp;",
                 "INSERT INTO dbo.RunLog VALUES (1);", "ROLLBACK;", "END CATCH")) == [("R3", 8)],
          "a ROLLBACK to a savepoint does not count as the ROLLBACK that ends the transaction")
    check(lint(S("BEGIN TRANSACTION;", "BEGIN TRY", "SELECT 1;", "END TRY", "BEGIN CATCH",
                 "UPDATE l SET s = 'x' FROM @log AS l;", "ROLLBACK;", "END CATCH")) == [],
          "a write to a table variable through an alias survives the ROLLBACK")
    check(lint(S("END TRY", "BEGIN CATCH", "INSERT INTO dbo.RunLog VALUES (1);", "ROLLBACK;",
                 "END CATCH")) == []
          and lint(S("CREATE PROCEDURE dbo.P AS", "BEGIN", catch_log, "END")) == [("R3", 10)],
          "a stray END TRY opens no transaction, and R3 also reads a procedure body")
    check(lint("END TRY\nEND CATCH\nEND") == []
          and lint(S("BEGIN CATCH", "UPDATE dbo.A SET x = 1;", "ROLLBACK;", "END CATCH")) == [],
          "stray END and CATCH tokens in a broken script do not crash or report")

    # ---- R5: a guard that stops only its own batch
    guard = "IF NOT EXISTS (SELECT 1 FROM dbo.Src) THROW 50001, 'source is empty', 1;"
    check(lint(S(guard, "GO", "DELETE FROM dbo.Target;", "GO")) == [("R5", 1)],
          "a THROW guard in one batch and a DELETE in the next is R5")
    check(lint(S(guard, "DELETE FROM dbo.Target;")) == []
          and lint(S(guard, "GO", "SELECT 1;")) == []
          and lint(S(guard, "GO", "DELETE FROM #t;")) == [],
          "a guard and write in one batch, a later batch with no write, and scratch are clean")
    check(lint(S(":on error exit", guard, "GO", "DELETE FROM dbo.Target;")) == []
          and lint(S(":on error exit", ":on error ignore", guard, "GO",
                     "DELETE FROM dbo.Target;")) == [("R5", 3)]
          and lint(S(":on error exit -- stop at the first error", guard, "GO",
                     "DELETE FROM dbo.Target;")) == [],
          ":on error exit silences R5 until :on error ignore turns it back on")
    check(lint(S("BEGIN TRY", "THROW 50001, 'x', 1;", "END TRY", "BEGIN CATCH", "SELECT 1;",
                 "END CATCH", "GO", "DELETE FROM dbo.T;")) == []
          and lint(S("BEGIN TRY", "SELECT 1;", "END TRY", "BEGIN CATCH", "THROW;", "END CATCH",
                     "GO", "DELETE FROM dbo.T;")) == [("R5", 5)],
          "a THROW in TRY jumps to CATCH and is no guard, a re-THROW in CATCH is one")

    def raised(args):
        return lint(S("IF 1 = 0 RAISERROR(" + args + ")", "GO", "DELETE FROM dbo.T;"))

    check(raised("'x', 16, 1") == [("R5", 1)] and raised("'x', 10, 1") == []
          and raised("'x', @sev, 1") == [("R5", 1)] and raised("'x', '16', 1") == [("R5", 1)]
          and raised("FORMATMESSAGE(1, 2), 11, 1") == [("R5", 1)],
          "RAISERROR is a guard at severity 11 and up or unknown, and not at 10 or below")
    check(lint(S("RAISERROR('x')", "GO", "DELETE FROM dbo.T;")) == [("R5", 1)]
          and lint(S("RAISERROR 50001 'x'", "GO", "DELETE FROM dbo.T;")) == [("R5", 1)]
          and lint(S("RAISERROR('x', 16", "GO", "DELETE FROM dbo.T;")) == [("R5", 1)],
          "RAISERROR with too few arguments, no parentheses, or an open bracket is a guard")
    check(lint(S("IF 1 = 0 THROW 50001, 'a', 1;", "GO", "IF 1 = 0 THROW 50001, 'b', 1;",
                 "DELETE FROM dbo.A;", "GO", "DELETE FROM dbo.B;")) == [("R5", 1), ("R5", 3)]
          and lint(S(guard, "GO", "DELETE FROM dbo.A;", "DELETE FROM dbo.B;")) == [("R5", 1)],
          "each guard is reported once, at the first write in a later batch")
    check(lint(S(guard, "GO", "IF OBJECT_ID(N'dbo.Target') IS NULL THROW 50002, 'x', 1;",
                 "BEGIN TRANSACTION;", "DELETE FROM dbo.Target;", "COMMIT;")) == [("R5", 1)]
          and lint(S(guard, "GO", "IF OBJECT_ID(N'dbo.Target') IS NULL THROW 50002, 'x', 1;",
                     "GO", "SELECT 1;")) == [],
          "a later batch with a guard of its own does not hide an earlier guard" + PIN)
    check(lint(S("CREATE PROCEDURE dbo.P AS", "IF 1 = 0 THROW 50001, 'x', 1;",
                 "DELETE FROM dbo.T;", "GO", "DELETE FROM dbo.U;")) == [],
          "a THROW inside a procedure body is not a script guard")
    check(raised("FORMATMESSAGE(1, 2), 10, 1") == [] and raised("'x', 1" + chr(178) + ", 1")
          == [("R5", 1)],
          "a comma inside a nested call does not move the severity, "
          "and a superscript digit is not a number" + PIN)
    check(lint(S("BEGIN TRY", "SELECT 1;", "END TRY", "BEGIN CATCH", "BEGIN TRY",
                 "THROW 50001, 'x', 1;", "END TRY", "BEGIN CATCH", "SELECT 1;", "END CATCH",
                 "END CATCH", "GO", "DELETE FROM dbo.T;")) == []
          and lint(S("BEGIN TRY", "BEGIN TRY", "SELECT 1;", "END TRY", "BEGIN CATCH",
                     "SELECT 2;", "END CATCH", "THROW 50001, 'x', 1;", "END TRY", "BEGIN CATCH",
                     "SELECT 3;", "END CATCH", "GO", "DELETE FROM dbo.T;")) == [],
          "the innermost TRY decides whether a guard counts, and END CATCH closes only its own")
    check(lint(S("BEGIN TRY", "IF 1 = 1", "BEGIN", "THROW 50001, 'x', 1;", "END", "END TRY",
                 "BEGIN CATCH", "SELECT 1;", "END CATCH", "GO", "DELETE FROM dbo.T;")) == []
          and lint(S("BEGIN TRY", "BEGIN", "SELECT 1;", "END", "END TRY", "BEGIN CATCH",
                     "SELECT 1;", "END CATCH", "IF 1 = 1 THROW 50001, 'x', 1;", "GO",
                     "DELETE FROM dbo.T;")) == [("R5", 9)],
          "a guard in a block inside TRY is still in the TRY, and one after END CATCH is not")

    # RAISERROR does not end its batch, so it guards the statements after it only with a RETURN
    def same_batch(*lines):
        return lint(S(*lines))

    ra = "RAISERROR(N'staging is empty', 16, 1);"
    check(same_batch("IF NOT EXISTS (SELECT 1 FROM dbo.Src) " + ra, "DELETE FROM dbo.T;")
          == [("R5", 1)]
          and same_batch("BEGIN", ra, "END", "DELETE FROM dbo.T;") == [("R5", 2)],
          "a RAISERROR guard and a write in the same batch is R5, because the batch goes on"
          + PIN)
    check(same_batch("IF NOT EXISTS (SELECT 1 FROM dbo.Src)", "BEGIN", ra, "RETURN;", "END",
                     "DELETE FROM dbo.T;") == []
          and same_batch("IF @x = 1 BEGIN", ra, "ROLLBACK TRAN;", "GOTO done;", "END",
                         "DELETE FROM dbo.T;") == []
          and same_batch("IF @x = 1 BEGIN RAISERROR(N'x', 16, 1) RETURN END",
                         "DELETE FROM dbo.T;") == []
          and same_batch(ra, "RETURN;", "DELETE FROM dbo.T;") == []
          and same_batch("IF @x = 1 BEGIN", ra, "IF @y = 1 BEGIN PRINT 1; END", "RETURN; END",
                         "DELETE FROM dbo.T;") == []
          and same_batch("IF @x = 1 BEGIN", ra, "SELECT CASE WHEN 1 = 1 THEN 1 END;",
                         "BEGIN TRAN; RETURN; END", "DELETE FROM dbo.T;") == [],
          "a RETURN or GOTO in the same block after the RAISERROR makes it a real guard")
    check(same_batch(ra, "DELETE FROM dbo.T;", "RETURN;") == [("R5", 1)]
          and same_batch("IF @x = 1 BEGIN", ra, "IF @y = 1 RETURN;", "END",
                         "DELETE FROM dbo.T;") == [("R5", 2)]
          and same_batch("IF @x = 1 BEGIN", ra, "END", "RETURN;", "DELETE FROM dbo.T;")
          == [("R5", 2)]
          and same_batch("IF @x = 1 BEGIN", ra, "BEGIN TRY", "SELECT 1;", "END TRY",
                         "BEGIN CATCH", "RETURN;", "END CATCH", "END", "DELETE FROM dbo.T;")
          == [("R5", 2)]
          and same_batch(ra, "GO", "RETURN;") == [] and same_batch(ra, "SELECT 1;") == [],
          "a RETURN that is conditional, after the block or after a write, does not clear it")
    check(same_batch(":on error exit", ra, "DELETE FROM dbo.T;") == []
          and same_batch("IF 1 = 0 THROW 50001, 'x', 1;", "DELETE FROM dbo.T;") == []
          and same_batch("BEGIN TRY", ra, "DELETE FROM dbo.T;", "END TRY", "BEGIN CATCH",
                         "SELECT 1;", "END CATCH") == []
          and same_batch("RAISERROR(N'info', 10, 1);", "DELETE FROM dbo.T;") == [],
          ":on error exit, a THROW, a RAISERROR in TRY and a severity below 11 are not R5 here")
    check(lint(S("IF (@x = 1) RAISERROR(N'x', 10, 1)", "GO", "DELETE FROM dbo.T;")) == []
          and lint(S("CREATE PROCEDURE dbo.P @x int AS", "BEGIN",
                     "IF @x IS NULL RAISERROR(N'x is required', 16, 1);",
                     "DELETE FROM dbo.T;", "END")) == [("R5", 3)],
          "a RAISERROR severity is read after an IF condition, and R5 also reads a procedure body")
    cross_t = lint_text(S(guard, "GO", "DELETE FROM dbo.T;"), "t.sql")[0].message
    cross_r = lint_text(S("IF 1 = 0 " + ra, "GO", "DELETE FROM dbo.T;"), "t.sql")[0].message
    same_r = lint_text(S("IF 1 = 0 " + ra, "DELETE FROM dbo.T;"), "t.sql")[0].message
    check("(line 3, DELETE on dbo.t)" in cross_t and "DELETE on ?" in lint_text(
        S(guard, "GO", "DELETE TOP 5 FROM dbo.T;"), "t.sql")[0].message,
          "the R5 message names the write and its table, or a ? when the table cannot be read")
    check("move the check into the batch that writes." in cross_t and "RETURN" not in cross_t
          and "follow RAISERROR with RETURN, or use THROW." in cross_r
          and "does not stop its batch" in same_r and "RETURN, or use THROW." in same_r,
          "the advice differs: THROW ends its batch, and a RAISERROR needs a RETURN" + PIN)

    # ---- R6: the UNC rule, with a host list
    hosts = {"backupsrv"}

    def r6(sql):
        return lint(sql, ("R6",), hosts)

    check(r6(q(unc(2, "backupsrv"))) == [] and r6(q(unc(1, "backupsrv"))) == [("R6", 1)]
          and r6(q(unc(4, "backupsrv"))) == [("R6", 1)]
          and r6(q(unc(3, "BackupSrv"))) == [("R6", 1)],
          "a listed host needs exactly two backslashes, in any case")
    check(r6(q("backupsrv" + BS + "INST")) == [] and r6(q("C:" + unc(1, "backupsrv", "x"))) == []
          and r6(q(BS * 2 + "?" + BS + "C:" + BS + "x")) == []
          and r6(q("x" + unc(2, "backupsrv"))) == [],
          "server\\instance, a drive path, a device path and a mid-word run are not UNC paths" + PIN)
    check(r6(q(unc(2, "otherhost"))) == [("R6", 1)] and r6(q(unc(2, "10.0.0.5"))) == [("R6", 1)]
          and r6(q(unc(1, "otherhost"))) == [],
          "a two-backslash path to an unlisted host is R6, and a one-backslash path to one is not")
    check(r6("SELECT [" + unc(1, "backupsrv") + "], " + unc(1, "backupsrv")) == [],
          "a bracketed name or a bare word is not a string, so it is not read as a path")
    check(r6("-- " + q(unc(1, "backupsrv")) + "\n/* " + unc(1, "backupsrv") + " */") == [],
          "a bad path in a comment is not a finding")
    check(r6(S("SELECT 1;", "EXEC('copy ''" + unc(1, "backupsrv") + "''')")) == [("R6", 2)]
          and r6(S("SELECT 1;", "SELECT '", unc(1, "backupsrv"), "'")) == [("R6", 2)],
          "a path inside dynamic SQL is found, and a multi-line string reports its opening line")
    check(lint(q(unc(1, "backupsrv")), DEFAULT_RULES, hosts) == [],
          "R6 does not run unless it is among the rules")
    found = lint_text(q(unc(1, "backupsrv", "secret$" + BS + "pwd=hunter2")), "t.sql",
                      ("R6",), hosts)
    check(len(found) == 1 and "hunter2" not in found[0].message
          and "secret" not in found[0].message,
          "an R6 message names the host and the count and never prints the path")

    # ---- code the tool does not read
    def notes(sql, rules=DEFAULT_RULES):
        return [(n.line, n.message.split()[0]) for n in analyse(sql, "t.sql", rules)[1]]

    check(notes("EXEC('DELETE FROM dbo.T');") == [(1, "EXEC")]
          and notes("EXECUTE (@sql)") == [(1, "EXECUTE")]
          and notes("EXEC ('INSERT ' + 'x')") == [(1, "EXEC")]
          and notes("EXEC sp_executesql N'update t set a = 1'") == [(1, "SP_EXECUTESQL")]
          and notes("EXEC sp_executesql @stmt, N'@a int', @a = 1") == [(1, "SP_EXECUTESQL")]
          and notes(S("SELECT 1;", "  :r other.sql")) == [(2, ":r")],
          "EXEC of a string with a write or a variable, sp_executesql and :r are listed as not read"
          + PIN)
    got = analyse(S("EXEC('DELETE FROM dbo.T');", ":r a.sql"), "t.sql")[1]
    check([n.message for n in got] == [
        "EXEC runs a string that is not read, and it may hold a write",
        ":r includes a file that is not read"] and got[0].rule == "note",
        "a note says what was not read")
    check(notes("EXEC('SELECT 1')") == [] and notes("EXEC dbo.Reload @a = 1") == []
          and notes("EXEC sp_executesql N'SELECT 1'") == [] and notes("EXEC") == []
          and notes("EXEC sp_executesql") == [] and notes("EXEC('SELECT ' + 'x')") == []
          and notes("EXEC('DELETE FROM t'); :r a.sql", ("R6",)) == [],
          "a procedure call, a harmless string and a run with only R6 list nothing")
    check(gate([], []) == 0 and gate([], [1]) == 3 and gate([1], [1]) == 1 and gate([1]) == 1,
          "the gate gives 3 for notes alone, and a finding still gives 1")

    # ---- the argument helpers
    check(parse_rules(None, False) == DEFAULT_RULES and parse_rules(None, True) == ALL_RULES
          and parse_rules("r2, R1", False) == ("R1", "R2") and parse_rules("R6", True) == ("R6",),
          "rules default to R1 R2 R3 R5, add R6 with hosts, and accept any case and spacing")
    raises(lambda: parse_rules("R9", False), "an unknown rule is refused")
    raises(lambda: parse_rules(",", False), "an empty rule list is refused")
    raises(lambda: parse_rules("R6", False), "R6 without a host list is refused, not silently off")
    check(parse_hosts(["BackupSrv", " fs-1.corp "]) == {"backupsrv", "fs-1.corp"}
          and parse_hosts(None) == set(),
          "host names are trimmed and lower-cased")
    for bad_host, what in ((BS * 2 + "srv", "a host with backslashes"),
                           ("srv share", "a host with a space"), ("", "an empty host"),
                           ("a/b", "a host with a slash")):
        raises(lambda b=bad_host: parse_hosts([b]), "%s is refused" % what)
    check(gate([]) == 0 and gate(lint_text(PAIR)) == 1,
          "the gate passes no findings and fails any finding")

    # ---- decoding
    text = "SELECT '" + chr(233) + "'"
    check(decode_sql(b"\xff\xfe" + text.encode("utf-16-le")) == text
          and decode_sql(b"\xfe\xff" + text.encode("utf-16-be")) == text
          and decode_sql(b"\xef\xbb\xbf" + text.encode("utf-8")) == text
          and decode_sql(text.encode("cp1252")) == text and decode_sql(b"SELECT 1") == "SELECT 1",
          "UTF-16 either way round, UTF-8 with a BOM, cp1252 and ASCII are all read")

    # ---- the command line, on real files
    good = put("good.sql", S("BEGIN TRANSACTION;", PAIR, "COMMIT;"))
    bad = put("bad.sql", PAIR)
    code, out, err = run_cli([good])
    check(code == 0 and "0 finding(s)." in out and "rules R1 R2 R3 R5; R6 is off" in out
          and err == "",
          "a clean file exits 0 and says which rules ran and that R6 is off")
    code, out, err = run_cli([bad])
    check(code == 1 and ":1: R1 DELETE FROM dbo.lookup runs outside a transaction" in out
          and "1 file(s) read, 1 finding(s)." in out,
          "a file with a bare pair exits 1 and names the line, rule and table")
    put("tree/a.sql", PAIR.replace("DELETE", "-- x"))
    put("tree/B.SQL", PAIR)
    put("tree/notes.txt", PAIR)
    put("tree/sub/c.sql", "SELECT 1")
    tree = os.path.join(tmp, "tree")
    check([os.path.basename(p) for p in collect([tree])] == ["B.SQL", "a.sql", "c.sql"]
          and sorted(os.path.basename(p) for p in collect([tree, bad]))
          == ["B.SQL", "a.sql", "bad.sql", "c.sql"],
          "a folder is walked in sorted order for .sql in any case, and other files are skipped")
    code, out, err = run_cli([tree])
    check(code == 1 and "3 file(s) read, 1 finding(s)." in out,
          "a folder run reads every .sql file under it")
    real_walk = os.walk

    # os.walk reports a folder it cannot list through onerror. The stand-in does that and
    # nothing else, so the test needs no folder with the permissions taken away.
    os.walk = lambda top, onerror=None, **kwargs: onerror(OSError("denied: " + top))
    try:
        code, out, err = run_cli([tree])
    finally:
        os.walk = real_walk
    check(code == 2 and "denied" in err and "finding" not in out,
          "a folder that cannot be listed exits 2, not a clean pass over files never read" + PIN)
    os.makedirs(os.path.join(tmp, "emptydir"))
    code, out, err = run_cli([os.path.join(tmp, "emptydir")])
    check(code == 2 and "no .sql file found" in err,
          "a folder with no .sql file exits 2 instead of passing")
    code, out, err = run_cli([os.path.join(tmp, "missing.sql")])
    check(code == 2 and "missing.sql" in err, "a file that cannot be read exits 2")
    utf16 = put("u16.sql", "", b"\xff\xfe" + PAIR.encode("utf-16-le"))
    check(run_cli([utf16])[0] == 1,
          "a UTF-16 script with a byte order mark is read and linted, not skipped as binary")
    broken16 = put("b16.sql", "", b"\xff\xfe\x00")
    code, out, err = run_cli([broken16])
    check(code == 2 and "b16.sql" in err, "a broken UTF-16 file exits 2")
    unclosed = put("unclosed.sql", "SELECT 'abc")
    code, out, err = run_cli([unclosed])
    check(code == 1 and ":1: E0 " in out, "an unclosed quote exits 1 through the command line")

    dyn = put("dyn.sql", "EXEC('DELETE FROM dbo.Lookup');")
    code, out, err = run_cli([dyn])
    check(code == 3 and ":1: note: EXEC runs a string that is not read" in out
          and "1 file(s) read, 0 finding(s)." in out
          and "1 place(s) hold code that was not read. A clean result does not cover them."
          in out,
          "a script made only of dynamic SQL exits 3, not the 0 that means clean" + PIN)
    mixed = put("mixed.sql", S(PAIR, "EXEC(@sql)"))
    mixed_csv = os.path.join(tmp, "mixed.csv")
    code, out, err = run_cli([mixed, "--out", mixed_csv, "--apply"])
    with open(mixed_csv, newline="") as fh:
        mixed_rows = list(csv.reader(fh))
    check(code == 1 and ": R1 " in out and ": note: " in out and len(mixed_rows) == 2,
          "a finding wins over a note with exit 1, and the CSV holds findings only")
    check(run_cli([dyn, "--rules", "R6", "--unc-host", "backupsrv"])[0] == 0,
          "a run with only R6 does not ask for notes")

    csv_path = os.path.join(tmp, "findings.csv")
    code, out, err = run_cli([bad, "--out", csv_path])
    check(code == 1 and not os.path.exists(csv_path) and "Check only." in out
          and "Re-run with --apply." in out,
          "--out without --apply writes nothing and says so")
    code, out, err = run_cli([bad, "--out", csv_path, "--apply"])
    with open(csv_path, newline="") as fh:
        rows = list(csv.reader(fh))
    check(code == 1 and rows[0] == ["path", "line", "batch", "rule", "message"]
          and rows[1][1:4] == ["1", "1", "R1"] and len(rows) == 2 and "wrote " in out,
          "--out with --apply writes the header and one row per finding")
    for flag in ("--ap", "--app", "--appl", "--ou", "--unc", "--rule", "--self"):
        check(run_cli([bad, flag])[0] == 64,
              "a unique prefix %s of a long flag is refused with 64" % flag
              + (PIN if flag == "--ap" else ""))
    ghost = os.path.join(tmp, "ghost.csv")
    check(run_cli([bad, "--out", ghost, "--ap"])[0] == 64 and not os.path.exists(ghost),
          "--ap next to --out writes nothing" + PIN)
    check(run_cli([bad, "--apply"])[0] == 64, "--apply without --out is a usage error")
    before = slurp(bad)
    code, out, err = run_cli([bad, "--out", bad, "--apply"])
    check(code == 64 and slurp(bad) == before and "reads" in err,
          "--out naming an input script is refused and the script is left alone")
    check(run_cli([bad, "--out", os.path.join(tmp, "nodir", "f.csv"), "--apply"])[0] == 2,
          "a report that cannot be written exits 2, not the 1 that means a finding")
    check(run_cli([])[0] == 64, "no path is a usage error")
    check(run_cli([bad, "--rules", "R9"])[0] == 64
          and run_cli([bad, "--rules", "R6"])[0] == 64
          and run_cli([bad, "--unc-host", BS + "x"])[0] == 64,
          "a bad rule list, R6 without hosts and a bad host are usage errors")
    check(run_cli([bad, "--rules", "R2"])[0] == 0 and run_cli([bad, "--rules", "r1,r2"])[0] == 1,
          "--rules picks which rules run")
    code, out, err = run_cli(["--help"])
    check(code == 0 and "--unc-host" in out, "--help exits 0 and lists the flags")

    uncfile = put("unc.sql", "EXEC xp_cmdshell "
                  + q("copy a " + unc(1, "backupsrv", "secret$" + BS + "pwd=hunter2")))
    check(run_cli([uncfile])[0] == 0,
          "a bad UNC path passes while R6 is off, and the header says so")
    code, out, err = run_cli([uncfile, "--unc-host", "backupsrv", "--out", csv_path, "--apply"])
    everything = out + err + slurp(csv_path)
    check(code == 1 and ":1: R6 " in out and "rules R1 R2 R3 R5 R6" in out
          and "hunter2" not in everything and "secret" not in everything,
          "--unc-host turns R6 on, and no part of the path reaches the output or the CSV")
    odd = put("odd.sql", "",
              ("DELETE FROM [t" + chr(233) + "st]\nINSERT INTO [t" + chr(233)
               + "st] SELECT 1").encode("utf-8"))
    code, out, err = run_cli([odd])
    check(code == 1 and "t\\xe9st" in out and out.isascii(),
          "a non-ASCII table name prints as ASCII escapes and does not crash the console")
    check(run_cli([bad])[1] == run_cli([bad])[1], "two runs on the same file print the same text")
    argv_before = sys.argv
    try:
        sys.argv = ["txnlint.py", good]
        check(run_cli(None)[0] == 0,
              "main with no argv reads the arguments after the program name")
    finally:
        sys.argv = argv_before


def self_test():
    """Assertions over the lexer, the rules and the command line. No SQL Server, no network."""
    passed = [0]
    failed = []

    def check(cond, label):
        if cond:
            passed[0] += 1
            print("PASS  %s" % label)
        else:
            failed.append(label)
            print("FAIL  %s" % label)

    def raises(fn, label):
        try:
            fn()
        except ValueError:
            check(True, label)
        except Exception as exc:
            check(False, "%s (wrong exception %r)" % (label, exc))
        else:
            check(False, "%s (no error raised)" % label)

    print("txnlint self-test: no SQL Server, no network, no credentials")
    print("-" * 68)
    tmp = tempfile.mkdtemp(prefix="txnlint_")
    try:
        _run_checks(check, raises, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    # ---- the harness itself. A check() that cannot record a failure would
    # report every defect above as a pass, which is the one failure no other
    # assertion here could see. The probe's own output is swallowed so a green
    # run prints no FAIL line.
    quiet = sys.stdout
    sys.stdout = io.StringIO()
    mark = len(failed)
    try:
        check(False, "probe: a false condition must be recorded as a failure")
        raises(lambda: None, "probe: a call that raises nothing must fail")
        raises(lambda: [][0], "probe: the wrong exception must fail")
    finally:
        sys.stdout = quiet
    probe = failed[mark:]
    del failed[mark:]
    check(len(probe) == 3,
          "check() and raises() really do record a failure" + PIN)
    raises(lambda: (_ for _ in ()).throw(ValueError("x")),
           "and raises() accepts the ValueError it is looking for")

    def summary(passed_count, failures):
        print("-" * 68)
        total = passed_count + len(failures)
        if failures:
            print("%d assertions, %d failed" % (total, len(failures)))
            for f in failures:
                print("  FAILED: %s" % f)
            return 1
        print("%d assertions, 0 failed" % total)
        return 0

    # The footer on its red path. A footer that could only print 0 failed
    # would hide every FAIL line above it from anyone who reads the last line.
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        red = summary(1, ["probe one", "probe two"])
    check(red == 1 and "3 assertions, 2 failed" in buf.getvalue()
          and "  FAILED: probe two" in buf.getvalue(),
          "the footer reports failures by count and by name, and exits 1" + PIN)

    # ---- importing the module runs nothing. Another script may import it for
    # the core, and must not start a lint run.
    spec = importlib.util.spec_from_file_location("txnlint_imported", os.path.abspath(__file__))
    imported = importlib.util.module_from_spec(spec)
    buf = io.StringIO()
    # No bytecode cache, so the self-test writes nothing outside its own temp folder.
    cache_before = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        with contextlib.redirect_stdout(buf):
            spec.loader.exec_module(imported)
    finally:
        sys.dont_write_bytecode = cache_before
    found = imported.lint_text("DELETE FROM t\nINSERT INTO t SELECT 1")
    check(buf.getvalue() == "" and [f.rule for f in found] == ["R1"],
          "importing the module prints nothing and exposes the core")

    return summary(passed[0], failed)


# ------------------------------------------------------------------------ cli

def _parse(argv):
    ap = argparse.ArgumentParser(
        prog="txnlint.py",
        description="Find the T-SQL transaction mistakes that only show across GO batches.",
        epilog="Nothing is written without --apply.",
        allow_abbrev=False,
    )
    ap.add_argument("paths", nargs="*", metavar="PATH",
                    help="a .sql file, or a folder that is searched for .sql files")
    ap.add_argument("--rules", help="comma-separated rule ids to run (default %s, plus "
                                    "R6 when --unc-host is given)" % ",".join(DEFAULT_RULES))
    ap.add_argument("--unc-host", dest="unc_hosts", action="append", metavar="HOST",
                    help="a host name your UNC paths may use. Repeat for more. Turns R6 on.")
    ap.add_argument("--out", help="path for the findings CSV")
    ap.add_argument("--apply", action="store_true",
                    help="write --out. Without this nothing is written.")
    ap.add_argument("--self-test", dest="self_test", action="store_true",
                    help="run the offline assertions and exit")
    return ap.parse_args(argv)


def main(argv=None):
    try:
        args = _parse(sys.argv[1:] if argv is None else argv)
    except SystemExit as exc:
        # argparse exits 2 on a flag it refuses, and 2 here means a file could
        # not be read. A mistyped --ap is a usage error, so it gets 64.
        return 64 if exc.code == 2 else exc.code

    if args.self_test:
        return self_test()

    if not args.paths:
        print("error: give a .sql file or a folder. Use --self-test to verify the "
              "tool without one.", file=sys.stderr)
        return 64
    try:
        hosts = parse_hosts(args.unc_hosts)
        rules = parse_rules(args.rules, bool(hosts))
    except ValueError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 64
    if args.apply and not args.out:
        print("error: --apply needs --out.", file=sys.stderr)
        return 64

    try:
        files = collect(args.paths)
    except OSError as exc:
        print("error: %s" % safe(exc), file=sys.stderr)
        return 2
    if not files:
        print("error: no .sql file found under %s." % safe(", ".join(args.paths)),
              file=sys.stderr)
        return 2
    if args.out:
        # Writing the report over a script would destroy the script.
        target = os.path.normcase(os.path.realpath(args.out))
        if any(os.path.normcase(os.path.realpath(f)) == target for f in files):
            print("error: --out names a file this run reads. Choose another path.",
                  file=sys.stderr)
            return 64

    print("txnlint: rules %s%s" % (" ".join(rules),
          "" if "R6" in rules else "; R6 is off (name hosts with --unc-host to turn it on)"))
    findings, notes = [], []
    for f in files:
        try:
            text = read_sql(f)
        except (OSError, UnicodeError) as exc:
            print("error: %s: %s" % (safe(f), safe(exc)), file=sys.stderr)
            return 2
        got, more = analyse(text, f, rules, hosts)
        findings += got
        notes += more

    for line in describe(findings, len(files), notes):
        print(line)

    if args.out:
        if args.apply:
            try:
                write_findings(args.out, findings)
            except OSError as exc:
                # Exit 1 means "a finding", so a failed write needs its own code.
                print("error: could not write %s: %s" % (safe(args.out), safe(exc)),
                      file=sys.stderr)
                return 2
            print("wrote %s" % safe(args.out))
        else:
            print("Check only. %s was not written. Re-run with --apply." % safe(args.out))
    return gate(findings, notes)


if __name__ == "__main__":
    sys.exit(main())
