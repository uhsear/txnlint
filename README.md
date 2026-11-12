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
PASS  a DELETE or UPDATE through a CTE names the table in the CTE's FROM  <-- pinned defect
...
PASS  a bare DELETE then an INSERT into the same table is R1
...
PASS  a delete of the recent window is a refill, so a later INSERT is R1  <-- pinned defect
...
PASS  a ROLLBACK in an early-exit branch does not end the transaction for the code after it  <-- pinned defect
...
PASS  a BEGIN TRANSACTION that an IF or WHILE governs does not make the DELETE safe  <-- pinned defect
...
PASS  SET XACT_ABORT ON with two bare writes is R2
...
PASS  a log INSERT in CATCH before the ROLLBACK is R3
...
PASS  a CATCH that tests XACT_STATE before it writes is not R3, and one that does not is  <-- pinned defect
...
PASS  an XACT_STATE test that only prints does not spare the write after it  <-- pinned defect
...
PASS  a THROW guard in one batch and a DELETE in the next is R5
...
PASS  a RAISERROR guard and a write in the same batch is R5, because the batch goes on  <-- pinned defect
...
PASS  a write in the ELSE of an IF whose branch is a RAISERROR cannot run when it fires  <-- pinned defect
...
PASS  a listed host needs exactly two backslashes, in any case
...
PASS  an R6 message about an unlisted host does not print it, because it may be a secret  <-- pinned defect
...
PASS  a bracket-quoted sp_executesql with a write or a variable is a note, not a clean run  <-- pinned defect
...
PASS  a string joined from literals is read as one, so a split verb is a note  <-- pinned defect
PASS  a procedure run in a CATCH before the ROLLBACK is a note, and the safe shapes are not  <-- pinned defect
...
PASS  a script made only of dynamic SQL exits 3, not the 0 that means clean  <-- pinned defect
...
PASS  a path and a message that start with equals each get one leading apostrophe, and line and batch stay bare numbers  <-- pinned defect
...
PASS  a unique prefix --ap of a long flag is refused with 64  <-- pinned defect
...
PASS  UTF-16 without a BOM file exits 2 and names the cause, and does not report it clean  <-- pinned defect
...
PASS  check() and raises() really do record a failure  <-- pinned defect
PASS  and raises() accepts the ValueError it is looking for
PASS  the footer reports failures by count and by name, and exits 1  <-- pinned defect
PASS  importing the module prints nothing and exposes the core
--------------------------------------------------------------------
227 assertions, 0 failed
```

The full run prints all 227 assertions. Each `...` line is where this block is cut. A line marked
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

Check one file, or a folder. A folder is searched for `.sql` files, in sorted order. The repository
holds four files and no sample scripts, so create the three small synthetic scripts below by hand in a folder named
`demo`. The first, `demo/reload_lookup.sql`, is the story at the top:

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
instead of 0. A clean result never covers an `EXEC` of a string or a `:r` that it could not read. It does not read a
procedure body either, so a bare `EXEC dbo.usp_reload;` is clean (see Limits).

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

`--out` may not name a file that the same run reads, by any path that resolves to that same file. The tool refuses,
so a typo cannot overwrite a script. A hard link to an input is not detected (see Limits).

## The rules

Each rule reports at the line of the statement that causes the problem. R4 is not used. The ids of
the first four rules stay as they are, so a later rule can take R4 without renumbering.

**R1. A `DELETE` or `TRUNCATE` that commits, then an `INSERT` into the same table.**
Each statement outside a transaction is committed or rolled back when it completes, so the `DELETE`
is permanent before the `INSERT` starts. A run-time error rolls back only the statement that failed.
A `TRUNCATE TABLE` can be rolled back, but only inside a transaction. Fix: put both statements
between `BEGIN TRANSACTION` and `COMMIT`. A purge of old rows is not a refill and is not reported: a `DELETE` whose
`WHERE` clause tests a date function against a cut-off in the past, such as `WHERE LogDate < DATEADD(DAY, -90, GETDATE())`
or `WHERE DATEDIFF(DAY, LogDate, GETDATE()) > 90`, may be followed by an `INSERT` into the same table (a log line, say)
with no finding. The date function must sit on the right of a `<` or on the left of a `>`. A delete of the recent window,
such as `WHERE LoadDate > DATEADD(DAY, -1, GETDATE())`, removes the newest rows and is still paired. Equality on today's
date, a key test, a `DELETE` with no `WHERE` and every `TRUNCATE` are still paired. A `DELETE` inside a transaction is reported too when that
transaction is committed before the `INSERT` runs, in a later batch or in a second transaction: the rows are
gone by then, and a failed `INSERT` leaves the table empty. An `INSERT` in the same transaction as the `DELETE`
is the safe refill and is not reported, and neither is a `DELETE` that the transaction rolls back. A `MERGE`
with a `WHEN NOT MATCHED THEN INSERT` clause counts as an `INSERT`; a `MERGE` with no `INSERT` clause does not.
The tool matches table names by their tail, ignoring case and
brackets. It ignores temp tables and table variables. It follows an alias, so `DELETE b FROM dbo.Helper b JOIN ...`
is a `DELETE` of `dbo.Helper`. It reads a table name that starts with a sqlcmd variable, such as `$(Schema).Lookup`, so a `DELETE` and an
`INSERT` that name it are paired. A `ROLLBACK` that sits under `IF`, `ELSE` or `WHILE`, or in a `BEGIN`/`END` block that one of
them controls, does not end the transaction for the code after it only when its branch then leaves with an
unconditional `RETURN`, `GOTO` or `THROW`. That keeps the early-exit idiom
`IF @@ERROR <> 0 BEGIN ROLLBACK TRAN RETURN END` from hiding the pair that follows it. Any other `ROLLBACK`
ends the transaction, including `IF @@ERROR <> 0 ROLLBACK TRAN` with no exit: the code after it also runs on the
path that rolled back, outside any transaction, so a `DELETE` and `INSERT` there are reported. R1 does not report a
`DELETE` or `TRUNCATE` of a temp table or table variable.
A `BEGIN TRANSACTION` that an `IF` or `WHILE` governs, such as `IF @UseTran = 1 BEGIN TRANSACTION`, opens a
transaction on one path only. The tool does not count it, so the `DELETE` after it is reported. A condition that reads
`@@TRANCOUNT` or `XACT_STATE()` is the exception: `IF @@TRANCOUNT = 0 BEGIN TRANSACTION` leaves a transaction open on
every path, because the other path already had one.

**R2. `SET XACT_ABORT ON`, with two or more writes outside any transaction.** `XACT_ABORT` rolls back
the transaction that a run-time error finds. With no open transaction, each write has already committed.
The setting lasts across `GO`, so a `SET` in batch 1 applies in batch 5. Inside a procedure only that
procedure's own `SET` counts, because the value is restored when the procedure returns. Fix: open a
transaction.

**R3. A write in a `CATCH` block before the `ROLLBACK` that follows it.** After an error, a transaction
can be uncommittable, and it can then run only reads and a `ROLLBACK`. If it is still committable, the
`ROLLBACK` that follows undoes the write. In both cases the log row is lost while the transaction is still open. A table variable survives a
`ROLLBACK`, so a write to one is not reported. A write that an `IF` or `ELSE` on `XACT_STATE()` governs, directly
or through a `BEGIN` block, is assumed to branch on the answer, and it is not reported. A `XACT_STATE()` test
that governs nothing, such as `IF XACT_STATE() = -1 PRINT ...;` followed by the log `INSERT`, is not a branch,
and the `INSERT` is reported. A test of `@@TRANCOUNT` does not spare a write either. Fix: put `ROLLBACK` first, then log.

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
a guard. A guard in a `TRY` block jumps to the `CATCH` block. If that `CATCH` holds a `THROW` or a
`RAISERROR` of its own, the error goes on and that statement is the guard. If the `CATCH` only prints or logs,
it swallows the error and the script goes on. The guard in the `TRY` is then reported at the first write in a later
batch, and `sqlcmd -b` and `:on error exit` do not change that, because the error never reaches `sqlcmd`. The fix is
a `THROW` in the `CATCH` block, or the check in the batch that writes. A write in the same batch, after the guard in
the `TRY`, is never reported, because the jump to `CATCH` skips it. R5 also reads procedure bodies,
where only the same-batch `RAISERROR` case can occur.

A write in the `ELSE` branch of the `IF` that holds the `RAISERROR` is not reported for that guard, because the
`ELSE` does not run when the guard fires: `IF @x = 0 RAISERROR(...) ELSE DELETE FROM dbo.Lookup;`. This holds when
the `RAISERROR` is in a `BEGIN`/`END` block under the `IF`, when the `ELSE` holds an `IF` chain or a block, and
through every enclosing `IF` block. A write after the whole `IF ... ELSE` still runs, and is still reported.

**R6. A UNC path with the wrong number of leading backslashes.** It runs only when you name hosts with
`--unc-host`. A path to a listed host that starts with one, three or four backslashes is reported. So is a
path with exactly two backslashes to a host that is not on the list, which catches a mistyped host. The
rule reads string literals, including SQL inside SQL, and ignores comments. It prints the backslash count and the name of a listed host, never an unlisted host name from the script and never the rest of the path.
Without the list the tool cannot tell a lost backslash from a real root-relative path such as `\Windows\Temp`,
so R6 is off, and the first line of the output says so. `--rules R6` without a host is a usage error.
A listed short name also covers its fully qualified form: with `--unc-host backupsrv`, a two-backslash path to
`backupsrv.corp.example` is not reported. The match is on a whole first label, so `backupsrv2.corp.example` is.

**E0. A quote or block comment that is never closed.** The tool stops reading that file, because
nothing after the quote can be trusted. It reports E0 and exits 1. A missing quote therefore never gives a clean pass.

**Notes.** A note is not a finding and is not written to the CSV. It marks a place where code may hold a write
that the tool did not read: `EXEC (...)` of a string that holds `INSERT`, `UPDATE`, `DELETE`, `MERGE` or
`TRUNCATE`, or is not made only of string literals, `sp_executesql` with such a string, and a `:r` line. Literals
joined with `+` are read as one string, so `N'DE' + N'LETE FROM dbo.Lookup'` is a note and `'SELECT ' + 'name'` is
not. A variable, a function call or a number in the string is always a note. `EXEC` of a procedure in a `CATCH`
block whose `TRY` held a transaction is a note when it runs before the `ROLLBACK`, because a procedure that
logs a row there is the R3 mistake in another script. An `IF` on `XACT_STATE()` that governs the `EXEC` spares it.
`EXEC` of a procedure is otherwise a note only when it follows a `DELETE` or `TRUNCATE` that runs outside a transaction (on a table that is not a
temp table or table variable): the procedure body is another script, and it may refill the table. `EXEC` of a
procedure that fits neither case is not a note. Notes are off when only R6 runs.

For a file `proc.sql` with `DELETE FROM dbo.Lookup;`, `GO`, `EXEC dbo.Refill;` and `GO` on four lines:

```
$ python txnlint.py proc.sql
txnlint: rules R1 R2 R3 R5; R6 is off (name hosts with --unc-host to turn it on)
proc.sql:3: note: EXEC runs a procedure that is not read, after DELETE FROM dbo.lookup at line 1 outside a transaction. It may refill the table.
1 file(s) read, 0 finding(s).
1 place(s) hold code that was not read. A clean result does not cover them.
```

The exit code is 3.

## The formula guard

The CSV holds text that comes from outside the tool: the `path` of each script and the `message` of each
finding. A message names tables and aliases that the script chose. A spreadsheet runs a text cell as a
formula when it starts with `=`, `+`, `-`, `@`, a tab or a carriage return. OWASP's
[CSV Injection](https://owasp.org/www-community/attacks/CSV_Injection) page lists those six characters,
and also line feed and the full-width forms of the first four. Python's `csv` module writes a cell exactly
as given. Before this guard, a script named `=evil.sql` reached the file as a cell that starts with `=`.

`write_findings` now passes the `path` and `message` cells through `cell()`:

- A text cell that starts with one of the six characters gets one leading apostrophe. `=evil.sql`
  becomes `'=evil.sql`. The apostrophe is data, so a CSV reader shows it.
