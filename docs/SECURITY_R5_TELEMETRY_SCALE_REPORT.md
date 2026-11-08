# SECURITY-R5 — Telemetry scale hardening

## Veredicto: BLOCKED / NO-GO

El candidato elimina la materialización del historial en Python y pasa las
pruebas de integridad, pero **no cierra el bloqueo de disponibilidad**.
En una única ráfaga local de ocho Players diferentes, activos y del mismo
cliente, cuatro heartbeats devolvieron HTTP 409 / `TENANT_RELATION_CONFLICT`.
Los cuatro errores fueron SQLSTATE `55P03` esperando **10.004–10.006 segundos**
el mismo advisory lock del cliente. No hubo conflictos de versiones preparadas,
credenciales inválidas, retrasos artificiales, retries ni OOM.

Esta reproducción usa almacenamiento Linux nativo, el código final y sólo
31,250 heartbeats + 31,250 playback events históricos por dispositivo. No se
presenta como prueba de 25 dispositivos ni como reproducción de toda producción.
Es suficiente para conservar el NO-GO: los índices y la reducción de memoria
Python no resuelven por sí solos el trabajo histórico dentro de la sección
exclusiva del cliente.

**No publicar/desplegar este candidato como R5 aprobada.** Los cambios locales
quedan disponibles para revisión. No se modificó producción ni el laboratorio
físico VW1; no se hizo staging, commit, push o deploy.

## Base y alcance

- Repositorio: `C:\tecnomania\tecno_software_videowall_1x3`.
- Rama: `feat/android-videowall-1x3`.
- HEAD inicial/final: `aea6ff3c43d45f3c4b5294a2d59cd06b69a20464`.
- Working tree inicial limpio; staging vacío. Delta R5: **11 archivos**
  (6 existentes modificados y 5 nuevos).
- 016/017, modelos, schemas, rutas Player, builder/runtime, scheduler, playlists,
  Media Retention, frontend, dependencias, Docker/Compose y worker permanecen
  byte-idénticos a la base. Ningún repositorio Player fue utilizado.
- El informe se genera después de las pruebas. Los hashes de código/fixtures
  se verifican por separado del documento final generado.

Datos productivos **proporcionados por el usuario**, sin acceso del agente a
producción en R5: PostgreSQL 16.14, aproximadamente 3,465,851 heartbeats / 6.33 GB
y 4,331,416 events / 2.54 GB, sólo PK indexes en ambas tablas, planes por FK con
Parallel Seq Scan. Host: 2 CPU, aproximadamente 4 GB RAM, sin swap.

QA: PostgreSQL **16.15** en imagen local fijada por digest, API Python 3.12 en
imagen R7 `280506a096af...`, bases/volúmenes nuevos con owner `SECURITY_R5`,
marker y system identifier comprobados, network=none y sin puertos PostgreSQL
publicados. Las pruebas no importan `app.main` ni su startup/create_all; las
fixtures crean únicamente sus bases/schemas locales aislados. No se reutilizó
la base física VW1. La API QA tiene límite de 2 CPU / 2 GiB; PostgreSQL tiene 1 GiB de RAM
y **ninguna cuota de CPU** configurada. No se afirma un presupuesto conjunto
equivalente al VPS. La diferencia de minor y el hardware real siguen pendientes
de calificación productiva.

## Causa y call graph real

Antes, `preflight_relationship_graph` descubría cada FK entrante mediante
`SELECT id ... WHERE FK=:parent ORDER BY id`, sin filtro de tenant, seguido de
snapshot completo/FOR UPDATE, contexto, semántica, dependencias y cola por cada
identidad histórica, en ambas pasadas. R4 evitó recomputaciones de pruebas puras,
pero no eliminó esa cardinalidad. Sin índices por FK se añadían escaneos de tabla.

Referencias actuales:

| Operación | Archivo / línea | Entrada al historial |
|---|---|---|
| GET runtime Player | `backend/app/api/routes/player.py:182`, guard en 194 | UPDATE last_seen de PlayerDevice; FK player_device_id de ambas tablas |
| POST heartbeat | `backend/app/api/routes/player.py:203`, intents en 251 | UPDATE Channel + Device y CREATE heartbeat; incoming por channel_id y device_id |
| POST playback event | `backend/app/api/routes/player.py:272`, intents en 296 | Device existente + CREATE event; incoming por device_id |
| Activación / edición de Device existente | `backend/app/api/routes/player.py:109`, guard en 156 | Channel/Device existentes; no se asume un endpoint administrativo independiente de Device |
| Edición de Channel, heartbeat administrativo, asignaciones sobre Channel | `backend/app/api/routes/channels.py`, operación update_channel | Incoming channel_id y, transitivamente, Device de ese Channel |
| Mutación/borrado Client o Branch | `backend/app/api/routes/clients.py`, `branches.py` | FK client_id/branch_id y recursos entrantes correspondientes |
| Mutación/borrado Campaign, ContentItem o Layout | Registro `backend/app/services/tenant_relations.py:164`–176 | FK histórica opcional que apunta al recurso modificado |
| Login | `backend/app/api/routes/auth.py:23`, User intent en 35 | **No llega al historial** por incoming traversal en el registro actual. Puede esperar detrás del mismo gate ocupado por otro request del cliente |
| Runtime administrativo / time | Operaciones READ y `/api/player/time` | No son la actualización last_seen de Player y no disparan este recorrido WRITE |

La clausura entrante del modelo User alcanza atribuciones LayoutRevision y
Publication/wall/group/nodes/assets, **no** Channel/Device como nuevos roots ni
telemetría. Los padres de contexto no se promocionan a roots de incoming.

Puntos de integración: `tenant_mutations.py:487` (preflight), 711 (write guard),
822 (player guard), 1192 (delete plan), 1252 (coverage) y 1277 (execution).
El advisory lock sigue en `tenant_mutations.py:409`; lock_timeout=10s del Player
en 866. La comparación telemetry de las dos pasadas está en 798, 905 y 1213.

También se eliminaron iteradores de owners históricos en los preflights de
borrado `channels.py:146`, `campaigns.py:178`, `content_cleanup.py:281`, usando
`tenancy.py:29`: EXISTS(owner IS DISTINCT FROM expected), incluyendo NULL.
Los demás checks de esos helpers permanecen.

## Invariante y clasificación de las 14 relaciones

Fuentes: modelos `backend/app/models/entities.py:1022`–1097, registro
`tenant_relations.py:164`–177 y reglas de `tenant_ownership.py`.

Para una fila histórica:

- PK no vacía y referencias requeridas presentes/existentes.
- Owner autoritativo = Client de row.client_id; mismo owner que Branch,
  Channel, Device y cada Content/Campaign/Layout opcional presente.
- row.branch_id = Channel.branch_id = Device.branch_id.
- row.channel_id = Device.channel_id.
- Contexto de tenant/sucursal/parent autorizado; closure de dependencias y
  semánticas de las entidades referenciadas válidas, actuales y propuestas.
- NULL es permitido sólo para las tres referencias opcionales. No se añade
  requisito de actividad a referencias históricas: se conserva profile DELETE.

| Modelo | FK | Destino | NULL | Clasificación R5 |
|---|---|---|---|---|
| Heartbeat | client_id | Client | No | B |
| Heartbeat | branch_id | Branch | No | B |
| Heartbeat | channel_id | Channel | No | B |
| Heartbeat | player_device_id | PlayerDevice | No | B |
| Heartbeat | current_content_id | ContentItem | Sí | B |
| Heartbeat | current_campaign_id | Campaign | Sí | B |
| Heartbeat | current_layout_id | Layout | Sí | B |
| PlaybackEvent | client_id | Client | No | B |
| PlaybackEvent | branch_id | Branch | No | B |
| PlaybackEvent | channel_id | Channel | No | B |
| PlaybackEvent | player_device_id | PlayerDevice | No | B |
| PlaybackEvent | content_id | ContentItem | Sí | B |
| PlaybackEvent | campaign_id | Campaign | Sí | B |
| PlaybackEvent | layout_id | Layout | Sí | B |

B significa probar en SQL que no existe una fila incompatible, no omitirla.
Los hechos A de cada fila se conservan mediante la prueba universal. C sólo
prueba existencia/nullabilidad con las FK actuales; **no** demuestra igualdad
entre owner/branch/channel ni semánticas opacas. No se excluyó ninguna de las
14 relaciones por rendimiento. D aplica a operaciones/superficies sin esos
roots de mutación, no a una ventana arbitraria del historial.

## Implementación local final

`backend/app/services/tenant_telemetry.py:97`:

1. Seleccionar **todas** las filas de la igualdad FK entrante, sin filtro previo
   por owner, fecha, cantidad o edad. Subquery ordenada con FOR UPDATE.
   COUNT consume las filas reales; siete array_agg(DISTINCT FK) devuelven sólo
   identidades de entidades padre, nunca IDs/payloads del historial a Python.
