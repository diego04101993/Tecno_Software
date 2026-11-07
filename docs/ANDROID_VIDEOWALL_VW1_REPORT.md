# Android Videowall 1x3 — SaaS VW1

Base: `d92ceefd8d0b1736236e769dc2b7672eda27e911`.
Worktree exclusivo: `C:\tecnomania\tecno_software_videowall_1x3`.
Rama: `feat/android-videowall-1x3`.

Implementación local. Sin staging, commit, push, deploy, VPS ni producción.
No se modifica ningún Player ni el checkout congelado de Campaign Overlays.

## Autoridad y contrato

`Videowall.media_delivery` es `legacy` por defecto. Sólo la selección explícita
de `node_slice` habilita el flujo nuevo, limitado a multi-node 1x3. La ruta de
runtime legacy conserva sus constructores y campos anteriores. No se convierte
automáticamente ninguna pantalla normal ni ningún videowall histórico.

Cada Android tiene su Channel/código/PlayerDevice propio. No son tres outputs
de un solo dispositivo. Cada runtime contiene un output local del tamaño del
panel; el archivo ya está recortado y no debe recortarse otra vez en Android.

La publicación es un snapshot persistido del programa, su geometría y todos los
assets. Preparar requiere campaña activa `sequential`, exclusivamente imágenes/
videos; shuffle y layouts se rechazan explícitamente, sin modificar la campaña.
Su estado recorre PENDING → PROCESSING → READY → ACTIVE, o FAILED.
`active_publication_id` es el único puntero vigente; READY requiere el conjunto
completo validado de tres assets por cada item. Una preparación fallida nunca
reemplaza la publicación vigente. La reactivación de la publicación actual es
idempotente; una ACTIVE histórica no se reutiliza como publicación nueva.

El runtime node_slice v3 expone:

```text
server_time: muestra ISO UTC fresca de la solicitud
videowall:
  enabled: true
  wall_id / videowall_id: identidad del mismo wall
  publication_id: publicación ACTIVE compartida
  media_delivery: node_slice
  rows: 1
  columns: 3
  total_width / total_height: canvas maestro
  sync_group: wall_id
  loop_duration_ms: suma persistida de duraciones enteras
  sync:
    mode: epoch_timeline_v1
    loop_start_epoch_ms: Unix milisegundos persistido al activar
    tolerance_ms: tolerancia del wall
  node:
    position_index: 1, 2 o 3
    row_index: 0
    column_index: 0, 1 o 2
    crop_x / crop_y / crop_width / crop_height: identidad y diagnóstico
playlist[]:
  id / campaign_id / sort_order: iguales entre los tres nodos
  duration_ms: entero autoritativo, idéntico entre nodos
  duration_seconds: duration_ms / 1000, compatibilidad sin redondear a segundos
  content_id: ContentItem derivado propio del nodo
  content:
    id: mismo content_id derivado
    download_url / file_path: archivo derivado, nunca maestro
    mime_type / width / height / checksum / size
    duration_ms / duration_seconds / type
```

`sequence_items` y `contents` referencian esos mismos derivados. No se emite
`node.assets`, `asset_mode` ni un maestro reproducible en node_slice. Si la
publicación, su ownership, sus archivos o su geometría no pueden demostrarse,
el runtime node_slice entrega playlist/sequence/contents vacíos y fallback; no
cae al selector legacy de campañas maestras. También detecta markers inválidos
por `primary_channel_id` antes de filtrar relaciones de lectura.

`loop_start_epoch_ms = activation_server_time + 10000` se calcula una sola vez,
después del gate de tenant y de los locks de activación. No cambia por una
consulta, un reintento idempotente o una nueva sesión/reinicio del Player.

Índice SaaS 1/2/3 → índice interno Android 0/1/2. El contrato SaaS no cambia a
base cero. Root `server_time` es la muestra autoritativa; el adaptador Android
debe estimar el tiempo del servidor sin confiar en su reloj de pared.

Fixture compartible: `backend/tests/fixtures/videowall_epoch_timeline_v1.json`.
La fórmula matemática es `(server_now_ms - epoch_ms) % loop_duration_ms`, con
residuo positivo, y búsqueda por duraciones acumuladas. **Antes del epoch el
Player debe esperar**: el caso negativo del fixture verifica la fórmula, no
autoriza iniciar reproducción antes del epoch. No se implementa un Player aquí.

