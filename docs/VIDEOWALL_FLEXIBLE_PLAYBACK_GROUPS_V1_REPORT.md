# Videowall Flexible Playback Groups V1

Repositorio exclusivo: `C:\tecnomania\tecno_software_videowall_1x3`.
Base: `d92ceefd8d0b1736236e769dc2b7672eda27e911`.
Rama: `feat/android-videowall-1x3`.

## Veredicto

Implementación local y validación automatizada terminadas: **2.234 casos backend únicos PASS y 11 SKIP; frontend 364 PASS y 3 SKIP**, sin FAIL/ERROR. **La prueba física está bloqueada por HTTP 409 en el login administrativo QA**, antes de aplicar el layout 1x2. No se afirma que las dos cajas hayan ejecutado A→B→A.

El laboratorio conserva el layout físico **1x3**, la publicación original `0680eafb-acca-4884-86d3-e63ecd136562` y su epoch `1793845922656`. No hay grupos creados en la base física. No se llamaron endpoints de activación de Player ni se modificaron códigos, hardware IDs, tokens o identidades.

La reproducción aislada del bloqueo demuestra un problema preexistente del guard, con el mismo clon de datos y sin DML/commit:

| Métrica | Imagen anterior | Candidato |
|---|---:|---:|
| Guard bajo cProfile | 25.4504 s | 25.8265 s |
| Consultas SQL | 2291 | 2293 |
| `media_reference_owners` | 550 llamadas | 550 llamadas |
| Tiempo acumulado de esas llamadas | 14.9638 s | 15.1517 s |

El candidato añadió dos consultas a la tabla vacía de miembros. La diferencia de tiempo de una ejecución perfilada no es una certificación estadística de rendimiento. La evidencia de contención real muestra transacciones de telemetría sosteniendo el gate del tenant durante aproximadamente 20 segundos frente al `lock_timeout` existente de 10 segundos. Hubo timeouts, sin deadlocks detectados en la ventana revisada.

La causa está en `backend/app/services/tenant_mutations.py:291,297`: descubrimiento de referencias entrantes que incorpora el historial de heartbeats y repetición de semánticas/ownership en las dos pasadas. `backend/app/services/media_references.py:204,247` reconstruye el mapa de ownership para esas comprobaciones. Los timeouts existentes están en `tenant_mutations.py:442,554`. Estos caminos ya existen en la imagen anterior. **No se ampliaron timeouts, eliminaron heartbeats, omitieron validaciones ni cambiaron guards para ocultarlo.**

Reproducción observada dos veces: con las dos cajas enviando heartbeats, `POST http://192.168.0.62:58180/api/auth/login`, usando las credenciales QA existentes, responde `409 {"detail":"TENANT_RELATION_CONFLICT"}`. El script `verify_physical_transitions.py` se detuvo en `login.raise_for_status()` antes del primer PUT de layout. Un JWT administrativo antiguo había expirado y devolvía 401; es un diagnóstico separado, no pérdida del token de los Players.

Evidencia privada/local: `.tmp/VW1_FLEXIBLE_GROUPS_QA/20261105T190823Z/PREEXISTING_GUARD_CONTENTION_PROOF.json`.

## Modelo y contrato

- `Videowall` y `VideowallNode` siguen describiendo la distribución física.
- `Videowall.playback_groups_enabled=false` mantiene el comportamiento anterior hasta opt-in explícito.
- `VideowallNode.layout_enabled=true` permite retirar un nodo del layout sin borrar su fila histórica, Channel o PlayerDevice.
- `VideowallPlaybackGroup` posee FKs tipadas de cliente, sucursal, muro, campaña y publicación activa. Modos: `INDIVIDUAL` / `SYNCHRONIZED`.
- `VideowallPlaybackGroupMember` referencia un nodo físico; su membresía actual es única. Coordenadas y posición locales se calculan en backend.
- Un grupo individual contiene exactamente un nodo. Un sincronizado contiene al menos dos nodos y debe cubrir un rectángulo contiguo sin huecos.
- Los grupos sin miembros se conservan como históricos. Volver al mismo grupo/campaña/geometría puede reutilizar su publicación activa y epoch, sin activación del dispositivo.
- Ninguna metadata de request establece ownership. Se usan las relaciones y guards Tenant/RBAC existentes.