- `line` and `batch` are integers and are never touched. `rule` and the header are fixed by the tool.
- Only the start of a cell matters, so `a=b` and `x-` are written as they came.
- The console report is not changed. It is not a CSV.
- The tool never reads its own CSV. No code outside the self-test calls `csv.reader`, and an assertion
  parses the source to show that. So no strip-on-read exists to get wrong.
  A consumer that joins on `path` must strip one leading apostrophe itself.

Create `=evil.sql` first, with a `DELETE FROM dbo.lookup` and an `INSERT INTO dbo.lookup` in it and no
transaction. If the file does not exist, the run exits 2 with `No such file`.

```
$ python txnlint.py --out findings.csv --apply -- =evil.sql
...
=evil.sql:1: R1 DELETE FROM dbo.lookup runs outside a transaction and commits by itself. ...
wrote findings.csv
$ cat findings.csv
path,line,batch,rule,message
'=evil.sql,1,1,R1,"DELETE FROM dbo.lookup runs outside a transaction and commits by itself. ..."
```

The `--` ends the options, so the name `=evil.sql` cannot be read as a flag. A script named `-evil.sql` needs it.

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
| A `BEGIN TRANSACTION` under an `IF` or `WHILE` does not count, unless the condition reads `@@TRANCOUNT` or `XACT_STATE()` | "a BEGIN TRANSACTION that an IF or WHILE governs", "IF @@TRANCOUNT = 0 BEGIN TRANSACTION, the XACT_STATE() form and a plain block", "an ELSE with no IF before it" |
| R1 reports a bare pair, across batches, through an alias, and not inside a transaction | "a bare DELETE then an INSERT", "a bare TRUNCATE", "the same pair inside BEGIN TRANSACTION", "a DELETE in one batch and its INSERT in the next", "R1 sees a DELETE written through an alias", "a transaction opened before GO", "after ROLLBACK", "one COMMIT of two nested", "one DELETE followed by two INSERTs", "a purge of old rows by date is not a refill", "an OR in the WHERE clause can match every row, so it is not a purge", "a delete of the recent window is a refill", "the date function on the old side of < or >", "an equality on today, a key test, a delete of every row and a truncate are still R1" |
| R1 reports a `MERGE` with a `THEN INSERT` as a refill, and a `DELETE` that commits in its own transaction before the `INSERT`, and spares the safe shapes | "a MERGE with WHEN NOT MATCHED THEN INSERT refills the table like an INSERT", "a MERGE with no INSERT clause is no refill", "a DELETE committed in its own transaction, then an INSERT in a later one", "a rolled back delete, a refill in the same transaction, scratch tables, nested transactions and another table are not R1" |
| A conditional `ROLLBACK` that exits keeps the transaction open, and any other one ends it | "a ROLLBACK in an early-exit branch", "a ROLLBACK under IF, ELSE or WHILE with no exit", "a ROLLBACK branch that ends in THROW, GOTO or an unconditional RETURN", "a ROLLBACK that nothing makes conditional", "a nested condition is skipped whole", "the search for an IF stops" |
| R1, R2 and R3 respect implicit transactions and savepoints | "with implicit transactions on" (three assertions), "ROLLBACK to a savepoint leaves the transaction open", "a ROLLBACK to a savepoint does not count" |
| R2 sees `XACT_ABORT` across `GO` and ignores scratch tables | "SET XACT_ABORT ON with two bare writes", "XACT_ABORT set in one batch", "inside a procedure only its own SET counts", "scratch writes do not count", "R2 points at the SET", "R2 lists five lines" |
| R3 reports a write before the `ROLLBACK`, and spares the safe shapes | "a log INSERT in CATCH", "the same CATCH with the ROLLBACK first", "a table variable survives ROLLBACK", "a CATCH that tests XACT_STATE", "an XACT_STATE test that only prints", "a write that an IF or ELSE on XACT_STATE governs", "a write before the XACT_STATE test", "a CATCH that commits and never rolls back", "a stray END TRY opens no transaction" |
| R5 reports a THROW guard and a later write, and honours `:on error` and `TRY` | "a THROW guard in one batch", ":on error exit silences R5", "a re-THROW in CATCH is the guard", "a THROW in TRY is a guard when its CATCH only prints", "a swallowed guard needs a later batch", "each guard is reported once", "a later batch with a guard of its own", "the innermost TRY decides which CATCH swallows a guard", "a guard in a block inside TRY" |
| A write in the `ELSE` of the `IF` that holds a `RAISERROR` is not reported for it, and a write after the `IF` still is | "a write in the ELSE of an IF whose branch is a RAISERROR", "the same holds when the RAISERROR is in a BEGIN block", "an ELSE IF chain, a WHILE, a nested IF", "a write after the whole IF and ELSE", "the skipped branch ends at a GO", "a skipped branch is marked on and off" |
| R5 reads `RAISERROR` as a message that ends nothing, unless a `RETURN` follows | "RAISERROR is a guard at severity 11", "a RAISERROR guard and a write in the same batch", "a RETURN or GOTO in the same block", "a RETURN that is conditional", ":on error exit, a THROW, a RAISERROR in TRY", "a RAISERROR severity is read after an IF condition", "the advice differs", "a comma inside a nested call" |
| R6 counts backslashes for a listed host and never prints the path or an unlisted host name | "a listed host needs exactly two backslashes", "a listed short host also covers its fully qualified name", "a two-backslash path to an unlisted host", "an R6 message about a listed host names the host and never prints the path", "an R6 message about an unlisted host does not print it", "an R6 message about a listed short name prints the listed name", "a path inside dynamic SQL", "a bracketed name or a bare word" |
| `--unc-host` turns R6 on, and neither the host name of a path nor anything after it reaches the output or the CSV | "--unc-host turns R6 on, and nothing after the host name reaches the output or the CSV" |
| The default run leaves R6 off and says so | "a clean file exits 0 and says which rules ran and that R6 is off", "a bad UNC path passes while R6 is off" |
| Code that is not read gives a note and exit 3 | "EXEC of a string with a write or a variable", "a note says what was not read", "a procedure call, a harmless string", "a string joined from literals is read as one", "a procedure run in a CATCH before the ROLLBACK", "a procedure in a CATCH before the ROLLBACK gives exit 3", "a procedure run after an unprotected DELETE or TRUNCATE is a note", "no note for a procedure before the delete", "the gate gives 3", "a script made only of dynamic SQL exits 3", "a finding wins over a note", "a run with only R6" |
| A path or message that starts with `=`, `+`, `-`, `@`, a tab or a CR is written with one leading apostrophe, and `line` and `batch` stay numbers | six assertions "a path and a message that start with equals", "plus", "minus", "at", "tab" and "CR" (each writes a CSV with `write_findings`, reads it back with `csv.reader` and checks the raw text) |
| Text that does not start with one of them, and a number, is not changed | "text that does not start with a formula character, and a number, pass through unchanged" |
| A script whose file name starts with `=`, `+`, `-` or `@` gets a guarded `path` in the CSV and an unguarded console line, through the real command line | four assertions "a script named equals", "plus", "minus" and "at" |
| The tool reads no CSV, so there is nothing to strip on read | "outside the self-test the tool has no csv reader" |
| `--out` writes only with `--apply`, and never over an input path that resolves to the same file (a hard link is not detected, see Limits) | "--out without --apply writes nothing", "--out with --apply writes the header", "--out naming an input script is refused" |
| A prefix of any long flag is refused with 64 | eight assertions, "a unique prefix --ap", "--app", "--appl", "--ou", "--unc", "--rule", "--self", and "--ap next to --out writes nothing" |
| UTF-16, UTF-8 with a byte order mark and cp1252 scripts are read | "UTF-16 either way round", "a UTF-16 script with a byte order mark", "a broken UTF-16 file exits 2" |
| A file with a NUL in its text (UTF-16 with no byte order mark, or UTF-32) exits 2 and is never reported clean | "UTF-16 without a BOM is refused", "UTF-32 with a BOM is refused", "UTF-16 without a BOM file exits 2", "UTF-32 file exits 2" |
| A folder run and the exit codes 0, 1, 2, 3 and 64 | "a folder is walked in sorted order", "a folder that cannot be listed exits 2", "a folder with no .sql file exits 2", "a file that cannot be read exits 2", "a report that cannot be written exits 2", "no path is a usage error" |
| A non-ASCII name prints as ASCII escapes | "a non-ASCII table name prints as ASCII escapes" |
| The harness can fail | the two `pinned defect` probes of `check()`, `raises()` and the footer |

