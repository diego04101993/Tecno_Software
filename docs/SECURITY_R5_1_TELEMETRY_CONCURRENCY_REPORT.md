# SECURITY-R5.1 — Telemetry hot-path concurrency

Final local validation: PASS. This is readiness for production re-preflight, not a deployment approval.

Repository: `C:\tecnomania\tecno_software_videowall_1x3`.
Base HEAD: `aea6ff3c43d45f3c4b5294a2d59cd06b69a20464`.
No staging, commit, push, deploy, VPS, production or Player changes.

## Root cause and scope

R5's native Linux burst of eight different, authenticated Players of one client
produced four HTTP 200 and four HTTP 409 / PostgreSQL 55P03 after ten seconds.
The exclusive client advisory gate serialized otherwise independent telemetry.
Context snapshots and media-path coordination were also exclusive, so removing
only the client gate would leave other shared bottlenecks.

The first R5.1 candidate made both eight-Player bursts succeed. The sixteen-Player
expanded-history run still exhausted a thirty-second QueuePool wait in one
request: it retained complete historical validation on every append. That
candidate and its raw failure remain preserved; it is not the final acceptance
candidate. The final design separates activity appends from structural mutation
preflight, without increasing pool capacity or any timeout.

The new local baseline for eight playback events also reproduces the failure:
seven HTTP 201, one HTTP 409 / 55P03. These are failed baseline measurements,
not passing results of R5.1.

Player telemetry consists of POST `/api/player/heartbeat` and POST
`/api/player/playback-events`. GET `/api/player/runtime-config` also writes
`last_seen_at`. Activation remains structural. GET `/api/player/time` writes
nothing. `/api/audio/events` is a separate User-authenticated administrative
writer and retains its original strong guard.

## Lock map

S/X below mean compatible shared / exclusive PostgreSQL transaction locks.

| Operation | Before R5.1 | After R5.1 | Safety reason |
|---|---|---|---|
| Heartbeat | Client gate X; all snapshots FOR UPDATE; media X; exhaustive incoming history | Same client barrier S; channel/device anchors FOR UPDATE; context FOR SHARE; media S; new-row/parent proof | Each Player's append scope stays stable; common authority may be read concurrently |
| Playback event | Client gate X; all snapshots FOR UPDATE; media X; exhaustive incoming history | Same barrier S; device anchors FOR UPDATE; other context FOR SHARE; media S; new-row/parent proof | Serializes same-device appends and protects all referenced authority |
| Player runtime last-seen update | Client gate X; all snapshots FOR UPDATE; media X | Same barrier S; device FOR UPDATE; other context FOR SHARE; media S | Only reviewed activity fields use the compatible path |
| Channel update/delete | Client gate X; fresh graph/row locks; media X | Unchanged | Excludes Player readers before structural discovery/mutation |
| PlayerDevice structural update/delete | Strong structural guard | Unchanged | Relationship, active and token changes cannot overlap an approved append |
| Branch update/delete | Strong client gate and complete graph | Unchanged | Fresh incoming discovery includes telemetry committed before the writer |
| Client update/delete | Strong structural gate and complete graph | Unchanged | Ownership/status/deletion authority remains exclusive |

The exclusive client-wide gate is not required for these reviewed activity
patches. A shared client-wide barrier is deliberately retained: it coordinates
with existing structural writers before either side takes graph row locks.
It is not an exclusive bottleneck between independent Players. This change does
not claim that the client advisory barrier has been removed entirely.

## Invariants and TOCTOU argument

1. Shared eligibility is internal and restricted to exact endpoint identities,
   reviewed UPDATE field sets and one new history row with authoritative scope.
   Other intent shapes and activation use the unchanged exclusive guard.
2. After acquiring the barrier, fresh locked snapshots revalidate the channel,
   device/token hash, client status, branch activity and owner/context relations.
   No request data establishes identity or ownership.