## Preparación multimedia y worker

Worker explícito: `python -m app.workers.videowall`. No importa `app.main`, no
ejecuta create_all, migraciones ni seeds. PostgreSQL conserva claims, tokens,
lease/heartbeat y reintentos; un token vencido no puede completar ni fallar la
generación de otro worker. Se omiten trabajos cuyo actor ya no está autorizado.
No se introduce Redis. El servicio Compose es opt-in con profile `videowall`,
sin listener ni puerto público; no se ha arrancado ningún Compose productivo.

FFmpeg/FFprobe 7.1.5, paquete Debian `7:7.1.5-0+deb13u1`, en ambos Dockerfiles.
Se usa staging privado fuera de MEDIA_ROOT. Una decodificación del maestro se
divide en tres ramas crop; no hay resize ni estiramiento.

| Preset | Maestro | Derivado | Crop X nodos 1/2/3 |
|---|---|---|---|
| Full HD | 5760×1080 | 1920×1080 | 0 / 1920 / 3840 |
| 4K | 11520×2160 | 3840×2160 | 0 / 3840 / 7680 |

Imágenes estáticas PNG/JPEG/WebP se convierten en tres PNG RGBA con crops exactos.
Video usa H.264 High, yuv420p, CRF18, preset fast, CFR verificado, PTS desde cero,
GOP cerrado aproximadamente cada dos segundos y scenecut deshabilitado. FFprobe
valida tamaños, duración, frames/FPS/PTS y keyframes alineados de las tres salidas.
La duración del video viene del probe real, no del default histórico de 15 s.
Audio AAC sólo en posición 1; 2/3 no llevan stream de audio. No hay audio
multinodo sincronizado V1. El Player deberá mutear 2/3 defensivamente.

Se rechazan dimensiones distintas, VFR, SAR no cuadrado, rotation no cero,
HDR/profundidades no soportadas y formato que no corresponda al tipo declarado;
no hay resize, corrección de rotación ni tonemapping silenciosos. La descripción
de error pública es segura y no refleja paths ni salida cruda de FFmpeg.

## Tenant, registro y retención

Se registran dos modelos y diez FKs nuevos (38 modelos / 97 FKs en total), además
de las tres operaciones HTTP. Los 87 fingerprints anteriores se conservan.
Snapshots JSON son datos tipados, no evidencia de ownership. Wall, branch,
campaign, masters y derivados deben resolver al mismo tenant y branch conforme
a su política. La atribución al creador no hace depender el runtime activo de
que ese usuario siga activo; nuevos trabajos sí revalidan al actor.

Cada slice es un ContentItem real. La reserva interna de paths UUID sólo se
publica dentro del plan/locks/transacción autorizados; ningún metadata/request
puede fabricar esa capacidad. Los archivos se copian completos tras el guard y
la publicación/assets/ContentItems se confirman en un único commit de BD.

Media Retention V1 conserva schema_version=1, complete, channel_id, revision,
protected_campaign_ids y assets. En node_slice enumera derivados propios de
publicaciones PROCESSING/READY/ACTIVE protegidas, nunca URLs maestras. Sin un
snapshot coherente devuelve complete=false. El servidor protege todos los
masters/derivados referenciados, incluso si son candidatos de cleanup.

Limitación deliberada V1: no hay release/GC de publicaciones. Una eliminación
cuyo cierre alcance publicaciones/assets devuelve 409 antes de efectos
(incluye masters, derivados, nodos, campañas y sus padres/creador). Se impiden
cambios de file_path/type en ContentItems referenciados. Históricas ACTIVE
permanecen protegidas; el almacenamiento/caché puede crecer. No hay borrados
automáticos para resolverlo. Un fallo de IO/commit puede dejar un archivo
completo huérfano; no deja una publicación READY parcial y no borra archivos
ajenos para limpiar ese huérfano.

## Migración 016 — sólo QA local

`backend/migrations/016_android_videowall_publications.sql` añade media_delivery
NOT NULL DEFAULT legacy y active_publication_id nullable al wall y crea las dos
tablas de publicaciones/assets. No se ejecutó 015 Campaign Overlays.