There is no network code in this tool, so there is no stub server. It opens no connection to SQL Server or
to anything else. It prints the rule text with table names and, for R6, the name of a host from your `--unc-host` list. It does not print string contents.

## Measured

- Branch coverage is 100 percent: 1248 statements and 530 branches, measured with
  `coverage run --branch txnlint.py --self-test` on Windows Python 3.13.2.
- `pyflakes` is clean. `python -W error` compiles the file and runs the whole self-test without a warning.
- The self-test prints 227 assertions with 0 failed on every interpreter I ran in this round: Python 3.9.25,
  3.12.10 and 3.13.2 on Windows, and Python 3.9.25, 3.11.16 and 3.12.3 on Linux (run with `python3 -W error`). The
  output text of the Windows 3.13.2 run and the Linux run is the same once Windows' carriage returns are
  removed, so there is no difference to explain.
- Coverage was measured on Windows only. The Linux host has no `coverage` package.
- I broke the R1 and R5 fixes seven ways in a scratch copy: a `MERGE` that never counts as an insert, no promotion of a
  committed delete, no dropping of a delete refilled in its own transaction, a `ROLLBACK` read as a `COMMIT`, an empty
  swallowed-guard set, a `CATCH` re-raise that is ignored, and `:on error exit` silencing a swallowed guard. The self-test
  went red every time.
