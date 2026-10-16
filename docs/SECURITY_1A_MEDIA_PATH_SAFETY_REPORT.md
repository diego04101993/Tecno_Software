# SECURITY-1A-R2 — Seguridad de archivos multimedia

Fecha: 2026-10-15. Implementación y validación locales, pendiente de reauditoría final. Este informe incorpora R2 sobre R1 y no afirma compatibilidad universal ni atomicidad DB/filesystem.

## Base y estado

```text
BASE_COMMIT=8fc01500d10f0d4e67cffac51725a56233fcd874
BRANCH=security/sec-1a-media-path-safety
FILES_CHANGED=14 (8 modificados existentes + 6 nuevos sin seguimiento)
```

Antes de R1 se verificaron la rama y HEAD indicados, exactamente los 11 archivos locales de SECURITY-1A, staging vacío y `git diff --check` sin errores. Se conservó esa implementación; no hubo reset, rebase, clean, staging, commit, fetch, push ni deploy. R1 agrega tres archivos al conjunto: el protocolo de referencias, sus pruebas PostgreSQL y las pruebas POSIX.

Antes de R2 se verificaron HEAD completo, rama, staging vacío, los mismos **14 archivos de R1** y ausencia de cambios ajenos. R2 modifica sólo cinco de esos archivos: `media_storage.py`, `content_cleanup.py`, `test_media_path_safety.py`, `test_media_storage_posix.py` y este informe. H2, H3, sus callers y las pruebas PostgreSQL permanecen idénticos a R1. No se agrega ningún archivo de producto adicional.

No se inició `app.main`, no se ejecutó startup/create_all/seed contra la configuración de aplicación, no se leyó ni modificó storage real y no se accedió a producción. Se llamó al CLI de cleanup **sólo dentro de un test**, inyectando una sesión SQLite en memoria y archivos ficticios. Ningún CLI se ejecutó con su configuración habitual.

## R2 — Bloqueo POSIX y cierre del borrado ante incertidumbre

La reauditoría reprodujo en Linux que R1 rechazaba el nombre histórico seguro `video:final.mp4`: GET/HEAD pasaban de 200 en la base a 404. Además, `_referenced_media_paths` omitía referencias que producían `MediaPathError`. El cascade de A podía recoger físicamente ese archivo y borrarlo, aunque sobreviviera un ContentItem de B que lo referenciaba. Era bloqueante porque dejaba una referencia viva sin archivo.

`media_storage._literal_relative_parts` (línea 27) ahora permite `:` de nombres POSIX seguros, pero sigue rechazando notación de unidad Windows en cualquier componente, UNC, backslashes, NUL/controles, absolutos y escape por symlink. Valida los componentes **antes** de que pathlib normalice `//`, `.` o `..`. En Windows mantiene rechazo de `:` (incluido ADS), nombres reservados y aliases de punto/espacio final. La validación de nuevos uploads conserva su propia prohibición explícita de `:` y las demás restricciones; no se modificó su algoritmo de UUID, suffix, temporal o promoción.

`content_cleanup._referenced_media_paths` (línea 122) ya no omite errores: emite `UNRESOLVED_SURVIVING_REFERENCE cause=<código controlado>` sin path, nombre ni cliente, y lanza `MediaPathError("UNRESOLVED_SURVIVING_REFERENCE")`. `_cleanup_files_locked` termina todo el barrido global antes del primer unlink; una referencia no resoluble preserva **todos** los candidatos del lote. `None` no representa un archivo. Una ruta faltante que todavía puede canonicalizarse con seguridad se analiza como ruta, usando `must_exist=False`; esto no omite filas ni autoriza equivalencias por parecido de nombre.

Para borrar deben cumplirse conjuntamente: candidato seguro bajo MEDIA_ROOT, archivo regular, contención tras resolver symlinks, lock correspondiente vigente, commit confirmado, barrido completo de todas las referencias supervivientes y ausencia de coincidencia canónica. No hay rmtree ni una implementación independiente por caller.