2. Bloquear esos padres y comprobar su provenance exacta de snapshot/transacción.
   Ejecutar una consulta fresca después de esos locks.
3. Agrupar en SQL los vectores de siete FK idénticos, incluyendo la validez de
   la identidad histórica y el ID cuando el parent scope de esa fila lo necesita.
   El predicado depende exactamente de esos valores y de padres bloqueados;
   por ello una clase idéntica puede comprobarse una vez sin omitir filas.
4. LEFT JOIN + predicado current/proposed +
   `coalesce(bool_or(predicate IS NOT TRUE), false)` rechaza FALSE/NULL y admite
   el conjunto vacío. Las propuestas de padres se superponen por identidad.
5. Mantener graph/context/opaque/media semantics existentes sobre las entidades
   referenciadas. Registrar certificados backend de scope en MutationPlan y
   compararlos en ambas pasadas frescas. No hay cache entre requests/pasadas.
6. DELETE comprueba el statement **original** con
   `EXISTS(target AND permitted IS NOT TRUE)` antes de añadir su frontera SQL.
   Un target falso o NULL fuera del lote produce 409 antes de cualquier DML.
   La pertenencia requiere la misma transacción/gate/token. No se reconstruye
   una lista de millones de IDs para UPDATE/DELETE de telemetría.

Se conservan advisory locks exclusivos, timeout 10s, READ COMMITTED, two-pass,
R4, locks de padres/filas, media locks, contexts y validaciones actuales/propuestas.
SQLite conserva el oráculo row-wise. Modelos/relaciones/adapters no revisados y
propuestas sobre filas históricas existentes conservan el fallback exhaustivo
original. Los handlers actuales de borrado de Channel/Branch usan limpieza
bulk; Client usa DB cascade. No se añadió un ejecutor DML reflectivo.

**Límite importante:** la base sigue examinando/bloqueando O(historial relacionado)
en cada pasada; Python escala por entidades padre distintas. No se afirma costo
DB constante ni calificación para todos los volúmenes/ráfagas productivos.

### Dos regresiones del candidato detectadas y corregidas antes de entrega

- PK histórica `id=''` con siete FK coherentes: RowRef antiguo devolvía 409,
  las primeras versiones SQL permitían. La proyección `_valid_identity` y su
  predicado restablecen el rechazo. BEFORE PG: dos subcasos fallaron por ALLOW;
  AFTER SQLite/PG y mutación específica: PASS. Sin conversión/reparación de datos.
- Coverage nullable: `NOT permitted` dejaba NULL fuera del anti-check y podía
  recortar silenciosamente un statement A+B, modificando A parcialmente.
  BEFORE: cuatro fallos PG reales (ambos modelos, DELETE/UPDATE), con A cambiada
  y B intacta; SQLite antiguo rechazaba. Se sustituyó por IS NOT TRUE en
  `tenant_mutations.py:1307`. AFTER y mutación específica: rechazo antes de DML.

Ambos fallos iniciales quedan conservados como evidencia, no contados como PASS
ni atribuidos a producción.

## Pruebas finales reales

Código final: main `d2f9601...`, helper `863df881...`, test `9447a31...`.

| Validación | Resultado |
|---|---:|
| R5 focal + cleanup | 45 PASS, 0 FAIL/ERROR/SKIP |
| Regresiones requeridas | 1,860 PASS, 1,860 IDs únicos, 0 FAIL/ERROR/SKIP |
| READ | 182 PASS |
| WRITE | 210 PASS |
| DELETE | 525 PASS |
| RUNTIME | 158 PASS |
| R4 | 53 PASS |
| Media Retention | 22 PASS |
| Videowall (016/017/publications/worker/groups/runtime/legacy) | 97 PASS |
| Catálogo original | 293/293 mutaciones detectadas, certificados válidos |
| Mutaciones R5 específicas | 13/13 detectadas |
| Supervivientes / errores / harness errors / certificados inválidos / SKIP | 0 |

Incluye violaciones en primera/medio/última fila entre 1,105 filas antiguas por
modelo, siete FK, ambos modelos, current/proposed, closures y media semántica;
regresión de canal/device, bloqueo de UPDATE de owner/branch existente, nuevos
heartbeat/event, parent update/delete, entrada cross-tenant, cambios entre
pasadas, PK vacía, scope nullable, límites de lote y transacción/token.