- I broke the formula guard six ways in a scratch copy: no apostrophe at all, no guard on `message`, no guard on
  `path`, no `isinstance` test, no tab in the list, and a `csv.reader` call added to production code. The
  self-test went red every time.
- An earlier one-off sweep of 425 single-token mutations (swap `and` and `or`, flip a comparison, turn `break` or
  `continue` into `pass`, flip `True` and `False`) of the code above the self-test left 16 mutants that the
  self-test did not catch. I read all 16. Each is equivalent for valid input, such as `<` to `<=` on an index
  that cannot be zero, or changes only the wording of an error message. An earlier sweep of 437 mutations of
  an earlier version left 45; the real gaps it showed are pinned by assertions. The sweep script is not part of
  this repository, so you cannot repeat the sweep from here.
- 20,000 random programs made of T-SQL keywords, quotes, sqlcmd lines and odd characters ran through the core
  with no exception, in 1.2 seconds on Windows. The driver is not part of this repository, so you cannot repeat the run from here.
- Run once by hand on Linux as an ordinary user: a subfolder with mode 000 gives `Permission denied` and exit 2
  instead of a clean pass; `ln victim.sql hl.sql` followed by `--out hl.sql --apply` overwrites `victim.sql`
  with the CSV (listed under Limits); a subfolder reached only through a symbolic link, in a folder that holds nothing else, gives exit 2, and a
  symbolic link beside a real folder is not followed. A symbolic link given directly as the path argument is followed and linted.
