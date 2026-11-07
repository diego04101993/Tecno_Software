# SECURITY-R4 — Tenant Gate Contention Fix

Repositorio exclusivo: `C:\tecnomania\tecno_software_videowall_1x3`.
Rama: `feat/android-videowall-1x3`. HEAD: `d92ceefd8d0b1736236e769dc2b7672eda27e911`.
Cierre: 2026-11-06T00:42:02.499495+00:00.

## Resultado

Fix local validado y cargado únicamente en la API y worker del laboratorio físico existente. Veinte logins reales: 20 HTTP200, cero409. Layout1x2 y modoA sincronizado preparados, ambos runtimes200 y cuatro slices completas. No se ejecutó físicamente A→B→A. No staging, commit, push, deploy, VPS, producción ni cambios de Players.

La mediana real fue 1024.35ms: la meta orientativa <1000ms quedó ligeramente pendiente; no se declara cumplida. La prueba PostgreSQL controlada demuestra concurrencia heartbeat/login. Las dos boxes no emitieron heartbeats recientes durante los20 logins reales: `LIVE_2_BOX_TRAFFIC=NOT_OBSERVED`. No se simula ni se afirma validación física bajo ese tráfico.

## Respaldo previo y restaurabilidad

Antes de modificar se guardó fuera del repositorio:

- `C:\tecnomania\tecno_software_backups\SECURITY_R4\20261105T223000Z\FULL_PRE_FIX.patch`.
- `PRE_FIX_HASH_MANIFEST.json`:45 paths acumulados, incluido Flexible Playback Groups V1.
- `ALL_SOURCE_SHA256.json` y `PRE_FIX_SOURCE_EXACT.zip`:328 archivos/bytes originales.
- `BACKUP_VERIFICATION.json`:patch restaurado45/45hashes Git canónicos y ZIP raw328/328; índice real intacto.

Patch SHA256: `1767f504ac36591657116c1bc02e160895ffc451bd141bfae2d860682528283a`.
ZIP SHA256: `87a61d82fa5bcf65110de75c577fec22e8522a7ceb7d10ef79d4c30003ec9e20`.
No se descartó ningún cambio previo. El checkout SaaS con Campaign Overlays quedó separado e intacto.

## Root cause y corrección

El login legítimo QA fallaba en10.407 s esperando el advisory lock: SQLSTATE55P03/LockNotAvailable→TENANT_RELATION_CONFLICT, antes del preflight/handler/DML/commit. Tenant esperado y real coincidían;105 FKs sin huérfanos,49 ownership relations sin cruces y50 constraints válidas. No se repararon relaciones ni se reseedearon datos.

El mismo snapshot QA de667 heartbeats reveló224 reconstrucciones de ownership en login y800 en heartbeat, más recorridos y validaciones puras repetidos sobre las mismas filas. El primer ajuste aislado todavía superaba 10 s en heartbeat y no se aceptó como resultado final.

Se conservan advisory locks, timeout 10 s, incoming relations, ownership, context integrity, detección de mutaciones, dos pasadas y comparación completa de planes/dependencias/paths. No se movió trabajo crítico fuera del lock.

1. Índice completo, sin filtrar tenant, inmutable y por raíz. Se reutiliza sólo en una fase ligada a Session, transacción, coordinador y batch completo de paths canónicos realmente bloqueados. Builder público y consultas sin ese batch siguen frescos. No hay caché global ni entre requests/transacciones/fases.
2. Hechos puros del graph por fase/vista inmutable. Exigen provenance del objeto cargado bajo FOR UPDATE en la transacción/gate/token exactos. Snapshots/ancestros custom o precargados sin locking descartan reutilización; propuestas distintas no heredan autoridad.
3. Sólo builtin exacto y filas persistidas/canónicas ContentItem/DatasetImport: primera evaluación completa y revalidación completa END de cada obligación única. DECLINE conserva esas obligaciones. No aplica a CREATE, propuestas alteradas, validadores custom ni publication/layout/group.
4. Discovery sigue no autoritativo: acumula la unión de paths vistos al inicio y END. Segunda fase fresca bajo locks completos y la igualdad original de paths/planes mantienen la autoridad. P→Q persistente no aprovecha un resultado preliminar.
5. HIT de owner ya probado evita repetir el owner-chain; mantiene comprobación O(1) de transacción/gate/token/stage/generación/vista. Ocho clases scalar-only retornan el mismo True anterior antes de calcular un owner no utilizado; autorización/ownership/contexto exteriores permanecen.

