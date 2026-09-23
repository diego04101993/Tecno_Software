# Screen Rotation MVP — S1

Fecha: 2026-09-23. Implementación local, detenida para revisión.

## Base y alcance

- Repositorio: `C:\tecnomania\tecno_software` (la barra antes del guion bajo se aclaró como un error de formato).
- Base y HEAD: `6af2313da493c647c49565de70da80a657f632ed`.
- El árbol de trabajo estaba limpio antes de crear `feat/screen-rotation-mvp`.
- No se incorporaron cambios preexistentes. No hay commit, push, deploy ni acceso a producción.
- No se ejecutaron migraciones ni se modificaron los modelos o la definición del esquema del producto. Las tablas de fixture se crearon sólo en bases aisladas de prueba. No se tocaron repositorios de Players.

El cambio permite configurar el giro del contenido para canales `normal/single` y evita que una edición ajena al giro lo restablezca. Reutiliza `DeviceOutput.rotation` y `outputs[].rotation`; `runtime_version` sigue siendo 3.

## Semántica exacta

`rotation` es el giro horario del contenido respecto de la superficie entregada por el sistema operativo del Player. Es independiente de `Channel.orientation`, de `DeviceOutput.orientation`, de la resolución y del canvas. No representa un movimiento físico del monitor ni acredita soporte en Players antiguos.

| Entrada administrativa | Resultado |
| --- | --- |
| Entero `0`, `90`, `180` o `270` | Se escribe el ángulo en la salida primaria del canal normal/single. `0` explícito sí actualiza el valor. |
| Campo omitido al crear | Se conserva el comportamiento por defecto: las salidas nuevas nacen con giro `0`. |
| Campo omitido al editar | Se conserva el giro existente, en todos los modos. |
| `null` | HTTP 422; no significa reset ni omisión. |
| `true`, `false`, strings, floats, listas, objetos u otros ángulos | HTTP 422. No se convierten/coercionan a ángulos. |
| Giro explícito fuera de `normal/single` | HTTP 422, incluso si el valor es `0`. |
| Canal histórico sin ninguna salida y giro explícito | Se crea su salida primaria canónica y se aplica el valor solicitado. |
| Existen salidas, pero falta la identidad primaria `display_order=0` | HTTP 409; no se aplica el giro a otra salida por su posición en la lista. |

Los errores de escritura no confirman cambios parciales. Se mantienen las comprobaciones existentes de rol, cliente y sucursal. En edición, el alcance se valida contra el modo/dispositivo solicitado o, si se omite, contra el persistido.

Los endpoints administrativos afectados son `POST /api/channels/` y `PATCH /api/channels/{channel_id}`. La lectura continúa en `ChannelRead.device_outputs[].rotation`, sin añadir un segundo valor persistido al canal.

Ejemplos de edición:

```json
{"rotation": 270}
```

```json
{"name": "Pantalla recepción", "notes": "Ubicación actualizada"}
```

El segundo body omite `rotation`; conserva el ángulo, las dimensiones y la configuración de las salidas existentes.

## Persistencia e identidad

La identidad de sincronización que ya utiliza el proyecto es `(root_channel_id, display_order)`. No se utiliza el índice accidental del array ni se inventa una identidad por nombre. Dentro de un canal, el builder conserva un mapa de rotaciones por `display_order`; sólo aplica `0` cuando esa salida es nueva. La sincronización reutiliza los registros e IDs existentes, incluyendo la preservación previa de `display_uid` y del canal asociado en modo independent.

La edición administrativa evita normalizar y regenerar salidas si los campos de configuración del dispositivo no cambiaron realmente. Por eso editar nombre/notas, enviar campos geométricos idénticos o modificar sólo el giro no altera geometría, recortes, habilitación, nombres de salida o grupos de sincronización históricos.

Una modificación explícita de topología/geometría sigue utilizando la normalización existente; los giros de las identidades conservadas sobreviven. Reducir deliberadamente el número de salidas sigue eliminando las salidas sobrantes como antes; no se promete recuperar configuraciones de filas eliminadas si luego se aumenta el número.

No hay conversión automática de `vertical` a 90°. Un canal histórico vertical con giro 0 sigue vertical con giro 0. Escribir un ángulo tampoco intercambia anchos/altos ni transforma la orientación lógica.

## Interfaz

Se añade el mismo control reutilizable, «Giro del contenido», con opciones `0°`, `90°`, `180°`, `270°`, en:

- Creación desde el dashboard (`ScreenCreateDrawer`), sólo para single/normal.
- Creación desde la página de canales (`ChannelsPage`), sólo para normal.
- Edición desde `ScreenEditDrawer`, para un canal normal/single existente mientras siga seleccionado single.

El texto de ayuda define la referencia horaria del giro y la necesidad de un Player compatible. No se reemplaza el selector de orientación lógica ni el de resolución.

