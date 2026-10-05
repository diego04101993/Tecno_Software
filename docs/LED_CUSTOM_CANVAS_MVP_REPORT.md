# LED Custom Canvas — S1

Fecha: 2026-10-05. Validación local completada para publicación en GitHub, sin deploy.

Repositorio: `C:\tecnomania\tecno_software`.
Base: `main`, HEAD `28a94253dea79cac729e5ec75340e1cf1ec49d30`, working tree limpio al comenzar.
Rama de desarrollo: `feat/led-custom-canvas-mvp`. Antes del commit, `HEAD` y `origin/main` se verificaron en esa base mediante fetch. La publicación autorizada incluye un commit S1, integración a `main` sin reescribir historia y push a `origin`; no incluye acceso al servidor ni deploy.

## Resultado

Alta y edición incorporan **Tipo de salida: Pantalla normal / LED personalizada**.
El preset LED se construye completo mediante `buildLedCustomMapping`, sin reutilizar slices de un mapping anterior.
Permite ancho/alto LED y posición X/Y opcional dentro del HDMI. Muestra por separado la salida HDMI y el canvas LED.

La resolución del canal sigue representando el HDMI. Para el ejemplo, `Channel.resolution_*`, `Channel.canvas_*` y las dimensiones/source de `DeviceOutput` permanecen en 1920×1080. Las dimensiones 516×860 viven exclusivamente en `output_mapping_json` y su representación de runtime.
Cambiar únicamente el preset no modifica geometría, orientación ni rotación del canal/salida. Cambiar expresamente la resolución HDMI sigue el flujo existente de geometría del canal.

No hay columnas, migraciones, contratos ni versiones nuevos. `runtime_version=3` y Media Retention V1 permanecen iguales. No se modificaron Players, selección/asignación de campañas, playlists, scheduler ni el normalizador del runtime.

## Mapping exacto del ejemplo

Para HDMI 1920×1080, LED 516×860 y posición (0,0), el frontend envía este objeto completo. Las pruebas comparan el mismo fixture con la respuesta administrativa, la persistencia y el runtime autenticado:

```json
{
  "enabled": true,
  "profile": "custom",
  "mode": "custom",
  "mapping_mode": "custom",
  "slice_count": 1,
  "slice_direction": "vertical_stack",
  "output_width": 1920,
  "output_height": 1080,
  "physical_width": 516,
  "physical_height": 860,
  "source_canvas_width": 516,
  "source_canvas_height": 860,
  "canvas_width": 516,
  "canvas_height": 860,
  "viewport_x": 0,
  "viewport_y": 0,
  "viewport_width": 516,
  "viewport_height": 860,
  "scale_x": 1,
  "scale_y": 1,
  "slices": [{
    "slice_index": 1,
    "source_x": 0,
    "source_y": 0,
    "source_width": 516,
    "source_height": 860,
    "output_x": 0,
    "output_y": 0,
    "output_width": 516,
    "output_height": 860,
    "scale_x": 1,
    "scale_y": 1
  }]
}
```

Los aliases adicionales son los del contrato existente. La dirección del único slice sigue el criterio existente según la proporción del canvas; no implica fraccionar este preset.
El runtime conserva exactamente este mapping tanto en `runtime.output_mapping` como en `runtime.channel.output_mapping_json`.

## Reconstrucción y validación

Cada cambio válido de ancho LED, alto LED, X, Y o resolución HDMI reconstruye todo el objeto: physical, source canvas, aliases canvas, viewport, único slice y escalas unitarias. No se hace deep merge.

Al cambiar 516×860 a 600×900, tanto source como destino del slice pasan a 600×900. Al mover X/Y, cambian simultáneamente `viewport_x/y` y `slices[0].output_x/y`; el origen source continúa en (0,0).

El helper exige números enteros seguros, dimensiones positivas y X/Y no negativos. Rechaza fracciones, valores no finitos, booleanos, strings y null. Debe cumplirse:

- `X + ancho LED <= ancho HDMI`.
- `Y + alto LED <= alto HDMI`.