3. FOR SHARE, unlike FOR KEY SHARE, conflicts with non-key UPDATEs, including
   owner, branch and active-state changes. Actual append anchors stay FOR UPDATE
   to coordinate same-device appends and structural/FK changes.
4. Only the exact internal telemetry graph, approved operation and immutable
   normalized intents may omit discovery of already-existing heartbeat/event
   leaves. Activity fields cannot change those rows' ownership/FK projections.
   Existing history does not authorize a new append. The new row still receives
   an exact primary-key absence probe, complete current/proposed relationship
   validation, locked authoritative parents, opaque/media validation and fresh
   two-pass plan comparison. Unknown shapes retain the strong complete path.
   Structural mutations retain R5's exhaustive set-based historical predicates,
   locks and parent closures, including old corrupt rows. No history is repaired
   or deleted by this optimization. There is no date window, LIMIT, historical
   Python materialization or cache carried across proof phases.
5. Shared media locks use the same canonical keys as exclusive writers/cleanup.
   Writers retain exclusive default behavior. Read-to-write upgrades, cleanup
   under a shared batch and proof-phase lock-mode drift are rejected.
6. Structural writers retain the exclusive tenant barrier. They either finish
   before a Player's fresh proof or wait for its transaction to finish and then
   rediscover the newly committed telemetry. Row/FK locks also coordinate direct
   SQL attempts during the proof; arbitrary uncooperative SQL after commit is
   not claimed to be an application authorization boundary.
7. Same-device activity is serialized at its device/channel anchors. Fresh
   activity fields do not cause stale-relationship conflicts. Approved
   last_seen_at/last_ping_at values are monotonic. Relationship/authentication
   changes still reject stale requests.

The ten-second lock timeout is unchanged. A genuine 55P03 in the reviewed Player
telemetry path rolls back and returns HTTP 503, never success. A real relationship
conflict remains HTTP 409. Structural/activation error handling is unchanged.
Load acceptance still requires zero timeouts and zero errors; changing an error
status is not counted as a concurrency fix.

Optional foreign campaign/content/layout IDs keep the existing endpoint
sanitization-to-NULL behavior. Scope/authentication attacks must be rejected;
sanitization is reported separately and is not called an HTTP rejection.

## Evidence and final results

The final clean regression run completed 1,860/1,860 PASS, with zero failures,
errors or SKIP. The final focal run completed 94/94 PASS (49 new controls and
45 existing R5 controls). Those 45 are already in the 1,860; the unique total
is **1,909 PASS**, not 1,954. All 306 mutation certificates are valid DETECTED:
293 existing and 13 R5-specific, zero survivors, invalid certificates, harness
errors or SKIP. No result from an incomplete or obsolete execution is counted.

| Final scope | Result |
|---|---:|
| READ matrix | 182 PASS |
| WRITE matrix | 210 PASS |
| DELETE matrix | 525 PASS |
| RUNTIME matrix | 158 PASS |
| R4 gate/proof regressions | 53 PASS |
| R5 historical scale/cleanup | 35 + 10 PASS |
| New R5.1 SQLite/PostgreSQL controls | 49 PASS |
| Auth / Player / WS R1 regression | 31 PASS |
| Selected critical runtime safety | 116 PASS |
| Media Retention | 22 PASS |
| Videowall derivatives/groups/publications/runtime | 30 + 25 + 29 + 13 = 97 PASS |

These are selected required regressions, not the entire backend discovery.
Discovery found 2,402 occurrences / 2,392 unique IDs: 1,860 unique IDs were selected
by the immutable required plan, and 49 new unique cases ran separately. The remaining
493 occurrences / 483 IDs were outside this rerun; ten duplicate A9 occurrences
were unexecuted. Omitted cases are neither SKIP nor PASS. All selected cases ran against
isolated SQLite and fenced local PostgreSQL fixtures, without application startup.

## Real concurrency and latency