- The self-test writes only inside its own temporary folder and removes it. It leaves nothing in the
  working directory.
- The file is pure ASCII and has no f-string. Python 3.9 runs it.

## Exit codes

| Code | Meaning |
|---|---|
| 0 | No finding, and no note, except that notes are off when only R6 runs (`--rules R6`), so exit 0 can hide an unread `EXEC` of a string. A procedure body is not read, except that a procedure run after an unprotected `DELETE` or `TRUNCATE`, or in a `CATCH` block before its `ROLLBACK`, gives a note. |
| 1 | At least one finding, including E0. |
| 2 | A file could not be read or decoded, a folder could not be listed or holds no `.sql` file, or `--out` could not be written. |
| 3 | No finding, but a note says that some code was not read (`EXEC` of a string that may hold a write, `sp_executesql`, `:r`, `EXEC` of a procedure after an unprotected `DELETE` or `TRUNCATE`, or `EXEC` of a procedure in a `CATCH` block before its `ROLLBACK`). |
| 64 | Usage error: no path, an unknown flag or abbreviation, a bad rule or host, `--apply` without `--out`, or `--out` naming an input. |

## Limits

**What it reads.**

- It is a token reader, not a T-SQL parser. It finds the shapes listed above and nothing else.
- It reads SQL in plain statements. The text inside `EXEC('...')` and `sp_executesql` is not read, except
  that R6 looks inside every string literal. The tool prints a note for such a string when it may hold a
  write, and for every `:r` line. Lint an included file on its own. A `CATCH` block that calls a logging
  procedure with `EXEC` before the `ROLLBACK` gives a note, not a finding, because the procedure body is not read.
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
- A `ROLLBACK` under `IF`, `ELSE` or `WHILE` is read as leaving the transaction open only when its branch ends in an
  unconditional `RETURN`, `GOTO` or `THROW`. A `RETURN` that is itself under an `IF` does not count, so the code after
  it is read as outside the transaction. The tool does not follow a `GOTO` to its label.