El test contra el código anterior falló realmente por materializar historial;
no fue un error de fixture. La adaptación del oráculo de DELETE conserva las
aserciones anteriores en el camino exact-ID y verifica tipo/key/parent/count,
SQL membership, require_delete_coverage y exclusión de IDs históricos en el
camino de certificados. Roles, casos foreign e intención de escenarios siguen.

Se descubrieron 2,353 casos; **493 fueron omitidos por alcance**, no ejecutados,
no SKIP y no PASS. No se presenta como una suite completa de backend ni como
repetición de las antiguas suites Windows/Linux de aproximadamente 2,000 casos.
La selección exacta está en SCOPE_SPEC/REQUIRED_PLAN del pipeline. Los 21 casos
pesados de media se ejecutaron después, 21 PASS, peak 3,650,924,544 bytes bajo
6 GiB y cpuset 0–3, sin OOM. Esa creación de fixtures no califica el worker
productivo de 4 GB ni una instalación física 4K.

## Rendimiento: separar mejoras, artefactos QA y blocker real

### Pareja exacta, 5,000 filas por tabla

Mismos IDs, historial e índices retenidos; runner idéntico al BEFORE, código
final exacto. La mitad de las filas corresponde al nodo observado. Una ejecución
por operación; no son percentiles/SLA.

| Operación | BEFORE ms | AFTER final ms | SQL antes → después |
|---|---:|---:|---:|
| Guard Channel | 33,399 | 756 | 10,058 → 66 |
| Heartbeat HTTP 200 | 38,481 | 1,202 | 10,077 → 85 |
| Playback event HTTP 201 | 37,638 | 599 | 10,046 → 50 |
| Runtime HTTP 200 | 38,022 | 2,722 | 10,109 → 113 |

Antes: 10,000 diccionarios históricos por operación (dos pasadas sobre 2,500
filas propias de cada tabla), además de IDs del discovery. Después: **0**
diccionarios/IDs históricos materializados por el preflight masivo. Las arrays
de padres se contabilizan, no se ocultan bajo una métrica de una sola fila
agregada. La nueva fila del POST y la lectura preexistente del último heartbeat
para resumen son operaciones de negocio separadas: su LIMIT 1 no constituye
una prueba de seguridad ni se usa para recortar la validación histórica.

### Un millón de filas, 16 dispositivos

500,000 por tabla, dos clientes, 31,250 filas por tabla/dispositivo observado.
Perfil uniforme: un vector FK y siete padres por modelo. Es best-case de
repetición, no peor caso productivo. Guard/HB/event/runtime medidos:
5.419 / 4.154 / 1.683 / 4.940 s. Los accesos relevantes usan Bitmap/PK; no quedan
los escaneos completos de 500,000 filas de los primeros EXISTS.

Las versiones iniciales y sus fallos se preservan: EXISTS elegía Seq Scan por
startup/early-hit cost, aunque no hubiera violaciones, y HB tardaba 13.140 s.
La versión final usa total-cost aggregate, subquery bloqueante en streaming y
vectores equivalentes. Quedan sort/temp y WAL por row locks: no se presentan
índices como eliminación del costo O(N). EXPLAIN ANALYZE/BUFFERS/WAL sólo se
usó en QA; cada replay es otra transacción, por lo que su WAL no se suma como
si fuera el WAL de un único request.

### Referencias diversas y control del filesystem

Perfil separado: 62,500 filas por tabla, 31,250 del nodo observado, 121 Content,
121 Campaign y 121 Layout por cliente; 14,641 vectores / 367 padres por modelo.
No se modificó el dataset uniforme ni la pareja 5,000.

Sobre bind Windows, las mediciones fueron 45.899 / 61.901 / 58.427 / 114.965 s.
**No se atribuyen esas cifras a cloud Linux.** El control con 242 PNG idénticos
(16,456 bytes), copiados/hash-verificados a tmpfs Linux antes de medir, dio:

| Operación | Native Linux ms | SQL |
|---|---:|---:|
| Guard | 4,970 | 786 |
| Heartbeat 200 | 4,397 | 805 |
| Event 201 | 6,275 | 770 |
| Runtime 200 | 7,076 | 833 |

Datos y runner son los mismos; la única diferencia de código entre la primera
medición bind y el control final es el fix de coverage DELETE, fuera de las
rutas medidas. Se conservan ambos hashes; no se afirma identidad byte a byte
entre esos dos snapshots completos. El diagnóstico cProfile se guarda aparte:
36,555 Path.resolve y 19 builds de media_reference_owners entre cuatro requests;
no es otra muestra válida de latencia/capacidad.