Al editar se busca el valor persistido de `display_order=0`, aunque el array llegue en otro orden. Si no existe la salida primaria, se muestra «Sin salida primaria configurada», sin presentar un cero inventado como valor persistido.

El formulario sólo envía `rotation` si el usuario utiliza el selector. Guarda nombre/notas sin reenviar giro, geometría, modo o output mapping que el usuario no editó. Se expone el campo de notas que ya tenía estado/persistencia en ese formulario. Los controles geométricos conservan los valores históricos al abrir el editor; la regeneración del canvas queda vinculada a una edición geométrica explícita.

No se añaden controles de giro para expanded, videowall, audio o touch. Sus ángulos existentes quedan preservados al hacer ediciones de metadatos.

## Archivos del cambio

| Archivo | Finalidad |
| --- | --- |
| `backend/app/schemas/domain.py` | Entrada opcional compartida, enum y validación estricta; null/booleanos rechazados. |
| `backend/app/api/routes/channels.py` | Alcance normal/single, aplicación a la salida primaria y preservación ante ediciones ajenas. |
| `backend/app/services/device_outputs.py` | Conservar rotación mediante la identidad existente; cero sólo para salida nueva. |
| `backend/tests/test_screen_rotation.py` | 19 pruebas de API, persistencia, alcance y regresión. |
| `backend/tests/run_screen_rotation_suite.py` | Ejecución segura en SQLite en memoria, sin startup del backend. |
| `frontend/src/components/ContentRotationField.tsx` | Selector compartido y semántica visible. |
| `frontend/src/components/dashboard/ScreenCreateDrawer.tsx` | Giro en creación normal/single. |
| `frontend/src/components/dashboard/ScreenEditDrawer.tsx` | Cargar giro real, enviar sólo cambios explícitos y conservar ajustes históricos. |
| `frontend/src/pages/ChannelsPage.tsx` | Giro en la otra interfaz de creación. |
| `frontend/tests/screen_rotation_form.test.mjs` | 6 pruebas del componente real, con requests interceptados en memoria. |
| `docs/SCREEN_ROTATION_MVP_REPORT.md` | Este informe. |

## Pruebas ejecutadas

### Backend: ejecución inicial, 41 descubiertas, 39 aprobadas, 2 omitidas

Se ejecutó la suite nueva junto con la suite existente de Media Retention: 19 pruebas S1 + 20 existentes aprobadas. Las dos opcionales de PostgreSQL se omiten intencionalmente en este runner; posteriormente se ejecutaron por separado y aprobaron, como se documenta a continuación.

El runner sobreescribe antes de importar la aplicación: `DATABASE_URL=sqlite://`, `AUTO_SEED=false`, `TEST_DATABASE_URL` vacío, una URL pública ficticia y un directorio temporal de media. Verifica el motor en memoria. Cada fixture de S1 verifica también `PRAGMA database_list` antes de crear sus tablas y activa claves foráneas. Se utiliza FastAPI TestClient en proceso, con una aplicación de prueba sin `app.main`, lifespan, seeds ni startup. Sólo se crean datos/tablas de fixture en memoria, no migraciones.

Cobertura ejecutada:

- Cuatro ángulos en creación y edición, incluido reset explícito a 0.
- Rechazo de null, booleanos, strings, floats y valores fuera del enum, sin guardar cambios parciales.
- Creación omitida con defaults existentes.
- Edición omitida, renombrado y notas sin pérdida del giro ni de otras propiedades de salida.
- Campos geométricos reenviados sin cambios: no regeneran salidas.
- Canal histórico vertical en 0: no reinterpretado.
- Giro explícito: no altera resolución, canvas histórico, orientación o mapping.
- Identidad por display_order con array invertido; IDs/UIDs y giros conservados; salida nueva en 0.
- Mirror, extended, independent, videowall, audio y touch: preservación al editar metadatos.
- Cambio geométrico explícito: conserva ID y rotación de la salida existente.
- Rechazo de escritura explícita en modos fuera del MVP y validación de transiciones de modo.
- Canal sin salidas y caso de identidad primaria ausente.
- Aislamiento por cliente/sucursal y rechazo del rol sin permisos de escritura. La identidad administrativa se inyecta en los tests; no constituyen pruebas nuevas del login/JWT.
- Runtime vía Bearer Player de fixture real: devuelve los cuatro valores después de renombrar; sigue en versión 3.
- Runtime antes/después con reloj fijo: sólo cambia `outputs[0].rotation`; selección por horario, playlist mixta imagen/video, campañas futuras/fallback y todo Media Retention V1 permanecen iguales, incluida `revision`.

La prueba existente de fallo inyectado de retención imprime un traceback esperado y termina aprobada.

### Validación posterior: PostgreSQL, 2 ejecutadas y 2 aprobadas