- A `RAISERROR` counts as a guard that stops the code after it only if a `RETURN` or `GOTO` follows in the same
  block before any write. A script that sets a flag and tests the flag later is reported anyway.
- The `ELSE` of a `RAISERROR` guard ends at its statement or block. A plain statement with no semicolon ends at the
  next statement word, so an `ELSE INSERT ... SELECT` ends at its `SELECT`. A block that never closes, or that
  spans a `GO`, is not matched, and the writes in it are reported.
- An `ELSE` after an `IF` that tests `@@TRANCOUNT` is judged by the nearest earlier `IF`. With nested `IF` statements the
  tool can pair the wrong one.
- A `:on error exit` that sits after an R5 guard but before the `GO` that ends its batch is still reported.
- R3 also reports a log write in a `CATCH` when the `TRY` had already committed and the error came later. The tool does
  not follow which statement threw, so that report is cautious, and the row is not lost in that case.
- After `SET IMPLICIT_TRANSACTIONS ON` the tool treats every later write as inside a transaction, even after a
  `COMMIT`, so a `DELETE`, `COMMIT`, `INSERT` sequence is not reported.
- A database part decides R1 when the tool knows both databases. The database of an unqualified name is the one
  in the last `USE` before it, so `USE ProdDb`, `DELETE FROM StagingDb.dbo.Lookup` and `INSERT INTO dbo.Lookup`
  are two tables and give no finding. When no `USE` came first, or the `USE` names a variable the tool cannot
  read, the tool cannot tell. It then reports `DELETE FROM dbo.Lookup` with `INSERT INTO OtherDb.dbo.Lookup`, so that
  a refill is never missed. A `USE` in a file that you run after another one is not carried over.
- A `THROW` in a `CATCH` block that sits inside an outer `TRY` is still counted as an R5 guard, although it jumps to the outer `CATCH`.
- A `DELETE` or `UPDATE` through a CTE (`WITH c AS (SELECT ... FROM dbo.t) DELETE FROM c`) is read as a write to the
  first table in the CTE's `FROM`. A CTE that joins two tables reads as the first one. A CTE written with a column list
  (`WITH c (a, b) AS`) is not seen through, and is read as a write to `c`.
- R5 counts only `INSERT`, `UPDATE`, `DELETE`, `MERGE` and `TRUNCATE` as writes. A later batch that only runs `EXEC` of a
  procedure, DDL (`ALTER`, `DROP`, `CREATE`) or `SELECT ... INTO` is not reported.
- Only a procedure named `sp_executesql` is read as dynamic SQL, bracket-quoted or not (`EXEC [dbo].[sp_executesql] N'...'`
  gives the same note). Other procedures that run a string inside their own body (`sp_MSforeachtable 'DELETE FROM ?'`,
  `xp_cmdshell 'sqlcmd -Q ...'`, `EXEC dbo.usp_x @sql = 'DELETE ...'`) give no note, and an `EXEC` whose target is a
  variable (`EXEC @procname`) gives none either, because the tool cannot see which procedure runs.
- Run time grows sharply on pathological input. A chain of 3,000 `IF @x = n RAISERROR(...) ELSE` links took 46 seconds
  in an audit run, and 30,000 nested `BEGIN` blocks took 62 seconds. Whole files are read into memory, about 1.2 seconds
  per megabyte.
- A `RAISERROR` with state 127 ends `sqlcmd`, and the `:EXIT` command also ends it. The tool models neither.
- A non-breaking space between keywords is not whitespace to the tokenizer, so `DELETE<NBSP>FROM` is missed.
  SQL Server would not parse such a script.
- R1 compares every unprotected `DELETE` with every later `INSERT`. A script with 4000 `DELETE`s and 4000 `INSERT`s on
  different tables took about 10 seconds in my run.
- R6 prints only a host name from your `--unc-host` list. It says that an unlisted host was named, and the line
  number points to the string. The report no longer shows which host, so read the script line.
- R6 needs a backslash after the host, so `'\\rogue'` with no share is not read, even when `rogue` is not listed.
- R2 and R5 skip a write to a temp table (`#name`) or table variable (`@name`). A guard followed by `INSERT INTO #stage`
  in a later batch is not reported, although that write runs.