Dirty state, flush/commit, CoreDML/DDL, writableCTE/SQL desconocido, fin de transacción, cambio de gate/token/stage/snapshot/binding invalidan y rechazan incluso al salir sin lookup posterior. Excepciones/rollback destruyen el scope. Sólo formas internas exactas SELECT1/current_database/current_schema permiten lectura y descartan reutilización.

Los validadores backend siguen siendo código confiable; no se introduce un sandbox SQL. Los locks media conservan su protocolo cooperativo, sin prometer protección ante SQL/filesystem externo que los ignore ni idéntico orden de sondeos ante cambios transitorios externos. Se preserva must_exist=False: desaparición de un archivo no se convierte en una nueva política DENY; controles negativos usan directorio, ruta canónica cambiada y ownership ambiguo.

Referencias productivas: `media_references.py:38,176,417`; `tenant_mutations.py:99,341,659,686,796,930,1159`. AST/proofs: `.tmp/SECURITY_R4/SERVICE_AST_INVARIANTS_FINAL.json`. Preflight, gates, builder público y media locks originales permanecen; guards son iguales al desenvolver los nuevos scopes; los dos cambios semánticos de despacho descritos arriba están identificados expresamente.

## Rendimiento y definición de métricas

Dump QA restaurado en PostgreSQL desechable separado, sin puertos ni red externa, mediaRO. Antes/después: tres muestras por operación, guard completo sin handler/DML/commit, rollback y fingerprints idénticos de todas las tablas. No se usó producción ni se modificó la DB física para perfilar.

| Operación | Mediana anterior | Mediana final R7 | Builds antes→después |
|---|---:|---:|---:|
| Login |17645.25ms|1586.06ms|224→1|
| Heartbeat NODE1 |59779.16ms|3419.12ms|800→1|

Login R7:1483.29–2623.63ms; heartbeat:2801.85–4096.32ms. Las dos pasadas y conjuntos completos coinciden: login26/26refs,18/18deps; heartbeat406/406refs,762/762deps. Sólo se normaliza el UUID del heartbeat sintético. Cada índice real final se construye en PASS2.

Perfil aparte con observador QA congelado, inclusivo/no aditivo y excluido de latencia/SLO:

| Métrica instrumentada R7 | Login | Heartbeat |
|---|---:|---:|
|LOCK_WAIT_MS|0.33|0.30|
|OWNERSHIP_INDEX_BUILD_MS|72.15|45.11|
|PREFLIGHT_MS|1282.96|7035.88|
|PASS1_MS|533.79|2887.93|
|PASS2_MS|749.18|4147.95|
|INCOMING_RELATIONS_MS|1273.83|7003.93|
|TOTAL_GATE_MS original|2776.36|7269.92|

El TOTAL_GATE_MS original mide guard completo, incluyendo preparación y espera previa; PASS1/2 y PREFLIGHT miden cuerpos preflight y excluyen replay END. No se confunden con tiempo real de retención del gate. Un recibo de precisión adicional, instrumentado mientras corrían otros tests, midió:

| Métrica precisa | Login | Heartbeat |
|---|---:|---:|
|TOTAL_GUARD_MS|3259.57|8232.75|
|GATE_HOLD_MS hasta fin de transacción|1813.36|8210.21|
|VALIDATION_PASS1_MS con END|767.48|4204.75|
|VALIDATION_PASS2_MS con END|965.87|3879.97|

Recibos: `baseline_58fa/R7_FINAL_PERFORMANCE_COMPARISON.json` y `AFTER_R7_PRECISE_METRICS_RECEIPT_V2.json`. No son latencias API. La mejora observada no garantiza la misma latencia con historia/carga futura mayor.

## Reproducción y regresiones

Mismo fixture/caso AST y PostgreSQL real, coste QA controlado300 ms por reconstrucción,48 heartbeats:

- Antes:98 builds protegidos; heartbeat200/login409, SQLSTATE55P03,10118.99ms.
- Final:1 build; heartbeat200/login200,396.70ms, sin55P03.
- Lock realmente retenido10s sigue devolviendo409/55P03; no se aumentó timeout ni se convierte409en200.

La latencia causal controlada no se presenta como benchmark natural. Recibo `test-evidence/FINAL_CAUSAL_PAIR_CERTIFICATION.json`.

