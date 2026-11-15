# Phase 3 de Blue-Green V2.6

**El pre-stage y el handoff requieren autorizaciones separadas.** El pre-stage termina en `READY_FOR_DOWNTIME_WINDOW=YES` con Edge cerrado. Sólo el handoff autorizado puede trasladar 80/443, la escritura TLS o la IP de confianza. Phase 3 termina con `ACTIVE_API_SLOT=BLUE` y `ACTIVE_FRONTEND_SLOT=BLUE`. El backend R3 conserva su contenedor y redes. El frontend BLUE sirve la imagen R3 exacta. El frontend histórico se conserva para reversión. Phase 4 y Videowall GREEN quedan fuera de esta intervención.

## 1. Precheck y calificación de imágenes en solo lectura

Verificar los hashes aprobados del tooling V2.6. Instalar esta revisión bajo `/opt/tecnocontrol-bgv2/v2.6`, fuera de `/opt/tecnocontrol`. Ejecutar sólo después de autorización independiente, como root en el propio host Linux, con Python 3.12.3 y el socket Docker local. Antes de mutar Docker, fijar mediante inspección las identidades completas de los servicios `backend`, `frontend` y `postgres` en el proyecto Compose `tecnocontrol`, exactamente un contenedor running por rol. Comprobar salud R3/PostgreSQL, SHA del Caddyfile histórico, Git principal R3, dump histórico, volúmenes, redes, propietarios actuales de TCP 80/443 e índice 019 válido. El verificador 019 es sólo lectura. No ejecutar 019.

Registrar el ID y la configuración completa del frontend histórico, sus dos redes, IDs de red, aliases, IPs y todos los `HostIp/HostPort` publicados. Fijar el backend y los volúmenes `tecnocontrol_backend_uploads`, `tecnocontrol_caddy_data`, `tecnocontrol_caddy_config` y `tecnocontrol_postgres_data`. La identidad de proxy observada debe seguir siendo `172.30.250.2`. Medir el endpoint privado del backend desde Docker, con expectativa histórica `172.30.250.3:8000`, y exigir que sólo ese contenedor lo posea. No confiar en una IP histórica que discrepe de la medición. Conservar el frontend histórico, los manifests y los recursos de reversión intactos.

En los comandos siguientes, `TOOL` es el directorio privado que contiene esta revisión, `R3_RELEASE` el JSON privado de release aprobado, `BACKEND_ID`, `OLD_FRONTEND_ID` y `PG_ID` los tres IDs completos recién verificados. `WORK` es una ruta nueva y absoluta fuera de Git. El JSON de release contiene `revision`, `tree` y `images` con claves `api` y `frontend`. El hash de Git R3 conocido es `0a7d5854bee17a64f6dba868dc44f2c0bbceed3d`, con tree `bd8d9a8969545cf98199579d0106d5982ff95453`. El registro independiente de aprobación debe sustentar los pins de revisión/tree y los IDs de imagen. `APPROVED_PG_ID` es el ID completo fijado por el precheck autorizado, con prefijo histórico `8a2a6899fd6f`. No inferir provenance por nombre, tag o una cadena encontrada en una imagen.

```bash
set -euo pipefail
umask 077
mkdir -m 0700 -- "$WORK"
docker_local() { docker --host unix:///var/run/docker.sock "$@"; }
running_service_id() {
  ids=$(docker_local ps --no-trunc --format '{{.ID}}' --filter status=running \
    --filter label=com.docker.compose.project=tecnocontrol \
    --filter "label=com.docker.compose.service=$1") || return
  [[ $ids =~ ^[0-9a-f]{64}$ ]] || return 1
  test "$(docker_local container inspect --format \
    '{{index .Config.Labels "com.docker.compose.project"}}/{{index .Config.Labels "com.docker.compose.service"}}/{{.State.Running}}' \
    "$ids")" = "tecnocontrol/$1/true" || return
  printf '%s\n' "$ids"
}
BACKEND_ID=$(running_service_id backend)
OLD_FRONTEND_ID=$(running_service_id frontend)
PG_ID=$(running_service_id postgres)
test "$PG_ID" = "$APPROVED_PG_ID"
test "$(docker_local container inspect --format '{{.State.Health.Status}}' "$PG_ID")" = healthy
test "$(GIT_OPTIONAL_LOCKS=0 git -C /opt/tecnocontrol rev-parse --show-toplevel)" = /opt/tecnocontrol
test "$(GIT_OPTIONAL_LOCKS=0 git -C /opt/tecnocontrol symbolic-ref --short HEAD)" = main
test "$(GIT_OPTIONAL_LOCKS=0 git -C /opt/tecnocontrol rev-parse HEAD)" = 0a7d5854bee17a64f6dba868dc44f2c0bbceed3d
test "$(GIT_OPTIONAL_LOCKS=0 git -C /opt/tecnocontrol rev-parse 'HEAD^{tree}')" = bd8d9a8969545cf98199579d0106d5982ff95453
GIT_OPTIONAL_LOCKS=0 git -C /opt/tecnocontrol diff --quiet --exit-code
GIT_OPTIONAL_LOCKS=0 git -C /opt/tecnocontrol diff --cached --quiet --exit-code
test "$(GIT_OPTIONAL_LOCKS=0 git -C /opt/tecnocontrol ls-files --others --exclude-standard)" = \
  backup_pre_013_20260904_182804.dump
test -f /opt/tecnocontrol/backup_pre_013_20260904_182804.dump
test ! -L /opt/tecnocontrol/backup_pre_013_20260904_182804.dump
sha256sum /opt/tecnocontrol/backup_pre_013_20260904_182804.dump > "$WORK/r3-dump-sha256.txt"
/usr/bin/python3 -B "$TOOL/qualify_r3.py" \
  --approved-release "$R3_RELEASE" --backend-id "$BACKEND_ID" --frontend-id "$OLD_FRONTEND_ID" \
  --legacy-frontend-manifest "$LEGACY_FRONTEND_MANIFEST" --legacy-frontend-sha256 "$LEGACY_FRONTEND_PIN" \
  > "$WORK/r3-qualification.json"
```

Esperar `R3_EXACT_IMAGE_QUALIFICATION`, `PASS`. Backend esperado `sha256:951cd0b55ce3b461c361cb9b9d83c29d08e32f938cff4eefbbfb842f58ab0832`. Frontend esperado `sha256:b2a1ae4a8515e9d1db4cb14744bba356311317c819a3a16b37c791d0257e905a`. El comando vuelve a leer los dos IDs vivos y registra `RepoDigests` si existen, labels OCI, plataforma, `Entrypoint`, `Cmd`, capas `RootFS`, referencia del contenedor, hashes de configuración y runtime. No imprime env values. El frontend nativo requiere los labels OCI `org.opencontainers.image.revision` e `io.tecnocontrol.git.tree` que coincidan con el pin de release. Para esta adopción inicial del frontend histórico, fijar `LEGACY_FRONTEND_MANIFEST` y el pin del payload `LEGACY_FRONTEND_PIN` a la aprobación independiente. El manifiesto verifica imagen, bytes estáticos, frontend histórico y contrato privado, incluidos los labels si existen. Cualquier label contradictorio, diferencia de provenance, `Cmd` vivo, Caddyfile o ID implica **STOP**. El comando de backend esperado del guard incluye `app.main:app`, un worker y `--lifespan off`. Cualquier diferencia requiere revisión, sin corregirla recreando el backend. La ausencia de las imágenes R3 exactas en QA local no reemplaza esta etapa productiva.