| Flujo | Resultado al quedar una referencia no resoluble | Regresión R2 |
|---|---|---|
| DELETE contenido | HTTP 409 controlado; archivo preservado | PASS |
| DELETE carpeta con cascade | HTTP 409 controlado; archivo preservado | PASS |
| `delete_client_cascade` | HTTPException 409; archivo preservado | PASS |
| Cleanup administrativo | `errors` registra el código; `files_deleted=0` | PASS |
| Cleanup CLI | Excepción controlada con ese código; no informa éxito | PASS |

La detección global ocurre **después del commit SQL**. Una respuesta 409 de esta fase no significa que las filas eliminadas se restauraron: el cliente/contenido puede haberse eliminado y el archivo queda conservado. R2 prioriza ese huérfano frente a romper referencias supervivientes; no ofrece rollback distribuido.

### Regresión Linux exacta

`PosixMediaIntegrationTests.test_colon_cross_client_cascade_preserves_surviving_reference_and_http` crea clientes A y B, archivo temporal `A/video:final.mp4` y **un ContentItem de cada cliente apuntando al mismo archivo**. Usa SQLite en memoria con foreign keys activas, el servicio real `delete_client_cascade` y el endpoint real `/media` por ASGI, sin arrancar `app.main`.

Resultado real: GET/HEAD 200 antes; A y su contenido eliminados; B y su contenido permanecen; bytes del archivo intactos; GET/HEAD 200 después. También pasa un caso Linux donde una referencia superviviente es un symlink externo: se informa el error y el candidato válido permanece. Una prueba adicional comprueba que una referencia inválida preserva un lote de dos candidatos y que el log no contiene los paths sensibles.

## H1 — Tres representaciones distintas

| Entrada | Implementación | Semántica |
|---|---|---|
| STORED_DB_PATH | `media_storage.py:80`, `resolve_stored_media_path` | Texto literal de DB; nunca `unquote`, nunca convierte `%20` en espacio ni `%2520` en `%20`. |
| HTTP_REQUEST_PATH | `media_storage.py:74`, `resolve_relative_media_path`; `media_streaming.resolve_media_path` | El servidor ASGI ya decodificó la URL. El helper no vuelve a decodificarla. |
| NEW_UPLOAD_FILENAME | `media_storage.py:115`, `normalize_upload_filename` | Metadata no confiable; validación estricta independiente, NFC, sin separadores, traversal, unidades, caracteres peligrosos, dispositivos Windows ni encodings que alteren el nombre al decodificar. |

Todas las resoluciones físicas comprueban `resolve()` y `relative_to(MEDIA_ROOT)`, archivo regular y, cuando corresponde, existencia. Rechazan traversal real, absolutos, unidades, backslashes, controles y escape por symlink. No se usa comparación de prefijos de strings. `None` sigue representando contenido sin medio local; cadena vacía no equivale a `None`.

Formatos históricos revisados:

- `/media/<cliente-o-carpeta>/<nombre>`: formato generado por uploads, preservado literalmente.
- `media/<...>`: creación declarativa aceptaba el string y `build_absolute_public_url` generaba la misma URL `/media/...`; se conserva esa compatibilidad demostrada. La limpieza antigua ignoraba este formato, no lo borraba físicamente. R1 permite resolverlo sin reescribir la fila.
- `<cliente>/<...>`: no es formato generado por uploads ni URL del endpoint `/media`; no se incorpora como alias del root.
- Rutas físicas absolutas: no son sustitutos de un path público; se rechazan. La limpieza antigua podía ignorarlas y eliminar sólo la fila; ahora falla cerrada.

En Linux se permiten nombres históricos `video:final.mp4`, `CON.mp4`, `video.mp4.`, `archivo%20literal.mp4`, `video%20final.mp4` y `video final.mp4`, si están contenidos y son archivos regulares. Se probaron en filesystem Linux real, junto con `%2520`, coexistiendo y devolviendo exactamente los bytes solicitados, sin fallback. Para uploads **nuevos**, colon, nombres reservados, punto final y escapes percent siguen rechazados. En Windows se falla cerrado para las representaciones incompatibles con ese sistema. No se promete compatibilidad con cualquier string histórico arbitrario.

Coexistencia probada:

| Nombre físico / texto final persistido | URL HTTP necesaria |
|---|---|
| `video final.mp4` | `/media/<cliente>/video%20final.mp4` |
| `video%20final.mp4` | `/media/<cliente>/video%2520final.mp4` |
| `video%2520final.mp4` | `/media/<cliente>/video%252520final.mp4` |

Para una URL se codifica cada `%` literal como `%25`. El stored path se mantiene literal, sin codificarlo ni decodificarlo al resolver DB. Los tres archivos coexistieron con bytes distintos y devolvieron el archivo correspondiente por GET/HEAD. Borrar la referencia literal `%20` no borró el archivo con espacio ni el `%2520`.

La versión local de Starlette TestClient aplica un `unquote` adicional a `httpx.URL.path`. Las pruebas percent usan `httpx.ASGITransport`, cuyo scope realiza una sola decodificación como el servidor ASGI. No se introdujo una compensación en producto para ese comportamiento del cliente de tests. Traversal directo se prueba con scope HTTP sin normalización previa del cliente; traversal codificado pasa por transporte ASGI. Ambos devuelven 404.

## H3 — Suffix y Content-Type

`write_media_upload` (`media_storage.py:141`) sigue generando UUID y conserva únicamente el suffix final seguro, en minúsculas. Regla: punto seguido de 1–16 caracteres ASCII alfanuméricos, guion o guion bajo. Se rechaza un suffix presente que sea inseguro o demasiado largo; un suffix seguro desconocido se conserva. Sólo si no hay suffix se usa el fallback MIME conocido o `.bin`.

Se retiró la whitelist que convertía WMV/M2V a `.bin`. Se probaron `.mp4`, `.mov`, `.webm`, `.wmv`, `.m2v`, `.jpg`, `.jpeg`, `.png`, `.webp` y una extensión desconocida segura. El nombre multipart con mayúsculas y varios puntos termina en UUID + suffix final normalizado.

El endpoint sigue usando su `mimetypes.guess_type` original, sin cambiar los headers ni el generador de respuesta. Las pruebas HTTP comparan el tipo del archivo publicado con el del nombre original en el mismo entorno. En esta PC los resultados reales fueron:

```text
.wmv -> video/wmv
.m2v -> video/m2v
```

En otro sistema el literal MIME puede variar por la tabla de `mimetypes`; preservar el suffix mantiene el mismo criterio que antes en ese sistema. No se afirma detección del codec ni validación de bytes. `runtime.contents[].mime_type` continúa viniendo de metadata; `file_path`, `download_url` y el endpoint no cambian de forma.

R2 repitió en Linux nueve suffixes con GET/HEAD 200, Range 206/416 y comparación con `mimetypes` del mismo sistema. WMV conserva `.wmv`; M2V conserva `.m2v`, nunca `.bin`. En el entorno Linux local M2V puede devolver `application/octet-stream` según su tabla MIME: no se añade una tabla ni se presenta esto como verificación de reproducción física.

## H2 — Coordinación de referencias y borrado

`backend/app/services/media_references.py` centraliza:

- `media_lock_key` (línea 27): SHA-256 de namespace fijo + path físico canónico, normalizado por OS; primeros 64 bits con signo para PostgreSQL. Una colisión sólo serializa archivos distintos, no autoriza un borrado. Las claves del lote se adquieren ordenadas.
- `media_reference_operation` (línea 34): abarca la mutación DB, commit, comprobación final de referencias y operación física. La sesión DB hace rollback de trabajo pendiente antes de liberar coordinación al salir, también ante excepciones.
- `lock_media_paths` (línea 89): `pg_advisory_xact_lock` en una **conexión dedicada NullPool** a la misma base. Su transacción permanece abierta mientras la sesión de datos hace commit. No se usa un lock transaccional de la sesión de datos que desaparecería antes de unlink, ni locks de sesión que puedan quedar retenidos en el pool.
- La conexión dedicada se cierra en `finally`, haciendo rollback de su transacción y liberando los locks. Se comprueba conectividad al reutilizar el lote y antes de unlink; no se reconecta/reintenta silenciosamente si se pierde coordinación. `lock_timeout` y `connect_timeout`: 10 segundos.
- `require_media_commit` (línea 83): exige un commit exterior confirmado dentro de la operación. Un flush o commit de savepoint no habilita unlink. Nuevas escrituras invalidan la confirmación. Hay un test que fuerza DELETE ya enviado a DB y verifica que todavía no puede borrarse el archivo.
- `save_content_reference` (línea 125): altas y actualización de `file_path` bloquean el mismo path; al actualizar bloquea viejo y nuevo, rechaza cambios concurrentes obsoletos y vuelve a verificar existencia **después** de esperar. Si el deleter ganó, no puede confirmarse una referencia al archivo que eliminó.