Ejecución real el 2026-09-23 a las 18:36:57 UTC (12:36:57, America/Mexico_City). Se ejecutaron únicamente los dos métodos existentes de `backend/tests/test_media_retention.py`, clase `PostgreSQLSnapshotTests`, sin modificar su código.

| Prueba | Cobertura real | Resultado |
| --- | --- | --- |
| `test_runtime_wrapper_opens_complete_postgres_snapshot` | Crea un canal de fixture y llama a `build_player_runtime_from_snapshot` contra PostgreSQL. Comprueba `runtime_version=3`, `media_retention.complete=true` y `assets=[]` para ese canal sin contenido. | PASS |
| `test_publication_after_snapshot_start_is_not_mixed` | Establece un snapshot `REPEATABLE READ` con campaña A asignada; otra sesión confirma la asignación B después. El runtime leído desde el snapshot mantiene A en selección y en `protected_campaign_ids`, con retención completa, sin mezclar la publicación B. | PASS |

Salida real de unittest, código de salida 0:

```text
test_runtime_wrapper_opens_complete_postgres_snapshot ... ok
test_publication_after_snapshot_start_is_not_mixed ... ok

Ran 2 tests in 0.441s

OK
```

Resultado: **2 ejecutadas, 0 fallos, 0 errores, 0 omitidas**. Sumando las dos ejecuciones del backend: **41 pruebas distintas aprobadas** (39 en SQLite y estas 2 en PostgreSQL). Las 6 pruebas de formulario y el build conservan sus resultados previos; no se volvieron a ejecutar en esta continuación dedicada a PostgreSQL.

Aislamiento verificado antes de crear las tablas de fixture:

- Docker Desktop local, contexto explícito `desktop-linux`, endpoint local `npipe:////./pipe/dockerDesktopLinuxEngine`. Se inició Docker Desktop porque el motor estaba apagado; no hubo bloqueo final del entorno.
- Contenedor exclusivo `tecno-s1-pg-a323876f6e83`, imagen ya disponible `postgres:16`, PostgreSQL **16.14**, ID de imagen `sha256:95206741a5b214807675e14165369d05b93a9cf692223b616d07cca227e74b0b`. No se descargó otra imagen ni se inició el compose de la aplicación.
- Único puerto publicado: **127.0.0.1:56988**. Se verificaron nombre, ID, etiqueta del contenedor, dirección de escucha y almacenamiento antes de conectar.
- Base **`tecno_control_retention_test`**, usuario de fixture `tecno_s1_test`; identidad confirmada con `current_database()` y `current_user()`. Consulta previa del catálogo: **0 tablas de usuario**.
- Datos PostgreSQL exclusivamente en `tmpfs` de 256 MiB, sin volúmenes ni carpetas del equipo montadas. Al terminar se detuvo únicamente ese contenedor, con eliminación automática; se verificó que ya no existía y se descartaron sus datos.
- El proceso Python fijó **antes de importar la aplicación** `DATABASE_URL=sqlite://`, `AUTO_SEED=false`, `TEST_DATABASE_URL` apuntando únicamente a la base local anterior, `PUBLIC_BASE_URL=http://testserver`, una clave ficticia y media temporal. Se verificó que el motor global de la aplicación seguía siendo SQLite en memoria.
- No se importó `app.main`, no se inició backend ni lifespan, no hubo seeds, migraciones o llamadas a producción. El `Base.metadata.create_all` existente en `setUpClass` creó exclusivamente el esquema del fixture vacío y desechable.

La ejecución se realizó con el entorno Python de pruebas ya preparado y el script temporal `work/validate_s1_postgres.py`, fuera del repositorio. Selección exacta cargada por `unittest.defaultTestLoader.loadTestsFromNames`:

```text
test_media_retention.PostgreSQLSnapshotTests.test_runtime_wrapper_opens_complete_postgres_snapshot
test_media_retention.PostgreSQLSnapshotTests.test_publication_after_snapshot_start_is_not_mixed
```

Estas pruebas validan los caminos PostgreSQL de snapshot/retención descritos; no sustituyen las pruebas S1 de ángulos y formularios, ni prueban reproducción física, todo el DDL de migraciones o una actualización de bases históricas.

### Evidencia de renombrado

`test_omitted_rotation_and_name_notes_edits_preserve_all_output_settings` crea giro 270 y configura nombre/UID de salida, estado deshabilitado, posición, recorte, sync_group y canvas históricos. Después de PATCH de nombre/notas, relee de la base aislada y compara todas las columnas de las salidas salvo timestamps: son idénticas.

`test_authenticated_player_runtime_delivers_persisted_angles_after_rename` repite para 0/90/180/270: escribir giro, PATCH sólo nombre y GET autenticado del runtime. El nombre nuevo y el ángulo anterior aparecen juntos en la respuesta.

### Formulario: 6 aprobadas