| Validación final | Resultado real |
|---|---|
| R4 Linux:37 SQLite + 16 PostgreSQL |53PASS;0FAIL/ERROR/SKIP|
| R4 Windows nativo SQLite |34PASS+3SKIP POSIX;0FAIL/ERROR|
| Backend completo Linux/SQLite/PostgreSQL, certificado compuesto |2297PASS ocurrencias+11SKIP;0fallos/errores restantes|
| IDs únicos backend |2287PASS+11SKIP;10ocurrencias duplicadas preexistentes explícitas|
| READ |182PASS|
| WRITE |210PASS|
| DELETE |525PASS|
| RUNTIME |158PASS|
| Catálogo Tenant/RBAC287+VW1 6 |293/293DETECTED;0survivors/errors/harness_errors/skips/invalid/non-discriminating|

Los53 R4 cubren reutilización/frescura por fase/transacción/request/raíz, mutaciones/flush/commit/SQL, provenance/ancestros/contextos custom, obligaciones END/DECLINE, cambios de path/media, ambos passes y dependencias. PostgreSQL cubre heartbeat+login concurrentes, row-lock legítimo, cross-tenant concurrente, ownership entre pasadas, delete/update, incomingFK, credenciales inválidas, disabled user y20 logins controlados. Regresiones existentes incluyen PhaseA, auth/WS/runtime/retention y FlexibleV1/publicaciones/worker/node_slice/legacy.

SKIP Linux11, nunca contados como PASS:1Caddy502 logging sin imagenQA configurada,1restricción filename exclusivaWindows,2Compose no disponible dentro del testcontainer y7proxy real sin imagenQA configurada. IDs/razones completos en el certificado. Los3SKIP del focoWindows son canonicalización/symlink POSIX, ejecutados en Linux. No se afirma haber revalidado las integraciones Caddy/proxy omitidas.

### Incidencias del entorno y completitud

Primera corrida completa: PostgreSQL temporal tmpfs384 MiB agotó WAL/espacio (ENOSPC); resultado archivado NO calificable como PASS ni fallo del producto. Se repitió con cuatro clusters nuevos/volúmenes independientes de disco y distintos systemIDs, sin tocar PostgreSQL físico.

V2 raw conserva2276PASS+11SKIP+21ERROR_FIXTURE: cuatro shards intentaban generar maestros FFV1 de11520x2160 con1200 MiB cada uno. El cgroup de un worker capturado confirmó OOM; no se inventó inspect para contenedores autoremovidos. Se ejecutó la clase completa21 casos, secuencial/mismos330 bytes/imagen/dependencias/aserciones,6 GiB y cpuset0–3:21PASS/0SKIP/FAIL/ERROR en22.823 s, pico3551383552bytes y0OOM. No se dedujo PASS del diagnóstico de recursos.

Certificado final `full_regression_v2/results/FULL_SOURCE_IDENTICAL_COMPOSITE_CERTIFICATION.json`:2308ordinales/2298IDs completos; sustituye explícitamente sólo esos21ERROR_FIXTURE por sus21ejecuciones PASS reales. Raw FAIL y ambas evidencias se conservan. SHA256: `153aa0ec9bc71e593a1d2fc6e5477b3b5ddaff6598b73688c42f6afe23a74be8`.

Catálogo de mutaciones: se preservan intentos iniciales con controlesPG ausentes/anchors obsoletos. Se completaron los grupos afectados, verificando original/recompilado, defense sites, health, identidad y sourceAST. Certificación compuesta293, no conteo de intentos: `mutations_final293/COMPOSITE_FINAL_MUTATION_CERTIFICATION.json`, SHA256 `2ba208d0c81276f2fcf9e25b584b199c82bacc4f207f938ec9d26d101386ad7d`. No se atribuye al catálogo antiguo cobertura de mutación de nuevos edges Flexible que no contiene; las matrices normales sí los ejercitan.

## Alcance exacto

Cinco archivos R4, sólo dos productivos:

| Archivo | Cambio R4 |
|---|---|
|backend/app/services/media_references.py|Índice ownership temporal/proofs/invalidation;175líneas añadidas,1retirada frente a ZIP previo|
|backend/app/services/tenant_mutations.py|Scope, hechos puros y primera/END validación;311añadidas,15retiradas|
|backend/tests/test_tenant_gate_performance.py|Nuevo,53casos|
|backend/tests/test_tenant_relation_matrix.py|Cuatro constantes de harness;ninguna aserción/fixture/ID cambiado|
|docs/SECURITY_R4_TENANT_GATE_REPORT.md|Este informe|