La sesión de datos debe usar PostgreSQL READ COMMITTED; un snapshot REPEATABLE READ se rechaza para estas mutaciones, para evitar revalidar con referencias antiguas. El snapshot de runtime no cambia y conserva sus pruebas independientes.

SQLite usa un `RLock` local sólo como sustituto para tests unitarios. **No representa prueba de coordinación multiproceso.** La prueba de esa propiedad usa PostgreSQL real, conexiones y PIDs distintos, y observa `pg_locks.granted=false` en la sesión bloqueada.

Los lotes se bloquean completos antes de mutar. Los helpers anidados pueden reutilizarlos pero no ampliarlos silenciosamente (`MEDIA_LOCK_SCOPE_CHANGED`). La limpieza de contenido vuelve a leer `file_path` bajo lock antes de eliminar, para no usar un objeto cargado con un destino obsoleto.

### Escritores y rutas auditadas

Se buscaron constructores `ContentItem`, asignaciones `file_path`, bulk deletes, cascades y operaciones físicas en `backend/app`.

| Flujo actual | Participación |
|---|---|
| `POST /contents/`, `contents.py:53` | `save_content_reference`; ya no acepta crear una referencia local inexistente/insegura. HTTP 409 de path controlado. Se conserva el schema. |
| `POST /contents/upload`, `contents.py:90` | Archivo completo con UUID; alta DB mediante el mismo `save_content_reference`. Audio usa este endpoint. |
| Actualización de `file_path` | No existe endpoint actual que la escriba; el helper central implementa y prueba el protocolo viejo+nuevo para quien lo use. El movimiento de carpeta no escribe `file_path`. |
| `data_sources.upload_dataset_import` | Crea `DatasetImport.storage_path`, no `ContentItem.file_path`; conserva el helper seguro de upload, parser y flujo existentes. |
| `seed.py` | Sólo crea contenidos TEXT/URL sin `file_path`; no hay referencia local que bloquear. No se ejecutó el seed. |
| Contenido, `contents.py:186` | Operación exterior → `delete_content_items` → commit → `cleanup_files` bajo los mismos locks. |
| Carpeta, `content_folders.py:152` | Misma operación alrededor del cascade, commit y cleanup; paths del lote preflighted. |
| Cliente, `client_cleanup.py:18` | Preflight de referencias y árboles físicos; locks del lote alrededor del cascade, commit y cleanup. Sin rmtree. |
| Administrativo, `cleanup_retention.py:338` | Bloquea candidatos antes de fases/savepoints; commit exterior antes del cleanup; no cambia Media Retention V1. |
| CLI `cleanup_unused_assets.py:52` | Mismo protocolo. Ahora imprime `deleted_files = cleanup_files(...)` (línea 92), no `len(file_paths)`. |
| CLI `run_cleanup.py` | Delega al cleanup administrativo; no tiene otro escritor ni borrado físico. |
| Branches/canales/videowalls | No crean/borran archivos ni referencias locales `ContentItem.file_path`. |

No se encontró un escritor normal de `ContentItem.file_path` fuera de estos flujos que quedara sin integrar. La consulta final de referencias no filtra por cliente: **SHARED_REFERENCE_SCOPE=GLOBAL**. Protege también referencias existentes de otro cliente y aliases físicos internos válidos; no corrige la autorización semántica de referencias cruzadas.

## Orden DB / filesystem y fallos