Se aplicó 016 en PostgreSQL 16 local aislado, bases vw1_migration_fresh y
vw1_migration_historical. Ambas pasaron 36→38 tablas, defaults legacy/puntero
null, reaplicación idempotente, diez FKs/acciones, cinco índices explícitos y
30 rechazos esperados de CHECK/FK/UNIQUE por base. Las 15 filas históricas
sintéticas conservaron byte por byte todas sus columnas originales (record_send).
Sin reseed. Las FKs simples no prueban tenant por sí solas: lo hacen los guards.

SQL SHA-256: `6b9a17c5140bb5d1ccaf3a4f90569ca8d45c7d4c13f14c8f249d1c91223385b8`.
Verify SHA-256: `9e50a59fe996c174a2030ebdbabb14080c7952aa16124e7844159afddf65b04c`.
Histórico antes/después: `40f9c62ad933662c1db208c4868c72e1ac0aa483ebd2634618687182a8927844`.

No se debe arrancar este backend contra un esquema antiguo sin plan de 016:
el startup create_all histórico no sustituye ALTER TABLE. Aprobar/aplicar en
producción queda fuera de esta tarea. Rollback operativo debe preservar la BD
y los archivos: cambiar a legacy explícitamente y detener el worker; un downgrade
destructivo de tablas de publicaciones no se ejecuta ni se propone como limpieza.

## Validación y evidencia

Validación del candidato final completada. Ningún SKIP cuenta como PASS; no se
suman ejecuciones repetidas como casos únicos.

| Ejecución | Resultado real |
|---|---|
| Windows completo final, copia QA congelada | 2117 ejecuciones: 1345 PASS, 772 SKIP, 0 FAIL/ERROR; 1750.369 s |
| Linux intento completo original | 2114 ejecuciones; 5 fallos de subtests de un método, 506 ERROR y 4 registros SKIP. Este intento NO se declara PASS |
| Linux PostgreSQL/fixtures, reejecución corregida | 573 PASS, 0 FAIL/ERROR/SKIP; 1213.436 s |
| Linux concurrencia PostgreSQL adicional | 2 PASS, 0 FAIL/ERROR/SKIP |
| Linux registry + endpoint catalog, imagen real | 20 PASS (11 + 9), 0 FAIL/ERROR/SKIP; los cinco imports son subtests, no cinco tests adicionales |
| Cobertura Linux final por ID y ordinal | 2124 ocurrencias del plan final: 2113 PASS + 11 SKIP; 0 pendientes; multiplicidad original conservada |
| Imagen backend real, dominio + runtime | 32 PASS (19 + 13), 0 FAIL/ERROR/SKIP; sólo SQLite en memoria, sin red, PostgreSQL ni app.main |
| Media/worker/cleanup focalizados | 22 PASS con FFmpeg real; 6 PASS adicionales PostgreSQL worker/cleanup |
| Nuevas relaciones tenant | 59 PASS SQLite + 49 PASS PostgreSQL; 20 metadata/catalog PASS; 503 filas HTTP RBAC validadas |
| Catálogo de mutaciones | 293/293 certificados válidos: 287 anteriores + 6 VW1; 0 inválidos finales |
| Frontend completo | 347 PASS, 3 SKIP conocidos de evidencia de navegador, 0 FAIL |
| Frontend VW1 | 16 PASS, incluidos en la suite anterior |
| Typecheck app + configuración Node | PASS ambos |
| Instalación/build frontend | PASS con Node 24.22.1 / pnpm 10.17.1, instalación frozen/offline y lockfile existente |
| Dockerfile.production backend real | Build PASS; FFmpeg/FFprobe 7.1.5; import y --help worker PASS sin conexiones DB |
| Caddy/proxy real en Docker local separado | 9 PASS proxy (2 configuración + 7 frontera real) y 1 PASS logs Caddy |

Los 772 SKIP Windows incluyen PostgreSQL, POSIX y FFmpeg, ejecutados en Linux/
PostgreSQL por separado. Los 11 SKIP del plan Linux equivalen a cuatro registros
unittest: Caddy real (1 ID), nombres Windows (1), configuración Proxy (2) y
frontera Proxy real (7). Caddy/proxy se ejecutaron por separado en Docker local;
la restricción de nombres Windows se ejecutó en Windows. Se conservan los SKIP
originales en sus informes, sin transformarlos en PASS dentro de ese intento.

