# txnlint

Find the T-SQL transaction mistakes that only show when you read a script across its `GO` batches.
One standard-library file. It reads `.sql` files, opens no connection, and writes a file only with `--apply`.
The one other write is `--self-test`: it builds fixture files in a temporary folder and deletes the folder when it ends.

A nightly script reloads a lookup table. It starts with `SET XACT_ABORT ON`, so its author believes
that any error rolls everything back. Next comes a guard: if the staging table is empty, `THROW`.
Then a `GO`, then `DELETE FROM dbo.Lookup`, then the `INSERT` that refills it.

One night the staging table is empty. The guard throws, and the error reaches the log. The `GO`
sends the next batch anyway. The `DELETE` empties the lookup table, the `INSERT` loads zero rows,
and the labels on the map go blank. On another night the `INSERT` is killed half way. The result is
the same: the `DELETE` had already committed by itself, and `XACT_ABORT` had no transaction to roll back.

Each of those statements is fine on its own. The mistake is in where they sit between `BEGIN`,
`COMMIT`, `ROLLBACK`, `CATCH` and `GO`. That is what this tool reads.

```
$ python txnlint.py --self-test
txnlint self-test: no SQL Server, no network, no credentials
--------------------------------------------------------------------
PASS  GO, go and GO with a count each end a batch
...
PASS  a GO line inside a multi-line string is text, not a batch break  <-- pinned defect
...
PASS  a DELETE or UPDATE through an alias names the table behind the alias  <-- pinned defect
...
PASS  a write straight after SET ... ON, with no semicolon, is still a statement  <-- pinned defect
...
PASS  a bare DELETE then an INSERT into the same table is R1
...
PASS  a ROLLBACK in an early-exit branch does not end the transaction for the code after it  <-- pinned defect
...
PASS  SET XACT_ABORT ON with two bare writes is R2
...
PASS  a log INSERT in CATCH before the ROLLBACK is R3
...
PASS  a CATCH that tests XACT_STATE before it writes is not R3, and one that does not is  <-- pinned defect
...
PASS  a THROW guard in one batch and a DELETE in the next is R5
...
PASS  a RAISERROR guard and a write in the same batch is R5, because the batch goes on  <-- pinned defect
...
PASS  a listed host needs exactly two backslashes, in any case
...
PASS  a script made only of dynamic SQL exits 3, not the 0 that means clean  <-- pinned defect
...
PASS  a unique prefix --ap of a long flag is refused with 64  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the ValueError it is looking for
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
171 assertions, 0 failed
```

The full run prints all 171 assertions. Each `...` line is where this block is cut. A line marked
`<-- pinned defect` guards a case that an earlier draft of this tool, or an earlier version of its
own test, got wrong. Bugs found in review are pinned the same way.

## What already exists

Several tools sit near this ground. I read their documentation and rule lists on 2026-10-13.
Treat this list as what I found, not as everything that exists: a broader search finds more tools than a narrow one.