- A single batch with thousands of `RAISERROR` statements and no write after them is slow, because each one rescans
  the rest of its batch. 2000 of them (42 KB) took 4.8 seconds in my run, and 4000 took about 20 seconds in an audit run.
- R6 reads each string literal on its own, so a UNC path split across a concatenation (`N'\\host' + N'\share'`) is not seen.
- R6 does not flag a listed host written with no leading backslash (`host\share`), because that looks like `server\instance`.
- R6 matches a listed host exactly or as the first label of a longer name. A listed `backupsrv` therefore also accepts
  `backupsrv.other.example`, a different host in a different domain. A listed full name does not accept the short name.

**What counts as a batch break.**

- Only a `GO` that is alone on its line, with an optional count and one trailing comment, ends a batch. `GO /* a */ -- b`, a `GO` with a leading
  byte order mark or no-break space, and `GO;` are read as ordinary words, so the batch break is lost. That can hide an R5.

**Files and encodings.**

- Only `.sql` files are picked up from a folder. A file named on the command line is read whatever its extension.
- A script is decoded as UTF-16 when it starts with a byte order mark, as UTF-8 otherwise, and as cp1252
  when the UTF-8 decode fails.
- A script whose decoded text holds a NUL character is refused with exit 2. That covers UTF-16 with no byte order
  mark and UTF-32 with or without one. Save such a file as UTF-8, or as UTF-16 with a byte order mark.
- A zero-byte or BOM-only `.sql` file is reported as `0 finding(s)` with exit 0, so a truncated deployment script looks clean.
  Check file sizes before you trust exit 0.
- The only procedure calls that give a note are those after an unprotected `DELETE` or `TRUNCATE`, and those in a
  `CATCH` block before its `ROLLBACK`. A procedure that deletes and refills inside its own body is another script.
  Lint that script on its own. A procedure that writes after an unwrapped `UPDATE` is not seen.
- R1 pairs every committed `DELETE` with every later `INSERT` into the same table, whatever the `WHERE` clause says,
  with one exception: a purge by age is spared (see R1). A `DELETE` that removes a slice of rows by a key, such as
  `WHERE BatchId = @id`, is paired with a later `INSERT`, because it may be the first half of a refill.
- R1 knows a purge only by its `WHERE` clause: a date function (`GETDATE`, `GETUTCDATE`, `SYSDATETIME`,
  `SYSUTCDATETIME`, `SYSDATETIMEOFFSET`, `CURRENT_TIMESTAMP`, `DATEADD` or `DATEDIFF`) on the old side of a `<` or `>`,
  that is on the right of `<` or on the left of `>`. A cut-off held in a variable (`WHERE LogDate < @cutoff`) is not seen
  and is still paired. Each `AND` condition is judged by its first `<` or `>`, and `<>` is never a purge. Any `OR` in the `WHERE` clause rules a purge out,
  because `LogDate < DATEADD(DAY, -90, GETDATE()) OR @Force = 1` can match every row, so a later `INSERT` is still paired.
- R1 reads a `COMMIT` as committing the `DELETE` even when the `COMMIT` is conditional, such as `IF @@TRANCOUNT > 0 COMMIT`.
- R5 reads any `THROW` or `RAISERROR` in a `CATCH` block as a re-raise, even one under an `IF`. A `CATCH` that raises
  again only on some paths hides the guard in its `TRY` from R5.
- A table name taken from a quoted identifier is printed as written, so do not put secrets in object names.
  Table names are printed in lower case: `DELETE FROM dbo.A` prints as `dbo.a`.
- A `\xNN` byte that cp1252 does not define is decoded as U+FFFD, and the file is still read. This affects only odd encodings.
- If the console or a pipe fails while the report prints, the exit code can be 1 or 120 and does not mean findings.
  Standard output on `/dev/full` gave 120 on Linux. See the closed-pipe entry below.
- A header-only CSV does not mean clean. Check the exit code.
- A write verb hidden by a variable, `CHAR()` or `EXEC` of a string built at run time is a note, never a finding,
  because the tool does not run the code.
- A double-quoted string, which is a string only when `QUOTED_IDENTIFIER` is off, is read as a name.
- A subfolder that is reached only through a symbolic link is not followed, and its `.sql` files are skipped without a message.
  The exit code does not warn about it, so a folder run can exit 0 without having read every script.
  A folder that holds only such a link exits 2 with `no .sql file found`.