Antes de preparar infraestructura, inspeccionar el frontend R3 exacto al menos 20 veces consecutivas en sólo lectura. Conservar en evidencia privada `raw_mounts`, `canonical_mounts` y `canonical_mounts_sha256`, además del orden observado por destino en cada muestra. Exigir el mismo hash canónico que el pin histórico y repetir la calificación del frontend. El orden exterior de `Mounts` puede variar. Todos los campos y tipos JSON de cada objeto se comparan completos y dos destinos iguales producen `DUPLICATE_MOUNT_DESTINATION`. Conservar el JSON de calificación con su evidencia por inspección y los archivos privados `legacy-blue-manifest.mounts.json` y `blue-ready.mounts.json`. Los manifiestos sellados y los recibos de READY mantienen su formato.

Calificar en solo lectura cualquier MAC que se vaya a declarar como generada por Docker. Para el backend, tomar del contenedor exacto los labels `com.docker.compose.project.working_dir`, `com.docker.compose.project.config_files` y, cuando exista, `com.docker.compose.project.environment_file`. Usar el directorio histórico, los archivos en su orden y el entorno de creación aprobado para resolver `docker compose --project-name tecnocontrol --project-directory "$COMPOSE_WORKDIR" … config --format json`. El directorio Git sigue siendo `/opt/tecnocontrol`. Comprobar los hashes de los overlays y env contra la evidencia aprobada. Inspeccionar el JSON en memoria y conservar sólo hashes, nombres de red y el dictamen sobre MAC. No guardar ni mostrar el JSON completo, ya que puede contener credenciales.

Para cada endpoint que se pretenda declarar generado, cotejar el nombre de red efectivo en `networks` con el nombre e ID de red inspeccionados. Confirmar que ni `services.backend.mac_address` ni `services.backend.networks.<red>.mac_address` se configuraron y que el historial aprobado de creación y conexión de ese endpoint descarta un override posterior. `NetworkSettings.Networks.<red>.MacAddress` por sí solo sólo muestra el valor operativo. Si existe una MAC explícita, falta algún archivo o registro, la interpolación no se puede reconstruir o la correspondencia de redes es ambigua, mantener la comparación exacta para ese endpoint. No reconectar ni recrear servicios para obtener evidencia.