- **[TSQLLint](https://github.com/tsqllint/tsqllint)** (MIT) is a configurable T-SQL linter. You install
  it as a .NET tool, with Homebrew, or with npm. Each rule has a severity of off, warning or error. It has
  an editor extension, inline disable comments and a plugin interface. It does the per-statement and
  style work well. Its rule pages include `delete-where` (a `DELETE` with no `WHERE`),
  `cross-database-transaction` and `duplicate-go`. None of its 31 rule pages mentions `XACT_ABORT`,
  `ROLLBACK`, `CATCH` or `THROW`.
- **[SqlServer.Rules](https://github.com/tcartwright/SqlServer.Rules)** (MIT) is a library of code-analysis
  rules for SQL Server Data Tools projects. It runs at build time. Its rule list holds
  `SRD0009`, "wrap multiple action statements within a transaction", and `SRD0013`, "wrap TRY..CATCH around
  multiple data manipulation statements". Both ask whether statements are wrapped. Neither looks at the
  order of a `DELETE` and an `INSERT`, at `XACT_ABORT`, at the order inside a `CATCH` block, or at a script
  of several batches.
- **[sqlfluff](https://github.com/sqlfluff/sqlfluff)** (MIT) is a Python linter and formatter for many SQL
  dialects. It parses real T-SQL grammar, including `SET XACT_ABORT`. Its four T-SQL rules (`TQ01` to `TQ04`)
  cover the `sp_` prefix, `BEGIN`/`END` around multi-statement procedures, empty batches and alias style. A
  search of its source finds `XACT_ABORT` only in the dialect files and the test fixtures.
- **[TeamTools.Linter.TSQL](https://github.com/IVNSTN/TeamTools.Linter.TSQL)** (MIT) is a .NET library of
  T-SQL rules with more than 500 documentation pages. Its transaction rules include `CS0920` (a
  `BEGIN TRAN` with no matching `COMMIT` or `ROLLBACK`), `CS0151` (`COMMIT` in `CATCH`), `CS0737`
  (`XACT_ABORT` in a trigger) and `CS0931` (a reused transaction name). I found no rule for a bare
  `DELETE` then `INSERT`, for `XACT_ABORT` with writes outside a transaction, for a write before `ROLLBACK`
  in a `CATCH`, or for a guard across `GO`.
- **[planizer](https://github.com/mustafaabasaran/planizer)** (MIT) is an offline migration linter for SQL
  Server. It has 53 rules. It also covers locking, table rewrites and reversibility, which this tool does not.
  Its transaction rules `MSSQL-TRAN-001` to `006` follow `XACT_ABORT` and `BEGIN TRAN` across `GO`:
  a transaction without `XACT_ABORT`, an unbalanced `BEGIN`/`COMMIT`/`ROLLBACK`, a transaction that spans
  batches, `BEGIN TRAN` in `TRY` with no `ROLLBACK` in `CATCH`, and a `CATCH` that swallows the error. They ask
  the opposite question to R2, and I found nothing like R1, R3 or R5. If you need a full migration-safety
  linter, use planizer; this file does not replace it.

Two smaller tools also came up. `microsoft/sqlskim` last changed in 2022 and has two rules (small
variable-length types, unused variables). `MLgentDev/tsqlscope` (MIT) has ten rules, none about transactions.

On 2026-10-13 I ran `gh search repos` for `txnlint`, `tsql transaction lint`, `tsql lint GO batch`,
`sql server linter transaction` and `tsql xact_abort lint`. None returned a repository. The broader
queries `tsql lint`, `tsql linter`, `t-sql lint`, `t-sql analyzer` and `tsql static analysis` returned
`microsoft/sqlskim`, `MLgentDev/tsqlscope`, some editor plugins and a few proofs of concept. They did not
return most of the tools listed above, so a search is a weak proof that nothing exists. I name the tools I know of.

What this tool adds is narrow. R1, R2, R3 and R5 are four rules about where a write sits between `BEGIN`,
`COMMIT`, `ROLLBACK`, `CATCH` and `GO`, in one file with no dependency. It follows the transaction depth,
the `XACT_ABORT` setting and the guard from one `GO` batch to the next.

## Requirements

Python 3.9 or later and nothing else. There is no package to install, and `--self-test` needs no SQL Server.

```
git clone https://github.com/uhsear/txnlint.git
python txnlint.py --self-test
```

## Usage

Check one file, or a folder. A folder is searched for `.sql` files, in sorted order. These three small
synthetic scripts are the whole `demo` folder. The first, `demo/reload_lookup.sql`, is the story at the top:

```
SET XACT_ABORT ON;
GO
IF NOT EXISTS (SELECT 1 FROM dbo.Staging)
    THROW 50001, N'staging is empty', 1;
GO
DELETE FROM dbo.Lookup;
INSERT INTO dbo.Lookup (code, label)
SELECT code, label FROM dbo.Staging;
GO
```

The second, `demo/safe_reload.sql`, is the fixed script. It puts `:on error exit` first and wraps the pair in `BEGIN TRANSACTION` and `COMMIT`:

```
:on error exit
SET XACT_ABORT ON;
GO
BEGIN TRANSACTION;
DELETE FROM dbo.Lookup;
INSERT INTO dbo.Lookup (code, label)
SELECT code, label FROM dbo.Staging;
COMMIT;
GO
```

The third, `demo/log_failure.sql`, logs a failure in the `CATCH` block before it rolls back:

```
CREATE OR ALTER PROCEDURE dbo.usp_Refresh
AS
BEGIN
    SET XACT_ABORT ON;
    BEGIN TRY
        BEGIN TRANSACTION;
        UPDATE dbo.Lookup SET label = UPPER(label);
        COMMIT;
    END TRY
    BEGIN CATCH
        INSERT INTO dbo.RunLog (msg) VALUES (ERROR_MESSAGE());
        IF @@TRANCOUNT > 0 ROLLBACK;
        THROW;
    END CATCH
END
GO
```

With all four default rules on, the folder gives four findings. The story file has one finding for each
mistake in the story. This is the output of a Linux run. On Windows the path separator is a backslash.

```
$ python txnlint.py demo
txnlint: rules R1 R2 R3 R5; R6 is off (name hosts with --unc-host to turn it on)
demo/log_failure.sql:11: R3 A write (INSERT) to dbo.runlog in the CATCH block runs before the ROLLBACK at line 12. An uncommittable transaction cannot run a write, and a write that does run is undone by the ROLLBACK. Put ROLLBACK first, or log to a table variable.
demo/reload_lookup.sql:1: R2 SET XACT_ABORT ON rolls back an open transaction, and none is open here. 2 writes (lines 6, 7) run outside a transaction, so each commits by itself. Put them between BEGIN TRANSACTION and COMMIT.
demo/reload_lookup.sql:4: R5 THROW at line 4 reports an error, but the next GO sends batch 3 (line 6, DELETE on dbo.lookup) anyway. Run the script with sqlcmd -b or put :on error exit above it, or move the check into the batch that writes.
demo/reload_lookup.sql:6: R1 DELETE FROM dbo.lookup runs outside a transaction and commits by itself. The INSERT at line 7 can fail or be killed, and the removed rows stay removed. Put both statements in one BEGIN TRANSACTION and COMMIT.
3 file(s) read, 4 finding(s).
```

The fixed script gives no finding:

```
$ python txnlint.py demo/safe_reload.sql
txnlint: rules R1 R2 R3 R5; R6 is off (name hosts with --unc-host to turn it on)
1 file(s) read, 0 finding(s).
```

Write the findings to a CSV only with `--apply`. Without it, nothing is written and the tool says so.

```
$ python txnlint.py demo/reload_lookup.sql --out findings.csv
txnlint: rules R1 R2 R3 R5; R6 is off (name hosts with --unc-host to turn it on)
...
Check only. findings.csv was not written. Re-run with --apply.

$ python txnlint.py demo/reload_lookup.sql --out findings.csv --apply
...
wrote findings.csv
```

Some code cannot be read. `EXEC (@stmt)` runs a string that the tool does not see, and `:r` pulls in another
file. When such code may hide a write, the tool prints a note. If it found nothing else, it exits with 3
instead of 0, so a clean result never covers code it could not read.

For a file `dynamic.sql` that holds only `EXEC (@stmt);`:

```
$ python txnlint.py dynamic.sql
txnlint: rules R1 R2 R3 R5; R6 is off (name hosts with --unc-host to turn it on)
dynamic.sql:1: note: EXEC runs a string that is not read, and it may hold a write
1 file(s) read, 0 finding(s).
1 place(s) hold code that was not read. A clean result does not cover them.
$ echo $?
3
```

| Flag | Default | What it does |
|---|---|---|
| `PATH ...` | none | A `.sql` file, or a folder that is searched for `.sql` files. At least one is required. |
| `--rules` | `R1,R2,R3,R5` | The rule ids to run, separated by commas. Case does not matter. |
| `--unc-host HOST` | none | A host name your UNC paths may use. Repeat it for more. It turns R6 on. |
| `--out FILE` | none | The path for the findings CSV. It is written only with `--apply`. |
| `--apply` | off | Write `--out`. Nothing is written without it, and it needs `--out`. |
| `--self-test` | off | Run the offline assertions and exit. |

The parser does not accept abbreviations. `--ap` is refused with exit code 64, and it writes nothing,
even next to `--out`. Argparse would otherwise read `--ap` as `--apply`.

`--out` may not name a file that the same run reads. The tool refuses, so a typo cannot overwrite a script.

## The rules

Each rule reports at the line of the statement that causes the problem. R4 is not used. The ids of
the first four rules stay as they are, so a later rule can take R4 without renumbering.

**R1. A `DELETE` or `TRUNCATE` outside a transaction, then an `INSERT` into the same table.**
Each statement outside a transaction is committed or rolled back when it completes, so the `DELETE`
is permanent before the `INSERT` starts. A run-time error rolls back only the statement that failed.
A `TRUNCATE TABLE` can be rolled back, but only inside a transaction. Fix: put both statements
between `BEGIN TRANSACTION` and `COMMIT`. The tool matches table names by their tail, ignoring case and
brackets. It ignores temp tables and table variables. It follows an alias, so `DELETE b FROM dbo.Helper b JOIN ...`
is a `DELETE` of `dbo.Helper`. It reads a table name that starts with a sqlcmd variable, such as `$(Schema).Lookup`, so a `DELETE` and an
`INSERT` that name it are paired. A `ROLLBACK` that sits under `IF`, `ELSE` or `WHILE`, or in a `BEGIN`/`END` block that one of
them controls, does not end the transaction for the code after it. That keeps the early-exit idiom
`IF @@ERROR <> 0 BEGIN ROLLBACK TRAN RETURN END` from hiding the pair that follows it. Any other `ROLLBACK`
ends the transaction.

**R2. `SET XACT_ABORT ON`, with two or more writes outside any transaction.** `XACT_ABORT` rolls back
the transaction that a run-time error finds. With no open transaction, each write has already committed.
The setting lasts across `GO`, so a `SET` in batch 1 applies in batch 5. Inside a procedure only that
procedure's own `SET` counts, because the value is restored when the procedure returns. Fix: open a
transaction.

**R3. A write in a `CATCH` block before the `ROLLBACK` that follows it.** After an error, a transaction
can be uncommittable, and it can then run only reads and a `ROLLBACK`. If it is still committable, the
`ROLLBACK` that follows undoes the write. Either way the log row is lost. A table variable survives a
`ROLLBACK`, so a write to one is not reported. A `CATCH` that tests `XACT_STATE()` before it writes is
assumed to branch on the answer, and it is not reported. Fix: put `ROLLBACK` first, then log.

**R5. A `THROW` or `RAISERROR` guard, then a write that runs although the guard fired.** `GO` is a command of
the client tools. Each batch it ends is sent separately. A `THROW` outside `TRY...CATCH` ends its own batch
only, so the rule reports a write in a later batch. By default, `sqlcmd` prints the error message and goes on.
It exits on an error only with `-b` or `:on error exit`. Fix: run the script with `sqlcmd -b`, or put
`:on error exit` above the guard, or move the check into the batch that writes.
A guard is reported once, at the first write that it fails to stop. A write in a later batch counts even
when that batch has a guard of its own, because the earlier guard did not stop it.

`RAISERROR` is different. Microsoft's `THROW` page says a `THROW` with no `TRY...CATCH` ends the statement batch.
The `RAISERROR` page says no such thing, so the tool reads a `RAISERROR` as a message that ends nothing.
A write after it in the same batch is reported, unless a `RETURN` or `GOTO` follows the `RAISERROR` in the same
block before any write. A `RETURN` that is itself the body of an `IF` does not count. For a `RAISERROR` the fix
is to follow it with `RETURN`, or to use `THROW`; moving the check next to the write does not help.
`:on error exit` turns R5 off until an `:on error ignore`. A `RAISERROR` with a literal severity below 11 is not
a guard. A guard in a `TRY` block is not a guard, because it jumps to `CATCH`. R5 also reads procedure bodies,
where only the same-batch `RAISERROR` case can occur.

**R6. A UNC path with the wrong number of leading backslashes.** It runs only when you name hosts with
`--unc-host`. A path to a listed host that starts with one, three or four backslashes is reported. So is a
path with exactly two backslashes to a host that is not on the list, which catches a mistyped host. The
rule reads string literals, including SQL inside SQL, and ignores comments. It never prints the path.
Without the list the tool cannot tell a lost backslash from a real root-relative path such as `\Windows\Temp`,
so R6 is off, and the first line of the output says so. `--rules R6` without a host is a usage error.

**E0. A quote or block comment that is never closed.** The tool stops reading that file, because
nothing after the quote can be trusted. It reports E0 and exits 1. A missing quote therefore never gives a clean pass.

**Notes.** A note is not a finding and is not written to the CSV. It marks a place where code may hold a write
that the tool did not read: `EXEC (...)` of a string that holds `INSERT`, `UPDATE`, `DELETE`, `MERGE` or
`TRUNCATE` or is not a plain literal, `sp_executesql` with such a string, and a `:r` line. `EXEC` of a procedure
is not a note, because the procedure body is another script. Notes are off when only R6 runs.

## What the SQL Server documentation says

Each fact below comes from the Microsoft Learn page named beside it. I read each page, or its Markdown source in the
`MicrosoftDocs/sql-docs` repository, on 2026-10-13. I did not reproduce them on a SQL Server instance, because the
build environment had none. The self-test checks what `txnlint` does with these facts. It does not check the facts.

| Fact the rules rely on | Page |
|---|---|
| `GO` is not a T-SQL statement. The client tools send the batch before it. A comment may share its line. A count repeats the batch. | [GO](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/sql-server-utilities-statements-go) |
| Without `TRY...CATCH`, `THROW` ends the statement batch. `THROW` honours `XACT_ABORT`. `RAISERROR` does not honour `XACT_ABORT`. | [THROW](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/throw-transact-sql) |
| `RAISERROR` does not honour `XACT_ABORT`. At severity 11 or more inside `TRY` it goes to `CATCH`. The page does not say that it ends the batch, so R5 assumes it does not. | [RAISERROR](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/raiserror-transact-sql) |
| With `XACT_ABORT` ON a run-time error ends and rolls back the whole transaction. | [SET XACT_ABORT](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-xact-abort-transact-sql) |
| A statement outside a transaction is committed or rolled back as it completes. An error rolls back only that statement. | [Transaction guide](https://learn.microsoft.com/en-us/sql/relational-databases/sql-server-transaction-locking-and-row-versioning-guide) |
| `TRUNCATE TABLE` can be rolled back within a transaction. | [TRUNCATE TABLE](https://learn.microsoft.com/en-us/sql/t-sql/statements/truncate-table-transact-sql) |
| An uncommittable transaction can only read or roll back. `TRY...CATCH` cannot span batches. | [TRY...CATCH](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/try-catch-transact-sql), [XACT_STATE](https://learn.microsoft.com/en-us/sql/t-sql/functions/xact-state-transact-sql) |
| Transaction rollbacks do not affect table variables. | [table](https://learn.microsoft.com/en-us/sql/t-sql/data-types/table-transact-sql) |
| A `SET` inside a procedure is restored when the procedure returns. | [SET statements](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-statements-transact-sql) |
| With `SET IMPLICIT_TRANSACTIONS ON`, an `INSERT`, `DELETE` and similar statements begin a transaction when `@@TRANCOUNT` is 0. | [SET IMPLICIT_TRANSACTIONS](https://learn.microsoft.com/en-us/sql/t-sql/statements/set-implicit-transactions-transact-sql) |
| `ROLLBACK TRANSACTION savepoint_name` rolls back only the part after the savepoint. | [ROLLBACK TRANSACTION](https://learn.microsoft.com/en-us/sql/t-sql/language-elements/rollback-transaction-transact-sql) |
| `sqlcmd -b` exits with an `ERRORLEVEL` on an error. `:On Error exit` and `ignore` exist, the default is to print an error message, and commands are read only at the start of a line. `:r` reads and runs another file. A `RAISERROR` with state 127 quits `sqlcmd`. | [sqlcmd](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-utility), [sqlcmd commands](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-commands) |
| A sqlcmd scripting variable `$(name)` can stand inside a statement, for example `SELECT x.$(ColumnName)`. Variables are not expanded inside a string. | [sqlcmd scripting variables](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-use-scripting-variables), [sqlcmd utility](https://learn.microsoft.com/en-us/sql/tools/sqlcmd/sqlcmd-use-utility) |
| A UNC path starts with `\\`, then a host. A path that starts with one `\` is relative to the root of the current drive. | [File path formats](https://learn.microsoft.com/en-us/dotnet/standard/io/file-path-formats) |

## What was run, and where

The self-test runs every claim below. The right-hand column is the start of the assertion's label.
Nothing in this table was described and left unrun. A folder that cannot be listed, and the hard-link and
symbolic-link limits, were also run once by hand on Linux. They are noted under Measured.

| Claim | Exercised by |
|---|---|
| Only a lone `GO` ends a batch, with count and comment, not inside a string or comment | "GO, go and GO with a count", "a comment on the GO line", "a GO line inside a multi-line string", "a GO line inside a block comment", "GOTO and a GO at the end" |
| Line numbers survive CRLF, multi-line strings and nested comments | "CRLF line endings", "line numbers stay right", "block comments nest" |
| An unclosed quote or comment fails closed | "an unclosed string", "bracket name", "double-quoted name", "block comment" (four assertions), and "an unclosed quote exits 1 through the command line" |
| Verbs in MERGE, DDL, grants, hints, strings and comments are not writes | "the UPDATE, DELETE and INSERT inside a MERGE", "verbs inside DDL, grants, hints, strings and comments", "every ON DELETE and ON UPDATE action" |
| A write straight after `SET ... ON` is a statement even with no semicolon | "a write straight after SET ... ON", "a bare pair straight after SET ... ON", "R2 counts writes that follow SET XACT_ABORT ON" |
| A sqlcmd `$(variable)` is part of a name | "a sqlcmd $(variable) is part of a name", "a table named through a sqlcmd variable" |
| R1 reports a bare pair, across batches, through an alias, and not inside a transaction | "a bare DELETE then an INSERT", "a bare TRUNCATE", "the same pair inside BEGIN TRANSACTION", "a DELETE in one batch and its INSERT in the next", "R1 sees a DELETE written through an alias", "a transaction opened before GO", "after ROLLBACK", "one COMMIT of two nested", "one DELETE followed by two INSERTs" |
| A conditional `ROLLBACK` does not end the transaction, and any other one does | "a ROLLBACK in an early-exit branch", "a ROLLBACK under IF, ELSE or WHILE", "a ROLLBACK that nothing makes conditional", "a nested condition is skipped whole", "the search for an IF stops" |
| R1, R2 and R3 respect implicit transactions and savepoints | "with implicit transactions on" (three assertions), "ROLLBACK to a savepoint leaves the transaction open", "a ROLLBACK to a savepoint does not count" |
| R2 sees `XACT_ABORT` across `GO` and ignores scratch tables | "SET XACT_ABORT ON with two bare writes", "XACT_ABORT set in one batch", "inside a procedure only its own SET counts", "scratch writes do not count", "R2 points at the SET", "R2 lists five lines" |
| R3 reports a write before the `ROLLBACK`, and spares the safe shapes | "a log INSERT in CATCH", "the same CATCH with the ROLLBACK first", "a table variable survives ROLLBACK", "a CATCH that tests XACT_STATE", "a write before the XACT_STATE test", "a CATCH that commits and never rolls back", "a stray END TRY opens no transaction" |
| R5 reports a THROW guard and a later write, and honours `:on error` and `TRY` | "a THROW guard in one batch", ":on error exit silences R5", "a THROW in TRY jumps to CATCH", "each guard is reported once", "a later batch with a guard of its own", "the innermost TRY decides", "a guard in a block inside TRY" |
| R5 reads `RAISERROR` as a message that ends nothing, unless a `RETURN` follows | "RAISERROR is a guard at severity 11", "a RAISERROR guard and a write in the same batch", "a RETURN or GOTO in the same block", "a RETURN that is conditional", ":on error exit, a THROW, a RAISERROR in TRY", "a RAISERROR severity is read after an IF condition", "the advice differs", "a comma inside a nested call" |
| R6 counts backslashes for a listed host and never prints the path | "a listed host needs exactly two backslashes", "a two-backslash path to an unlisted host", "an R6 message names the host and the count and never prints the path", "a path inside dynamic SQL", "a bracketed name or a bare word" |
| `--unc-host` turns R6 on and no part of a path reaches the output or the CSV | "--unc-host turns R6 on, and no part of the path reaches the output or the CSV" |
| The default run leaves R6 off and says so | "a clean file exits 0 and says which rules ran and that R6 is off", "a bad UNC path passes while R6 is off" |
| Code that is not read gives a note and exit 3 | "EXEC of a string with a write or a variable", "a note says what was not read", "a procedure call, a harmless string", "the gate gives 3", "a script made only of dynamic SQL exits 3", "a finding wins over a note", "a run with only R6" |
| `--out` writes only with `--apply`, and never over an input | "--out without --apply writes nothing", "--out with --apply writes the header", "--out naming an input script is refused" |
| A prefix of any long flag is refused with 64 | seven assertions, "a unique prefix --ap", "--app", "--appl", "--ou", "--unc", "--rule", "--self", and "--ap next to --out writes nothing" |
| UTF-16, UTF-8 with a byte order mark and cp1252 scripts are read | "UTF-16 either way round", "a UTF-16 script with a byte order mark", "a broken UTF-16 file exits 2" |
| A folder run and the exit codes 0, 1, 2, 3 and 64 | "a folder is walked in sorted order", "a folder that cannot be listed exits 2", "a folder with no .sql file exits 2", "a file that cannot be read exits 2", "a report that cannot be written exits 2", "no path is a usage error" |
| A non-ASCII name prints as ASCII escapes | "a non-ASCII table name prints as ASCII escapes" |
| The harness can fail | the two `pinned defect` probes of `check()`, `raises()` and the footer |

There is no network code in this tool, so there is no stub server. It opens no connection to SQL Server or
to anything else. It prints the rule text with table and host names. It does not print string contents.

## Measured

- Branch coverage is 100 percent: 913 statements and 360 branches, measured with
  `coverage run --branch txnlint.py --self-test` on Windows Python 3.13.2.
- `pyflakes` is clean. `python -W error` compiles the file and runs the whole self-test without a warning.
- The self-test prints 171 assertions with 0 failed on every interpreter I ran: Python 3.9.25, 3.10.20,
  3.11.15, 3.12.10 and 3.13.2 on Windows, and Python 3.11.16 and 3.12.3 on Linux. The output text of all seven
  runs is the same once Windows' carriage returns are removed, so there is no difference to explain.
- A one-off sweep of 425 single-token mutations (swap `and` and `or`, flip a comparison, turn `break` or
  `continue` into `pass`, flip `True` and `False`) of the code above the self-test left 16 mutants that the
  self-test did not catch. I read all 16. Each is equivalent for valid input, such as `<` to `<=` on an index
  that cannot be zero, or changes only the wording of an error message. An earlier sweep of 437 mutations of
  an earlier version left 45; the real gaps it showed are pinned by assertions. The sweep script is not part of
  this repository, so you cannot repeat the sweep from here.
- 60,000 random programs made of T-SQL keywords, quotes, sqlcmd lines and odd characters ran through the core
  with no exception.
- Run once by hand on Linux as an ordinary user: a subfolder with mode 000 gives `Permission denied` and exit 2
  instead of a clean pass; `ln victim.sql hl.sql` followed by `--out hl.sql --apply` overwrites `victim.sql`
  with the CSV (listed under Limits); a folder reached only through a symbolic link gives exit 2, and a
  symbolic link beside a real folder is not followed.
- The self-test writes only inside its own temporary folder and removes it. It leaves nothing in the
  working directory.
- The file is pure ASCII and has no f-string. Python 3.9 runs it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No finding, and no code was left unread. |
| 1 | At least one finding, including E0. |
| 2 | A file could not be read or decoded, a folder could not be listed or holds no `.sql` file, or `--out` could not be written. |
| 3 | No finding, but a note says that some code was not read (`EXEC` of a string that may hold a write, `sp_executesql`, `:r`). |
| 64 | Usage error: no path, an unknown flag or abbreviation, a bad rule or host, `--apply` without `--out`, or `--out` naming an input. |

## Limits

**What it reads.**

- It is a token reader, not a T-SQL parser. It finds the shapes listed above and nothing else.
- It reads SQL in plain statements. The text inside `EXEC('...')` and `sp_executesql` is not read, except
  that R6 looks inside every string literal. The tool prints a note for such a string when it may hold a
  write, and for every `:r` line. Lint an included file on its own. A `CATCH` block that calls a logging
  procedure with `EXEC` is not seen, so R3 reports only direct writes.
- It reads a script from top to bottom. Both branches of an `IF ... ELSE` count as one run, and a loop body
  counts once. A `DELETE` in one branch and an `INSERT` in the other is reported by R1.
- It cannot know who calls a procedure. R1 and R2 read a procedure body as if nothing wraps it. A
  procedure that every caller wraps in a transaction still gets a finding. Use `--rules` to leave the rule out.
  A procedure that depends on a `SET XACT_ABORT ON` made by its caller is not reported by R2.
- `SET XACT_ABORT ON` also stops a batch at the first run-time error, even with no transaction. R2 does not
  say the setting is useless. It says the writes before the error stay committed.
- R5 cannot know how you run the script. If you always use `sqlcmd -b`, leave R5 out with `--rules`.
- An alias is resolved from the `FROM` or `JOIN` of the same statement. A derived table keeps the alias as its name.
- A savepoint is recognised by its name from `SAVE TRAN`. `ROLLBACK TRAN name` with any other name is a full rollback.
- A `ROLLBACK` under `IF`, `ELSE` or `WHILE` is read as not ending the transaction. If it did end it, a `DELETE` and
  `INSERT` after it are not reported.
- A `RAISERROR` counts as a guard that stops the code after it only if a `RETURN` or `GOTO` follows in the same
  block before any write. A script that sets a flag and tests the flag later is reported anyway.
- After `SET IMPLICIT_TRANSACTIONS ON` the tool treats every later write as inside a transaction, even after a
  `COMMIT`, so a `DELETE`, `COMMIT`, `INSERT` sequence is not reported.
- Cross-database names match on their tail, so `DELETE FROM dbo.Lookup` and `INSERT INTO OtherDb.dbo.Lookup`
  give an R1 finding for two different tables.
- A `THROW` in a `CATCH` block that sits inside an outer `TRY` is still counted as an R5 guard, although it jumps to the outer `CATCH`.
- R1 does not treat a `MERGE` as a refill of a deleted table. A `DELETE` through a CTE name
  (`WITH c AS (...) DELETE FROM c`) is read as a delete of `c`, so it does not pair with an `INSERT` into the base table.
- R5 counts only `INSERT`, `UPDATE`, `DELETE`, `MERGE` and `TRUNCATE` as writes. A later batch that only runs `EXEC` of a
  procedure, DDL (`ALTER`, `DROP`, `CREATE`) or `SELECT ... INTO` is not reported.
- A `RAISERROR` with state 127 ends `sqlcmd`, and the `:EXIT` command also ends it. The tool models neither.
- A non-breaking space between keywords is not whitespace to the tokenizer, so `DELETE<NBSP>FROM` is missed.
  SQL Server would not parse such a script.
- R1 compares every unprotected `DELETE` with every later `INSERT`. A script with 4000 `DELETE`s and 4000 `INSERT`s on
  different tables took about 10 seconds in my run.
- R6 reads each string literal on its own, so a UNC path split across a concatenation (`N'\\host' + N'\share'`) is not seen.
- R6 does not flag a listed host written with no leading backslash (`host\share`), because that looks like `server\instance`.
- R6 compares the host exactly, so `\\backupsrv.example.net\share` is reported as an unlisted host when you passed only
  `backupsrv` to `--unc-host`. List the full name too.

**What counts as a batch break.**

- Only a `GO` that is alone on its line, with an optional count and one trailing comment, ends a batch. `GO /* a */ -- b`, a `GO` with a leading
  byte order mark or no-break space, and `GO;` are read as ordinary words, so the batch break is lost. That can hide an R5.

**Files and encodings.**

- Only `.sql` files are picked up from a folder. A file named on the command line is read whatever its extension.
- A script is decoded as UTF-16 when it starts with a byte order mark, as UTF-8 otherwise, and as cp1252
  when the UTF-8 decode fails.
- A UTF-16 or UTF-32 script with no byte order mark is decoded as UTF-8. Its NUL bytes hide every keyword, and the file passes as clean with exit 0.
- A zero-byte or BOM-only `.sql` file is reported as `0 finding(s)` with exit 0, so a truncated deployment script looks clean.
- A double-quoted string, which is a string only when `QUOTED_IDENTIFIER` is off, is read as a name.
- A subfolder that is reached only through a symbolic link is not followed, and its `.sql` files are skipped without a message.
  A folder that holds only such a link exits 2 with `no .sql file found`.
- Naming the same file twice, or a file and its folder, reads it twice and doubles the findings and the file count.
- One unreadable file (a permission error or truncated UTF-16) stops the run with exit 2. It prints none of the
  findings already collected from earlier files.

**What it writes and prints.**

- `--out` refuses a path that resolves to an input file, but it does not detect a hard link to an input.
  Then `--apply` overwrites the script with the CSV. I reproduced it on Linux with `ln victim.sql hl.sql` and `--out hl.sql --apply`.
- `--out` with `--apply` overwrites any existing file that is not an input of this run, for example a sibling `.sql` script, without asking.
- The CSV is opened for writing before it is filled. A disk-full or I/O error part way through leaves a truncated report file, and the tool exits 2.
- `safe()` rewrites a non-ASCII path with backslash escapes in the report and in the CSV, so the printed path may not match the real file name.
- `safe()` escapes only non-ASCII characters. A table, alias or file name that holds a control character or an escape sequence is printed raw,
  so a hostile `.sql` file can put terminal escape codes or fake lines into the report.
- E0 exits 1, the same code as an ordinary finding. A caller must read the output to tell an unreadable file from a rule that fired.
- An unexpected internal error exits 1 with a Python traceback, the same code as "findings exist". `main()` has no catch-all that maps a crash to its own code.

## Contributing

Open an issue or pull request on GitHub.

## Author

Built by [Asir Khan](https://www.linkedin.com/in/asir-khan-310317264/).

## License

MIT.

## Related

Other single-file tools in this portfolio that pair with this one:

- [safe-republish](https://github.com/uhsear/safe-republish) - the same truncate-then-load disaster for a feature class, refused before the truncate
- [bkpbase](https://github.com/uhsear/bkpbase) - a restorable-full verdict over exported `msdb.backupset` rows
- [stepflow](https://github.com/uhsear/stepflow) - SQL Server Agent job steps that never run, found from exported `msdb` rows
- [evwlint](https://github.com/uhsear/evwlint) - views, procedures, triggers and job steps that read a versioned base table