La primera suite Windows se inició mientras aún cambiaba el archivo de tests:
terminó con 29 FAIL y 2 ERROR. Se conserva como intento fallido y fue sustituida
por la ejecución completa final desde una copia congelada de 322 archivos,
verificados 322/322 sin cambios. No se usó para declarar éxito.

Los errores Linux originales fueron 505 rechazos del nombre de BD QA incorrecto
en A7 (antes de abrir conexiones) y un fixture legado que creaba también las dos
publicaciones nuevas protegidas contra borrado. Su bucle ahora construye los
mismos modelos del HEAD base; las aserciones originales no cambian y el caso
nuevo de borrado protegido sigue exigiendo 409. La corrección de fixture y los
controles del harness pasaron 47 pruebas focalizadas en Windows y Linux.
Los cinco fallos de import correspondían al cwd de un subproceso que elegía el
app antiguo de la imagen QA: registry/catalog se repitieron en la imagen real
con cwd correcto, dependencias propias y origen de módulos comprobado.

El catálogo de mutaciones requirió delimitar el anchor A8 al _clean de nivel
superior (prueba causal: dos matches antiguos frente a uno correcto), y corregir
PUBLIC_BASE_URL en el bootstrap QA. No se cambiaron defensas productivas para
satisfacer los mutants. Se conservaron 254 certificados válidos y se repitieron
los 39 de los tres grupos afectados por el origen QA; resultado final 293/293.

Los 119 hashes de producto backend/Compose/Caddy de Linux permanecieron
idénticos. Sólo cambió el archivo de tests autorizado para los fixtures/harness;
los otros 30 tests permanecieron iguales. SHA final de test_tenant_relation_matrix:
`274cf6dfc7c155fd80d0acc80a04bf1e1f43ee03dcf25e5966d4ae7bb2759ee2`.
Los 38 archivos de implementación/tests/configuración del cambio tienen
checkpoint SHA-256 final; el informe es el archivo 39. Staging vacío.

Imagen real: `sha256:379382120509cf277445b247c8991c1ed22be119680b9aeffdb84fd5d4ec5209`.
Python 3.12.16, FastAPI 0.115.0, Pydantic 2.14.2 y SQLAlchemy 2.0.36. Los 109
archivos copiados a la imagen coinciden con sus hashes y los 111 inputs de build
permanecieron intactos. Los tests añadieron sólo paquetes QA puros de httpx,
sin reemplazar dependencias de aplicación ni modificar requirements/imagen.

Lockfile y package.json no cambian. Build advierte Browserslist desactualizado
y chunk principal >500 kB. Git avisa de una posible conversión LF/CRLF futura en
tenant_relations; no se normalizó ni stageó. diff --check PASS.

Los intentos fallidos y los errores de preparación/selección de runner están
conservados fuera del repositorio. No se ocultaron con SKIP ni se relajaron las
aserciones de seguridad anteriores. El certificado Linux identifica cada ID y
su ordinal, la ejecución válida que lo cubre y los intentos previos fallidos.

Evidencia QA raíz: `C:\Users\diego\AppData\Local\Temp\vw1-saas-qa-a7872063c047`.
Evidencia UI/registry: `C:\Users\diego\AppData\Local\Temp\videowall-vw1-ui-9afee3c31c01464bb0df3afa4a2d36af`.
Certificado Linux: `C:\Users\diego\AppData\Local\Temp\vw1-media-44d1393f005f\LINUX_VALIDATION_CERTIFICATION.json`.
Matriz de cobertura: mismo directorio, `LINUX_COVERAGE_MATRIX.csv`.

## Pendiente físico y límites

La sincronización visible, precisión de seek/reloj, decodificación CODE302,
fluidez y supervivencia offline deben validarse en las tres TV Box reales con
el adaptador final Android. QA de archivos 4K no equivale a aprobación física.
FOUR_K_PHYSICAL_APPROVAL=NO.
No se midió capacidad CPU/disco/memoria del servidor productivo; no se accedió
al VPS. El worker sigue siendo un servicio opt-in pendiente de integración.