| Players | Heartbeat result | Heartbeat p50 / p95 / max ms | Event result | Event p50 / p95 / max ms |
|---:|---|---|---|---|
| 8 | 8/8 HTTP 200 | 1300.861 / 1303.752 / 1303.752 | 8/8 HTTP 201 | 1063.842 / 1069.734 / 1069.734 |
| 16 | 16/16 HTTP 200 | 2293.639 / 2423.086 / 2423.086 | 16/16 HTTP 201 | 1871.762 / 1970.518 / 1970.518 |
| 32 | 32/32 HTTP 200 | 4412.988 / 4598.507 / 4603.606 | 32/32 HTTP 201 | 3379.314 / 3667.367 / 3669.348 |

The paired eight-Player R5 baseline on the same synthetic QA identities/data had
heartbeat 4/8 success, four 409/55P03, p50/p95/max 10676.505/12282.218/12282.218 ms.
Its event baseline had 7/8 success, one 409/55P03,
5721.679/11239.723/11239.723 ms. The original native R5 eight-Player evidence is
also retained unchanged. Baseline failures are not passing candidate tests.

The six final independent bursts returned 112 correct responses. Six additional
scenarios returned 48 correct responses plus two expected 403 scope rejections:
**162 HTTP responses = 80 HTTP 200 + 80 HTTP 201 + 2 HTTP 403**.
There were zero false 409, zero 55P03, zero server errors and zero historical
Python row snapshots. Data and media-coordination Engines were both observed.

Eight concurrent heartbeats of the SAME device all succeeded, with monotonic
last_seen_at/last_ping_at. Eight events of the same device all succeeded with
unique server IDs. Mixed heartbeat/event/runtime requests are covered by the
focal suite. Same-device requests serialize at their real anchors.

Cross-scope cases ran eight valid requests alongside a separately identified
invalid device/channel scope. Both endpoints rejected the invalid request with
403 while all legitimate requests succeeded; no invalid telemetry was inserted.
The temporary device and its new unique channel were removed by exact ID/scope.
Optional foreign IDs were tested separately: existing HTTP sanitization was
preserved and all three optional references were stored as NULL. This is not
misreported as an HTTP rejection. Direct backend-authored invalid new FK
proposals are rejected by the complete preflight.

The original workload has eight Players per client and 500,000 rows per history
table. The expanded workload has 32 Players per client, two clients and 2,000,000
rows per table: four million historical rows, 31,250 per device per model.
All 160 newly committed test telemetry rows were scope/UUID-verified and removed
selectively. Original device IDs, token hashes, hardware, bindings, channel IDs,
scope and channel-code hashes were preserved. No activation endpoint was called.

Measurements use nearest-rank percentiles from one burst per scenario, no
request retries, no artificial sleep and no concurrent preparation/regression
workloads. Product pool settings remain 5 + 10 overflow, 30-second checkout;
lock timeout remains ten seconds. Media was on native Linux tmpfs with verified
bytes; code/data source snapshots were read-only.

## Structural and historical safety

Actual PostgreSQL tests cover heartbeat with Channel update/delete, device
deactivation, relationship move, device delete, Branch delete, and events with
parent mutation. Both orderings are tested: a writer waits for approved telemetry,
or a request revalidates fresh authority after the structural writer finishes.
Non-key parent owner/active changes are blocked by FOR SHARE; KEY SHARE alone
would not meet this guarantee. Guarded cleanup leaves no orphan history.

The public Channel DELETE correctly rejects an online screen with its existing
business message; it is not counted as a false tenant conflict. Internal guarded
Channel cleanup is tested separately. Relationship moves incompatible with
committed history remain rejected. Current/proposed state, incoming relations,
ownership and opaque media checks remain covered by the final suites/catalog.