HB nativo aislado quedó por debajo de 10s: la prueba causal HB+login propuesta
no se ejecutó ni se contó como PASS. Se efectuó en cambio la ráfaga real siguiente.

### Blocker reproducible final: ocho Players simultáneos

- Mismo cliente; ocho Device IDs y ocho Channel IDs diferentes, tokens existentes
  activos verificados contra sus hashes. Sin activación ni IDs nuevos de Player.
- Dataset uniforme existente: 500,000 filas por tabla, 31,250 por dispositivo.
- API app final exacta, PostgreSQL aislado, native tmpfs y dos PNG con hashes
  comprobados antes de medir. Barrera simultánea; sin sleeps, retries o cambios
  del timeout/pool/QoS. Se esperó al fin de las demás suites antes de ejecutarla.
- **4 HTTP 200 y 4 HTTP 409**; los cuatro rechazos son 55P03 en la clave exacta
  del tenant advisory lock, esperando 10.004–10.006 s.
- Esperas de las cuatro adquisiciones exitosas: aproximadamente 9 / 3,602 /
  6,870 / 9,118 ms. Latencias exitosas: 4.479 / 7.740 / 9.993 / 12.526 s.
- Prepared-state conflicts=0, OOM=0. El exit0 del runner sólo indica evidencia
  completa; **no** convierte el resultado de capacidad en PASS.
- Sólo los cuatro nuevos heartbeat UUIDs, con tag y scope comprobados, fueron
  eliminados de la fixture. Conteos restaurados a 500,000 por tabla; los 16
  Device IDs y hashes de token siguen iguales. Activations=0.

Reproducción exacta y registros por request:
`.tmp/SECURITY_R5/burst_native/NATIVE_EIGHT_DEVICE_BURST.json` y su launcher
`burst_native/launch.py --run` (ver BURST_PREPARATION.json).
El launcher protege la evidencia existente y rechaza una segunda ejecución en
esa misma carpeta. Para reproducir más adelante, sin ejecutarlo ahora: copiar
los tres scripts byte-idénticos a una carpeta hermana nueva
`.tmp/SECURITY_R5/burst_native_repro_<timestamp>`, mantener intactas las fixtures,
comprobar los hashes/fences y ejecutar `launch.py --run` desde esa copia con el
Python QA. Esperar el JSON de esa carpeta; el arranque del contenedor es asíncrono.
No borrar el resultado original para eludir su protección.
No es un fallo de integridad: es **contención legítima / disponibilidad** bajo
el gate exclusivo conservado. Archivo/línea: `tenant_mutations.py:409`, timeout
Player en 866; trabajo dentro del gate en 822 y preflight 487, con las consultas
históricas de `tenant_telemetry.py:97`.

## Índices y fase 018 — sólo artefacto, no producción

`backend/migrations/018_telemetry_guard_indexes.sql` es un script **psql de
predeploy NO transaccional**, con DB esperada explícita y autocommit. No se
incluyó CONCURRENTLY dentro de BEGIN ni en startup/create_all.

Definiciones nuevas cuando no hay un índice elegible equivalente:

```sql
CREATE INDEX CONCURRENTLY ix_player_heartbeats_player_device_id ON public.player_heartbeats (player_device_id, id);
CREATE INDEX CONCURRENTLY ix_player_heartbeats_channel_id ON public.player_heartbeats (channel_id, id);
CREATE INDEX CONCURRENTLY ix_player_heartbeats_client_id ON public.player_heartbeats (client_id, id);
CREATE INDEX CONCURRENTLY ix_player_heartbeats_branch_id ON public.player_heartbeats (branch_id, id);
CREATE INDEX CONCURRENTLY ix_player_heartbeats_current_content_id ON public.player_heartbeats (current_content_id, id) WHERE current_content_id IS NOT NULL;
CREATE INDEX CONCURRENTLY ix_player_heartbeats_current_campaign_id ON public.player_heartbeats (current_campaign_id, id) WHERE current_campaign_id IS NOT NULL;
CREATE INDEX CONCURRENTLY ix_player_heartbeats_current_layout_id ON public.player_heartbeats (current_layout_id, id) WHERE current_layout_id IS NOT NULL;
CREATE INDEX CONCURRENTLY ix_player_playback_events_player_device_id ON public.player_playback_events (player_device_id, id);
CREATE INDEX CONCURRENTLY ix_player_playback_events_channel_id ON public.player_playback_events (channel_id, id);
CREATE INDEX CONCURRENTLY ix_player_playback_events_client_id ON public.player_playback_events (client_id, id);
CREATE INDEX CONCURRENTLY ix_player_playback_events_branch_id ON public.player_playback_events (branch_id, id);
CREATE INDEX CONCURRENTLY ix_player_playback_events_content_id ON public.player_playback_events (content_id, id) WHERE content_id IS NOT NULL;
CREATE INDEX CONCURRENTLY ix_player_playback_events_campaign_id ON public.player_playback_events (campaign_id, id) WHERE campaign_id IS NOT NULL;
CREATE INDEX CONCURRENTLY ix_player_playback_events_layout_id ON public.player_playback_events (layout_id, id) WHERE layout_id IS NOT NULL;
```