Tres constantes sólo actualizan indentación del anchor de revalidación al nuevo scope; la cuarta apunta a la defensa vigente de cardinalidad dinámica len(geometry[nodes]) en vez de3. Fixture3nodos, intención, assertion sites y defensa iguales. AST fuera de esas cuatro constantes, número de líneas yCRLF idénticos: `full_regression/results/HARNESS_COMPAT_INDEPENDENT_PROOF.json`.

45 paths acumulados previos, incluidos29 Flexible V1;47 paths acumulados finales respecto a HEAD. Tres archivos previos cambiados y dos nuevos R4;los otros 325 archivos raw del ZIP permanecen iguales. No confundir47acumulados con47cambios de esta tarea.

Hashes finales de código/tests:

- media_references.py: `d79a9a017bd90e104ccb90e60e7580fcd575866ccfb4524d07f394fa51c99896`.
- tenant_mutations.py: `d6a181e18c2262906769d0f38bf181d4b88cca2bc5bc7cec01862e2bafc5715f`.
- test_tenant_gate_performance.py: `4716a6054751e8aadd37214658e7065f1a5d8b2db173d516cbdeaeb88fea6dcc`.
- test_tenant_relation_matrix.py: `5aa961d544e501e586e95bfd78ee41feb4ab3998f44de002b5cea0bdd4da52e0`.

Imagen R7 validada/instalada sólo QA: `sha256:280506a096afc455c09aeea83a1beacbdbb1845b0739396c4d5d7835002b7e58`.79/79app Python coinciden; dependencias/FFmpeg intactos. Después de las pruebas sólo se actualizó este informe; los329 archivos restantes se comparan en el recibo final. Sin cambios R4 en frontend, Players, modelos, schemas, migraciones, dependencias ni Docker/Compose productivo. DiffcheckPASS e índice vacío; advisoryLF→CRLF preexistente de tenant_relations.py no se autocorrigió.

## Laboratorio físico existente

API `http://192.168.0.62:58180`; frontend `http://192.168.0.62:58173/`; time fresco200. Sólo se reemplazaron los dos contenedores API/worker por R7; los anteriores se conservaron detenidos. PostgreSQL `db305f4f…`, systemID7704506771504058402, volumen vw1_physical_qa_pgdata y frontend intactos. No migrations/seed/activation.

20 POST /api/auth/login secuenciales, mismos usuario/password sintéticos privados, sin reintentos automáticos:
min=996.04ms;median=1024.35ms;p95=1107.42ms;max=1297.62ms. P95 nearest-rank ceil(0.95×20). Todos200,0×401/403/409/429. JWT/password no aparecen en el informe ni recibo público. Captura pública `live_qa/LIVE_LOGIN_20_PROOF.json`.

Wall `e202bb29-1140-44d4-9fb6-390e88f7cb13`:1x2, NODE1posición1/NODE2posición2. NODE3 permanece existente, con layout_enabled=false; su PlayerDevice y activación conservan su estado previo. Tres canales, PlayerDevices, códigos, hardware IDs, hashes de tokens y contadores activation se conservaron. NODE1channel `1e86380b-9ba3-4149-b85a-3c47e448e756`; NODE2channel `7baad9ff-9c8a-4733-88ae-96f4f05a963e`.

ModoA inicial: grupo `8290fc89-02f6-44de-8532-ed7fedd6f458`, campaña `49f4cbba-3d00-4dc8-8c8a-0370f9d2cf6a`, publicación `c5f42f6e-6757-4569-92c7-bc3108015b8c` ACTIVE, epoch=1793925568567. Masters3840x1080;dos imágenes+dos videos derivados1920x1080READY. Cuatro descargas LAN verificadas por bytes/tamaño/SHA antes de activar y nuevamente vía runtime;playlist10s+12s y timeline/epoch compartidos, crops0/1920. NODE1/NODE2runtime200,retentioncomplete,ningún master/asset de otro nodo. Publicación histórica0680eafb… y su epoch se conservaron.

Se añadieron sólo las4 derivadas/publicación/grupo/members del setup autorizado. Totales:3channels,3devices,3nodes,4campaigns,16contents,2publications,10assets,1group,2members. No reparación/reseed/activación.

ModoB preparado, NO aplicado: campañas individuales `bf09ab0c-4962-4447-a36b-5bd6731788bb` y `21d47b6a-357f-4a33-801f-7e70025bbed5`,assets1920x1080 existentes. Payloads `live_qa/MODE_B_REQUEST.json` y `MODE_A_RETURN_REQUEST.json` conservan el ID del grupoA/publicación/epoch para probar después. No se afirma observación de sincronización física, calidad audiovisual ni continuidad A→B→A.