Important semantic boundary: prior corrupt history is left untouched. A valid
new report can now be accepted despite such unmodified historical corruption;
the prior rows do not grant authority to the append. The SAME corrupt state still
fails the exhaustive structural preflight. This intentional append-versus-mutation
classification is explicitly tested, not hidden by a repaired database or test skip.

MutationGate, write_guard, delete_guard and delete execution/coverage retain their
R5 structural AST. Removing only the exact new bound-history choice restores the
original preflight AST. tenant_telemetry.py is byte-identical to R5. No history
window, LIMIT or partial cleanup was introduced. Shared media mode cannot be
upgraded silently, used for cleanup, or changed inside a reusable proof phase.

## Fixture changes and retained unsuccessful attempts

All three existing-test adaptations have independent AST/byte proofs preserving
their assertions. The historical no-guard replay returns its original requested
patches solely so the unsafe-write witness can execute; it grants no production
capability. The R5 history-certificate fixture changes only get_runtime_config
to activate_player to exercise the still-strong structural Player path. The A8
fixture keeps its historical foreign heartbeat and original request, adding one
CURRENT foreign DeviceOutput. Its normal case passes; omitting the Player guard
makes the unchanged zero-effects assertion fail, proving a non-vacuous rejection.

The initial shared-lock-only candidate passed eight Players but failed the expanded
sixteen-Player workload through a 30-second pool wait. Its recorder lost one
non-JSON response's HTTP status; that status is UNKNOWN, not called HTTP 200 or
invented as a captured HTTP 500. The recorder correction was controlled separately.
Initial focal fixture/session/connection-budget errors and the obsolete historical
A8 expectation are preserved. The obsolete full run was cancelled only in its four
identified QA containers; unfinished cases were never PASS. A fresh final full run
then completed all 1,860 cases. A new no-history-scan control also demonstrably
failed against the old candidate before passing against the final candidate.

## Scope, compatibility and limits

R5.1 changes eight paths: three product files, four test files and this report.
The cumulative uncommitted R5 + R5.1 change has 16 paths versus HEAD. No staging,
commit, push, deploy, VPS, production or Player files were touched.

| R5.1 path | Change |
|---|---|
| [backend/app/api/routes/player.py](C:/tecnomania/tecno_software_videowall_1x3/backend/app/api/routes/player.py) | Apply approved monotonic receipt timestamps in heartbeat/runtime. |
| [backend/app/services/tenant_mutations.py](C:/tecnomania/tecno_software_videowall_1x3/backend/app/services/tenant_mutations.py) | Reviewed activity shape, compatible structural barrier, row-lock modes and transaction-bound prior-history omission. |
| [backend/app/services/media_references.py](C:/tecnomania/tecno_software_videowall_1x3/backend/app/services/media_references.py) | Shared read coordination; exclusive writer default, cleanup/upgrade denial and proof-phase mode binding. |
| [backend/tests/test_player_telemetry_concurrency.py](C:/tecnomania/tecno_software_videowall_1x3/backend/tests/test_player_telemetry_concurrency.py) | 49 new SQLite/PostgreSQL cases, including append/current authority, concurrency, exact UUID collision and no prior-history scan. |
| [backend/tests/test_telemetry_scale_safety.py](C:/tecnomania/tecno_software_videowall_1x3/backend/tests/test_telemetry_scale_safety.py) | One fixture operation literal: exercise strong Player activation certificate confirmation rather than activity-only runtime. |
| [backend/tests/test_tenant_relation_matrix.py](C:/tecnomania/tecno_software_videowall_1x3/backend/tests/test_tenant_relation_matrix.py) | Historical guard-omission replay supplies the handler patches needed by approved timestamp assignment. |
| [backend/tests/test_tenant_isolation.py](C:/tecnomania/tecno_software_videowall_1x3/backend/tests/test_tenant_isolation.py) | One current foreign DeviceOutput fixture added; existing historical heartbeat, request and all assertions remain. |
| [docs/SECURITY_R5_1_TELEMETRY_CONCURRENCY_REPORT.md](C:/tecnomania/tecno_software_videowall_1x3/docs/SECURITY_R5_1_TELEMETRY_CONCURRENCY_REPORT.md) | This report. |

