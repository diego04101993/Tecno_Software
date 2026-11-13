# Blue-Green V2.2

Phases 1 and 2 cover local tooling and disposable QA. [Phase 3](PHASE3_RUNBOOK.md) describes adoption of the permanent ingress and requires separate authorization. Its only successful initial state is BLUE API and BLUE frontend. Production R3, PostgreSQL, uploads, backups and OBS6.4 remain protected throughout adoption.

## Invariants

- The stable Edge owns public TCP 80/443, certificates, HTTPS redirects and the API and frontend routes. Frontend slots serve private HTTP, with `admin off` and no writable certificate store. Slot services publish no ports. Native slots use exclusive DNS names and fixed private IPs. The initial legacy BLUE backend retains its existing private endpoint and network attachments. Only backend slots reach the database network.
- The API and frontend pointers move independently. The first promotion moves `(BLUE, BLUE)` to `(GREEN, BLUE)`. The Player gate precedes `(GREEN, GREEN)`. The controller can restore the preceding pair in one route transaction.
- `routectl` changes routes through the private Unix socket. Slot lifecycle runs separately. `routectl` has no code path that creates, starts, stops, reconnects or recreates a slot. The controller's Docker interface runs local read-only inspection and reads process metadata inside the expected container.
- A READY receipt binds a release and role to a specific process. The controller rechecks container ID, PID, start ticks, `StartedAt`, image ID, OCI revision/tree, runtime configuration, mounts, network endpoints, health, `RestartCount=0` and no OOM. Any identity drift blocks promotion or marks an already-applied transition failed. Keep the prior slot healthy for rollback.
- Use only approved fixtures and existing JWTs for read-only gates. Do not call login or Player-authenticated runtime in shared production QA. Both write to the database. Native backend slots use one worker, `AUTO_SEED=false` and `--lifespan off` to prevent automatic DDL. Phase 3 adopts the exact already-running R3 backend under its independently qualified command and configuration. Run the approved readiness and functional gates on the processes that will receive traffic.

## Files and authority

`compose.edge.yml` and `Caddyfile.edge` define the independent ingress. `compose.slots.yml` and `Caddyfile.slot` define the release slots. `release_state.py` holds the committed routing generation and hash-addressed sealed configurations. Caddy runtime config is the effective state. Its autosave at `/config/caddy/autosave.json` is evidence used in reconciliation. `edge-entrypoint.sh` clears admission and starts Caddy with `edge-bootstrap.json`, which exposes only the Unix admin socket. The service starts without any application listeners or upstream selection.

The state directory and socket directory are owned by the local operator, mode `0700`. The Unix socket and private state, bindings and signing key files are mode `0600` on the Linux control plane. The socket is `/run/bgv2/admin.sock` in the Edge container. Mount its parent explicitly to the control plane. Never publish or bind an admin TCP port. Restrict access to the socket and the Docker daemon to the operator. The release tooling does not need to listen for network requests.

At initialization seal the canonical adapted configuration hash, image IDs and OCI release metadata. The Linux operator must be able to read and fsync the Caddy autosave. `routectl` compares the complete effective configuration and durable copy with the hash in `state.json` before taking action. The root Caddy scope is deliberate. It covers both route pointers and prevents unrelated admin changes from being overwritten by a stale read. A change is restricted to the public generation and the two public pointers and their matching log fields. The private routes remain pinned to their own slots.

The state model has `PREPARED`, `STARTING`, `READY`, `ACTIVE`, `STANDBY`, `FAILED` and `RETIRED`. During a split API/frontend stage the two role pointers are authoritative for active traffic. `retire` records an already approved retirement. It performs no deletion and relinquishes the previous routing rollback target only when a fully active pair exists and still passes its identity guards.

## Durable route transaction