No existe transacción distribuida DB/filesystem. Se conserva el orden más simple: preflight → locks → mutación DB → commit confirmado → consulta global de referencias → revalidación → unlink → liberación. No se incorporan quarantine, rename de históricos ni restauraciones especulativas.

| Caso | Resultado probado |
|---|---|
| A. DB falla antes del commit en borrado de contenido | Rollback, fila/archivo conservados; unlink/rename no se llaman. SQLite y PostgreSQL. No generalizar a los savepoints del cleanup administrativo; ver riesgos diferidos. |
| B. Commit funciona, unlink falla | Fila eliminada; archivo completo queda huérfano; HTTP 409 `MEDIA_DELETE_FAILED`; lock liberado. SQLite y PostgreSQL. |
| C. Paso físico previo funciona, DB falla | Upload promovido completo queda huérfano, sin fila confirmada. El borrado no tiene paso físico provisional: se prueba que no hace unlink/rename antes de commit. |
| Fallo de promoción previo al commit | `os.link` falla cerrado; no fila ni fallback con overwrite; se retira sólo el temporal propio. |
| D. Archivo compartido | Sobrevive al borrar una referencia, incluso si la restante pertenece a otro cliente. |
| E. Nueva referencia concurrente | Las dos intercalaciones reales y una actualización de `file_path` se verifican en PostgreSQL. |
| F. Archivo inexistente | Se rechaza la operación; no se confirma una nueva referencia faltante. |
| G. Path inválido / directorio / escape symlink | Se aborta el lote antes de mutar durante preflight; lectura HTTP 404. |
| H. Referencia superviviente global no resoluble | Tras commit, se detiene la eliminación física de todo el lote con error controlado. Filas ya eliminadas no se restauran; archivos conservados. |

N2: un test invoca el CLI con dos referencias al mismo archivo ficticio: elimina un contenido antiguo, conserva el archivo y la otra referencia, e informa **0 archivos eliminados**.

## Hard links y N3

Upload conserva temporal exclusivo en el directorio de destino → escritura completa → flush/fsync → `os.link` hacia UUID final → retirada del temporal propio por identidad. Reintenta colisiones sin sobrescritura; no usa `os.replace` como fallback. Temporal y destino deben estar en el **mismo filesystem** y permitir hard links. Si no existe soporte/permisos, falla cerrado.

Verificación Docker Desktop local, contexto npipe `desktop-linux` verificado, sin red ni arranque de servicios: Python 3.14.7 de una imagen **ya cacheada**, entrypoint reemplazado por el test stdlib. Se montó exclusivamente una carpeta ficticia bajo `.tmp/security-1a/` sobre C:, equivalente al filesystem de los bind mounts del compose de desarrollo; nunca `backend/storage`. Se creó temporal → hard link en el mismo directorio → se comprobaron inode/dispositivo/bytes → se retiraron fixtures. También pasó `write_media_upload` real y no dejó `.upload-*`.

R2 ejecutó 10 métodos Linux en `/tmp` tmpfs y repitió el test de hard link en un bind mount de una carpeta temporal de C:. Los nombres POSIX se probaron sólo en tmpfs Linux, no sobre NTFS. Contenedor eliminado, fixtures temporales eliminadas y carpeta bind vacía verificados. Se copiaron sólo fuentes Python de backend/app y tests; no configuraciones, `.env` ni medios. Linux corrió con red deshabilitada y root filesystem de sólo lectura. El intérprete cacheado Python 3.14.7 y sus dependencias no son la imagen backend de producción; no se certifica esa imagen ni su filesystem.

**Preflight obligatorio antes de un futuro deploy en VPS:** comprobar en un directorio desechable del mismo filesystem destinado a medios, con el usuario efectivo del servicio, creación de temporal, hard link sin reemplazo, identidad inode/dispositivo, lectura tras retirar el temporal y limpieza propia. Si falla por permisos o soporte, detener la publicación; no agregar fallback con overwrite. Este preflight **no se ejecutó aquí**, porque producción y servidor no están autorizados en esta tarea.

## Validación final R2