Los scripts exigieron certificados completos/hashes antes de actuar y se ejecutó sólo primer intento limpio. No se usó su mecanismo de reanudación; cualquier intento parcial requiere revisión manual, sin retry/rollback automático. Evidencia `live_qa/INITIAL_MODE_A_APPLY_PROOF.json`. Fixtures PostgreSQL de tests permanecen separados del laboratorio y sus recibos se conservan; no se borró ningún volumen físico.

## Entrega solicitada

```text
ROOT_CAUSE_CONFIRMED=YES; SQLSTATE55P03_ADVISORY_CONTENTION
FIX_STRATEGY=PHASE_SCOPED_PROOFS; FULL_FIRST_AND_END_REVALIDATION
TENANT_GATE_TIMEOUT_SEC=10
ADVISORY_LOCK_PRESERVED=YES
TWO_PASS_PRESERVED=YES
INCOMING_RELATION_VALIDATION_PRESERVED=YES
OWNERSHIP_INDEX_BUILDS_BEFORE=LOGIN224; HEARTBEAT800
OWNERSHIP_INDEX_BUILDS_AFTER=1_PER_OPERATION_IN_LOCKED_PASS2
OWNERSHIP_INDEX_CACHE_SCOPE=ONE_PHASE; EXACT_SESSION_TRANSACTION_ROOT_COORDINATED_LOCKED_BATCH; NO_GLOBAL_CACHE
PRE_FIX_REPRODUCTION=PASS; HTTP409_55P03_REPRODUCED
POST_FIX_REPRODUCTION=PASS; HEARTBEAT200_LOGIN200
LOGIN_20_RUNS=20/20_HTTP200
LOGIN_LATENCY_MIN_MS=996.04
LOGIN_LATENCY_MEDIAN_MS=1024.35
LOGIN_LATENCY_P95_MS=1107.42
LOGIN_LATENCY_MAX_MS=1297.62
FALSE_409_COUNT=0
CONCURRENCY_TESTS=PASS; REAL_ISOLATED_POSTGRESQL
CROSS_TENANT_TESTS=PASS
MUTATION_HARNESS=293/293_DETECTED; 0_SURVIVORS; 0_HARNESS_ERRORS
READ_ISOLATION=182_PASS
WRITE_ISOLATION=210_PASS
DELETE_ISOLATION=525_PASS
RUNTIME_ISOLATION=158_PASS
REAL_CONFLICT_STILL_BLOCKED=YES
BACKEND_SUITE=2297_PASS_OCCURRENCES; 11_SKIP; SOURCE_IDENTICAL_COMPOSITE
SECURITY_FILES_CHANGED=5_INCLUDING_2_TESTS_AND_REPORT; 2_PRODUCTIVE_SERVICES
VIDEOWALL_FILES_PREEXISTING=45_ACCUMULATED_PATHS; INCLUDING_29_FLEXIBLE_V1
TOTAL_FILES_CHANGED=47_CUMULATIVE; 5_THIS_TASK
STAGING_EMPTY=YES
DIFF_CHECK=PASS
LOGIN_QA_AFTER=HTTP200_20_OF_20
LIVE_2_BOX_TRAFFIC=NOT_OBSERVED
NODE1_ACTIVATION_PRESERVED=YES
NODE2_ACTIVATION_PRESERVED=YES
LAYOUT_1X2_PREPARED=YES
SYNC_1X2_PREPARED=YES; ACTIVE_INITIAL_MODE_A
INDIVIDUAL_1_PREPARED=YES; CAMPAIGN_MEDIA_AND_PAYLOAD; NOT_ASSIGNED
INDIVIDUAL_2_PREPARED=YES; CAMPAIGN_MEDIA_AND_PAYLOAD; NOT_ASSIGNED
READY_FOR_PHYSICAL_A_B_A_TEST=YES_API_AND_MEDIA_PREPARED; PHYSICAL_VALIDATION_PENDING
A_B_A_EXECUTED=NO
COMMIT=NO
PUSH=NO
DEPLOY=NO
PRODUCTION_ACCESSED=NO
VPS_ACCESSED=NO
```

SECURITY-R4 TENANT GATE CONTENTION FIX PASS — FLEXIBLE VIDEOWALL QA UNBLOCKED