Los campos del formulario convierten texto numérico a número antes del helper; un campo vacío es inválido. Mientras el borrador es inválido se muestra el error, se deshabilita guardar y el manejador de submit tampoco envía la petición. Se mantiene el último mapping válido en memoria, pero no se guarda silenciosamente en lugar del borrador inválido.

**La validación estricta pertenece al preset del frontend.** El endpoint continúa aceptando el contrato avanzado existente. Sin un marcador nuevo no se puede distinguir de forma segura un preset de un custom histórico. No se agregó validación global de límites al API ni se afirma que una llamada directa al API rechace esos límites. Esto preserva los custom avanzados que usan recortes, escalas o coordenadas fuera del HDMI.

## NORMAL y compatibilidad histórica

Seleccionar explícitamente Pantalla normal genera un mapping normal completo: `enabled=false`, profile/mode/mapping_mode normal, dimensiones HDMI coherentes, viewport completo en (0,0), escalas 1 y slices vacíos. No se limita a cambiar `enabled`.

El contrato existente no registra si un custom fue creado con el preset. Por eso la selección del preset es temporal al formulario y no se infiere a partir de la forma del JSON:

- Al reabrir un custom, incluido uno creado con este preset, se muestran sus valores guardados como configuración existente.
- Para usar el editor simplificado se selecciona expresamente LED personalizada; se cargan canvas y posición existentes.
- Sin esa selección no se reconstruye un custom/sliced/led_wide. El editor avanzado sigue disponible y conserva sus reglas.
- Esto evita convertir automáticamente históricos; como límite de UX, el preset debe seleccionarse de nuevo después de reabrir si se quiere editarlo desde los campos simplificados.

Editar nombre/notas/rotation sin tocar mapping omite `output_mapping_json` en el PATCH. El backend ahora conserva el JSON almacenado siempre que el campo se omite, incluso si el PATCH incluye resolución. Se eliminó la renormalización implícita en ese caso. `null` conserva el comportamiento previo de no reemplazarlo; un objeto explícito se normaliza y reemplaza completo. `{}` explícito normaliza a NORMAL.

No se convierten canales horizontales/verticales en LED. Rotation sigue siendo independiente. No se recorren ni actualizan canales existentes.

## Archivos del cambio

| Archivo | Cambio |
| --- | --- |
| `frontend/src/lib/ledCustomMapping.ts` | Helper puro y validación del preset completo. |
| `frontend/src/components/dashboard/LedOutputMappingPanel.tsx` | Selector simple, campos LED, validación y acceso al editor avanzado existente. |
| `frontend/src/components/dashboard/ScreenCreateDrawer.tsx` | Integración de validación y bloqueo de submit en alta. |
| `frontend/src/components/dashboard/ScreenEditDrawer.tsx` | Integración en edición, preservando la omisión de mapping ajeno. |
| `backend/app/api/routes/channels.py` | Conservar mapping omitido también al editar resolución. |
| `backend/tests/fixtures/led_custom_516x860.json` | Contrato esperado compartido por pruebas frontend/backend. |
| `backend/tests/test_led_custom_canvas.py` | Persistencia, API/runtime, aislamiento y compatibilidad. |
| `frontend/tests/led_custom_canvas.test.mjs` | Helper y formularios reales montados con React test renderer. |
| `docs/LED_CUSTOM_CANVAS_MVP_REPORT.md` | Este informe. |

## Verificación ejecutada

| Grupo | Resultado real |
| --- | --- |
| Frontend LED | 9 pruebas PASS. |
| Frontend rotación existente | 6 pruebas PASS. |
| Backend LED | 10 pruebas PASS. |
| Backend rotación existente | 19 pruebas PASS. |
| Backend retención existente, SQLite | 20 pruebas PASS. |
| Backend snapshots PostgreSQL | 2 pruebas PASS en PostgreSQL local aislado, ejecutadas por separado. |
| Build frontend | PASS: TypeScript y Vite. |