Operaciones nuevas:

```text
PUT /api/videowalls/{id}/physical-layout
GET /api/videowalls/{id}/playback-groups
PUT /api/videowalls/{id}/playback-groups
```

El PUT físico recibe filas, columnas, dimensiones y asignaciones `{node_id,row_index,column_index}`. Los nodos omitidos quedan `layout_enabled=false`; sus Channels/PlayerDevices no se borran. No cambia silenciosamente resolución, canvas, rotation u orientación de un Channel.

El PUT de grupos recibe el conjunto actual completo:

```json
{
  "groups": [
    {
      "name": "SYNC 1X2",
      "mode": "SYNCHRONIZED",
      "campaign_id": "<campaña propia>",
      "node_ids": ["<nodo físico 1>", "<nodo físico 2>"]
    }
  ]
}
```

Para reutilizar un grupo se incluye su `id`. El backend valida el estado propuesto completo antes de escribir, rechaza solapamientos/sucursales o clientes incompatibles y conserva los registros históricos de grupos/publicaciones.

La API de publicaciones existente admite `playback_group_id` opcional al preparar y filtrar. El puntero activo del grupo es la autoridad para sus publicaciones; la ruta antigua conserva el puntero del muro. Se exige `número de items × número de miembros` assets validados antes de activar. Fallos de procesamiento no publican conjuntos parciales.

Runtime individual: `runtime_version=3`, `mode="normal"`, `channel.mode="normal"`, `videowall=null`, campaña individual y media completa. La proyección no modifica el `Channel.mode` persistido ni impone crop/sync. Se conserva el shape legacy de `content`: no se inventan campos `width`/`height` en esa proyección.

Runtime sincronizado: contrato `node_slice` existente con geometría del grupo; `videowall.node.position_index` es local. `videowall.physical_node` mantiene aparte la posición física y `playback_group_id` identifica el ámbito. Por ejemplo, en el caso sintético 1x3, las posiciones físicas 2/3 pasan a locales 1/2, con crops X=0/1920 y canvas 3840x1080. NODE1 individual no comparte esa timeline.

El worker conserva CFR/PTS/keyframes/checksums y audio sólo en posición local 1. Procesa rectángulos con paneles FHD/UHD, sin resize. Capacidad V1 explícita: máximo `11520×2160 = 24.883.200` píxeles, equivalente al mayor master ya cualificado. **No constituye aprobación física de 4K.**

## Migración QA

Archivo: `backend/migrations/017_videowall_playback_groups.sql`.
SHA-256: `59ac397c33bfeb2e98dee007932539329c723dfd90a364dbad6bccb03a61276e`.

- Dos tablas nuevas: grupos y miembros, IDs/FKs `varchar(36)`, nombres `varchar(120)`, modo `varchar(24)`, posiciones enteras y timestamps con zona.
- Dos columnas booleanas NOT NULL con defaults conservadores: muro `false`, nodo `true`.
- FK nullable `videowall_publications.playback_group_id`, sin backfill de grupos.
- FKs y unicidad de nodo actual/posición local; check de modo y posiciones positivas.
- Sustituye checks fijos `%3` / `BETWEEN 1 AND 3` por positividad; la aplicación comprueba cardinalidad exacta y geometría.
- No borra ni renombra datos/columnas; no modifica Channels, PlayerDevices, códigos, tokens, campañas o media existentes.

Se detalló antes de ejecutar. Ensayo PASS en el clon aislado `vw1_qa`, conservando los 3 Channels, 3 PlayerDevices, publicación, 6 assets y epoch. Después se aplicó exclusivamente a `vw1_physical_qa`, con backup adicional inmediatamente anterior. No hubo reseed ni PostgreSQL nuevo para el laboratorio físico.