The eight unchanged R5-only paths already present in the working tree are:

- `backend/app/api/routes/campaigns.py`
- `backend/app/api/routes/channels.py`
- `backend/app/services/content_cleanup.py`
- `backend/app/services/tenancy.py`
- `backend/app/services/tenant_telemetry.py`
- `backend/migrations/018_telemetry_guard_indexes.sql`
- `backend/tests/test_telemetry_cleanup_safety.py`
- `docs/SECURITY_R5_TELEMETRY_SCALE_REPORT.md`

No R5.1 migration or dependency was added. Existing migration 018 remains the
R5 artifact with unchanged bytes. Migrations 016/017, models, schemas, frontend,
runtime builders, worker and Media Retention are unchanged from the R5 snapshot.
No production migration was run. Schema/index setup occurred only in verified
disposable local QA fixtures. H1 remains open; this is not an H1 implementation.

Success JSON/activation/token/runtime contracts are unchanged; a genuine hot-path
lock timeout is the explicitly documented 503 operational error. No Windows,
Android or Samsung binary was built or physically validated in this task.

QA used PostgreSQL 16.15, while the manually reported production version is 16.14.
The benchmark app was limited to 2 CPU / 2 GiB; QA PostgreSQL had 1 GiB and NO CPU quota
on a 16 CPU Docker host. Therefore this is not combined production 2 CPU / 4 GiB,
cold-cache, Caddy/Uvicorn/network or physical 25-screen qualification. The primary
measurements use real handlers/authentication/PG through minimal FastAPI TestClient.
Media coordination uses a separate NullPool connection, potentially surviving
commit/refresh: production must budget data PLUS coordinator connections. Existing
structural operations can still require substantial historical database work.
The next step is a read-only production re-preflight, not deployment.

## Final hashes and evidence

| File | Raw SHA-256 |
|---|---|
| backend/app/api/routes/player.py | `c2eebca1e4dd287e3f12dec8581bcdef66db11b74fb93ac6abb1e1b7cc79c6c4` |
| backend/app/services/tenant_mutations.py | `12362ba77bdc4e3d43e1a6fd9cd81654a630476fd46806cf0077ef33b0274967` |
| backend/app/services/media_references.py | `c1423c7d222632dd10c8eb739d104ce4ae1ce62e85b9bd5b75a628254964e06b` |
| backend/tests/test_player_telemetry_concurrency.py | `fd94d98e12e1c07439cc6e22ea147b4a1d55e5c9179758a406200c87e49c98ce` |
| backend/tests/test_telemetry_scale_safety.py | `b9340e57af7cb6a326206938215c7e41942667840166be0f82533be4c21d4ffe` |
| backend/tests/test_tenant_relation_matrix.py | `26f05aed95202df2e0c83a971e5297a8e3cccd6e2d59aa8f5e803b3d3809a6a2` |
| backend/tests/test_tenant_isolation.py | `611c20c2b70b87d4cada85a005216300fc5384f4339815b17b3c1c23eeee7610` |

The final report's own hash and all 337 final file hashes are saved externally in
`.tmp/SECURITY_R5_1/FINAL_SOURCE_SHA256.json` by the final review step. Final app80
bytes match all regression, mutation and performance snapshots. The only allowed
post-test source change is this completed report. No source EOL autofix was done;
Git's prospective LF-to-CRLF notices are informational and recorded separately.

Primary evidence under `.tmp/SECURITY_R5_1`:

- `required_regression_v3_final/results/REQUIRED_STRICT_CERTIFICATION.json`
- `focused_v3_final/results/FOCUSED_R51.json`
- `FINAL_MUTATION_CERTIFICATION_306_FINAL.json`
- `PERFORMANCE_CERTIFICATION_v2.json` and all 12 raw burst receipts
- `FINAL_STRUCTURAL_GUARD_PRESERVATION.json`
- `a8_fixture_v2_final_review/FIXTURE_AST_PROOF.json`
- `focused_v3_final/results/A8_TARGET_FINAL.json`
- `V2_CERTIFICATE_FIXTURE_ADAPTATION.json`
- `early_historical_v2/HARNESS_ADAPTER_PROOF.json`
- `FINAL_DELIVERY_PROOF.json`, raw SHA manifests and review patches

`SECURITY_R5_1_INCREMENTAL.patch` is relative to the frozen R5 candidate;
`SECURITY_R5_R5_1_FULL_REVIEW.patch` is the 16-file cumulative diff against HEAD.
Both are generated without staging and reverse-checked without modifying files.

## Required result fields

```text
ROOT_CAUSE_R5_CONTENTION=exclusive client/context/media locks plus unnecessary prior-history validation on activity appends
HEARTBEAT_REQUIRES_CLIENT_WIDE_LOCK=NO_EXCLUSIVE; shared structural barrier retained
PLAYBACK_EVENT_REQUIRES_CLIENT_WIDE_LOCK=NO_EXCLUSIVE; shared structural barrier retained
NEW_LOCKING_STRATEGY=compatible tenant barrier + per-device/channel anchors + FOR_SHARE authority + shared media reads + bound append preflight
WHY_SECURITY_EQUIVALENT=complete current/new append authority invariant; exhaustive history remains structural, not per-report
CLIENT_WIDE_GATE_PRESERVED_FOR_STRUCTURAL_MUTATIONS=YES
TOCTOU_SAFE=PASS
FK_ROW_LOCK_COORDINATION=PASS
R5_SET_BASED_GUARDS_PRESERVED=YES_STRUCTURAL_ACTIVATION_AND_STRONG_FALLBACK
8_PLAYER_SUCCESS=8/8_HEARTBEAT_AND_8/8_EVENTS
16_PLAYER_SUCCESS=16/16_HEARTBEAT_AND_16/16_EVENTS
32_PLAYER_RESULT=32/32_HEARTBEAT_AND_32/32_EVENTS
FALSE_409_COUNT=0
LOCK_TIMEOUT_COUNT=0
SAME_PLAYER_CONCURRENCY=PASS_8/8_EACH_ENDPOINT_MONOTONIC_AND_MIXED_RUNTIME
CROSS_TENANT_CONCURRENCY=PASS_LEGITIMATE_SUCCEEDS_INVALID_SCOPE_403_NO_FOREIGN_NEW_ROWS
STRUCTURAL_MUTATION_CONCURRENCY=PASS
MUTATION_HARNESS=306/306_DETECTED_0_SURVIVORS_ERRORS_INVALID_SKIPS
READ_ISOLATION=PASS
WRITE_ISOLATION=PASS
DELETE_ISOLATION=PASS
RUNTIME_ISOLATION=PASS
VIDEOWALL_REGRESSION=97_PASS
REQUIRED_REGRESSIONS=1860_PASS
NEW_TESTS=49_PASS
UNIQUE_FINAL_TESTS=1909_PASS
FAIL=0
ERROR=0
SKIP=0
FILES_CHANGED=8_R5_1_INCREMENTAL
CUMULATIVE_FILES_CHANGED=16
STAGING_EMPTY=YES
DIFF_CHECK=PASS
COMMIT=NO
PUSH=NO
DEPLOY=NO
PRODUCTION_ACCESSED=NO
VPS_ACCESSED=NO
PHYSICAL_LAB_ACCESSED=NO
PLAYERS_CHANGED=NO
DAYBREAK_USED=NO
```

SECURITY-R5.1 TELEMETRY CONCURRENCY PASS — READY FOR FINAL PRODUCTION RE-PREFLIGHT