The controller holds nonblocking, exclusive locks for the local state directory and for the Edge admin socket. Every attempt records generation, previous generation, from/to role pointers, timestamp, release, expected ETag, old/new config hashes and intent hash. The state envelope is written via a private temporary file, file fsync and durable replacement. The intent is first `PREPARED`, then `DISPATCHED` immediately before the single `PATCH` with `If-Match`. The controller reads effective routing and the autosave, checks the exact target, fsyncs the autosave and its directory and commits the new state only after the two copies agree.

| Observed state | `reconcile` |
| --- | --- |
| Pending dispatched intent, effective and durable copies match exact target | Commit observed generation and recheck slot identities. If identity drifted, persist failed status and stop. |
| Intent never dispatched, or authoritative `412`, with both copies exactly at the predecessor | Abort the intent and record rollback of the state transition. |
| Timeout after dispatch with both copies still at the predecessor | `STOP_AMBIGUOUS`. The server might still apply the request. |
| Copies disagree, hash or generation is unknown, or state is corrupt or stale | `STOP_AMBIGUOUS` or an explicit state-validation failure. |

`reconcile` never issues a second route mutation. After a committed but failed change, `rollback` validates the prior process and uses a fresh explicit routing transaction. A failed recovery leaves evidence and requires operator review. Caddy autosave can fail after accepting live config. A successful HTTP response is insufficient.

## Cold start and admission

The host supervisor runs `routectl.py supervise-ingress` from `bgv2-edge-ingress.service`. The Edge entrypoint clears the private admission marker on every Caddy boot and publishes a fresh boot ID. The supervisor waits for the private socket, obtains the writer locks, and validates the committed state, sealed configuration, pending intent and autosave before loading any route. The service remains closed when the supervisor fails. A deliberate `recover-ingress` is available for operator reconciliation after a recorded STOP.

An Edge-only restart can recover when the validated slots retain their exact identities. When Linux `boot_id` changes, `LEGACY_BLUE_REATTEST_AFTER_HOST_REBOOT` can attest new process epochs for the same approved backend and private frontend containers. It requires committed BLUE/BLUE, no pending intent or rollback target, exact stable identity, healthy slots, and the same approved healthy PostgreSQL container. The supervisor persists the epochs before admitting traffic. A backend PID, start time or restart-count drift with the same host boot closes ingress with `STOP_REATTEST_REQUIRED`. Stable identity drift, an unprovable runtime epoch and ambiguous routing also keep ingress closed.

Phase 3 binds each approved Docker-generated MAC to its exact role and network. Only those operational MAC fields may change after a host reboot. The runtime epoch retains the observed MACs and every other stable network setting stays pinned.

| Cold-start evidence | Result before opening ingress |
| --- | --- |
| Autosave equals the committed config or a sealed predecessor | Load the committed config. |
| Autosave missing or invalid JSON, committed state and sealed config valid | Reconstruct the exact committed config. |
| Autosave is newer than state and matches the exact durable `DISPATCHED` intent | Recheck identities and confirm that generation once. |
| Unknown newer autosave, invalid state, or unresolved pending intent | Stop with ingress held. |

Private preview may be staged before the first committed generation. The full config keeps both public servers behind a fixed admission-file check. Until exact effective config and fsynced autosave match the committed hash, no public request reaches an app. A configured public listener can return `503` while that check holds. `INGRESS_READY` is the final private marker creation after verification. The administrative socket and marker parent stay owner-only. Never write the marker by hand or use the socket outside the controller. The cold-start recovery command may load a proven committed configuration. Runtime `reconcile` continues to observe and journal without issuing a second switch.

## Initial legacy BLUE identity

`legacy_blue.py` captures stable container, image, command, Compose identity, configuration, health contract, mounts and endpoint evidence separately from the initial process epoch. Approve the checksum independently before passing the private manifest and pin to `routectl.py`. The classifier is scoped to API BLUE and validates its trusted proxy owner. An unexpected container or endpoint fails closed. Native frontend BLUE requires the V2 container identity and exact R3 OCI image provenance. It serves the original image's `/srv` privately. The original R3 frontend remains a stopped rollback asset after handoff.

