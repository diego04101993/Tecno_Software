# SECURITY-R5.2 — Migration 018 database-guard exit status

Local validation PASS. No production migration, VPS, deploy, staging, commit or push.

Repository: `C:\tecnomania\tecno_software_videowall_1x3`.
Branch: `feat/android-videowall-1x3`.
HEAD remains `aea6ff3c43d45f3c4b5294a2d59cd06b69a20464`.

## Change and root cause

Only migration 018 lines 14 and 20 changed. psql 16.15 treats the argument of
`\quit 3` as an ignored extra argument and quits normally with exit 0. It is a
client warning, not the SQL error required by ON_ERROR_STOP. Raw BEFORE output
reproduces this for both a wrong database and a missing expected-database variable.

Both abort lines now contain:

```sql
DO $r5_guard$ BEGIN RAISE EXCEPTION 'R5 database guard failed'; END; $r5_guard$;
```

This produces a real server SQL exception; the unchanged `ON_ERROR_STOP on`
terminates psql with observed exit code 3. The caller can distinguish abort from
success. Existing echo messages identify the failed precondition. Every other
byte of 018 is identical: guards, variable quoting, session settings, column and
index-name checks, CREATE INDEX CONCURRENTLY generation, reuse and postcondition.
No index definition, 016/017, model, runtime, application or Player code changed.

Local psql 16 `--help=commands` documents `\q` without an exit-status argument;
`--help=variables` documents ON_ERROR_STOP batch failure behavior. The help,
real process exit status, stdout and stderr are retained in the evidence folder.
No unsupported `\quit` argument is relied on by the fix.

## Actual database and index results

| Case | Migration process exit | DDL after fixture setup | Result |
|---|---:|---:|---|
| BEFORE, wrong compatible database | 0 | 0 | Original silent-success bug reproduced |
| BEFORE, expected variable absent | 0 | 0 | Original silent-success bug reproduced |
| AFTER, wrong compatible database | 3 | 0 | PASS: abort is detectable |
| AFTER, expected variable absent | 3 | 0 | PASS: abort is detectable |
| Correct fresh database | 0 | 14 CREATE INDEX | PASS |
| Re-run after fresh success | 0 | 0 | PASS: definitions and all index OIDs unchanged |
| Two preexisting eligible FK-leading indexes | 0 | 12 CREATE INDEX | PASS: 2 reused, 14 eligible total |
| Re-run after reuse | 0 | 0 | PASS: no duplicate indexes |
| Conflicting canonical index name | 3 | 0 | PASS: existing catalog unchanged |
| Missing required column | 3 | 0 | PASS: no partial migration |
| Existing invalid concurrent index | 3 | 0 | PASS: no silent reuse, drop or repair |

Wrong-database fixtures contain compatible history tables, so the guard result
is not a false positive from missing tables. A DDL event trigger is reset after
setup and records zero schema DDL for rejected migrations; catalogs remain exact.
The original CLI stderr explicitly reports that the extra argument `3` is ignored.
The new error is `R5 database guard failed`, with SQL exception context.

Fresh success creates all 14 expected B-tree `(FK, id)` indexes, seven per table.
The four required FKs per table have no partial predicate. The three optional
content/campaign/layout FKs per table use exactly `WHERE FK IS NOT NULL`.
Names, tables, key order, complete pg_get_indexdef text, and valid/ready/live
flags were checked explicitly; 14/14 definitions match and 14/14 are usable.

All 14 selective natural `EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)` plans use the
expected BitmapIndexScan with the FK in Index Cond and positive Actual Rows.
No planner index/sequence-scan override was applied. Fixtures have 32,768 rows
per table, 4,096 distinct FK values and nullable optional references, followed by
ANALYZE. This proves expected selective index use, not that every production
predicate must select an index or meet a latency target.

018 remains intentionally nontransactional and re-executable. It uses autocommit
and serial concurrent builds, recognizes equivalent valid FK-leading indexes
even under different names or with one key, and does not auto-repair invalid or
conflicting names. It must still not run with --single-transaction, inside BEGIN
or during application startup. Production still requires explicit separate approval.

## Tests and regression scope

The new module has 10 unique cases: 10/10 PASS, zero FAIL/ERROR/SKIP. After adding
the stronger Index Cond assertion to one case, that case was rerun: 1/1 PASS with
all 14 plans. The other nine methods and every helper are AST-identical to the
recorded full run; reconstructing the prior source reproduces its exact SHA.
The two AFTER rejection regressions also ran against the exact old SQL and both
failed specifically because it returned 0: two expected negative-control failures,
zero errors/SKIP. Those failures are not counted as final product failures or PASS.