- Suite completa: **119 métodos descubiertos; 101 PASS, 18 SKIP** en el runner Windows/SQLite. Los SKIP son 8 PostgreSQL y 10 Linux; se ejecutaron realmente en sus entornos correspondientes.
- PostgreSQL local: **8 PASS, 0 fallos, 0 errores, 0 SKIP**.
- Linux: **10 PASS, 0 fallos, 0 errores, 0 SKIP**, más 1 repetición del hard link en bind mount (no cuenta como método único nuevo).
- Total combinado de métodos únicos: **119 PASS**. R2 añade **15 tests** a los 104 de R1; seguridad acumulada: **68 métodos** (52 generales + 6 PostgreSQL + 10 Linux), más **51 existentes** (19 rotation + 10 LED + 22 Media Retention, incluidos sus dos snapshots PostgreSQL).

Runner general:

```text
python -B -X utf8 backend/tests/run_screen_rotation_suite.py
```

Sobrescribe DATABASE_URL con SQLite en memoria, MEDIA_ROOT temporal, AUTO_SEED=false, SECRET_KEY ficticia y URLs test antes de cualquier import. No importa `app.main`. Las fixtures SQLite verifican `PRAGMA database_list` sin archivos persistentes.

### PostgreSQL real

Ejecución final: **2026-10-15 20:26:50 UTC**, PostgreSQL **16.14**, base `tecno_control_retention_test`, identidad y **0 tablas de usuario** comprobadas antes de crear fixtures. Docker local `desktop-linux`; puerto publicado sólo en `127.0.0.1:50569`; datos en tmpfs de 256 MiB, **sin bind mounts ni volúmenes persistentes**, imagen `postgres:16` local sin pull. Motor global de la aplicación permaneció SQLite en memoria. `Base.metadata.create_all` se usó sólo para fixtures de la base temporal vacía; ninguna migración.

Contenedor exclusivo `tecno-sec1a-pg-32b21bd3b046`, ID `08456bdafa55750e61f9755d6a47cbc21c7953da07770c09bc8cdbc3653600a9`: detenido/eliminado al finalizar, eliminación verificada. Contenedor Linux `tecno-sec1a-r2-linux-8aa81d7c923f`: eliminado también.

| Test PostgreSQL | Qué confirmó | Resultado |
|---|---|---|
| `test_delete_wins_concurrent_create_cannot_commit_dangling_reference` | A hace commit y mantiene lock antes del check; B espera realmente en PG; A borra; B rechaza FILE_NOT_FOUND sin fila. | PASS |
| `test_create_wins_deleter_rechecks_global_references_and_keeps_file` | B confirma referencia de otro cliente bajo lock; A espera, revalida y conserva archivo. | PASS |
| `test_delete_wins_concurrent_file_path_update_is_rejected` | Actualización hacia destino eliminado espera y falla; conserva su path previo. | PASS |
| `test_db_failure_rolls_back_before_releasing_lock_without_unlink` | Fallo DB, fila/archivo presentes, sin unlink y lock disponible al terminar. | PASS |
| `test_unlink_failure_after_commit_releases_lock_and_keeps_orphan` | Commit confirmado, fallo físico, huérfano y lock liberado. | PASS |
| `test_stale_snapshot_is_rejected_before_deletion` | READ COMMITTED requerido; rechaza snapshot viejo sin borrar. | PASS |
| `test_runtime_wrapper_opens_complete_postgres_snapshot` | Runtime v3, Media Retention completo con snapshot real. | PASS |
| `test_publication_after_snapshot_start_is_not_mixed` | Publicación B no se mezcla con A en snapshot REPEATABLE READ de runtime. | PASS |

Las tres carreras usan sesiones independientes y PIDs PostgreSQL distintos; observan locks concedidos y bloqueados en `pg_locks`. No se presentan sleeps ni mocks de locking como evidencia. Se verificó disponibilidad posterior de la clave con `pg_try_advisory_xact_lock`.