Los cuatro device/channel son necesarios para hot paths; los otros diez cubren
las igualdades realmente usadas por Client/Branch/media mutations. FK+id sirve
al filtro y orden estable de lock. No se añadieron composites client-first ni
INCLUDE sin prueba. Reutiliza B-tree válido/ready/live con FK como primera key y
predicado compatible, incluso con otro nombre o índice legacy 007; no duplica
ciegamente por nombre. Un conflicto/índice inválido con el nombre previsto detiene
el script. 007 declaraba cuatro índices que no aparecen en el catálogo productivo
aportado; no se asumió que estuvieran aplicados.

QA real: dos PK → 16 índices de telemetría; 14 nuevos, 7,798,784 bytes en la
fixture pequeña; segunda ejecución conserva los mismos 16 OIDs, no 28. Durante
la fase concurrente se confirmaron 45 INSERT, cero errores. No extrapolar esos
tiempos/tamaños al historial productivo.

Plan productivo diseñado, **no ejecutado ni aprobado para este candidato**:

1. Verificar DB/owner, catálogo antes, espacio de data/WAL/temp y backup/restauración
   por separado. Sin leer/imprimir secretos o tocar el dump histórico.
2. Construir uno por uno con CONCURRENTLY, maintenance_work_mem=64MB,
   max_parallel_maintenance_workers=0 y lock_timeout de sesión=2s. El build puede
   durar mucho: no bloquear DML de Players con una transacción normal larga.
3. Monitorizar IO/WAL/lag/latencia/progreso; con 2CPU/4GB/0swap no se presupone
   capacidad sobrante. Un fallo puede dejar INVALID: detener, inspeccionar y
   decidir manualmente; ningún DROP/repair automático.
4. Comprobar forma/elegibilidad/valid/ready, planes reales y data prechecks
   exhaustivos de las 14 relaciones. Los prechecks básicos previos no prueban
   todos los cruces históricos de telemetría.
5. **No avanzar al deploy** mientras siga el blocker NativeBurst8. La presencia
   de índices no equivale a calificación de throughput.

## Rollback y trabajo pendiente

No hay datos/columnas/tipos/defaults/FKs nuevos ni backfill. Los índices son
no-unique: retirar únicamente índices nuevos identificados en el catálogo antes/
después (si se autoriza, concurrent drop fuera de transacción) no cambia la
semántica de datos. Nunca retirar uno preexistente reutilizado o constraint-backed.
Retirarlos puede restaurar el costo de scan; no es una mitigación de disponibilidad.

R5 no se desplegó: producción permanece en su release actual. No se debe diseñar
un rollback que requiera volver a materializar millones de filas en el guard
Phase A bajo tráfico. Antes de cualquier release R5 debe existir una ruta
operacional calificada; este candidato bloqueado no aporta una aprobación de
rollback live ni de despliegue. No se tocaron 016/017.

Siguiente decisión de arquitectura, **sólo propuesta, sin implementación**:

- Evitar repetir la revisión/bloqueo del historial completo dentro del gate por
  request manteniendo la prueba exhaustiva. Estudiar invariantes durables en DB
  con validación inicial de todo el histórico y referencias/identidades históricas
  inmutables; simples FK separadas o un LIMIT no bastan.
- Si se revisa granularidad/modo de locks, demostrar seguridad para todos los
  writers, propuestas, media locks y dos pasadas. No cambiar a shared locks,
  omitir historia, ampliar caches ni aumentar timeout como parche sin ese diseño.
- Repetir NativeBurst8 y luego la carga/cadencia objetivo de 25 pantallas, con
  distribución máxima real, cold IO, PG16.14 y presupuesto conjunto 2CPU/4GB.