Frontend: 15/15 PASS. Backend: **51 pruebas distintas PASS**: 49 en SQLite y las 2 de PostgreSQL ejecutadas por separado. El runner SQLite sigue omitiendo esas dos pruebas por diseño; ya no están pendientes de ejecución.

La validación previa de frontend, backend SQLite y build conserva sus resultados; se completaron las dos comprobaciones PostgreSQL pendientes antes de publicar. No fue necesario ampliar la implementación.

Cobertura nueva:

- Creación 1920×1080 + LED516×860 y comparación exacta del mapping.
- Runtime autenticado entrega el objeto persistido, mantiene runtime 3 y retención V1.
- Reapertura conserva valores sin reinterpretar custom histórico.
- Cambios de tamaño, posición y HDMI reconstruyen el preset en ambos formularios.
- Un slice residual 1920×1080 desaparece al seleccionar explícitamente el preset 516×860.
- Nombre/notas omiten mapping; rotation cambia solo el giro. En backend se compara el JSON persistido antes/después y todos los campos de DeviceOutput.
- Una edición exclusivamente de mapping conserva incluso geometría/canvas históricos.
- NORMAL limpia todos los campos custom coherentemente.
- Custom avanzado fuera de límites y sliced/led_wide permanecen válidos bajo las reglas existentes.
- Los borradores inválidos, incluyendo reducir HDMI por debajo del LED, no producen peticiones desde los formularios.
- Escrituras cruzadas entre clientes/sucursales y por rol sin permisos reciben 403 y conservan los registros.
- Una campaña mixta imagen/video mantiene selección, playlist y Media Retention al cambiar mapping.

### PostgreSQL: resultados finales

1. `test_runtime_wrapper_opens_complete_postgres_snapshot`: **PASS**. Crea un canal vacío y llama al wrapper `build_player_runtime_from_snapshot`; comprueba runtime 3, `media_retention.complete=true` y assets vacíos.
2. `test_publication_after_snapshot_start_is_not_mixed`: **PASS**. Establece un snapshot `REPEATABLE READ` con campaña A, confirma la asignación B en otra sesión y comprueba que el runtime del snapshot conserva A en selección y retención, sin incorporar B.

Ejecución real: **2026-10-05 19:41:47 UTC**. Resultado: **2 ejecutadas, 0 fallos, 0 errores, 0 omitidas**.

- Docker Desktop local, contexto explícito `desktop-linux`, endpoint `npipe:////./pipe/dockerDesktopLinuxEngine`.
- Contenedor exclusivo `tecno-led-s1-pg-8e223fe1957b`, PostgreSQL **16.14**, imagen local `postgres:16`, ID `sha256:95206741a5b214807675e14165369d05b93a9cf692223b616d07cca227e74b0b`. No se descargó una imagen ni se inició el compose de la aplicación.
- Único puerto publicado: **127.0.0.1:50845**. Se verificaron identidad y etiqueta del contenedor, dirección de escucha y almacenamiento antes de conectar.
- Base **tecno_control_retention_test**, usuario de fixture `tecno_led_s1_test`; identidad confirmada mediante SQL y **0 tablas de usuario** antes de crear fixtures.
- Datos en `tmpfs` de 256 MiB, sin volúmenes ni directorios del equipo montados. El contenedor se detuvo y eliminó al terminar; eliminación comprobada.
- Antes de importar la aplicación se fijaron `DATABASE_URL=sqlite://`, `AUTO_SEED=false`, media temporal, configuración ficticia y `TEST_DATABASE_URL` con el endpoint local verificado. El motor global de la aplicación se comprobó como SQLite en memoria.
- No se importó `app.main`, ni se ejecutó startup, seed o migraciones. `Base.metadata.create_all` creó sólo las tablas de fixture en la base vacía y desechable.

Se seleccionaron exclusivamente los dos métodos existentes de `PostgreSQLSnapshotTests` mediante `unittest.defaultTestLoader.loadTestsFromNames`, sin cambiar sus pruebas. Runner temporal: `.tmp/led-custom/validate_postgres.py`; evidencias: `.tmp/led-custom/postgres-tests.log` y `.tmp/led-custom/postgres-results.json`, todos excluidos de Git.