Rollback antes del uso de grupos: retirar las adiciones sólo tras verificar que las restricciones antiguas siguen siendo válidas. Después de crear publicaciones de grupos se requiere recuperación desde una copia QA aprobada; no borrar historia para forzar el downgrade. No se ejecutó rollback.

## Laboratorio conservado y material preparado

API: `http://192.168.0.62:58180`; frontend: `http://192.168.0.62:58173/`.
Hora: `GET /api/player/time` en la misma API.
PostgreSQL conserva contenedor `db305f4f748cb5b122dd95b8e1cd1be78221e7769ec8bc6c3aa38b6e9cc83b79` y volumen `vw1_physical_qa_pgdata` sin puerto host publicado.

| Nodo | Channel | PlayerDevice | Código existente |
|---|---|---|---|
| 1 | `1e86380b-9ba3-4149-b85a-3c47e448e756` | `e7d417ba-9a18-4a28-854d-7e4a26b50e63` | `TC-B25E-5DB5` |
| 2 | `7baad9ff-9c8a-4733-88ae-96f4f05a963e` | `fe1c2a1c-a4e0-407b-81ba-cb33b76a127c` | `TC-5C76-3E51` |
| 3, sin caja física | `00a4e419-e42a-4367-a2f2-d6caedc828dc` | `b337d46a-a151-447e-a2a0-cd22c66fb8a2` | `TC-ED30-9988` |

Videowall: `e202bb29-1140-44d4-9fb6-390e88f7cb13`.

Campañas nuevas, aún sin asignar en el laboratorio físico:

- `VW1 QA — 1X2 SYNC`: `49f4cbba-3d00-4dc8-8c8a-0370f9d2cf6a`.
- `VW1 QA — INDIVIDUAL 1`: `bf09ab0c-4962-4447-a36b-5bd6731788bb`.
- `VW1 QA — INDIVIDUAL 2`: `21d47b6a-357f-4a33-801f-7e70025bbed5`.

Masters sintéticos generados, verificados y subidos: imagen/video 3840x1080 LEFT/RIGHT, frontera X=1920, video H264 12s/30fps/360 frames con movimiento/timecode; dos imágenes individuales 1920x1080. Se guardan en `.tmp/VW1_FLEXIBLE_GROUPS_QA/20261105T190823Z/masters`.

API/worker locales usan la imagen final R2 `sha256:58fad4ebbc836a3149ee63430fb08ee87383291fc4e368ee85e7a16aafa22985`, con **79/79 archivos Python iguales al candidato en ambos servicios**. Se conservaron las dependencias y FFmpeg 7.1.5 de la imagen QA anterior. Los contenedores anteriores quedan detenidos, sin borrar. Se mantuvieron IP, puertos, redes, media y base originales; no hubo cambios de firewall.

El perfil comparó la imagen anterior `379382…` con el primer candidato `aa49…`; **no se repitió sobre la imagen final `58fa…`**. La última diferencia productiva fue añadir los dos nuevos tipos de recursos al descriptor `delete_client.models`, sin cambiar los caminos de login/heartbeat. No se atribuyen mediciones de una imagen a otra.

Comprobación final read-only: hora HTTP 200 fresca, LAN `192.168.0.62`, frontend HTTP 200 y sus JS/CSS idénticos al build QA. Las tres identidades, códigos, hardware, hashes de tokens y contadores de activación coinciden con el baseline privado. Evidencia: `FINAL_PHYSICAL_QA_READONLY_R2_PROOF.json`, SHA-256 `e1259be29e23b41763cd6bbef917ccf5e1f239bc935623fd3364b490661b2c7d`.

El build desde el Dockerfile original con red deshabilitada no pudo obtener su capa de paquetes. La imagen QA validada se construyó sobre la imagen local anterior exacta, copiando sólo `app`; no se afirma un build productivo nuevo desde cero. Los intentos de preparación QA fallidos se conservan en evidencia, separados de los resultados del producto.