No se publica master como contenido reproducible node_slice; el delivery /media
existente y H1 no se rediseñan en esta fase. No hay cambios de auth/activation,
heartbeat, protocolos legacy, Overlays, dependencias Python/npm ni repositorios de Players.

## Entrega final solicitada

```text
MIGRATION_016=PASS_QA_LOCAL_ONLY
WORKER=IMPLEMENTED_POSTGRESQL_DURABLE_LEASE_QA_PASS
FFMPEG_VERSION=7.1.5
FFPROBE_VERSION=7.1.5
FINAL_RUNTIME_CONTRACT=runtime_v3/node_slice/epoch_timeline_v1
NODE_INDEX_MAPPING=SAAS_1_2_3_TO_ANDROID_0_1_2
FULLHD_IMAGE=PASS
FULLHD_VIDEO=PASS
FOUR_K_IMAGE=PASS_QA
FOUR_K_VIDEO=PASS_QA
FOUR_K_PHYSICAL_APPROVAL=NO
SHARED_PUBLICATION=PASS
SHARED_CAMPAIGN=PASS
SHARED_TIMELINE=PASS
PERSISTED_EPOCH=PASS
SAME_EPOCH_3_REQUESTS=PASS
LATE_JOIN=PASS_SHARED_FIXTURE
AUDIO_POLICY=SOURCE_AUDIO_ONLY_POSITION_1;POSITIONS_2_3_NO_AUDIO
TENANT_RBAC=PASS
RETENTION=PASS_V1_WITH_CONSERVATIVE_REFERENCE_PROTECTION
LEGACY_REGRESSION=PASS
BACKEND_TESTS=PASS_CURRENT_CANDIDATE;ATTEMPTS_AND_SKIPS_SEPARATED_ABOVE
POSTGRESQL_TESTS=PASS_LOCAL_ISOLATED
FRONTEND_TESTS=347_PASS_3_SKIP_0_FAIL
FRONTEND_BUILD=PASS
FILES_CHANGED=39
STAGING_EMPTY=YES
H1_CHANGED=NO
PLAYERS_TOUCHED=NO
COMMIT=NO
PUSH=NO
DEPLOY=NO
VPS_ACCESSED=NO
PRODUCTION_ACCESSED=NO
```

ANDROID VIDEOWALL 1x3 SAAS VW1 IMPLEMENTED — READY FOR ANDROID FINAL ADAPTER QA

## Archivos del cambio (39: 38 fuentes/tests/configuración y este informe)

```text
backend/Dockerfile
backend/Dockerfile.production
backend/app/api/routes/videowalls.py
backend/app/models/entities.py
backend/app/schemas/domain.py
backend/app/services/cleanup_retention.py
backend/app/services/media_references.py
backend/app/services/media_storage.py
backend/app/services/player_runtime.py
backend/app/services/tenant_mutations.py
backend/app/services/tenant_ownership.py
backend/app/services/tenant_queries.py
backend/app/services/tenant_relations.py
backend/app/services/videowall_derivatives.py
backend/app/services/videowall_integrity.py
backend/app/services/videowall_publications.py
backend/app/services/videowall_runtime.py
backend/app/workers/__init__.py
backend/app/workers/videowall.py
backend/migrations/016_android_videowall_publications.sql
backend/migrations/016_android_videowall_publications_verify.sql
backend/tests/run_tenant_graph_mutations.py
backend/tests/test_rbac_matrix.py
backend/tests/test_tenant_endpoint_coverage.py
backend/tests/test_tenant_relation_matrix.py
backend/tests/test_tenant_relation_registry.py
backend/tests/fixtures/videowall_epoch_timeline_v1.json
backend/tests/test_videowall_derivatives.py
backend/tests/test_videowall_publications.py
backend/tests/test_videowall_runtime.py
docker-compose.yml
docker-compose.production.yml
frontend/src/components/dashboard/VideowallEditDrawer.tsx
frontend/src/components/dashboard/VideowallPublicationsPanel.tsx
frontend/src/pages/VideowallPage.tsx
frontend/src/pages/client/ClientOverviewPage.tsx
frontend/src/types/domain.ts
frontend/tests/videowall_publications.test.mjs
docs/ANDROID_VIDEOWALL_VW1_REPORT.md
```