- Conservar sólo como recomendación futura `INITIAL_WORKER_CONCURRENCY=1` hasta
  calificación independiente de FFmpeg. No se optimizó el worker ni se inició H1.

## Archivos del delta R5

1. backend/app/services/tenant_telemetry.py (nuevo)
2. backend/app/services/tenant_mutations.py
3. backend/app/services/tenancy.py
4. backend/app/api/routes/channels.py
5. backend/app/api/routes/campaigns.py
6. backend/app/services/content_cleanup.py
7. backend/migrations/018_telemetry_guard_indexes.sql (nuevo)
8. backend/tests/test_telemetry_scale_safety.py (nuevo)
9. backend/tests/test_telemetry_cleanup_safety.py (nuevo)
10. backend/tests/test_tenant_relation_matrix.py
11. docs/SECURITY_R5_TELEMETRY_SCALE_REPORT.md (nuevo)

## Campos solicitados

```text
ROOT_CAUSE_SCALE=UNINDEXED_INCOMING_FK_LOOKUPS_PLUS_PER_HISTORY_ID_PYTHON_TWO_PASS_TRAVERSAL
HEARTBEATS_GUARD_ENTRYPOINTS=PLAYER_RUNTIME_HEARTBEAT_PLAYBACK_EVENT_ACTIVATION_AND_CHANNEL_DEVICE_CLIENT_BRANCH_CAMPAIGN_CONTENT_LAYOUT_MUTATIONS_DELETES
PLAYBACK_EVENTS_GUARD_ENTRYPOINTS=PLAYER_RUNTIME_HEARTBEAT_PLAYBACK_EVENT_ACTIVATION_AND_SAME_PARENT_MUTATIONS_DELETES
SECURITY_INVARIANT_HEARTBEATS=VALID_ID_REQUIRED_REFS_SAME_OWNER_BRANCH_CHANNEL_DEVICE_PLUS_OPTIONAL_PARENT_CONTEXT_SEMANTICS_CURRENT_PROPOSED
SECURITY_INVARIANT_PLAYBACK_EVENTS=SAME_INVARIANT_WITH_CONTENT_CAMPAIGN_LAYOUT_FIELDS
OLD_GUARD_STRATEGY=FETCH_HISTORY_IDS_AND_FULL_SNAPSHOTS_VALIDATE_EACH_ROW_IN_PYTHON_TWICE
NEW_GUARD_STRATEGY=LOCK_ALL_MATCHING_ROWS_SQL_COUNT_DISTINCT_PARENTS_FRESH_UNIVERSAL_PREDICATE_AND_SCOPED_DELETE_CERTIFICATES
FULL_HISTORY_STILL_VERIFIED=YES_RELATED_SCOPE_EXHAUSTIVE_NO_TENANT_PREFILTER
WINDOWING_USED=NO
PYTHON_HISTORY_MATERIALIZATION_BEFORE=10000_HISTORY_DICTS_PER_OPERATION_IN_PAIRED_5000_TEST_PLUS_DISCOVERY_IDS
PYTHON_HISTORY_MATERIALIZATION_AFTER=0_IN_MEASURED_POSTGRES_HOT_PATHS_PARENT_REFERENCES_EXPLICITLY_COUNTED
INDEXES_REQUIRED=14_FK_LEADING_INDEX_SHAPES_REUSE_ELIGIBLE_EXISTING
INDEX_DEFINITIONS=018_TELEMETRY_GUARD_INDEXES_SQL_AND_REPORT_ABOVE
PRODUCTION_INDEX_BUILD_STRATEGY=SEPARATE_SERIAL_CONCURRENT_PREDEPLOY_NO_TRANSACTION_NO_PRODUCTION_EXECUTION
BEFORE_QUERY_PLAN=PROD_USER_SUPPLIED_PARALLEL_SEQ_SCAN_AND_LOCAL_PK_ONLY_SEQ_SCAN
AFTER_QUERY_PLAN=INDEXED_BITMAP_PK_FOR_SELECTIVE_1M_DATASET_NO_FULL_500000_ROW_SCAN_PER_HOT_QUERY
BEFORE_QA_LATENCY=GUARD33.399S_HB38.481S_EVENT37.638S_RUNTIME38.022S_PAIRED5000
AFTER_QA_LATENCY=GUARD0.756S_HB1.202S_EVENT0.599S_RUNTIME2.722S_EXACT_FINAL_PAIRED5000
HOT_PATH_HEARTBEAT=SINGLE_REQUEST_IMPROVED_BURST_AVAILABILITY_FAIL_4_OF_8_HTTP409_55P03
HOT_PATH_PLAYBACK_EVENT=NATIVE_DIVERSE6.275S_NO_THROUGHPUT_CERTIFICATION
HOT_PATH_RUNTIME=NATIVE_DIVERSE7.076S_NO_THROUGHPUT_CERTIFICATION
TWO_PASS_PRESERVED=YES
ADVISORY_LOCK_PRESERVED=YES_EXCLUSIVE_TENANT_KEY_TIMEOUT10S
R4_PRESERVED=YES_53_PASS_AND_SOURCE_SCOPE_CONTROLS
CONCURRENCY_TESTS=INTEGRITY_PASS_NATURAL_EIGHT_DEVICE_AVAILABILITY_FAIL
DEEP_HISTORY_VIOLATION_TESTS=PASS_FIRST_MIDDLE_LAST_OLD_ALL_14_FKS_BOTH_MODELS
MUTATION_HARNESS=293_OF_293_PLUS_13_OF_13_VALID_DETECTED_ZERO_SURVIVORS_ERRORS_SKIPS
READ_ISOLATION=182_PASS
WRITE_ISOLATION=210_PASS
DELETE_ISOLATION=525_PASS
RUNTIME_ISOLATION=158_PASS
VIDEOWALL_REGRESSION=97_PASS
NEW_MIGRATION_REQUIRED=INDEX_ONLY_NONTRANSACTIONAL_PREDEPLOY_UNIT018_NOT_016_OR_017
NEW_FILES_CHANGED=11_TOTAL_6_MODIFIED_5_NEW
STAGING_EMPTY=YES
PRODUCTION_SCALE_QUALIFIED=NO
DEPLOY_STATUS=NO_GO_NATIVE_EIGHT_DEVICE_TENANT_GATE_TIMEOUT_REPRODUCED
COMMIT=NO
PUSH=NO
DEPLOY=NO
PRODUCTION_ACCESSED=NO
VPS_ACCESSED=NO
H1_STARTED=NO
```