## Pruebas

- Frontend: **364 PASS, 3 SKIP de navegador existentes, 0 FAIL**, typecheck y build QA PASS. Node 24.22.1 / pnpm 10.17.1. Lockfile sin cambios. API LAN explícita, sin API productiva ni fallback `:8000` en el bundle.
- Grupos Windows/SQLite: **25 PASS, 0 FAIL/ERROR/SKIP**, incluidas identidad, A→B→A, posición física/local, campaña individual, grupo mixto, publicaciones parciales, clientes/sucursales y roles.
- Primer foco Linux/PostgreSQL: **68 PASS, 0 SKIP**. Resultado histórico previo a los últimos casos adicionales; no sustituye el foco final.
- Media/lease, publicaciones y worker Linux: 33 PASS; un fixture nuevo de historia representaba incorrectamente un puntero incompatible. Se corrigió únicamente el fixture conforme a la API, manteniendo sus aserciones: retry 1 PASS. Intento inicial preservado.
- RBAC HTTP: inventario ampliado de 503 a 509 por tres rutas nuevas × dos actores; cambio exclusivo del contador. Repetición de clase **2 PASS**, 509/509 casos HTTP seguros y 37/37 casos WS seguros; expectativas de seguridad intactas.
- Registro: 40 modelos / 105 relaciones; los 97 fingerprints anteriores se conservan. Esto no se presenta como una nueva certificación exhaustiva de mutaciones de las ocho relaciones añadidas.
- Foco final Linux/PostgreSQL: **97 PASS, 0 FAIL/ERROR/SKIP** en 208.44 s: grupos 25, publicaciones 29, runtime 13, derivados/worker 30. Sus 83 archivos de aplicación/tests permanecieron idénticos antes/después.
- Integración de las ocho nuevas relaciones y borrado de cliente: **90/90 PASS**, 49 SQLite y 41 PostgreSQL, sin FAIL/ERROR/SKIP. Registro, inventario de endpoints y RBAC: **22 PASS** adicionales de validación dirigida. Estos retests no se suman otra vez al conteo de la suite completa.
- **Suite backend completa Linux/SQLite/PostgreSQL FINAL-V2: 2.234 IDs únicos PASS + 11 IDs SKIP, 0 FAIL/ERROR/PENDING.** El plan conserva sus diez IDs duplicados: 2.255 ocurrencias = 2.244 PASS + 11 SKIP. Todas las ocurrencias están contabilizadas exactamente una vez, y los duplicados tienen estados coherentes. `unittest.testsRun` suma 2.246 porque nueve métodos omitidos desde `setUpClass` no se incluyen en ese contador. No se cuentan SKIP como PASS.
- Los cuatro shards usan clusters PostgreSQL temporales independientes, sin red externa, puertos host ni volúmenes persistentes; identidad y marcador de cada cluster verificados antes de ejecutar. El PostgreSQL físico quedó excluido. Los **328 archivos** del snapshot y del working tree coincidieron al certificar; los **148 archivos backend** conservaron el digest `f8e9964fef38ba62fb3ac660b1ef1993d9edc0af62a05361a0a7e9d29361330a` antes/después. Después se actualizó únicamente este informe.

SKIP backend separados, pendientes de sus condiciones específicas:

| Casos distintos | Motivo real |
|---:|---|
| 2 | `ProxyConfigurationTests`: Docker Compose no disponible dentro del contenedor de pruebas |
| 7 | `RealProxyBoundaryTests`: falta `SEC1H_PROXY_BACKEND_IMAGE` en este runner aislado |
| 1 | Integración de logs Caddy: falta `SEC1H_CADDY_IMAGE` |
| 1 | Restricciones de nombres exclusivas de Windows, omitidas al ejecutar Linux |

Los tres SKIP frontend son los de navegador ya existentes. No equivalen a pruebas físicas ni a esas integraciones Docker ejecutadas.