Se monta el componente React real con React 18.3.1 / react-test-renderer 18.3.1, instalados en un directorio temporal de herramientas, fuera del proyecto frontend. No se modificaron package.json ni el lockfile. Todos los fetch son interceptados; no hay servidor backend ni acceso de red durante estas pruebas.

Se comprueba carga por identidad y no por orden del array, las cuatro opciones numéricas, omisión del giro al editar nombre/notas, preservación de dimensiones/orientación, vertical histórico en 0, ausencia de salida primaria y exclusión de los modos fuera del MVP.

La prueba de nombre/notas inspecciona el body HTTP generado: no contiene `rotation`, `orientation`, dimensiones, `mode`, `device_mode`, `screen_count` ni `output_mapping_json`; el selector sigue mostrando 270°.

## Build y dependencias

- `pnpm install --frozen-lockfile`: satisfactorio, utilizando el `frontend/pnpm-lock.yaml` existente.
- `pnpm run build`: satisfactorio (`tsc -b && vite build`), 1,694 módulos transformados.
- Node 20.20.0, pnpm 10.17.1, TypeScript 5.9.3 y Vite 5.4.21.
- Advertencia no bloqueante: bundle principal mayor de 500 kB. No se hizo una optimización ajena a S1.
- Backend de pruebas: Python 3.11, requisitos existentes, HTTPX 0.28.1, tzdata; Pydantic resuelto 2.13.5.
- Se resolvió un fallo inicial de certificados de las instalaciones usando certificados públicos de confianza de Windows, sin desactivar validación TLS ni modificar configuración del repositorio.
- Los archivos de compilación versionados regenerados por TypeScript fueron restaurados a la base; no forman parte del diff.

Reproducción desde la raíz del repositorio, usando un entorno Python de pruebas con los requisitos del backend y `httpx==0.28.1`, `tzdata`:

```powershell
python -B backend/tests/run_screen_rotation_suite.py
```

Pruebas del formulario: preparar fuera del frontend un directorio temporal con `react@18.3.1` y `react-test-renderer@18.3.1`, sin tocar su lockfile, y señalarlo mediante `SCREEN_ROTATION_TEST_DEPS`:

```powershell
$env:SCREEN_ROTATION_TEST_DEPS = '<directorio-absoluto-de-dependencias-de-prueba>'
node --test frontend/tests/screen_rotation_form.test.mjs
```

Build desde el directorio frontend:

```powershell
pnpm install --frozen-lockfile
pnpm run build
```

## Pendientes y límites de compatibilidad

- Las dos pruebas PostgreSQL de snapshot/MVCC ya aprobaron en una base local nueva y desechable. No se verificó el estado de migraciones de ninguna base instalada ni se ensayaron migraciones; su ejecución no certifica una actualización de datos históricos.
- No se hizo prueba visual manual en navegador ni prueba sobre hardware real; las pruebas de interfaz ejercitan el componente y sus requests en memoria.
- Windows, Samsung Tizen y Android no fueron modificados ni probados. Un Player antiguo puede ignorar `outputs[].rotation`; mantener el campo y runtime v3 no demuestra su aplicación.
- No se valida aquí transformación de video por hardware, geometría final, interacción con orientación del sistema operativo ni ausencia de doble giro.
- S1 no demuestra caché persistente, arranque offline, cambio de campaña sin red o ejecución offline de horarios.

## Seguimientos separados: fuera del diff funcional

| Hallazgo previo | Estado en S1 |
| --- | --- |
| Upload sobrescribe por nombre | Registrado; no corregido. |
| Referencias corruptas entre clientes en el runtime anterior | Registrado; no corregido. |
| Publicación acumulativa frente a reemplazo | Registrado; políticas sin cambios. |
| Horarios offline | Registrado; no implementados. |

El builder del runtime, Media Retention V1, scheduler, playlists, asignaciones, output mapping, modelos, migraciones, package.json y pnpm-lock.yaml no tienen cambios. El formulario evita reenviar mapping intacto; su algoritmo y contrato permanecen iguales.

## Entrega de esta validación

Se actualizó únicamente este informe dentro del repositorio. Se verificó por SHA-256 que los otros 10 archivos del cambio S1 mantienen exactamente su contenido previo a esta continuación. El patch `SCREEN_ROTATION_MVP_S1.patch` contiene el cambio S1 completo contra HEAD, incluidos los archivos nuevos y este informe actualizado; no incluye herramientas temporales ni dependencias de prueba. Se comprobó su correspondencia con el árbol de trabajo mediante `git apply --reverse --check` sin aplicar cambios.

Estado de entrega: cambios locales sin stage ni commit, detenidos para revisión del alcance S1. HEAD permanece en la base auditada y la rama en `feat/screen-rotation-mvp`. No se realizó push, despliegue, migración ni operación de producción.