Targeted R5/R5.1 regression: 94/94 PASS, zero FAIL/ERROR/SKIP. This comprises
35 set-based scale guards, 10 cleanup guards and 49 telemetry concurrency controls:
shared barrier, per-device/channel locks, structural exclusive gate, eight-Player
heartbeats/events, same-player, cross-tenant, fresh authority and structural races.
Mutation smoke: all 13 existing R5 mutation certificates DETECTED, zero survivors,
invalid certificates, harness errors or SKIP. Both QA jobs exit 0 with no OOM.

There are 104 unique positive test cases (94 + 10); the extra plan retest is not
counted as another unique case. This was the requested targeted rerun, not a new
claim to have rerun all 1,909/2,391 historical backend cases or all 306 mutations.
The configured migration suite runs without SKIP; unconfigured discovery of this
Docker/PostgreSQL-specific class deliberately skips until the explicit QA fence
is supplied. No application startup or production configuration was used.

## Isolation, scope and review artifacts

Migration tests use a newly labelled SECURITY_R5_2 PostgreSQL/psql 16.15 cluster,
network none, no host ports, marker and system identifier verified before DB
creation/deletion. Each test owns a fresh r52_UUID database; only that database
is dropped. A separately identified compatible BEFORE database is retained.
The physical videowall lab and every prior R5/R5.1 database/volume are preserved.
Regressions use the previously fenced local R5 UUID-schema fixtures. No production
database, production env, VPS, deployment, Player repository or binary was accessed.

R5.2 changes exactly three paths:

- `backend/migrations/018_telemetry_guard_indexes.sql`: only two abort lines.
- `backend/tests/test_migration_018_safety.py`: deterministic psql/index tests.
- `docs/SECURITY_R5_2_MIGRATION_018_REPORT.md`: this report.

All other 336 files from the approved 337-file R5.1 checkpoint remain byte-identical,
including all 80 app files and existing tests. The cumulative R5/R5.1/R5.2 working
tree has 18 changed paths versus HEAD; staging stays empty. No EOL autofix occurred.
Git's prospective LF-to-CRLF notices, if present, are informational, not mutations.

Evidence under `.tmp/SECURITY_R5_2`:

- `BEFORE_REPRODUCTION.json`, `AFTER_REPRODUCTION.json` and raw stdout/stderr.
- `MODULE_BEFORE_NEGATIVE.json`, `MODULE_AFTER.json`, `INDEX_COND_TARGETED.json`.
- `FINAL_MODULE_VALIDATION.json` (10 cases plus exact source/AST retest proof).
- `TARGETED_STRICT_CERTIFICATION.json` (94 + 13, isolated fixtures and source proof).
- `MIGRATION_DIFF_PROOF.json` (only two exact abort-line replacements).
- `FINAL_DELIVERY_PROOF.json`, final raw SHA manifests and `SECURITY_R5_2.patch`.

The patch is incremental against the approved R5.1 candidate, contains only the
three R5.2 files and is reverse-checked without applying it or changing staging.

## Required results

```text
ROOT_CAUSE=psql16 ignores the extra quit argument; normal quit returns 0
OLD_ABORT_MECHANISM=\quit 3
NEW_ABORT_MECHANISM=DO RAISE EXCEPTION under unchanged ON_ERROR_STOP
WRONG_DB_EXIT_CODE_BEFORE=0
WRONG_DB_EXIT_CODE_AFTER=3
WRONG_DB_DDL_EXECUTED=0
CORRECT_DB_EXIT_CODE=0
INDEXES_CREATED=14_FRESH;12_NEW_PLUS_2_REUSED_IN_REUSE_CONTROL
INDEXES_VALID=14/14_VALID_READY_LIVE
INDEX_DEFINITIONS_MATCH=14/14
QUERY_PLANS_PASS=14/14_NATURAL_WITH_INDEX_COND
R5_REGRESSION=45_PASS
R5_1_REGRESSION=49_PASS
CONCURRENCY_SMOKE=PASS
MUTATION_SMOKE=13/13_DETECTED
MIGRATION_TESTS=10_UNIQUE_PASS
FINAL_PLAN_RETEST=1_PASS_14_PLANS
FAIL=0
ERROR=0
SKIP=0
FILES_CHANGED=3_R5_2;18_CUMULATIVE_VS_HEAD
STAGING_EMPTY=YES
MIGRATION_016_CHANGED=NO
MIGRATION_017_CHANGED=NO
INDEX_DEFINITION_SQL_CHANGED=NO
APP_SOURCE_CHANGED=NO
PLAYERS_CHANGED=NO
COMMIT=NO
PUSH=NO
DEPLOY=NO
PRODUCTION_ACCESSED=NO
VPS_ACCESSED=NO
DAYBREAK_USED=NO
```

SECURITY-R5.2 MIGRATION 018 EXIT-STATUS FIX PASS — READY FOR TARGETED INDEPENDENT RE-AUDIT