La matriz genérica necesitó fixtures válidos para los nuevos grupos/miembros, en lugar de sembrar un modo sintético que violaba el CHECK antes del guard. Sólo las dos relaciones de publicación usan la familia de borrado protegido; las otras seis mantienen el escenario de borrado íntegro sin publicación. Se preservaron las aserciones previas y se añadieron cuatro comprobaciones estrictas para `RESTRICT`. El descriptor productivo `delete_client.models` incorporó exclusivamente los dos nuevos modelos. Las ocho relaciones siguen cubiertas por las matrices READ/WRITE/RUNTIME/DELETE; su exclusión del generador histórico A7 conserva el catálogo anterior y **no certifica mutaciones nuevas de esas ocho relaciones**.

El recolector QA inicial FINAL-V2 reutilizaba IDs de objetos liberados: dos eventos `_ErrorHolder` de SKIP de clase sobrescribieron dos PASS POSIX y dejaron tres registros pendientes. Los cuatro logs originales terminan en `OK`, con cero FAIL/ERROR. Se conservaron sin modificar los ocho JSON/logs crudos y el certificado original `BLOCKED`. Un certificado **derivado separado** reconcilia exactamente esos cinco registros con eventos originales, líneas `ok` y un retest estándar independiente de cinco IDs: **2 PASS + 3 SKIP de clase**, sin cambiar aserciones/fuente. Ese retest no se suma al total. Evidencias en `test-results/final-v2`: `PARALLEL_FULL_RECOVERED_CERTIFICATION.json`, `RECORDER_RECOVERY_DERIVATION.json`, `RECORDER_RECOVERY_MINIMAL_RETEST.json` y `PARALLEL_FULL_UNIQUE_ID_SUMMARY.json`.

La primera preparación de la suite completa omitía `PUBLIC_BASE_URL`: controles de mutación reprodujeron `UNKNOWN_DOCUMENT_ORIGIN`. A/B con los mismos bytes y sólo `PUBLIC_BASE_URL=http://testserver` produjo 2 tests PASS/29 mutaciones certificadas. El intento incorrecto fue interrumpido y no se clasifica como fallo del producto ni PASS.

Los intentos amplios anteriores fueron sustituidos por FINAL-V2: detectaron el contador antiguo 503, los fixtures genéricos de grupos y el descriptor de borrado incompleto, corregidos antes del candidato final. Una ejecución paralela también fue interrumpida por el controlador al retener objetos de test y crecer su memoria; se corrigió sólo el runner QA. Sus resultados parciales no se mezclan con la certificación final.

Tras revisar el certificado se retiraron únicamente los cuatro PostgreSQL temporales FINAL-V2, cotejando ID completo, etiquetas, marcador, identificador del cluster, ausencia de red/puertos y almacenamiento tmpfs. No se retiró ni alteró el PostgreSQL/volumen físico. Logs, snapshots y recibos permanecen; `FINAL_QA_CLEANUP.json` y `FINAL_SKIP_SUMMARY.json` documentan la limpieza y los once SKIP.

## Prueba física pendiente

No se ejecutó en las cajas A→B→A ni se generó publicación 1x2 en la base física: el script se detuvo en el login 409. La primera mutación de layout está después de ese login. No se reclama activación posterior, sincronía visual ni compatibilidad física de CODE302 con 1x2/mixto.

Cuando se resuelva por separado la contención preexistente:

1. Abrir el frontend QA y refrescarlo para cargar la UI local nueva; mantener APK, datos y tokens actuales.
2. Editar el mismo muro a 1x2, asignando NODE1/NODE2; NODE3 debe aparecer sin asignar, nunca borrado.
3. Configurar ambos en un grupo sincronizado con `VW1 QA — 1X2 SYNC`; preparar, esperar 4/4 READY y activar la publicación. Revisar LEFT/RIGHT, frontera, movimiento, timecode y audio sólo a la izquierda.
4. Seleccionar dos individuales y sus campañas 1/2; guardar grupos y esperar polling normal. Revisar imágenes completas y ausencia de dependencia sync.
5. Volver al mismo grupo sincronizado histórico. Reutilizar publicación/epoch cuando su geometría y campaña sigan vigentes. No introducir códigos, reinstalar ni borrar datos de las cajas.