- The fuzz and mutation drivers are not shipped, so the 20,000-program run, the 425 and 437 mutation sweeps and the seven R1/R5 and six formula-guard mutations in Measured cannot be repeated from this repository.
- The timings in this section depend on the machine and were taken with generator scripts that are not shipped. Treat them as orders of magnitude, not promises.
- One audit run of a 300 KB file with 30,000 nested `BEGIN` blocks exited 1 after 48.9 seconds with empty output. Two reruns exited 0 and I did not reproduce the exit 1. An exit 1 with no output is not a verdict, so treat it as a failed run and run again.
- Keywords are matched after Python upper-casing, so a Unicode letter such as the dotless i (U+0131) or the long s (U+017F) is read as ASCII `I` or `S`. A script that SQL Server would reject for such a word can still be read as a write verb.
- A CSV from a run that exits 3 holds only the header row and carries no marker that code was not read. Read the exit code and the console output, not the file alone.
- Naming the same file twice, or a file and its folder, reads it twice and doubles the findings and the file count.
- One unreadable file (a permission error or truncated UTF-16) stops the run with exit 2. It prints none of the
  findings already collected from earlier files.

**What it writes and prints.**

- `--out` refuses a path that resolves to an input file, but it does not detect a hard link to an input.
  Then `--apply` overwrites the script with the CSV. I reproduced it on Linux with `ln victim.sql hl.sql` and `--out hl.sql --apply`.
- `--out` with `--apply` overwrites any existing file that is not an input of this run, for example a sibling `.sql` script, without asking.
- The CSV is opened for writing before it is filled. A disk-full or I/O error part way through leaves a truncated report file, and the tool exits 2.
- A note is not written to the CSV, so a header-only CSV does not mean clean. Read the console output or the exit
  code 3. This holds for a run with `--apply`.
- A run that fails with exit 2 before it writes leaves an older `--out` file untouched. A caller that reads only the file
  sees the result of the previous run.
- Write the report outside the folder you scan, or give it a name that does not end in `.sql`. A report named
  `d/rep.sql` inside a scanned folder `d` is read as a script by the next run and counted as an extra file. Only the
  exact inputs of the current run are refused.
- `--self-test` ignores every other argument, so `--self-test --out x.csv --apply` runs the assertions and writes nothing.
- The demo scripts are printed in this README only. Create them by hand to repeat the sample runs.
- Messages start with fixed tool text (a verb, a rule phrase, `EXEC`), so in practice only `path` can start with a
  formula character: a hostile folder argument or a file named on the command line. The guard on `message` is a second layer.
- The formula guard covers the six characters listed above. OWASP also lists line feed and the full-width
  `=`, `+`, `-` and `@`. `safe()` already turns a leading full-width character in `path` into a backslash escape, so only a
  leading line feed in `path`, or any of these in a `message`, is written as it came.
- OWASP warns that an apostrophe prefix is not reliable in Microsoft Excel after the file is saved and opened
  again, and names a tab prefix as the Excel-resistant form. No assertion opens Excel or any spreadsheet:
  the guard is checked on the CSV text only. Treat it as one layer.
- A cell that already starts with an apostrophe in front of text is written as it came, so `'=x` and a guarded
  `=x` cannot be told apart in the CSV. The tool never reads the CSV back, so it does not matter here.
  A consumer that does read it back must know that a leading apostrophe on `path` may be the guard.
- The guard is applied to the CSV only. The header is fixed by the tool and is not guarded.
- `safe()` rewrites a non-ASCII path with backslash escapes in the report and in the CSV, so the printed path may not match the real file name.
- `safe()` escapes only non-ASCII characters. A table, alias or file name that holds a control character or an escape sequence is printed raw,
  so a hostile `.sql` file can put terminal escape codes or fake lines into the report.
- The committed `LICENSE` uses LF like the other three files. A Windows checkout with `core.autocrlf=true` may show CRLF in the working tree. The license text and the copyright line are correct.
- The guard on `message` is exercised only through `write_findings` with a synthetic finding. No real message can start
  with `=`, `+`, `-`, `@`, tab or carriage return, so no command-line case reaches that guard end to end.
- A hostile file name from the command line is tested for `=`, `+`, `-` and `@` only. Tab and carriage return are
  tested through `write_findings`, because the tests do not create file names that start with them on Windows.
  Microsoft lists the control characters 1 through 31 as reserved in a file name outside alternate data streams
  (https://learn.microsoft.com/en-us/windows/win32/fileio/naming-a-file, "Naming Files, Paths, and Namespaces").
- The `isinstance` test inside the cell guard has no assertion of its own. Removing it turns the self-test red by a
  `TypeError` in the assertion list, not by a named assertion. `cell()` is called only with path and message strings.
- A closed output pipe ends in a Python traceback with exit 1 or, on Windows, an "Exception ignored on flushing
  sys.stdout" message with exit 120 (`python txnlint.py x.sql | head -c 0`). An `--out` path that holds a NUL character
  ends in a traceback with exit 1. Exit 1 is also the code for "findings exist". Read the output, not only the exit code.
- E0 exits 1, the same code as an ordinary finding. A caller must read the output to tell an unreadable file from a rule that fired.
- An unexpected internal error exits 1 with a Python traceback, the same code as "findings exist". `main()` has no catch-all that maps a crash to its own code.
  A 20,000-program random token run in this round raised no exception. The driver is not in this repository, so you cannot repeat it from here.

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