TLS data may pass between the original frontend and Edge, with one running writer at a time. The old `/config` volume stays with the old frontend. Edge uses its own `/config`, private socket directory and release-state directory. The handoff test transfers TCP 80/443, the trusted IP and TLS ownership in both directions while keeping the backend container and its endpoints intact. The exact R3 images must be qualified separately if the local rehearsal uses sealed stand-ins.

## Private smoke and Player evidence

The preview listeners are bound to loopback inside the Edge namespace, BLUE on `8444` and GREEN on `8443`. The operator reaches them from that namespace and sends the public hostname as Host and SNI with certificate verification. Preview uses the same upstream dial target, transport and header policy as public ingress for that slot. The API preview accepts only audited GET/HEAD paths. The app preview accepts GET/HEAD. Neither allows upgrade. Public routes choose the slot from configuration, independently of request headers, cookies, query strings and URLs. Edge replaces the upstream request ID and forwarding headers. R3 currently trusts `172.30.250.2`. Phase 3 must prove that the new Edge uses that exact identity or approve a separate trust transition.

READY evidence is signed by a trusted QA runner and bound to the live process and exact release. Require `/api/channels/` HTTP 200 within 3.000 s, 36 serial and 16 concurrent runtime reads with four clients and a 10 s transport timeout, frontend listener readiness, one explicit root GET and successful asset validation. Initial legacy BLUE uses the scoped `LEGACY_BLUE` receipt with the historical Windows legacy and Android normal contracts, unchanged schema/media and Phase 3 infrastructure capacity. Native releases retain the Android node-slice and dual-slot gates and isolated pool evidence. Keep signing keys outside fixture containers and restrict signing to the reviewed gate runner.

For Players capture the established 120 s activity baseline before the API switch and observe live traffic for at least 120 s after it. The switch requires a signed fresh baseline bound to the predecessor generation and candidate identity. Its hash is recorded in the intent and enforced against the final Player receipt. Correlate a heartbeat response from the selected Edge generation and exact upstream with the existing ACK's `(channel_id, received_at)` fields. Resolve that tuple to exactly one heartbeat row in a bounded PostgreSQL read-only query. Only a committed row joined to an observed GREEN `200` counts. A missing or ambiguous join fails closed. Keep raw fields in memory and retain only HMAC device and request references with safe timing and counts. Reject missing baseline IDs, incompatible HTTP responses, `409` and `5xx`. Observe playback `201` when present. Absence of playback or an empty baseline does not establish physical-platform compatibility. A rejected Player gate blocks frontend and triggers backend route rollback if the predecessor remains valid.

Edge records an opaque request ID, ingress, route generation, upstream, status and timing. Evidence excludes credentials, request bodies and raw Player IDs. The passive observer reads Edge records and bounded upstream ACK evidence collected independently of clients. The DB resolver uses a read-only transaction and requires a unique committed heartbeat row. A shared DB row by itself never qualifies as GREEN traffic. Keep the capture inside the trusted Edge namespace, restrict and lock its packet filter and treat capture gaps, unmatched records and ambiguous ACKs as failures. Review capture privilege and receipt handling before any production execution.

The observer starts the restricted collector before the API switch so it can see connections open, then waits for collector readiness. The collector has neither a Docker socket, DB access nor the signing key. A trusted parent passes its bounded metadata through a private pipe to the post-switch observer, which reads the exact Edge log, signed pre-switch baseline and read-only PostgreSQL results. Allow at least 120 seconds after the switch and require the collector completion record with zero packet drops. If the observer fails before writing a receipt, the frontend remains unchanged and the release operator executes the explicit API rollback. Do not save raw packet captures or spool pipe contents to disk.

## Disposable QA and future adoption