El caso mixto 1x3 es sintético; la tercera caja no existe físicamente. Los archivos y resultados QA contienen sólo datos sintéticos. Credenciales/tokens/backups privados permanecen fuera de carpetas servidas y del diff Git.

## Alcance final

Sin cambios en Players, repositorio Campaign Overlays, producción, VPS, Git staging/commit/push, release o deploy. No se inició H1. Cambios adicionales y hashes de este trabajo se enumeran en la evidencia final del mismo directorio QA.

`FINAL_SOURCE_HASHES.json` y `FINAL_CANDIDATE_VALIDATION.json` registran el estado entregado y sus comprobaciones. La única diferencia respecto a los 328 hashes certificados de FINAL-V2 es este informe actualizado después de las pruebas; los otros 327 archivos deben permanecer byte-idénticos. El working tree conserva los cambios locales, con staging vacío; no se pretende presentarlo como limpio.

29 archivos cambiados respecto al candidato VW1 previo (23 modificados y 6 nuevos). El repositorio ya contenía 39 cambios acumulados de VW1 al comenzar; esos cambios se preservaron y el estado acumulado respecto a HEAD contiene 45 paths, no 45 cambios nuevos de esta tarea.

```text
backend/app/api/routes/videowalls.py
backend/app/models/entities.py
backend/app/schemas/domain.py
backend/app/services/player_runtime.py
backend/app/services/tenant_mutations.py
backend/app/services/tenant_ownership.py
backend/app/services/tenant_queries.py
backend/app/services/tenant_relations.py
backend/app/services/videowall_derivatives.py
backend/app/services/videowall_integrity.py
backend/app/services/videowall_playback_groups.py
backend/app/services/videowall_publications.py
backend/app/services/videowall_runtime.py
backend/app/workers/videowall.py
backend/migrations/017_videowall_playback_groups.sql
backend/tests/test_rbac_matrix.py
backend/tests/test_tenant_endpoint_coverage.py
backend/tests/test_tenant_relation_matrix.py
backend/tests/test_tenant_relation_registry.py
backend/tests/test_videowall_derivatives.py
backend/tests/test_videowall_playback_groups.py
backend/tests/test_videowall_publications.py
frontend/src/components/dashboard/VideowallEditDrawer.tsx
frontend/src/components/dashboard/VideowallPlaybackGroupsPanel.tsx
frontend/src/pages/VideowallPage.tsx
frontend/src/pages/client/ClientOverviewPage.tsx
frontend/src/types/domain.ts
frontend/tests/videowall_playback_groups.test.mjs
docs/VIDEOWALL_FLEXIBLE_PLAYBACK_GROUPS_V1_REPORT.md
```

## Campos solicitados