### Aislamiento y comandos

Se reutilizaron dependencias de pruebas locales existentes. El backend se ejecutó con Python 3.11 mediante:

```powershell
& 'C:\Users\diego\Documents\Codex\2026-09-23\tecno-control-cloud-nueva-pc-auditor\work\rotation-venv\Scripts\python.exe' -B -X utf8 backend/tests/run_screen_rotation_suite.py
```

El runner fija `DATABASE_URL=sqlite://`, `AUTO_SEED=false`, un MEDIA_ROOT temporal y direcciones testserver antes de importar la aplicación. Las fixtures comprueban SQLite en memoria y ausencia de archivos en `PRAGMA database_list` antes de crear tablas efímeras. Se usa FastAPI TestClient en proceso; no se importa `app.main`, ni se inicia startup, seed o un servidor backend. No se accede a bases externas.

Frontend, desde `frontend`:

```powershell
$env:SCREEN_ROTATION_TEST_DEPS = 'C:\Users\diego\Documents\Codex\2026-09-23\tecno-control-cloud-nueva-pc-auditor\work\rotation-ui-tests'
node --test tests/led_custom_canvas.test.mjs tests/screen_rotation_form.test.mjs
pnpm run build
```

Las pruebas montan los dos formularios reales y capturan sus peticiones mediante un fetch sustituido que exige rutas locales específicas. No realizan peticiones de red. React/test-renderer: 18.3.1; Node: 20.20.0; pnpm: 10.17.1; Vite: 5.4.21.

Se usaron las dependencias instaladas correspondientes a `pnpm-lock.yaml`: el lock instalado y el del repositorio son iguales al normalizar finales de línea. No se instalaron dependencias ni se cambió el lockfile. SHA256 del lockfile antes/después: `2BF4990DD02FC904CC55CACF950B66ED7037E711B013E99DAF18A4EF3EF253DD`.

El build muestra una advertencia sobre chunks mayores de 500 kB: bundle JavaScript de 867.14 kB (199.79 kB gzip). No es un error de compilación. Los artefactos generados versionados se restauraron a su copia inicial, para no incorporarlos al diff. `git diff --check` terminó sin errores; Git también avisa sobre la conversión LF a CRLF configurada para tres componentes frontend.

Logs locales: `.tmp/led-custom/frontend-tests.log`, `.tmp/led-custom/backend-tests.log`, `.tmp/led-custom/frontend-build.log`, más las evidencias PostgreSQL anteriores.
Diff completo: `.tmp/led-custom/LED_CUSTOM_CANVAS_S1.patch`, incluyendo archivos nuevos y este informe. Estos artefactos locales están ignorados por Git; el commit se limita a los nueve archivos enumerados arriba.

## Pendientes y límites

- Validación física con el controlador LED y Player compatible: confirmar la señal HDMI real, la región tomada por el controlador y que el Player aplica el mapping. No se probaron pantallas físicas.
- FIT_CENTER, proporción intacta, fondo negro y adaptación de medios 516×860/1920×1080 pertenecen a la reproducción del Player; S1 configura y persiste su región, no implementa ese renderizado ni garantiza soporte en Players anteriores.
- La validación estricta no protege llamadas directas al API avanzado, por la compatibilidad explicada arriba.
- Sin un marcador persistido no hay reconocimiento automático del preset al reabrir. No se agregó uno ni se diseñó otro contrato.
- No se hizo validación visual manual en navegador; se validaron comportamiento de formularios, payloads y compilación.
- Las pruebas PostgreSQL ya aprobaron; no certifican migraciones ni actualizaciones de bases históricas. La advertencia de tamaño del bundle continúa.

Publicación autorizada únicamente en GitHub. No se hicieron deploy, migraciones ni llamadas a producción. No se modificaron repositorios ni código de Players. Después de verificar el push y el hash final se debe detener el trabajo; el servidor queda fuera de este alcance.