## Evidencia final

Todo bajo `.tmp/SECURITY_R5` (ignorado por Git):

- `required_regression_v3/results/REQUIRED_STRICT_CERTIFICATION.json`:
  SHA256 `0e5175a6e055fe43286a53caa11240922b142a1c63547eb688231aafa364c562`.
- `FINAL_MUTATION_CERTIFICATION_306.json`:
  SHA256 `234436da8ea4fcedda15edba9727c779902ac8006a6f926deb2917a37dc3e084`.
- `focused_final45/results/FOCUSED_FINAL45.json` (45/45).
- `BEFORE_5000.json`, `AFTER_EXACT_FINAL_5000.json` y runner congelado idéntico.
- `AFTER_FINAL_SCALE_500000.json`, `UNIFORM_DATASET_SHAPE.json`.
- `AFTER_NATIVE_DIVERSE_62500.json`, `NATIVE_MEDIA_SEED_PROOF.json`.
- `burst_native/NATIVE_EIGHT_DEVICE_BURST.json` (blocker final).
- `FINAL_PERFORMANCE_CERTIFICATION.json`: SHA256
  `c2da18411c08b4c4fdffa6563227eb54ed96cc0690112344a0ce8c325e0d889b`;
  verdict BLOCKED, app80=live, jobs QA activos=0.
- `INDEX_QA_PROOF.json` y 14 definiciones/catálogo/OIDs.
- `FINAL_DELIVERY_PROOF.json`, hashes finales y patch de revisión generados
  después de este informe; no contienen envs privados ni artefactos de laboratorio.

Hashes de código clave:

```text
tenant_mutations.py=d2f9601e3f6cb4454d2e6d7a73a7c96d1266c248b26865e3b6efc2004e3fd926
tenant_telemetry.py=863df88161004174ddf50ea868e9dccc9ec2494cc6d0f67686fe5bfd2459d638
test_telemetry_scale_safety.py=9447a31a5a18f3acd9362ab5baa017c302dd5410f2af4d1d45ff30900eebd852
test_tenant_relation_matrix.py=673ca735b57077a94a5d5a34f4d99770e62350e31f0bc32d39b81464ba731608
```

**SECURITY-R5 BLOCKED — 4/8 heartbeats legítimos concurrentes reciben HTTP 409/55P03 por el tenant gate de 10s, incluso con índices y almacenamiento Linux nativo.**
