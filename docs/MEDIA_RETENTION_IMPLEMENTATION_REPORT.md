# Media retention v1 implementation report

## Change scope

- `backend/app/services/media_retention.py`: channel-scoped candidate and asset enumeration, safe incomplete schema, deterministic revision.
- `backend/app/services/player_runtime.py`: additive runtime field, isolated retention failure, PostgreSQL snapshot wrapper.
- `backend/app/api/routes/player.py` and `backend/app/api/routes/channels.py`: use the snapshot wrapper after existing token/user authorization; the Player route rejects a channel whose client/branch do not match the authenticated device.
- `backend/tests/test_media_retention.py`: isolated in-memory rule tests and opt-in dedicated local PostgreSQL concurrency test.
- `docs/PLAYER_MEDIA_RETENTION_V1.md`: wire contract and retention semantics.

No model, migration, scheduler, Player, frontend, file deletion, publication policy, or runtime version change is included.

At the final review, `git diff --stat` for already tracked files shows 3 files, 58 insertions and 5 deletions. Four new files are still untracked: `backend/app/services/media_retention.py`, `backend/tests/test_media_retention.py`, and the two documents in `docs/`. No temporary binary, generated build artifact, migration, or unexpected Player/frontend file appears in `git status --short`. Nothing is staged or committed.

## Local test plan

1. Run `PYTHONPATH=backend python -m unittest discover -s backend/tests -p test_media_retention.py -v` using project backend dependencies. The rule tests use SQLite in memory and never connect to the normal application database.
2. For MVCC validation, first create a **dedicated local disposable** PostgreSQL database named `tecno_control_retention_test` (for the compose stack: `docker compose exec postgres psql -U postgres -d postgres -c "CREATE DATABASE tecno_control_retention_test;"`). Then run `docker compose exec -e TEST_DATABASE_URL=postgresql+psycopg://postgres:postgres@postgres:5432/tecno_control_retention_test backend python -m unittest discover -s tests -p test_media_retention.py -v`. The test refuses another database name or a nonlocal host. It creates fixture tables and commits only fixture rows in this disposable database; it never touches the normal application database.
3. For an HTTP smoke test, start a temporary backend instance pointed **only** at the isolated test database with `AUTO_SEED=false`, create a fixture channel/PlayerDevice token there, request `/api/player/runtime-config`, and compare `playlist` and `contents` with the previous contract. Create/deactivate a future schedule in that test database and poll again. Never use production or the normal local application database for this validation, and do not delete media.
4. Measure response bytes/elapsed time and SQL statements with representative campaigns in the isolated test database. The enumerator uses batched queries rather than one content query per asset.

## Rollback proposal (not executed)

Revert only the additive service/runtime/route changes in a reviewed release, leave the database untouched, and let clients treat absence of `media_retention` as no purge authorization. Existing `runtime_version=3` fields continue to serve playback. No migration rollback is required.

## Results and limitations

Local verification on 2026-09-15: 22 tests discovered, 20 passed, two PostgreSQL wrapper/snapshot-concurrency tests skipped because the Docker daemon was unavailable and no isolated PostgreSQL server could be reached. The failure-injection test intentionally logged a traceback and still returned `complete=false` while preserving the old playlist. A fixture with 3 campaigns and 3 images confirmed that old `playlist` and `contents` still contain all 3 entries; removing `media_retention` from the before/after payloads yields identical legacy fields under a frozen clock. A 3-campaign/150-asset in-memory fixture used 5 SQL statements, produced 32,549 bytes of `media_retention` JSON, and took 18.8 ms in the last measured run. `python -m compileall -q backend/app backend/tests` succeeded. `git diff --check` found no whitespace errors (Git emitted only its existing LF/CRLF conversion warnings). No frontend file changed, so frontend build/typecheck was not relevant to this backend-only change.

**Release limitation:** PostgreSQL MVCC/concurrent-publication behavior and authenticated HTTP smoke tests are not yet executed. The wrapper is designed for PostgreSQL `REPEATABLE READ`; if that snapshot cannot be obtained, the old runtime path is used and `media_retention.complete=false`. Physical Player compatibility and response timing on PostgreSQL are also unverified. Do not authorize local purge or production deployment from these in-memory results alone.

**Pre-existing security risk for review:** the old `_resolve_runtime_campaigns` and `_build_sequence_runtime` paths do not check client ownership of an assignment's campaign or a campaign's content before serializing the legacy runtime fields. A deliberately corrupt cross-client reference causes the **new retention field** to be incomplete with empty arrays, but may still expose cross-client IDs or media in the **pre-existing playlist**. Changing old selection/serialization semantics was explicitly outside this task, so this was not modified. Review and correct that separate issue before treating the entire runtime as tenant-safe in production.

No production data or media was accessed or removed. No commit/push/deploy was performed.