Run from this directory with Python 3.11 or later. `python -B -m unittest discover -s tests` runs the unit and fault-injection controls. `python -B tests/qa_integration.py` runs the disposable Docker suite. It provisions its own labeled networks, test PostgreSQL, uploads, certificates and isolated slots from exact existing local images. It cleans up resources it owns. Its fixtures and attested results are QA-only. Preserve any pre-existing manual fixture. The runner uses pinned sealed release receipts for platform contracts and reruns private runtime, route, Player, database, media and capacity checks on its disposable topology. Set `BGV2_QA_BLUE_CONTRACT_REPORT` and `BGV2_QA_GREEN_CONTRACT_REPORT` if those receipts are stored outside their local default paths. The disposable workload qualifies the test envelope only.

`python -B tests/qa_edge_recovery.py` exercises the real Unix-socket Caddy API, a stale ETag, response loss before and after application, and recovery after restart. This test uses an isolated Caddy without published ports or PostgreSQL. The process identity records in this test are synthetic. The integration suite supplies actual Docker identity evidence.

`python -B tests/qa_edge_boot.py` checks cold-start admission with actual HTTP. `python -B tests/qa_initial_handoff.py` measures ten handoffs and ten reversions using container TCP 80/443, isolated host port mappings, the trusted proxy identity and historical test TLS material. Each direction has a 30-second maximum. `python -B tests/qa_player_observer.py` exercises passive attribution. `python -B tests/qa_phase3_player.py` exercises passive continuity for disposable Players through the exact legacy BLUE backend and generation zero for at least 120 seconds.

For a disposable POSIX Edge using the Compose file directly, create new private directories and render generation zero from this directory. This example presumes disposable paths and a cached Caddy image. `edge_config.py` refuses to overwrite an existing baseline or autosave. The Caddyfile uses an internal CA by default.

```sh
set -eu
umask 077
install -d -m 0700 "$BGV2_EDGE_CONFIG_DIR" "$BGV2_EDGE_SOCKET_DIR" "$BGV2_STATE_DIR"
docker run --rm --pull never --network none \
  --mount "type=bind,source=$PWD/Caddyfile.edge,target=/edge,readonly" \
  caddy:2.11.4-alpine caddy adapt --adapter caddyfile --config /edge \
  > "$BGV2_STATE_DIR/adapted.json"
python3 -B edge_config.py --config-dir "$BGV2_EDGE_CONFIG_DIR" \
  --baseline "$BGV2_STATE_DIR/edge-baseline.json" < "$BGV2_STATE_DIR/adapted.json"
```

Use new isolated external networks and volumes, image IDs and private slot environment files when rendering Compose. The CLI requires the private bindings, signing key, state directory, Edge name, Unix socket path and autosave path. `preview` starts private-only validation. `initialize` binds the sealed generation-zero hash and both BLUE readiness receipts. `recover-ingress` opens from the verified committed state. `prepare`, `start`, `ready` and `invalidate` record the candidate lifecycle. `switch-api`, `switch-frontend`, `rollback` and `reconcile` operate on the recorded release and routing state. Slot startup remains an explicit lifecycle action outside the controller. Limit Compose lifecycle commands to the two named services of the inactive slot. The active and rollback slots are protected. `routectl.py --help` lists the exact arguments.

Phase 3 qualifies the infrastructure footprint of the existing R3 backend and PostgreSQL, private frontend BLUE, Edge and the retained historical frontend. Live capacity with both backends belongs to Phase 4, before any GREEN promotion. The initial Phase 3 public port, IP and TLS transfer requires separate authorization, exact R3 qualification and a production-specific rollback plan. Routine V2 promotion and rollback leave validated slot processes alive. Verify client reconnect and old hashed-asset availability across split and rollback states. Future releases use expand/contract compatibility and defer destructive changes until after retirement. Migration 019 remains verify-only when the exact valid index already exists.

The disposable R3-shaped rehearsal uses the historical Caddyfile with an isolated CA and cached sealed stand-in images if the exact R3 IDs are absent locally. It verifies strict HTTPS through a mapped host port. The runbook moves backup, stable identity qualification, private smoke and generation-zero preparation before the transfer. After transfer it verifies public HTTPS before the longer observation. Phase 3 still requires a fresh read-only qualification of the exact R3 image provenance, published address families and approved handoff window on the target host.