```text
DEVICE_IDENTITY_DECOUPLED=YES — modelo y pruebas
LAYOUT_EDIT_PRESERVES_ACTIVATION=PASS_SQLITE_POSTGRES — pendiente en cajas
NODE1_CHANNEL_PRESERVED=YES
NODE2_CHANNEL_PRESERVED=YES
NODE1_PLAYERDEVICE_PRESERVED=YES
NODE2_PLAYERDEVICE_PRESERVED=YES

LAYOUT_1X2=IMPLEMENTADO_Y_PROBADO — NO_APLICADO_AL_LAB_FISICO
CAMPAIGN_SYNC_1X2=49f4cbba-3d00-4dc8-8c8a-0370f9d2cf6a
SYNC_MASTER_DIMENSIONS=3840x1080 — imagen/video QA reales
SYNC_NODE1_SLICE=1920x1080 LEFT — PASS_WORKER_AISLADO; NO_PUBLICADO_EN_LAB
SYNC_NODE2_SLICE=1920x1080 RIGHT — PASS_WORKER_AISLADO; NO_PUBLICADO_EN_LAB

CAMPAIGN_INDIVIDUAL_1=bf09ab0c-4962-4447-a36b-5bd6731788bb
CAMPAIGN_INDIVIDUAL_2=21d47b6a-357f-4a33-801f-7e70025bbed5
INDIVIDUAL_NODE1_RUNTIME=PASS_SQLITE_POSTGRES — NO_VALIDADO_EN_CAJA
INDIVIDUAL_NODE2_RUNTIME=PASS_SQLITE_POSTGRES — NO_VALIDADO_EN_CAJA
SYNC_TO_INDIVIDUAL_WITHOUT_REACTIVATION=PASS_SQLITE_POSTGRES — FISICO_BLOQUEADO
INDIVIDUAL_TO_SYNC_WITHOUT_REACTIVATION=PASS_SQLITE_POSTGRES — FISICO_BLOQUEADO

MIXED_1X3_SYNTHETIC=PASS_SQLITE_POSTGRES
NODE1_INDIVIDUAL=PASS_SINTETICO
NODE2_NODE3_SYNC=PASS_SINTETICO — posiciones locales 1/2
PHYSICAL_POSITION_MODEL=VideowallNode + layout_enabled
GROUP_LOCAL_POSITION_MODEL=VideowallPlaybackGroupMember — derivado en backend

MIGRATION_REQUIRED=YES — 017
MIGRATION_QA=APLICADA_EN_CLON_Y_BASE_QA_EXISTENTE
TENANT_RBAC=PASS_MATRICES_GRUPOS_REGISTRO_HTTP_WS_Y_SUITE_COMPLETA; SKIP_PROXY_CADDY_SEPARADOS
LEGACY_REGRESSION=PASS_SUITE_COMPLETA_LINUX_SQLITE_POSTGRES_Y_FOCO_WINDOWS_SQLITE
BACKEND_TESTS=2234_UNIQUE_PASS + 11_SKIP; 0_FAIL_ERROR_PENDING
BACKEND_PLAN_OCCURRENCES=2255 — 2244_PASS + 11_SKIP; 10_DUPLICADOS_IDENTIFICADOS
WINDOWS_SQLITE_GROUPS=25_PASS; 0_FAIL_ERROR_SKIP
FRONTEND_TESTS=364_PASS + 3_SKIP_EXISTENTES; TYPECHECK_BUILD_PASS

TASK_FILES_CHANGED=29 — 23_MODIFIED + 6_NEW
STAGED_FILES=0
DIFF_CHECK=PASS — aviso LF/CRLF de Git, sin normalizar bytes
LOCKFILE_CHANGED=NO
PLAYER_CODE_OR_APK_CHANGED=NO

READY_FOR_TWO_BOX_PHYSICAL_TEST=NO
CURRENT_PHYSICAL_LAYOUT=1x3
CURRENT_PLAYBACK_GROUPS=0
CURRENT_PUBLICATION=0680eafb-acca-4884-86d3-e63ecd136562 — ACTIVE; 6/6 READY
CURRENT_EPOCH=1793845922656
EXISTING_TOKENS_AND_ACTIVATION_COUNTS_PRESERVED=YES
PLAYER_ACTIVATION_REQUESTS_IN_PHYSICAL_LAB=0

COMMIT=NO
PUSH=NO
DEPLOY=NO
PRODUCTION_ACCESSED=NO
VPS_ACCESSED=NO
H1_STARTED=NO
```

No reutilizar los scripts de bootstrap/activación iniciales para esta prueba. `START_QA.ps1` / `STOP_QA.ps1` conservan su función limitada a los cuatro servicios existentes; no reactivan dispositivos.

VIDEOWALL FLEXIBLE PLAYBACK GROUPS V1 BLOCKED — login administrativo QA HTTP 409 TENANT_RELATION_CONFLICT por contención preexistente del guard bajo heartbeats; la transición física 1x3→1x2 y A→B→A aún no se ha ejecutado.