Docker puede regenerar una MAC asignada por el bridge al recrear el endpoint. La API de inspección no expone por separado la MAC configurada de cada endpoint en ejecución. [Implementación de bridge en Moby 29.6.0](https://github.com/moby/moby/blob/docker-v29.6.0/daemon/libnetwork/drivers/bridge/bridge_linux.go#L1101-L1111), [contrato de inspección de Moby](https://github.com/moby/moby/pull/51203).

## 2. Evidencia protegida y verificaciones de sólo lectura

Conservar `WORK/r3-qualification.json`, modo `0600`. Nunca reutilizar un STATE anterior. Registrar las definiciones de red, nombres/IDs de contenedores y configuración efectiva aprobada, con secretos excluidos. Mantener una referencia al snapshot DigitalOcean aprobado y a los backups y recibos previos. No sobrescribir o limpiar esos artefactos. El snapshot y los backups se conservan para recuperación excepcional. La reversión de esta intervención actúa sobre el ingreso.

Preservar el backup y los recibos anteriores como evidencia. El pre-stage califica en sólo lectura R3, esquema y media y no crea un backup completo. La sección 4 exige un backup fresco para el handoff autorizado.

Verificar el paquete OBS6.4 sellado aprobado antes de invocar su verificador. Su `SHA256SUMS` tiene SHA-256 aprobado `17d5d403305a0aaf734a8365d355b07a57e12da62c0826fb7dba543842336d5d`. Fijar `OBS64_SHA256SUMS_SHA256` a esa aprobación. Si el paquete o la verificación no están disponibles, STOP. `candidate`, `apply019`, `verify019` de la cadena de deployment y `cutover` quedan fuera de Phase 3. La función independiente `verify_index` comprueba índice y PostgreSQL en sólo lectura antes y después del SQL sellado.

```sh
(
  cd "$OBS64_BACKUP_TOOL"
  printf '%s  SHA256SUMS\n' "$OBS64_SHA256SUMS_SHA256" | sha256sum -c -
  sha256sum -c SHA256SUMS
  /usr/bin/python3 -I -B - <<'PY' > "$WORK/index019-read-only.json"
import json
from pathlib import Path
import sys

package = Path.cwd()
sys.path.insert(0, str(package))
from deployment_phases import verify_index

print(json.dumps(verify_index(package), sort_keys=True))
PY
)
```

Medir huella actual sin carga artificial. Registrar RAM y CPU del host, conexiones PostgreSQL, disco y Docker. Comparar con la proyección aprobada de Phase 3 para PostgreSQL, backend R3, frontend-blue, Edge y frontend histórico retenido. La topología desechable de un backend midió un pico combinado de 550387712 bytes con cuatro CPU. La medición productiva se obtiene en el host durante Phase 3. La calificación simultánea de dos backends corresponde a `PHASE4_DUAL_SLOT_CAPACITY=REQUIRES_LIVE_QUALIFICATION`. Los dos timeouts de transporte GREEN, código curl 28, y el control posterior de 6.344 s quedan en `PHASE4_GREEN_STABILITY_FOLLOWUP`. La causa permanece indeterminada.

## 3. Preparación con R3 todavía público

Mantener el frontend histórico atendiendo 80/443 durante todo este apartado. Preparar `EDGE_CFG` y `EDGE_RUN` separados del `/config` histórico, propiedad root `0700`, con `EDGE_CFG/caddy` también `0700`. Los mounts aprobados de Edge son `/config` propio, `/run/bgv2` privado y los dos archivos de bootstrap en solo lectura. Montar el TLS persistente de R3 en `/data`. No seleccionar ese storage en la configuración privada. La función `preview_config()` utiliza sólo `/config/preview-tls` con CA local desechable, sin servidores públicos ni escritura en el TLS histórico. Verificar fingerprints de certificados existentes por medio del antiguo propietario y conservar la huella antes de transferir. La configuración pública usa la política TLS aprobada y el storage persistente `/data`, sin `tls internal`. Comparar ambos dominios y la cadena pública antes de detener R3. Fijar `BACKEND_PROXY_IP` al valor leído de `NetworkSettings.Networks` del backend exacto, `PROXY_NET` e `INGRESS_NET` a los dos nombres de red verificados y `TLS_DATA_VOLUME` al nombre de volumen observado. Registrar el ID y los aliases de cada endpoint histórico para poder restaurarlo.

Crear `frontend-blue` sólo desde `$R3_FRONTEND_IMAGE`, el ID completo calificado, con los labels V2 requeridos, IP privada verificada libre y alias exclusivo. Permanece vivo hasta el final. `R3_FRONTEND_IMAGE` se obtiene del resultado PASS, nunca de `latest`. `EDGE_IMAGE` se fija a la imagen Caddy 2.11.4 sellada, local y aprobada. Las rutas de frontend son `/srv` con `Caddyfile.slot`, `admin off`, tmpfs de sólo lectura para `/data` y `/config`, sin publish ni red de base de datos. Para el manifiesto legacy ya sellado, fijar `SLOT_CADDYFILE` a su `slot_caddyfile_source`, `/opt/tecnocontrol-bgv2/v2.3/Caddyfile.slot`. Instalar allí únicamente los bytes versionados de `$TOOL/Caddyfile.slot` cuando el archivo esté ausente. Exigir archivo regular de root modo `0444`, sin enlaces simbólicos, y cotejar el SHA-256 exacto con el manifiesto antes de montar. Conservar el manifiesto y el pin tal como fueron aprobados y detenerse ante un archivo existente distinto. La calificación nativa usa `$TOOL/Caddyfile.slot`.

```sh
docker_local create --pull=never --name "$FRONTEND_BLUE_NAME" --restart unless-stopped \
  --network "$PROXY_NET" --ip "$FRONTEND_BLUE_IP" --network-alias frontend-blue \
  --label io.tecnocontrol.bgv2.slot=BLUE --label io.tecnocontrol.bgv2.role=frontend \
  --tmpfs /config:ro --tmpfs /data:ro \
  --mount "type=bind,source=$SLOT_CADDYFILE,target=/etc/caddy/Caddyfile.bgv2,readonly" \
  --health-cmd 'nc -z -w 2 127.0.0.1 80' --health-interval 5s --health-timeout 3s \
  --health-start-period 5s --health-retries 6 "$R3_FRONTEND_IMAGE" \
  caddy run --config /etc/caddy/Caddyfile.bgv2 --adapter caddyfile
docker_local start "$FRONTEND_BLUE_NAME"
```

Crear Edge con los bindings finales ya fijados en `HostConfig`, inicialmente sólo sobre la red proxy Docker `Internal=true` en una IP temporal libre. Para la topología histórica `0.0.0.0/[::]` en TCP 80/443, los argumentos `--publish 80:80/tcp --publish 443:443/tcp` reproducen el modo wildcard. Si la inspección fresca difiere, STOP y calificar el cambio de bindings antes de continuar. No usar Compose `up` para cambiar las publicaciones, pues recrearía Edge. El último argumento de puerto se conserva durante las reconexiones. No conectar todavía la red externa. Probar mediante `docker inspect`, listeners del host y un cliente independiente que R3 sigue siendo el único publicador. Si el Engine productivo no reproduce la semántica de publicación desde la red interna, STOP antes de detener R3. Fijar `EDGE_ID` al ID devuelto por `create`.

```sh
mkdir -m 0700 -- "$EDGE_CFG" "$EDGE_RUN" "$STATE_DIR"
mkdir -m 0700 -- "$EDGE_CFG/caddy"
EDGE_ID=$(docker_local create --pull=never --name "$EDGE_NAME" --restart unless-stopped \
  --network "$PROXY_NET" --ip "$TEMP_EDGE_IP" \
  --publish 80:80/tcp --publish 443:443/tcp \
  --mount "type=bind,source=$TOOL/edge-entrypoint.sh,target=/opt/bgv2/edge-entrypoint.sh,readonly" \
  --mount "type=bind,source=$TOOL/edge-bootstrap.json,target=/opt/bgv2/edge-bootstrap.json,readonly" \
  --mount "type=bind,source=$EDGE_CFG,target=/config" \
  --mount "type=bind,source=$EDGE_RUN,target=/run/bgv2" \
  --mount "type=volume,source=$TLS_DATA_VOLUME,target=/data" \
  --entrypoint /bin/sh "$EDGE_IMAGE" /opt/bgv2/edge-entrypoint.sh)
docker_local start "$EDGE_ID"
```

El supervisor host sigue detenido en la preparación. Capturar la adopción legacy con el nombre del frontend histórico como único peer de confianza de `.2`, `postgres_id` igual a `PG_ID` en `api:BLUE` y el backend exacto como endpoint. `BINDINGS` es privado, con las dos entradas `api:BLUE` y `frontend:BLUE`. Cada una lleva `name`, `network`, `ip`, `upstream`, `image_id`, `revision` y `tree`. La API lleva también `proxy_ip`, `uploads_volume` y `postgres_id`. El frontend lleva `frontend_config_sha256` del archivo `Caddyfile.slot` y, para esta adopción, `legacy_frontend_adoption` con el objeto `manifest` íntegro y su pin `sha256`. Preparar `BACKEND_BINDINGS` privado con la misma entrada `api:BLUE` que el definitivo. La captura inicial del backend usa ese archivo porque todavía no tiene el pin backend que exige el binding del frontend legacy. Mantener `LEGACY_PIN` como el SHA-256 del payload del manifiesto de backend, revisado y aprobado por un canal separado. El SHA de los bytes del fichero puede registrarse por separado. La fase de handoff nunca reconecta el backend.

`generated_mac_networks` es opcional por rol. Contiene una lista JSON ordenada y sin duplicados con los nombres Docker exactos de las redes cuya MAC generada quedó demostrada. En `api:BLUE`, incluir sólo los endpoints aprobados en el precheck. En `frontend:BLUE`, el único nombre elegible es el de `$PROXY_NET`, si la creación y conexión aprobadas arriba se ejecutaron sin una opción MAC. Omitir el campo si la lista queda vacía. Fijar esas listas antes de capturar el manifiesto y confirmar BLUE/BLUE. La identidad estable vincula la lista y mantiene el resto de los campos de cada red. La época conserva también la MAC operativa observada. Así, con el mismo boot una MAC cambiada sigue siendo deriva y después de un reboot sólo se puede readmitir una MAC regenerada para un endpoint aprobado expresamente. Una MAC fija permanece fijada al valor original.

Si se reconectó el frontend histórico, usar la calificación con el pin estable descrita en [Frontend histórico R3](LEGACY_FRONTEND_BLUE.md#calificación-tras-una-reconexión). Mantener `manifest` y `sha256` originales en el binding y añadir `stable_pin` y `compose_proof`. Calificar el frontend histórico mientras la IP y el alias del slot privado aún estén libres. Si una instancia privada anterior ocupa el slot, sólo retirar su attachment con autorización expresa y tras comprobar que el contenedor es el aprobado, que no publica puertos ni posee TLS y que R3 sirve el tráfico. Repetir los smokes públicos y exigir que la IP y el alias estén libres antes de crear el slot nuevo.

```sh
/usr/bin/python3 -B "$TOOL/legacy_blue.py" --bindings "$BACKEND_BINDINGS" \
  --trusted-proxy-name "$OLD_FRONTEND_NAME" --project tecnocontrol \
  --manifest "$WORK/legacy-blue-manifest.json"
```

El manifiesto separa identidad estable y época inicial del host. Revisar el hash de imagen/config, comando, creación del contenedor, montajes, IDs de red, IP, servicio/proyecto y salud, además del `boot_id`, PID, `StartedAt`, `RestartCount` y timestamp de attestación. Renderizar en Edge la configuración completa BLUE/BLUE con los hosts canónicos y política TLS pública aprobada. `BGV2_TLS_DIRECTIVE=` omite el bloque `tls internal` del template. Comprobar el resultado de adaptación, el storage TLS previsto y el uso de certificados de R3 antes de sellarlo. En el shell root con `set -o pipefail`, adaptar y sellar sin cargar la configuración pública.

```sh
docker_local exec -i -e BGV2_TLS_DIRECTIVE= "$EDGE_ID" \
  caddy adapt --config - --adapter caddyfile < "$TOOL/Caddyfile.edge" > "$WORK/adapted.json"
PYTHONPATH="$TOOL" /usr/bin/python3 -B - "$WORK" "$LEGACY_PIN" <<'PY'
from pathlib import Path
import sys
from edge_config import bootstrap_config, seal_new
from release_state import canonical, decode, read_private
from routectl import bind_legacy_blue
work, pin = Path(sys.argv[1]), sys.argv[2]
manifest = decode(read_private(work / 'legacy-blue-manifest.json'))
adapted = decode(read_private(work / 'adapted.json'))
config = bootstrap_config(canonical(bind_legacy_blue(adapted, manifest, pin)))
seal_new(work / 'sealed-edge.json', canonical(config.config))
print(config.hash)
PY
```

Registrar la salida como `GEN0_SHA256`. Las funciones `routectl_pre` y `routectl_live` tienen los mismos binds, lock, estado y manifest. Sólo cambia el nombre del propietario de `.2`. `STATE_DIR` nuevo, `KEY_FILE` privado de al menos 32 bytes, sin imprimir clave. Mantener `OLD_FRONTEND_NAME` como valor de inspección fresca.

```sh
routectl_as() {
  local peer=$1; shift
  local -a command=(/usr/bin/python3 -B "$TOOL/routectl.py" --state-dir "$STATE_DIR" \
    --socket "$EDGE_RUN/admin.sock" --autosave "$EDGE_CFG/caddy/autosave.json" \
    --bindings "$BINDINGS" --key "$KEY_FILE" --edge-name "$peer" \
    --legacy-blue-manifest "$WORK/legacy-blue-manifest.json" --legacy-blue-sha256 "$LEGACY_PIN")
  if [[ ${ROUTECTL_HANDOFF_CHILD:-0} == 1 ]]; then exec "${command[@]}" "$@"; fi
  "${command[@]}" "$@"
}
routectl_pre() { routectl_as "$OLD_FRONTEND_NAME" "$@"; }
routectl_live() { routectl_as "$EDGE_NAME" "$@"; }
routectl_pre preview "$WORK/sealed-edge.json"
```

Verificar Host/SNI privados a través del mismo Edge y de la CA de `/config/preview-tls`. La IP temporal del Edge no amplía `FORWARDED_ALLOW_IPS`. Antes del handoff comprobar la política de sanitización del proxy y que el antiguo propietario mantiene la identidad de confianza. En la primera validación pública comprobar el cliente real mediante el nuevo propietario `.2`. Seleccionar en PostgreSQL mediante transacción read-only uno o más canales normales, modo de dispositivo SINGLE, una salida habilitada, fuera de videowall y sin autoridad `hardware-single-input`. Escribir sólo los UUID en `channels.json` privado y no incluirlos en recibos finales. `reviewed-checks.json` fija `qualification_sha256` (SHA-256 de JSON canónico), `schema_unchanged=true`, `media_unchanged=true`, `phase3_infra_capacity=true` y `pool=ISOLATED_NOT_APPLICABLE`. Sustentar esquema, media y capacidad con el precheck de sólo lectura. Fijar también `phase3_product_contracts="UNCHANGED_PRODUCT_NOT_REQUALIFIED"` y `phase3_real_player_continuity_gate="REQUIRED_AFTER_HANDOFF"`. La política `PHASE3_PRODUCT_CONTRACTS=UNCHANGED_PRODUCT_NOT_REQUALIFIED` aplica exclusivamente al R3 exacto que conserva imagen, código de API y proceso. Cualquier cambio de imagen del backend bloquea esta adopción. La evidencia histórica de Windows y Android queda informativa. Phase 4 exige los contratos sellados Windows legacy, Android normal y Android node_slice/Videowall antes de promover GREEN.

```sh
/usr/bin/python3 -B "$TOOL/phase3_ready.py" \
  --qualification "$WORK/r3-qualification.json" --bindings "$BINDINGS" \
  --legacy-manifest "$WORK/legacy-blue-manifest.json" --legacy-sha256 "$LEGACY_PIN" \
  --trusted-proxy-name "$OLD_FRONTEND_NAME" --edge-id "$EDGE_ID" --key "$KEY_FILE" \
  --channels "$WORK/channels.json" --reviewed-checks "$WORK/reviewed-checks.json" \
  --receipts "$WORK/blue-ready.json"
routectl_pre initialize "$R3_RELEASE" "$WORK/blue-ready.json" "$GEN0_SHA256" \
  --sealed-config "$WORK/sealed-edge.json" > "$WORK/blue-committed-closed.json"
```

El signer abre un prompt oculto para un JWT administrativo existente. No pegarlo en shell, logs ni archivos. Verifica API privada, `/api/channels/` ≤3.000 s, runtime 36+16 con cuatro clientes y timeout 10 s, frontend un GET raíz, assets y estabilidad de ambos procesos. No genera login ni tráfico Player. `initialize` confirma generación 0 `BLUE/BLUE` con ingreso cerrado. Probar que el público sigue en R3, el TLS histórico mantiene su huella y ningún listener público de Edge acepta la aplicación. Registrar `T_BLUE_BLUE_COMMITTED` ahora, fuera de la ventana de indisponibilidad. Revalidar la evidencia de reversión. La calificación local del handoff preparado exige 30 intentos, al menos 20 entradas a la ventana y todas las entradas y reversiones correctas dentro de sus límites de 30 s.

Preparar el colector con permisos mínimos y comprobar su entorno en el namespace de Edge. El filtro se armará en el endpoint final `.2` durante el handoff autorizado. Conservar la preparación dentro de la vida del colector y comprobar otra vez su disponibilidad antes de la ventana. El pre-stage no captura un baseline, no abre una ventana de observación y no genera tráfico Player. [Operación del observador](PLAYER_OBSERVER.md)

```sh
# Imagen Python ya cacheada y calificada localmente. El backend R3 es apto
# si provee Python y sleep con el contrato del colector.
COLLECTOR_ID=$(docker_local create --pull=never --name "$COLLECTOR_NAME" \
  --network "container:$EDGE_ID" --cap-drop ALL --cap-add NET_RAW \
  --security-opt no-new-privileges --read-only \
  --mount "type=bind,source=$TOOL/player_capture.py,target=/observe.py,readonly" \
  --entrypoint /bin/sh "$CAPTURE_IMAGE" -ec 'exec sleep 900')
docker_local start "$COLLECTOR_ID"
docker_local exec "$COLLECTOR_ID" python -I -B -c 'import fcntl, socket, struct'
```

Cerrar el pre-stage con R3 sano, frontend histórico como dueño de 80/443 y escritor TLS, Edge con `INGRESS_READY=NO`, smokes privados en PASS, colector preparado y capacidad aprobada. Registrar `READY_FOR_DOWNTIME_WINDOW=YES` en un recibo privado y STOP. Guardar las identidades y la evidencia de reversión para la autorización posterior.

## 4. Ventana de traspaso con autorización independiente

Antes del handoff autorizado, crear un backup fresco bajo un `BACKUP_STATE` absoluto y nuevo con las fases `preflight` y `backup` del paquete OBS6.4 sellado. El paquete valida R3 y PostgreSQL, espacio y consistencia de la copia de DB/media. Conserva `backup-pass.json` privado, confirma PGDMP y TOC con descriptores frescos y registra hashes. Si ese recibo no existe, no pertenece al nuevo intento o no supera la validación de actualidad e integridad, STOP. El backup previo permanece preservado.

```sh
(
  cd "$OBS64_BACKUP_TOOL"
  printf '%s  SHA256SUMS\n' "$OBS64_SHA256SUMS_SHA256" | sha256sum -c -
  sha256sum -c SHA256SUMS
  /usr/bin/python3 -I -B deployment_phases.py preflight "$BACKUP_STATE"
  /usr/bin/python3 -I -B deployment_phases.py backup "$BACKUP_STATE"
  /usr/bin/python3 -I -B - "$BACKUP_STATE" <<'PY' > "$WORK/handoff-backup-check.json"
import json
from pathlib import Path
import sys

package = Path.cwd()
sys.path.insert(0, str(package))
from deployment_phases import check_backup, require_baseline
from release_guard import load_release, open_state, read_receipt

release = load_release(package)
state = open_state(sys.argv[1], release)
payload = read_receipt(state, "backup")
require_baseline(state, release, read_receipt(state, "preflight"))
check_backup(state, payload, verify_bytes=True)
print(json.dumps({"database_sha256": payload["database_archive"]["sha256"],
                  "media_sha256": payload["media_archive"]["sha256"]}, sort_keys=True))
PY
)
```

Mientras R3 sigue atendiendo, preparar `HANDOFF_PREPARED`. El recibo privado y su SHA-256 fijan generación 0, estado exacto sin intención pendiente, identidades BLUE y Edge, ETag y preview, autosave, payload público y evidencia de restauración con sus hashes. La preparación no carga configuración ni abre ingreso. Conservar el SHA-256 devuelto en el mismo shell Bash.

```bash
FRONTEND_BLUE_ID=$(docker_local container inspect --format '{{.Id}}' "$FRONTEND_BLUE_NAME")
HANDOFF_ARGS=(--historical-id "$OLD_FRONTEND_ID" --backend-id "$BACKEND_ID" \
  --frontend-id "$FRONTEND_BLUE_ID" --edge-id "$EDGE_ID" --postgres-id "$PG_ID" \
  --proxy-network "$PROXY_NET" --ingress-network "$INGRESS_NET" --tls-volume "$TLS_DATA_VOLUME" \
  --timeline "$WORK/phase3-timeline.jsonl")
HANDOFF_PREPARED_SHA256=$(
  routectl_pre prepare-handoff "$WORK/handoff-prepared.json" "${HANDOFF_ARGS[@]}" |
    /usr/bin/python3 -I -B -c 'import json,re,sys; row=json.load(sys.stdin); assert row["status"]=="HANDOFF_PREPARED" and re.fullmatch(r"[0-9a-f]{64}",row["sha256"]); print(row["sha256"])'
)
```

Justo antes de detener el histórico, capturar `BASELINE_ACTIVE` fresco mediante `player_devices.id`. El recibo liga PostgreSQL y el backend exactos a generación 0. Arrancar la captura en el endpoint final dentro del namespace de Edge con filtro bloqueado por IP/puerto, permisos mínimos y un pipe privado. El colector debe emitir `ready` antes de abrir ingreso. Si se supera el presupuesto de handoff, revertir.

```sh
/usr/bin/python3 -B "$TOOL/player_observer.py" --state-dir "$STATE_DIR" \
  --postgres-id "$PG_ID" --key "$KEY_FILE" --receipt "$WORK/phase3-baseline.json" phase3-baseline
```

El único cambio de dueño es frontend histórico → Edge. Mantener backend, PostgreSQL, uploads, frontend-blue y el proceso Edge ya validado. Congelar los comandos en la intervención y registrar eventos UTC y monotónicos. Si falta una comprobación o excede el presupuesto de 30 s hasta el primer HTTP válido, pasar inmediatamente a la sección 6. No ocultar fallos ni recalcular el inicio. No ejecutar `compose up`, `restart` de Edge ni ningún smoke largo dentro de la ventana.

1. Iniciar el writer de handoff con el SHA-256 preparado. Esperar la confirmación sólo después de la revalidación de estado, ETag, procesos, PostgreSQL, red, puertos y TLS. Ante una diferencia registrar `PRE_DOWNTIME_STOP` y dejar R3 atendiendo. El writer conserva los locks de Edge y estado hasta confirmar el ingreso.
2. Registrar `T_FRONTEND_STOP` justo antes de `docker_local stop --time 3 "$OLD_FRONTEND_ID"`. Confirmar que el histórico está detenido. Liberar sólo sus endpoints y exigir 80/443 libres y ambas IPs sin dueño. Registrar `T_PORTS_FREE`.
3. Transferir `.2` al mismo Edge, conservar las épocas del Edge y del backend y registrar `T_PROXY_READY`. Conectar Edge a la IP histórica de ingreso y comprobar los bindings de 80/443.
4. Registrar `T_TLS_READY` tras la transferencia y armar el colector preparado en el endpoint final. Enviar al writer la confirmación de transferencia y captura. El writer valida propiedad real de puertos/TLS, peer y procesos y aplica ambos payloads preparados con CAS, primero bootstrap para retirar la CA temporal y después el público con el ETag obtenido. Verifica config efectiva y autosave, mantiene generación 0 y registra `T_INGRESS_READY`.
5. Hacer un único GET público HTTPS `/` en la API con Host/SNI canónicos, certificado válido y presupuesto restante. Registrar `T_FIRST_HTTP_200` sólo al obtener HTTP 200 válido. Ante error, muerte del writer o exceso de 30 s desde `T_FRONTEND_STOP`, usar la sección 6 desde la sesión de reversión preparada.

Los nombres de red históricos esperados son `tecnocontrol_proxy_net` y `tecnocontrol_edge`. Los aliases del histórico se conservan para reconectar en rollback. Para Edge, el ingreso utiliza la IP antigua sólo después de demostrar que quedó libre. Los dos contenedores de ingreso deben mantener sus propios IDs, aun cuando los endpoints de red del frontend histórico se vuelvan a conectar.

Preparar estas funciones, incluida `first_public_200`, y una sesión de reversión independiente antes de entrar a la ventana. Fijar `OLD_PORTS`, `OLD_INGRESS_IP`, los aliases, `EDGE_PROCESS` y `BACKEND_PROCESS` al snapshot aprobado usando los mismos inspectores de abajo. Registrar cada evento en un archivo nuevo privado del intento. Comprobar el presupuesto monotónico entre pasos. Si un comando o comprobación devuelve un error, ejecutar la sección 6 desde la sesión preparada. El primer bloque transfiere la red y el segundo arma el colector y abre ingreso.

```bash
mark_phase3() {
  /usr/bin/python3 -I -B - "$1" <<'PY' >> "$WORK/phase3-timeline.jsonl"
from datetime import datetime, timezone
import json
import sys
from time import monotonic_ns
print(json.dumps({"event": sys.argv[1], "utc": datetime.now(timezone.utc).isoformat(),
                  "monotonic_ns": monotonic_ns()}, sort_keys=True))
PY
}
process_identity() {
  docker_local container inspect --format '{{.Id}} {{.Image}} {{.State.Pid}} {{.State.StartedAt}} {{.RestartCount}}' "$1"
}
port_bindings() {
  docker_local container inspect --format '{{json .NetworkSettings.Ports}}' "$1" | \
    /usr/bin/python3 -I -B -c 'import json,sys; raw=json.load(sys.stdin); print(json.dumps({key:sorted(value,key=lambda row:(row["HostIp"],row["HostPort"])) for key,value in raw.items() if value},sort_keys=True))'
}
has_network() {
  docker_local container inspect --format "{{if index .NetworkSettings.Networks \"$1\"}}yes{{else}}no{{end}}" "$2"
}
release_endpoint() {
  state=$(has_network "$1" "$2") || return
  case "$state" in
    yes) docker_local network disconnect "$1" "$2" ;;
    no) ;;
    *) return 1 ;;
  esac
  test "$(has_network "$1" "$2")" = no
}
require_free_ip() {
  assigned=$(docker_local network inspect --format '{{range .Containers}}{{println .IPv4Address}}{{end}}' "$1") || return
  while IFS= read -r endpoint; do
    test "${endpoint%%/*}" != "$2" || return 1
  done <<< "$assigned"
}

first_public_200() {
  status=$(curl --noproxy '*' \
    --connect-to "api.tecnocontrolcloud.com:443:$PUBLIC_ADDRESS:443" \
    --max-time "${1:-10}" --silent --show-error --fail --output /dev/null --write-out '%{http_code}' \
    https://api.tecnocontrolcloud.com/) || return $?
  test "$status" = 200
}

handoff_budget() {
  /usr/bin/python3 -I -B - "$WORK/phase3-timeline.jsonl" "$1" <<'PY'
import json
import sys
from time import monotonic_ns

with open(sys.argv[1], encoding="utf-8") as log:
    rows = [json.loads(line) for line in log]
started = [row["monotonic_ns"] for row in rows if row["event"] == "T_FRONTEND_STOP"]
finished = [row["monotonic_ns"] for row in rows if row["event"] == "T_FIRST_HTTP_200"]
if len(started) != 1 or len(finished) > 1:
    raise SystemExit("HANDOFF_TIMELINE_INVALID")
if finished:
    if not 0 <= finished[0] - started[0] <= 30_000_000_000:
        raise SystemExit("HANDOFF_DOWNTIME_LIMIT_EXCEEDED")
else:
    milliseconds = min(int(sys.argv[2]) * 1000, (started[0] + 30_000_000_000 - monotonic_ns()) // 1000000)
    if milliseconds <= 0:
        raise SystemExit("HANDOFF_DOWNTIME_LIMIT_EXCEEDED")
    print(f"{milliseconds / 1000:.3f}")
PY
}

test "$(port_bindings "$OLD_FRONTEND_ID")" = "$OLD_PORTS"
test "$(process_identity "$EDGE_ID")" = "$EDGE_PROCESS"
test "$(process_identity "$BACKEND_ID")" = "$BACKEND_PROCESS"
HANDOFF_TRANSFER_STARTED=0
abort_handoff_writer() {
  if [[ -n ${HANDOFF_IN:-} ]]; then exec {HANDOFF_IN}>&-; fi
  if [[ -n ${HANDOFF_OUT:-} ]]; then exec {HANDOFF_OUT}<&-; fi
  if [[ -n ${HANDOFF_ROUTING_CHILD:-} ]]; then
    kill -TERM "$HANDOFF_ROUTING_CHILD" 2>/dev/null || true
    wait "$HANDOFF_ROUTING_CHILD" 2>/dev/null || true
  fi
  if [[ $HANDOFF_TRANSFER_STARTED == 1 ]]; then
    printf '%s\n' PHASE3_ROLLBACK_REQUIRED >&2
  else
    mark_phase3 PRE_DOWNTIME_STOP
  fi
}
trap abort_handoff_writer EXIT
trap 'exit 1' HUP INT TERM
coproc HANDOFF_ROUTING {
  ROUTECTL_HANDOFF_CHILD=1 routectl_pre execute-handoff \
    "$WORK/handoff-prepared.json" "$HANDOFF_PREPARED_SHA256" "${HANDOFF_ARGS[@]}"
}
HANDOFF_ROUTING_CHILD=$HANDOFF_ROUTING_PID
HANDOFF_SOURCE_OUT=${HANDOFF_ROUTING[0]}
HANDOFF_SOURCE_IN=${HANDOFF_ROUTING[1]}
exec {HANDOFF_OUT}<&"$HANDOFF_SOURCE_OUT" {HANDOFF_IN}>&"$HANDOFF_SOURCE_IN"
exec {HANDOFF_SOURCE_OUT}<&- {HANDOFF_SOURCE_IN}>&-
IFS= read -r HANDOFF_READY <&"$HANDOFF_OUT"
test "$HANDOFF_READY" = HANDOFF_PREPARED_VALIDATED
kill -0 "$HANDOFF_ROUTING_CHILD"
mark_phase3 T_FRONTEND_STOP
HANDOFF_TRANSFER_STARTED=1
docker_local stop --time 3 "$OLD_FRONTEND_ID"
test "$(docker_local container inspect --format '{{.State.Running}}' "$OLD_FRONTEND_ID")" = false
release_endpoint "$PROXY_NET" "$OLD_FRONTEND_ID"
release_endpoint "$INGRESS_NET" "$OLD_FRONTEND_ID"
require_free_ip "$PROXY_NET" 172.30.250.2
require_free_ip "$INGRESS_NET" "$OLD_INGRESS_IP"
test -z "$(ss -H -ltn '( sport = :80 or sport = :443 )')"
mark_phase3 T_PORTS_FREE
docker_local network disconnect "$PROXY_NET" "$EDGE_ID"
docker_local network connect --ip 172.30.250.2 "$PROXY_NET" "$EDGE_ID"
test "$(docker_local container inspect --format "{{(index .NetworkSettings.Networks \"$PROXY_NET\").IPAddress}}" "$EDGE_ID")" = 172.30.250.2
test "$(process_identity "$EDGE_ID")" = "$EDGE_PROCESS"
test "$(process_identity "$BACKEND_ID")" = "$BACKEND_PROCESS"
mark_phase3 T_PROXY_READY
docker_local network connect --ip "$OLD_INGRESS_IP" "$INGRESS_NET" "$EDGE_ID"
test "$(port_bindings "$EDGE_ID")" = "$OLD_PORTS"
mark_phase3 T_EDGE_OWNS_PORTS
```

Continuar de inmediato en el mismo shell Bash root, sin xtrace. El cierre anormal del shell cancela y espera al writer de este intento. La sesión independiente vigila `T_FRONTEND_STOP` y los 30 s aunque el shell de transferencia esté bloqueado. Si aparece `PHASE3_ROLLBACK_REQUIRED`, muere el writer o se agota el plazo tras T0, iniciar la sección 6. Conservar el recibo preparado y la línea temporal.

```bash
mark_phase3 T_TLS_READY
# Dentro del mismo shell Bash root, sin xtrace. Encontrar la interfaz por la
# IP exacta desde el namespace del colector. Rechazar una coincidencia ambigua.
EDGE_PROXY_IF=$(docker_local exec -i "$COLLECTOR_ID" python -I -B - <<'PY'
import fcntl
import socket
import struct

matches = []
with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
    for _, interface in socket.if_nameindex():
        try:
            result = fcntl.ioctl(sock.fileno(), 0x8915, struct.pack("256s", interface.encode()))
        except OSError:
            continue
        if socket.inet_ntoa(result[20:24]) == "172.30.250.2":
            matches.append(interface)
if len(matches) != 1:
    raise SystemExit(1)
print(matches[0])
PY
)
coproc PHASE3_CAPTURE {
  exec {HANDOFF_IN}>&- {HANDOFF_OUT}<&-
  docker_local exec "$COLLECTOR_ID" python -I -B /observe.py \
    "$EDGE_PROXY_IF" 172.30.250.2 "$BACKEND_PROXY_IP" 165
}
CAPTURE_PID=$PHASE3_CAPTURE_PID
exec 3<&"${PHASE3_CAPTURE[0]}"
IFS= read -r -t 5 CAPTURE_READY <&3
printf '%s\n' "$CAPTURE_READY" | /usr/bin/python3 -I -B -c \
  'import json,sys; event=json.load(sys.stdin); sys.exit(not (set(event)=={"type","started_ns"} and event["type"]=="ready" and type(event["started_ns"]) is int and event["started_ns"]>0))'
mark_phase3 T_PLAYER_CAPTURE_READY
HANDOFF_BUDGET=$(handoff_budget 30)
printf '%s\n' TRANSFER_AND_CAPTURE_READY >&"$HANDOFF_IN"
exec {HANDOFF_IN}>&-
unset HANDOFF_IN
HANDOFF_BUDGET=$(handoff_budget 30)
IFS= read -r -t "$HANDOFF_BUDGET" HANDOFF_RESULT <&"$HANDOFF_OUT"
exec {HANDOFF_OUT}<&-
unset HANDOFF_OUT
wait "$HANDOFF_ROUTING_CHILD"
unset HANDOFF_ROUTING_CHILD
printf '%s\n' "$HANDOFF_RESULT" > "$WORK/ingress-recovery.json"
OPENED_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
STARTED_AT=$OPENED_AT
FINISHED_AT=$(date -u -d "$STARTED_AT + 120 seconds" +%Y-%m-%dT%H:%M:%S.%NZ)
PUBLIC_HTTP_BUDGET=$(handoff_budget 10)
first_public_200 "$PUBLIC_HTTP_BUDGET"
mark_phase3 T_FIRST_HTTP_200
handoff_budget 30
trap - EXIT HUP INT TERM
```

## 5. Verificación después del primer HTTP

Comprobar público 80→443, TLS/Host/SNI, app/índice/assets, API y media de lectura. Verificar IP cliente real y sanitización, cotejar los fingerprints públicos de ambos hosts con los previos, y confirmar `routectl_live status` en generación 0 BLUE/BLUE. Verificar otra vez la imagen R3 exacta del frontend privado y la identidad estable/época del backend R3. Revisar media, PostgreSQL, índice y recursos de rollback. Los smokes largos y la observación no forman parte del tiempo hasta el primer HTTP válido. Si aparecen fallos, presión inesperada de recursos o discontinuidad de Players, revertir según sección 6.

Para comprobar los headers, usar un GET de solo lectura a `/api/player/time?bgv2_probe=<marca>` desde el origen aprobado y verificar que Edge reemplaza el ID enviado. Conservar el ID de la respuesta del probe, su marca hexadecimal de 32 caracteres y la ventana RFC3339Nano que contiene sus registros. Fijar previamente la IP cliente esperada según el mismo origen. El helper sellado exige una única línea de Edge del ID exacto, generación 0, ingreso público, upstream R3 y HTTP 200. También exige el GET correlacionado con la IP real en el backend. Conserva ambos streams raw en archivos privados con ID, ventana y hash del comando. Guarda los datos de correlación del probe en `public-proxy-probe.json` privado antes de buscar coincidencias. La vista temporal se ordena por timestamp de Docker y conserva el nombre del stream.

```sh
/usr/bin/python3 -B "$TOOL/phase3_evidence.py" \
  --edge-id "$EDGE_ID" --backend-id "$BACKEND_ID" \
  --since "$PROBE_SINCE" --until "$PROBE_UNTIL" \
  --request-id "$PROBE_REQUEST_ID" --marker "$PROBE_MARKER" \
  --expected-client "$PROBE_EXPECTED_CLIENT" --evidence-dir "$WORK"
```

`PHASE3_REAL_PLAYER_CONTINUITY_GATE` usa sólo tráfico real. El padre confiable entrega al adjudicador el pipe privado completo y la ventana de al menos 120 segundos, iniciada después de `OPENED_AT` y de la señal `ready`. Usar `--opened-at` en UTC de la misma máquina, posterior al mtime del marcador. El baseline debe tener menos de 120 s al abrir. `FINISHED_AT` queda cubierto por el último registro del colector. Los IDs y la captura sin redacción no salen del pipe. Los valores de salida son `0` evidencia válida, `2` gate negativo y `1` prueba incompleta. `2` o `1` obligan a ejecutar de inmediato la reversión de infraestructura de la sección 6 desde la sesión preparada. No continuar los controles de handoff tras ese resultado. Guardar `phase3-player.json` privado y hash.

```bash
{
  printf '%s\n' "$CAPTURE_READY"
  cat <&3
} | /usr/bin/python3 -B "$TOOL/player_observer.py" --state-dir "$STATE_DIR" \
    --postgres-id "$PG_ID" --key "$KEY_FILE" --receipt "$WORK/phase3-player.json" phase3-observe \
    --edge-id "$EDGE_ID" --baseline "$WORK/phase3-baseline.json" \
    --opened-at "$OPENED_AT" --started-at "$STARTED_AT" --finished-at "$FINISHED_AT" \
    --socket "$EDGE_RUN/admin.sock" --autosave "$EDGE_CFG/caddy/autosave.json"
wait "$CAPTURE_PID"
exec 3<&-
```

Lanzar el adjudicador tras el primer HTTP sin esperar a los smokes largos. Puede correr mientras esos controles se ejecutan, evitando bloquear el colector al llenarse el pipe. Para un baseline no vacío, exigir `ALL_BASELINE_CONTINUOUS` con un heartbeat HTTP 200 de cada pseudónimo HMAC del baseline observado tras la apertura, atribuido a generación 0, slot BLUE y upstream R3 exacto. Un dispositivo nuevo no sustituye a uno faltante. Los heartbeats anteriores a la apertura quedan excluidos. Ante un dispositivo faltante, errores incompatibles, 409/5xx o evidencia incompleta, ejecutar inmediatamente la sección 6. Con baseline vacío registrar `PASS_NO_ACTIVE_BASELINE` y evidencia física `UNVERIFIED`. Acreditar playback HTTP 201 únicamente cuando exista evidencia real. Registrar RAM/CPU/conexiones Docker y PostgreSQL de forma pasiva. El colector de este intento puede detenerse por ID cuando ya se verificó su salida y se guardó el recibo.

Si todos los controles pasan y se verificó la ruta de reversión, instalar y habilitar `bgv2-edge-ingress.service` con un `edge-supervisor.env` privado. Aprobar las rutas y el pin exactos del controlador final. Usar rutas absolutas sin espacios y confirmar que el nombre corresponde a `$EDGE_NAME`. Crear los archivos sólo durante la intervención autorizada, después de verificar que sus rutas todavía están libres. Conservar las rutas definitivas incluso después de que termine el shell. El supervisor reconoce un marcador de admisión ya abierto y conserva esa admisión mientras verifica la época de los procesos.

```sh
test ! -e /etc/systemd/system/bgv2-edge-ingress.service
test ! -e /etc/tecnocontrol-bgv2/edge-supervisor.env
install -d -m 0700 /etc/tecnocontrol-bgv2
cat > "$WORK/edge-supervisor.env" <<EOF
BGV2_TOOLING=$TOOL
BGV2_STATE_DIR=$STATE_DIR
BGV2_SOCKET=$EDGE_RUN/admin.sock
BGV2_AUTOSAVE=$EDGE_CFG/caddy/autosave.json
BGV2_BINDINGS=$BINDINGS
BGV2_KEY=$KEY_FILE
BGV2_EDGE_NAME=$EDGE_NAME
BGV2_LEGACY_MANIFEST=$WORK/legacy-blue-manifest.json
BGV2_LEGACY_SHA256=$LEGACY_PIN
EOF
install -m 0600 "$WORK/edge-supervisor.env" /etc/tecnocontrol-bgv2/edge-supervisor.env
install -m 0644 "$TOOL/bgv2-edge-ingress.service" /etc/systemd/system/bgv2-edge-ingress.service
systemctl daemon-reload
routectl_live status > "$WORK/blue-status-before-supervisor.json"
systemctl enable --now bgv2-edge-ingress.service
systemctl is-active --quiet bgv2-edge-ingress.service
```

El arranque posterior compara el `boot_id` del host y la época sellada. Con mismo boot y PID/`StartedAt` distinto se cierra ingreso y se exige `STOP_REATTEST_REQUIRED`. Con nuevo boot y la misma identidad estable, PostgreSQL exacto healthy y BLUE/BLUE coherente, `LEGACY_BLUE_REATTEST_AFTER_HOST_REBOOT` actualiza la época de forma durable antes de abrir. Si hay deriva estable, corrupción o ambigüedad, conservar el ingreso cerrado.

Escribir `phase3-final.json` en directorio privado, con `phase3=PASS`, `active_api_slot=BLUE`, `active_frontend_slot=BLUE`, `generation=0`, IDs exactos e imágenes de roles, hash del manifest, épocas, hash de estado/config/autosave, identidad de PG/índice, huellas TLS, eventos monotónicos y UTC, downtime, recibos de backup y Players, política de contratos, resultado de continuidad, snapshot, capacidad observada y recursos de reversión. No incluir cuerpos, tokens, env ni IDs Player crudos. Confirmar que frontend histórico sigue intacto y detenido en la ventana acordada.

## 6. Reversión exacta a la topología histórica

Ejecutar `mark_phase3 T_ROLLBACK_DECISION` antes de toda acción. Si el supervisor V2 quedó instalado, ejecutar `systemctl disable --now bgv2-edge-ingress.service` y comprobar que quedó inactivo. Eso impide que reabra ingreso incluso tras reiniciar el host. Ejecutar `routectl_live close-ingress` bajo el lock de escritor. Si el socket no permite verificar cierre, detener sólo el Edge exacto para hacer imposible seguir sirviendo. Comprobar que el marcador ya no admite tráfico. Detener `$EDGE_ID` con `docker_local stop --time 3 "$EDGE_ID"`, verificar 80/443 liberados, cero escritores TLS y que el proceso esperado terminó. Si aún aparecen sus endpoints, desconectarlo de proxy e ingreso por ID. No tocar otros contenedores. Registrar `T_EDGE_RELEASED`.

Reconectar `$OLD_FRONTEND_ID` a `$INGRESS_NET` con su IP y aliases del snapshot inicial y a `$PROXY_NET` con `--ip 172.30.250.2` y sus aliases históricos. Confirmar que el contenedor sigue fijado al mismo digest, Caddyfile, `/config` histórico y volumen TLS `/data`. Arrancar el mismo contenedor con `docker_local start "$OLD_FRONTEND_ID"`. Registrar `T_R3_START_RETURN` y verificar las publicaciones exactas. Esperar el listener local TCP 443 del frontend histórico mediante `nc -z`, dentro de los 30 s medidos desde la decisión. Usar el presupuesto restante, con un máximo de 10 s, para un único GET público de la API con el mismo Host/SNI y certificado válido. Registrar `T_FIRST_R3_HTTP_200` sólo con HTTP 200. Verificar app, TLS, propiedad única del storage y puertos, `.2`, IP cliente real, backend original y PostgreSQL/media intactos. Comparar imágenes, red, aliases, hash canónico de `Mounts` y configuración del frontend restaurado con el snapshot. Su nuevo PID/`StartedAt` por stop/start es esperado. No recrear contenedores ni restaurar DB en la reversión normal. Retener Edge cerrado, el frontend-blue privado, estado y recibos para análisis. Escribir un recibo de rollback con los tiempos desde la decisión y el resultado real.

Para las reconexiones deliberadas de esta sección, conservar en el precheck `historical-frontend-compose-proof.json` en `$WORK` con modo `0600` y su SHA-256. Obtener el registro del Compose original y sus labels. Comprobar los hashes de los archivos histórico y overlays contra el precheck y cotejar `docker compose config --hash frontend` con el label de creación del contenedor. Registrar `compose_files_match_prestage` y `compose_hash_matches_container` a partir de esas verificaciones, `container_create_hash` del label y `container_declared_mac` y `container_network_mode` del inspect. En `endpoints`, incluir para cada red `name` con el nombre efectivo, `service_mac`, `endpoint_mac`, `endpoint_driver_opts`, `ipv4_address`, `ipv6_address` y `link_local_ips` del Compose resuelto. Usar `null` si el ajuste no está declarado. Excluir el resto del Compose resuelto y sus credenciales. Fijar `HISTORICAL_FRONTEND_COMPOSE_PROOF_SHA256` al hash registrado antes del handoff.

El contrato estable exige las redes e IDs, direcciones efectivas, prefijos, gateways, aliases y pertenencia del contenedor exactos. Conserva ajustes explícitos y campos desconocidos. En la reconexión declarada, `EndpointID` puede renovarse. Una MAC generada y `DriverOpts` ausente, nulo o vacío sólo admiten sus equivalencias cuando el Compose verificado demuestra la ausencia del ajuste. `IPAMConfig` puede incorporar la IPv4 pasada a `--ip` si coincide con la intención y con la IP efectiva. Conservar los cuatro campos raw. Para la admisión inicial y las comparaciones de una misma época se mantiene el guard de identidad correspondiente.

```bash
# OLD_INGRESS_ALIASES y OLD_PROXY_ALIASES son arrays del snapshot aprobado.
# Ejecutar después de bloquear la reapertura del supervisor y del Edge.
connect_historical_endpoint() {
  network=$1; ip=$2; shift 2
  args=(--ip "$ip")
  for alias in "$@"; do args+=(--alias "$alias"); done
  docker_local network connect "${args[@]}" "$network" "$OLD_FRONTEND_ID"
}
require_free_ip "$INGRESS_NET" "$OLD_INGRESS_IP"
require_free_ip "$PROXY_NET" 172.30.250.2
connect_historical_endpoint "$INGRESS_NET" "$OLD_INGRESS_IP" "${OLD_INGRESS_ALIASES[@]}"
connect_historical_endpoint "$PROXY_NET" 172.30.250.2 "${OLD_PROXY_ALIASES[@]}"
docker_local start "$OLD_FRONTEND_ID"
mark_phase3 T_R3_START_RETURN
test "$(port_bindings "$OLD_FRONTEND_ID")" = "$OLD_PORTS"
ROLLBACK_HTTP_BUDGET=$(/usr/bin/python3 -I -B - "$WORK/phase3-timeline.jsonl" "$OLD_FRONTEND_ID" <<'PY'
import json
import os
import subprocess
import sys
from time import monotonic_ns, sleep

with open(sys.argv[1], encoding="utf-8") as log:
    decisions = [event["monotonic_ns"] for line in log if (event := json.loads(line))["event"] == "T_ROLLBACK_DECISION"]
if len(decisions) != 1:
    raise SystemExit("ROLLBACK_DECISION_TIME_INVALID")
deadline = decisions[0] + 30_000_000_000
command = ["docker", "--host", "unix:///var/run/docker.sock", "exec", sys.argv[2],
           "nc", "-z", "-w", "1", "127.0.0.1", "443"]
env = {key: value for key, value in os.environ.items() if not key.startswith("DOCKER_")}
while (remaining := (deadline - monotonic_ns()) / 1_000_000_000) > 0:
    try:
        ready = subprocess.run(command, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               timeout=min(1.5, remaining), check=False).returncode == 0
    except subprocess.TimeoutExpired:
        ready = False
    if ready:
        # Round down to milliseconds to keep curl inside the remaining budget.
        milliseconds = min(10_000, (deadline - monotonic_ns()) // 1_000_000)
        if milliseconds > 0:
            print(f"{milliseconds / 1000:.3f}")
            break
    sleep(0.1)
else:
    raise SystemExit("ROLLBACK_R3_TCP_READINESS_DEADLINE")
PY
)
first_public_200 "$ROLLBACK_HTTP_BUDGET"
mark_phase3 T_FIRST_R3_HTTP_200
```

Después del primer HTTP 200, atestar la red reatada y la identidad histórica con los bindings y manifiestos sellados. El comando escribe únicamente `rollback-network.json` privado en el directorio del intento. Completar también las verificaciones públicas de TLS/Host/SNI y proxy antes de registrar el resultado final de rollback.

```bash
/usr/bin/python3 -I -B - "$TOOL" "$WORK" "$BINDINGS" "$LEGACY_PIN" "$LEGACY_FRONTEND_PIN" \
  "$OLD_FRONTEND_NAME" "$INGRESS_NET" "$OLD_INGRESS_IP" "$PROXY_NET" \
  "$HISTORICAL_FRONTEND_COMPOSE_PROOF_SHA256" <<'PY'
from hashlib import sha256
from pathlib import Path
import sys

tool, work, bindings, backend_pin, frontend_pin, frontend_name, ingress, ingress_ip, proxy, proof_pin = sys.argv[1:]
sys.path.insert(0, str(Path(tool).resolve()))
from gates import DockerProbe
from release_state import canonical, decode, read_private, require, write_private_atomic

work = Path(work)
compose_proof = read_private(work / "historical-frontend-compose-proof.json")
require(sha256(compose_proof).hexdigest() == proof_pin, "LEGACY_FRONTEND_RECONNECT_COMPOSE_PROOF_DRIFT")
probe = DockerProbe(decode(read_private(Path(bindings))), edge_name=frontend_name,
                    legacy_blue=decode(read_private(work / "legacy-blue-manifest.json")),
                    legacy_blue_sha256=backend_pin)
require(probe.legacy_frontend_blue_sha256 == frontend_pin, "LEGACY_FRONTEND_MANIFEST_APPROVAL_REQUIRED")
evidence = probe.verify_reconnected_legacy_frontend_source(
    compose_proof=decode(compose_proof), reconnected_ipv4={ingress: ingress_ip, proxy: "172.30.250.2"})
write_private_atomic(work / "rollback-network.json", canonical(evidence) + b"\n", replace=False)
print("ROLLBACK_NETWORK_ATTACHMENT_CONTRACT=PASS")
PY
```

Si no se recupera R3 dentro de 30 s, registrar el tiempo real y escalar conforme al plan de recuperación aprobado. El timeout no autoriza reintentos automáticos, cambios de producto, restauraciones de datos ni el uso de Phase 4.