Cobertura HTTP y funcional adicional: GET/HEAD 200/206/416, Range normal/suffix/open-ended, Accept-Ranges, Content-Range, ETag, Last-Modified, Content-Disposition inline, Content-Type; percent literales, traversal, symlink, contenido compartido, preflight de lotes, tenants, CSV, activación/runtime/heartbeat/playback-events, rotation, LED, selección de campaña, playlists y Media Retention.

`compileall` de app/tests: PASS, cache temporal eliminado. `git diff --check`: PASS, incluidos los nuevos archivos mediante diff no-index. Comparación AST de funciones de streaming/headers/Range: sin cambios. Git confirmó sin cambios en runtime/Player, rutas media, modelos, schemas, migraciones y frontend.

Evidencia local **ignorada por Git**, sin credenciales persistidas:

- `.tmp/security-1a/backend-tests-r2.log`
- `.tmp/security-1a/postgres-tests-r2.log`, `postgres-results-r2.json`, `validate_postgres_r2.py`
- `.tmp/security-1a/linux-tests-r2.log`, `linux-results-r2.json`, `validate_linux_r2.py`
- `.tmp/security-1a/SECURITY_1A_MEDIA_PATH_SAFETY.patch`: diff acumulado respecto a la base, incluidos los nuevos archivos; verificación inversa sin aplicarlo ni hacer staging.

## Compatibilidad y límites restantes

1. Contrato Player y URL conservados: mismo shape de `contents[]`, `file_path` público, builder de `download_url`, `/media` público, headers y runtime v3/Retention v1. La creación declarativa ahora valida existencia/seguridad antes de confirmar, cambio administrativo necesario para H2. No cambia el schema Player. No hubo prueba física Windows/Android9/Android13.
2. Coordinación **cooperativa**: todos los workers que compartan medios deben usar este protocolo, la misma base y la misma ruta física canónica. SQL manual, versiones antiguas concurrentes, escritores externos o mount paths distintos no están protegidos por una constraint de DB. Una publicación futura debe evitar mezclar escritores viejos/nuevos. No se hizo deploy.
3. DB/filesystem no ofrecen atomicidad común. Pueden quedar huérfanos ante fallo/resultado ambiguo de commit de upload, fallo físico tras commit, caída del proceso o cambios concurrentes de colecciones. No se incorporó saneamiento. Se priorizó no borrar un archivo con referencia activa confirmada bajo el protocolo.
4. Los locks suponen conexiones PostgreSQL disponibles durante la sección crítica y storage administrado por el servicio. Se falla al detectar pérdida de coordinación; no se certifica atomicidad ante caída de DB en el instante de una operación filesystem, un actor local que sustituya symlinks simultáneamente o manipulación externa de archivos. No se prueba tolerancia distribuida a fallos arbitrarios.
5. Una conexión adicional NullPool por operación local de medios y locks mantenidos durante el disco añaden coste. Todos los workers deben tener conectividad directa compatible con advisory transaction locks; no se validó el límite de conexiones ni infraestructura real. Contención prolongada falla a los 10 s. El barrido global de referencias sigue siendo O(n), sin índice/migración nuevos.
6. Paths históricos inválidos o faltantes pueden bloquear preflight de borrado; una referencia superviviente no canonicalizable bloquea globalmente la fase física, aunque pertenezca a otro cliente. No se reparan ni reinterpretan automáticamente. Derivados por nombre y directorios vacíos se conservan. Se prefiere conservar huérfanos y reportar error.
7. No hay análisis de codec, antivirus, cuotas, autenticación nueva de media, reparación de referencias entre clientes ni cambios de autorización semántica del path. La lectura legacy de tamaño en runtime y los filtros de Media Retention sobre percent permanecen intactos. No se promete compatibilidad universal de cada subsistema con nombres arbitrarios.
8. Hard links probados sólo localmente; preflight en VPS obligatorio antes de publicar. Sin soporte, error sin overwrite. No se probó reproducción física ni se construyó frontend, que no cambia.
9. **Diferido: transacciones SQLite en `cleanup_retention`.** La reauditoría reprodujo que la combinación del driver SQLite/savepoints puede confirmar cambios antes de un fallo simulado del commit exterior. El archivo quedó conservado. No se corrige en R2 y no se presenta SQLite como evidencia de atomicidad de ese flujo.
10. **Diferido: resumen PostgreSQL del cleanup administrativo.** Tras un fallo genérico de commit pueden informarse contadores de operaciones revertidas. El rollback conserva filas y archivos en el caso reauditado. R2 no corrige esos contadores.
11. **Diferido: lotes grandes de advisory locks.** Se bloquean todos los candidatos preflighted, incluidos algunos todavía referenciados, sin límite nuevo de lote. El timeout de 10 segundos es por espera, no un plazo máximo total; quedan costes de conexiones, memoria y contención por dimensionar.

## Archivos del diff acumulado

1. `backend/app/services/media_storage.py` — nuevo: tres contextos de path, suffix seguro, storage y preflight.
2. `backend/app/services/media_references.py` — nuevo en R1: coordinación y persistencia de referencias.
3. `backend/app/services/media_streaming.py` — resolución segura; streaming/headers intactos.
4. `backend/app/services/content_cleanup.py` — contención, locks, referencias globales y unlink tras commit.
5. `backend/app/services/client_cleanup.py` — lote de cascade bajo protocolo, sin rmtree.
6. `backend/app/services/cleanup_retention.py` — lote administrativo, commit y cleanup coordinados.
7. `backend/app/api/routes/contents.py` — uploads/creación/borrado bajo protocolo.
8. `backend/app/api/routes/content_folders.py` — cascade coordinado.
9. `backend/app/api/routes/data_sources.py` — escritor dataset protegido de SECURITY-1A, preservado en R1.
10. `backend/app/scripts/cleanup_unused_assets.py` — coordinación y conteo real.
11. `backend/tests/test_media_path_safety.py` — 52 tests unitarios/HTTP.
12. `backend/tests/test_media_reference_postgres.py` — nuevo en R1: 6 tests PostgreSQL de referencias.
13. `backend/tests/test_media_storage_posix.py` — nuevo en R1, ampliado en R2: 10 tests Linux, incluidos DB temporal y endpoint real.
14. `docs/SECURITY_1A_MEDIA_PATH_SAFETY_REPORT.md` — este informe.

```text
POSIX_COLON_HTTP=PASS
POSIX_COLON_STORED_PATH=PASS
POSIX_COLON_CROSS_CLIENT_CASCADE=PASS
UNRESOLVED_REFERENCE_FAIL_CLOSED=PASS
SHARED_REFERENCE_PROTECTION=PASS
H1_HISTORICAL_PATHS=PASS
H2_CONCURRENT_REFERENCES=PASS
H3_MIME_COMPATIBILITY=PASS
STORED_PATH_NO_URL_DECODE=PASS
HTTP_PATH_NO_DOUBLE_DECODE=PASS
NEW_UPLOAD_STRICT=PASS
SHARED_REFERENCE_LOCK_POSTGRES=PASS
SHARED_REFERENCE_SCOPE=GLOBAL
WMV_CONTENT_TYPE=PASS
M2V_CONTENT_TYPE=PASS
CLEANUP_REAL_COUNT=PASS
HARDLINK_LOCAL_CHECK=PASS
PLAYER_CONTRACT_CHANGED=NO
MEDIA_URL_CONTRACT_CHANGED=NO
RUNTIME_CHANGED=NO
MEDIA_RETENTION_CHANGED=NO
MIGRATIONS_CHANGED=NO
FRONTEND_CHANGED=NO
NEW_TESTS=15 R2 PASS; 68 SECURITY-1A acumulados
TOTAL_TESTS=119 PASS combinados únicos
POSTGRES_TESTS=8 PASS
LINUX_TESTS=10 PASS; 1 repetición adicional de hard link PASS
COMPILEALL=PASS
DIFF_CHECK=PASS
DATA_DELETED=NO (datos reales; sólo fixtures temporales)
REAL_MEDIA_MODIFIED=NO
PRODUCTION_ACCESSED=NO
STAGING=VACÍO
COMMIT=NO
PUSH=NO
DEPLOY=NO
```

SECURITY-1A-R2 IMPLEMENTADA — LISTA PARA REAUDITORÍA FINAL
