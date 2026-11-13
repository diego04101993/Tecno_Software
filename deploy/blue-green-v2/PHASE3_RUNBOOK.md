# Phase 3 de Blue-Green V2.2

**Plan de operación para una autorización posterior.** Prepararlo o aprobar V2.2 localmente no autoriza acceso, transferencia ni ejecución en producción. Phase 3 termina con `ACTIVE_API_SLOT=BLUE` y `ACTIVE_FRONTEND_SLOT=BLUE`. El backend R3 conserva su contenedor y redes. El frontend BLUE sirve la imagen R3 exacta. El frontend histórico se conserva para reversión. Phase 4 y Videowall GREEN quedan fuera de esta intervención.

## 1. Precheck y calificación de imágenes en solo lectura

Verificar el paquete V2.2 y su hash aprobado en un directorio de tooling separado de `/opt/tecnocontrol`. Ejecutar sólo después de autorización independiente, como root en el propio host Linux, con Python 3.12.3 y el socket Docker local. Antes de mutar Docker, fijar mediante inspección las identidades completas de los servicios `backend`, `frontend` y `postgres` en el proyecto Compose `tecnocontrol`, exactamente un contenedor running por rol. Comprobar salud R3/PostgreSQL, SHA del Caddyfile histórico, Git principal R3, dump histórico, volúmenes, redes, propietarios actuales de TCP 80/443 e índice 019 válido. El verificador 019 es sólo lectura. No ejecutar 019.

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
  > "$WORK/r3-qualification.json"
```

Esperar `R3_EXACT_IMAGE_QUALIFICATION`, `PASS`. Backend esperado `sha256:951cd0b55ce3b461c361cb9b9d83c29d08e32f938cff4eefbbfb842f58ab0832`. Frontend esperado `sha256:b2a1ae4a8515e9d1db4cb14744bba356311317c819a3a16b37c791d0257e905a`. El comando vuelve a leer los dos IDs vivos y registra `RepoDigests` si existen, labels OCI, plataforma, `Entrypoint`, `Cmd`, capas `RootFS`, referencia del contenedor, hashes de configuración y runtime. No imprime env values. El frontend privado requiere los labels OCI `org.opencontainers.image.revision` e `io.tecnocontrol.git.tree` que coincidan con el pin de release. Si los labels, la provenance, el `Cmd` vivo, el Caddyfile o cualquiera de los IDs no califican, **STOP antes del handoff**. El comando de backend esperado del guard incluye `app.main:app`, un worker y `--lifespan off`. Cualquier diferencia requiere revisión, sin corregirla recreando el backend. La ausencia de las imágenes R3 exactas en QA local no reemplaza esta etapa productiva.

Calificar en solo lectura cualquier MAC que se vaya a declarar como generada por Docker. Para el backend, tomar del contenedor exacto los labels `com.docker.compose.project.working_dir`, `com.docker.compose.project.config_files` y, cuando exista, `com.docker.compose.project.environment_file`. Usar el directorio histórico, los archivos en su orden y el entorno de creación aprobado para resolver `docker compose --project-name tecnocontrol --project-directory "$COMPOSE_WORKDIR" … config --format json`. El directorio Git sigue siendo `/opt/tecnocontrol`. Comprobar los hashes de los overlays y env contra la evidencia aprobada. Inspeccionar el JSON en memoria y conservar sólo hashes, nombres de red y el dictamen sobre MAC. No guardar ni mostrar el JSON completo, ya que puede contener credenciales.

Para cada endpoint que se pretenda declarar generado, cotejar el nombre de red efectivo en `networks` con el nombre e ID de red inspeccionados. Confirmar que ni `services.backend.mac_address` ni `services.backend.networks.<red>.mac_address` se configuraron y que el historial aprobado de creación y conexión de ese endpoint descarta un override posterior. `NetworkSettings.Networks.<red>.MacAddress` por sí solo sólo muestra el valor operativo. Si existe una MAC explícita, falta algún archivo o registro, la interpolación no se puede reconstruir o la correspondencia de redes es ambigua, mantener la comparación exacta para ese endpoint. No reconectar ni recrear servicios para obtener evidencia.

Docker puede regenerar una MAC asignada por el bridge al recrear el endpoint. La API de inspección no expone por separado la MAC configurada de cada endpoint en ejecución. [Implementación de bridge en Moby 29.6.0](https://github.com/moby/moby/blob/docker-v29.6.0/daemon/libnetwork/drivers/bridge/bridge_linux.go#L1101-L1111), [contrato de inspección de Moby](https://github.com/moby/moby/pull/51203).

## 2. Evidencia protegida y backup antes de crear Edge

Conservar `WORK/r3-qualification.json`, modo `0600`. Nunca reutilizar un STATE anterior. Registrar las definiciones de red, nombres/IDs de contenedores y configuración efectiva aprobada, con secretos excluidos. Mantener una referencia al snapshot DigitalOcean aprobado y a los backups y recibos previos. No sobrescribir o limpiar esos artefactos. El snapshot y los backups se conservan para recuperación excepcional. La reversión de esta intervención actúa sobre el ingreso.

Usar exclusivamente `preflight` y `backup` del paquete OBS6.4 sellado aprobado, con `BACKUP_STATE` nuevo y absoluto. El SHA-256 local de su `SHA256SUMS` es `17d5d403305a0aaf734a8365d355b07a57e12da62c0826fb7dba543842336d5d`. Fijar `OBS64_SHA256SUMS_SHA256` a la aprobación independiente y verificarla antes de invocar el paquete. Estas fases validan R3/PG, disco, consistencia DB/media y el dump usando un descriptor fresco para cada operación. Conservar el recibo privado `backup-pass.json` y hashes, revisar su vigencia antes del handoff. Si el paquete o la verificación no están disponibles, STOP. `candidate`, `apply019`, `verify019` de la cadena de deployment y `cutover` quedan fuera de Phase 3. El índice se comprueba con la función read-only independiente `verify_index`, que vuelve a verificar PostgreSQL y el índice después de ejecutar el SQL sellado.

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
  /usr/bin/python3 -I -B deployment_phases.py preflight "$BACKUP_STATE"
  /usr/bin/python3 -I -B deployment_phases.py backup "$BACKUP_STATE"
)
```

Medir huella actual sin carga artificial. Registrar RAM y CPU del host, conexiones PostgreSQL, disco y Docker. Comparar con la proyección aprobada de Phase 3 para PostgreSQL, backend R3, frontend-blue, Edge y frontend histórico retenido. La topología desechable de un backend midió un pico combinado de 550387712 bytes con cuatro CPU. La medición productiva se obtiene en el host durante Phase 3. La calificación simultánea de dos backends corresponde a `PHASE4_DUAL_SLOT_CAPACITY=REQUIRES_LIVE_QUALIFICATION`. Los dos timeouts de transporte GREEN, código curl 28, y el control posterior de 6.344 s quedan en `PHASE4_GREEN_STABILITY_FOLLOWUP`. La causa permanece indeterminada.

## 3. Preparación con R3 todavía público

Mantener el frontend histórico atendiendo 80/443 durante todo este apartado. Preparar `EDGE_CFG` y `EDGE_RUN` separados del `/config` histórico, propiedad root `0700`, con `EDGE_CFG/caddy` también `0700`. Los mounts aprobados de Edge son `/config` propio, `/run/bgv2` privado y los dos archivos de bootstrap en solo lectura. Montar el TLS persistente de R3 en `/data`. No seleccionar ese storage en la configuración privada. La función `preview_config()` utiliza sólo `/config/preview-tls` con CA local desechable, sin servidores públicos ni escritura en el TLS histórico. Verificar fingerprints de certificados existentes por medio del antiguo propietario y conservar la huella antes de transferir. La configuración pública usa la política TLS aprobada y el storage persistente `/data`, sin `tls internal`. Comparar ambos dominios y la cadena pública antes de detener R3. Fijar `BACKEND_PROXY_IP` al valor leído de `NetworkSettings.Networks` del backend exacto, `PROXY_NET` e `INGRESS_NET` a los dos nombres de red verificados y `TLS_DATA_VOLUME` al nombre de volumen observado. Registrar el ID y los aliases de cada endpoint histórico para poder restaurarlo.

Crear `frontend-blue` sólo desde `$R3_FRONTEND_IMAGE`, el ID completo calificado, con los labels V2 requeridos, IP privada verificada libre y alias exclusivo. Permanece vivo hasta el final. `R3_FRONTEND_IMAGE` se obtiene del resultado PASS, nunca de `latest`. `EDGE_IMAGE` se fija a la imagen Caddy 2.11.4 sellada, local y aprobada. Las rutas de frontend son `/srv` con `Caddyfile.slot`, `admin off`, tmpfs de sólo lectura para `/data` y `/config`, sin publish ni red de base de datos.

```sh
docker_local create --pull=never --name "$FRONTEND_BLUE_NAME" --restart unless-stopped \
  --network "$PROXY_NET" --ip "$FRONTEND_BLUE_IP" --network-alias frontend-blue \
  --label io.tecnocontrol.bgv2.slot=BLUE --label io.tecnocontrol.bgv2.role=frontend \
  --tmpfs /config:ro --tmpfs /data:ro \
  --mount "type=bind,source=$TOOL/Caddyfile.slot,target=/etc/caddy/Caddyfile.bgv2,readonly" \
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

El supervisor host sigue detenido en la preparación. Capturar la adopción legacy con el nombre del frontend histórico como único peer de confianza de `.2`, `postgres_id` igual a `PG_ID` en `api:BLUE` y el backend exacto como endpoint. `BINDINGS` es privado, con las dos entradas `api:BLUE` y `frontend:BLUE`. Cada una lleva `name`, `network`, `ip`, `upstream`, `image_id`, `revision` y `tree`. La API lleva también `proxy_ip`, `uploads_volume` y `postgres_id`. El frontend lleva `frontend_config_sha256` del archivo `Caddyfile.slot`. Mantener `LEGACY_PIN` como el SHA256 del payload del manifiesto, revisado y aprobado por un canal separado. El SHA de los bytes del fichero puede registrarse por separado. La fase de handoff nunca reconecta el backend.

`generated_mac_networks` es opcional por rol. Contiene una lista JSON ordenada y sin duplicados con los nombres Docker exactos de las redes cuya MAC generada quedó demostrada. En `api:BLUE`, incluir sólo los endpoints aprobados en el precheck. En `frontend:BLUE`, el único nombre elegible es el de `$PROXY_NET`, si la creación y conexión aprobadas arriba se ejecutaron sin una opción MAC. Omitir el campo si la lista queda vacía. Fijar esas listas antes de capturar el manifiesto y confirmar BLUE/BLUE. La identidad estable vincula la lista y mantiene el resto de los campos de cada red. La época conserva también la MAC operativa observada. Así, con el mismo boot una MAC cambiada sigue siendo deriva y después de un reboot sólo se puede readmitir una MAC regenerada para un endpoint aprobado expresamente. Una MAC fija permanece fijada al valor original.

```sh
/usr/bin/python3 -B "$TOOL/legacy_blue.py" --bindings "$BINDINGS" \
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
  peer=$1; shift
  /usr/bin/python3 -B "$TOOL/routectl.py" --state-dir "$STATE_DIR" \
    --socket "$EDGE_RUN/admin.sock" --autosave "$EDGE_CFG/caddy/autosave.json" \
    --bindings "$BINDINGS" --key "$KEY_FILE" --edge-name "$peer" \
    --legacy-blue-manifest "$WORK/legacy-blue-manifest.json" --legacy-blue-sha256 "$LEGACY_PIN" "$@"
}
routectl_pre() { routectl_as "$OLD_FRONTEND_NAME" "$@"; }
routectl_live() { routectl_as "$EDGE_NAME" "$@"; }
routectl_pre preview "$WORK/sealed-edge.json"
```

Verificar Host/SNI privados a través del mismo Edge y de la CA de `/config/preview-tls`. La IP temporal del Edge no amplía `FORWARDED_ALLOW_IPS`. Antes del handoff comprobar la política de sanitización del proxy y que el antiguo propietario mantiene la identidad de confianza. En la primera validación pública comprobar el cliente real mediante el nuevo propietario `.2`. Seleccionar en PostgreSQL mediante transacción read-only uno o más canales normales, modo de dispositivo SINGLE, una salida habilitada, fuera de videowall y sin autoridad `hardware-single-input`. Escribir sólo los UUID en `channels.json` privado y no incluirlos en recibos finales. `reviewed-checks.json` fija `qualification_sha256` (SHA256 de JSON canónico), `schema_unchanged=true`, `media_unchanged=true`, `phase3_infra_capacity=true`, `pool=ISOLATED_NOT_APPLICABLE` y `contracts` con `windows_legacy=true`, `android_normal=true`. Cada valor se sustenta con el backup/precheck o la evidencia aprobada de plataforma existente de la release R3 exacta. Si falta esa evidencia, STOP. No requerir node-slice ni instrumentar el pool productivo.

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

El signer abre un prompt oculto para un JWT administrativo existente. No pegarlo en shell, logs ni archivos. Verifica API privada, `/api/channels/` ≤3.000 s, runtime 36+16 con cuatro clientes y timeout 10 s, frontend un GET raíz, assets y estabilidad de ambos procesos. No genera login ni tráfico Player. `initialize` confirma generación 0 `BLUE/BLUE` con ingreso cerrado. Probar que el público sigue en R3, el TLS histórico mantiene su huella y ningún listener público de Edge acepta la aplicación. Registrar `T_BLUE_BLUE_COMMITTED` ahora, fuera de la ventana de indisponibilidad. Revalidar backup y evidencia de reversión. Autorizar la entrada a ventana sólo cuando los 10 ensayos y las 10 reversiones estén calificados con máximos ≤30 s.

Tomar baseline poco antes de detener el histórico. El recibo liga el PostgreSQL y backend exactos y la generación 0. El controlador de observación inicia el colector antes de habilitar ingreso para observar los SYN. El colector está aislado en el namespace de Edge con filtro bloqueado por IP/puerto, permisos mínimos y un pipe privado, sin log ni spool del contenido. Durante el intercambio de red Edge recibe una interfaz/epoch de endpoint nueva. El padre debe arrancar o vincular el colector al endpoint `.2` final y comprobar su señal `ready` antes de abrir el marcador. Si eso no puede sostener el máximo calificado, STOP y revertir. Mantener esta preparación acotada y no fabricar tráfico de prueba. [Operación del observador](PLAYER_OBSERVER.md)

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
/usr/bin/python3 -B "$TOOL/player_observer.py" --state-dir "$STATE_DIR" \
  --postgres-id "$PG_ID" --key "$KEY_FILE" --receipt "$WORK/phase3-baseline.json" phase3-baseline
```

## 4. Ventana de traspaso

El único cambio de dueño es frontend histórico → Edge. Mantener backend, PostgreSQL, uploads, frontend-blue y el proceso Edge ya validado. Congelar los comandos en la intervención y registrar eventos UTC y monotónicos. Si falta una comprobación o excede el presupuesto de 30 s hasta el primer HTTP válido, pasar inmediatamente a la sección 6. No ocultar fallos ni recalcular el inicio. No ejecutar `compose up`, `restart` de Edge ni ningún smoke largo dentro de la ventana.

1. Confirmar identidad histórica final y registrar `T_R3_STOP_COMMAND` justo antes de `docker_local stop --time 3 "$OLD_FRONTEND_ID"`. El supervisor V2.2 aún está detenido.
2. Confirmar que el frontend histórico está detenido. Si Docker todavía lista sus endpoints, desconectarlo de `$PROXY_NET` y `$INGRESS_NET`, sólo con ese ID. Verificar 80/443 libres, endpoints antiguos sin dueño y cero escritores TLS. Registrar `T_80_443_RELEASED`.
3. Soltar el endpoint proxy temporal del mismo `$EDGE_ID`. Ejecutar `docker_local network connect --ip 172.30.250.2 "$PROXY_NET" "$EDGE_ID"`. Verificar peer exacto `.2`, backend en su endpoint y PID/StartedAt de Edge sin cambio. Registrar `T_PROXY_IDENTITY_READY`.
4. Ejecutar `docker_local network connect --ip "$OLD_INGRESS_IP" "$INGRESS_NET" "$EDGE_ID"` con la IP histórica fresca. Docker activa los bindings 80/443 ya fijados. Verificar IPs/familias/puertos equivalentes y ningún otro publicador. Registrar `T_EDGE_OWNS_PORTS`.
5. Confirmar que el histórico no está running, que sólo el mismo Edge tiene derecho de escritura en `/data` y que el storage de configuración antiguo sigue separado. Registrar `T_TLS_WRITER_READY`. Armar el colector ya preparado con la interfaz proxy final. El tiempo hasta la señal `ready` forma parte de la ventana y se mide como `T_PLAYER_CAPTURE_READY`.
6. Ejecutar `routectl_live recover-ingress`. Con el marcador cerrado, descarta el preview temporal cuando corresponde, preserva el proceso Caddy y reconcilia la configuración pública sellada con el nuevo peer. Verifica state BLUE/BLUE, configuración efectiva exacta y autosave persistido. Registrar `T_BLUE_BLUE_RECONCILED`, `T_INGRESS_READY` y `OPENED_AT` UTC tras la apertura. Conservar el recibo. La generación confirmada sigue en `0`.
7. Hacer un único GET público HTTPS `/` en la API, con Host/SNI canónicos y verificación real de certificado. Usar el origen autorizado, la IP pública observada y el comando de abajo. Registrar `T_FIRST_PUBLIC_HTTP_200` sólo al obtener HTTP 200 válido. Si el primer intento falla, revertir. Guardar diferencia desde `T_R3_STOP_COMMAND`.

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

test "$(port_bindings "$OLD_FRONTEND_ID")" = "$OLD_PORTS"
test "$(process_identity "$EDGE_ID")" = "$EDGE_PROCESS"
test "$(process_identity "$BACKEND_ID")" = "$BACKEND_PROCESS"
mark_phase3 T_R3_STOP_COMMAND
docker_local stop --time 3 "$OLD_FRONTEND_ID"
test "$(docker_local container inspect --format '{{.State.Running}}' "$OLD_FRONTEND_ID")" = false
release_endpoint "$PROXY_NET" "$OLD_FRONTEND_ID"
release_endpoint "$INGRESS_NET" "$OLD_FRONTEND_ID"
require_free_ip "$PROXY_NET" 172.30.250.2
require_free_ip "$INGRESS_NET" "$OLD_INGRESS_IP"
test -z "$(ss -H -ltn '( sport = :80 or sport = :443 )')"
mark_phase3 T_80_443_RELEASED
docker_local network disconnect "$PROXY_NET" "$EDGE_ID"
docker_local network connect --ip 172.30.250.2 "$PROXY_NET" "$EDGE_ID"
test "$(docker_local container inspect --format "{{(index .NetworkSettings.Networks \"$PROXY_NET\").IPAddress}}" "$EDGE_ID")" = 172.30.250.2
test "$(process_identity "$EDGE_ID")" = "$EDGE_PROCESS"
test "$(process_identity "$BACKEND_ID")" = "$BACKEND_PROCESS"
mark_phase3 T_PROXY_IDENTITY_READY
docker_local network connect --ip "$OLD_INGRESS_IP" "$INGRESS_NET" "$EDGE_ID"
test "$(port_bindings "$EDGE_ID")" = "$OLD_PORTS"
mark_phase3 T_EDGE_OWNS_PORTS
```

Comprobar los peers y el dueño TLS contra el snapshot. Continuar con el mismo shell Bash root, sin xtrace.

```bash
mark_phase3 T_TLS_WRITER_READY
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
  docker_local exec "$COLLECTOR_ID" python -I -B /observe.py \
    "$EDGE_PROXY_IF" 172.30.250.2 "$BACKEND_PROXY_IP" 165
}
CAPTURE_PID=$PHASE3_CAPTURE_PID
exec 3<&"${PHASE3_CAPTURE[0]}"
IFS= read -r -t 5 CAPTURE_READY <&3
printf '%s\n' "$CAPTURE_READY" | /usr/bin/python3 -I -B -c \
  'import json,sys; event=json.load(sys.stdin); sys.exit(not (set(event)=={"type","started_ns"} and event["type"]=="ready" and type(event["started_ns"]) is int and event["started_ns"]>0))'
mark_phase3 T_PLAYER_CAPTURE_READY
routectl_live recover-ingress > "$WORK/ingress-recovery.json"
OPENED_AT=$(date -u +%Y-%m-%dT%H:%M:%S.%NZ)
mark_phase3 T_BLUE_BLUE_RECONCILED
mark_phase3 T_INGRESS_READY
STARTED_AT=$OPENED_AT
FINISHED_AT=$(date -u -d "$STARTED_AT + 120 seconds" +%Y-%m-%dT%H:%M:%S.%NZ)
first_public_200
mark_phase3 T_FIRST_PUBLIC_HTTP_200
```

## 5. Verificación después del primer HTTP

Comprobar público 80→443, TLS/Host/SNI, app/índice/assets, API y media de lectura. Verificar IP cliente real y sanitización, cotejar los fingerprints públicos de ambos hosts con los previos, y confirmar `routectl_live status` en generación 0 BLUE/BLUE. Verificar otra vez la imagen R3 exacta del frontend privado y la identidad estable/época del backend R3. Revisar media, PostgreSQL, índice y recursos de rollback. Los smokes largos y la observación no forman parte del tiempo hasta el primer HTTP válido. Si aparecen fallos, presión inesperada de recursos o discontinuidad de Players, revertir según sección 6.

El padre confiable entrega al adjudicador el pipe privado completo y la ventana de al menos 120 segundos, iniciada después de `OPENED_AT` y de la señal `ready`. Usar `--opened-at` en UTC de la misma máquina, posterior al mtime del marcador. El baseline debe tener menos de 120 s al abrir. `FINISHED_AT` queda cubierto por el último registro del colector. Los IDs y la captura sin redacción no salen del pipe. Los valores de salida son `0` evidencia válida, `2` gate negativo y `1` prueba incompleta. `2` o `1` implican rollback del ingreso. No hay cambio a GREEN. Guardar `phase3-player.json` privado y hash.

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

Lanzar el adjudicador tras el primer HTTP sin esperar a los smokes largos. Puede correr mientras esos controles se ejecutan, evitando bloquear el colector al llenarse el pipe. Verificar continuidad de todos los `BASELINE_ACTIVE` vía BLUE. Con baseline vacío registrar que no hubo evidencia física. Playback sólo se acredita si existe evidencia real. Registrar RAM/CPU/conexiones Docker y PostgreSQL de forma pasiva. El colector de este intento puede detenerse por ID cuando ya se verificó su salida y se guardó el recibo.

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

Escribir `phase3-final.json` en directorio privado, con `phase3=PASS`, `active_api_slot=BLUE`, `active_frontend_slot=BLUE`, `generation=0`, IDs exactos e imágenes de roles, hash del manifest, épocas, hash de estado/config/autosave, identidad de PG/índice, huellas TLS, eventos monotónicos y UTC, downtime, recibos de backup y Players, snapshot, capacidad observada y recursos de reversión. No incluir cuerpos, tokens, env ni IDs Player crudos. Confirmar que frontend histórico sigue intacto y detenido en la ventana acordada.

## 6. Reversión exacta a la topología histórica

Ejecutar `mark_phase3 T_ROLLBACK_DECISION` antes de toda acción. Si el servicio V2.2 quedó instalado, ejecutar `systemctl disable --now bgv2-edge-ingress.service` y comprobar que quedó inactivo. Eso impide que reabra ingreso incluso tras reiniciar el host. Ejecutar `routectl_live close-ingress` bajo el lock de escritor. Si el socket no permite verificar cierre, detener sólo el Edge exacto para hacer imposible seguir sirviendo. Comprobar que el marcador ya no admite tráfico. Detener `$EDGE_ID` con `docker_local stop --time 3 "$EDGE_ID"`, verificar 80/443 liberados, cero escritores TLS y que el proceso esperado terminó. Si aún aparecen sus endpoints, desconectarlo de proxy e ingreso por ID. No tocar otros contenedores. Registrar `T_EDGE_RELEASED`.

Reconectar `$OLD_FRONTEND_ID` a `$INGRESS_NET` con su IP y aliases del snapshot inicial y a `$PROXY_NET` con `--ip 172.30.250.2` y sus aliases históricos. Confirmar que el contenedor sigue fijado al mismo digest, Caddyfile, `/config` histórico y volumen TLS `/data`. Arrancar el mismo contenedor con `docker_local start "$OLD_FRONTEND_ID"`. Registrar `T_R3_START_RETURN` y verificar las publicaciones exactas. Esperar el listener local TCP 443 del frontend histórico mediante `nc -z`, dentro de los 30 s medidos desde la decisión. Usar el presupuesto restante, con un máximo de 10 s, para un único GET público de la API con el mismo Host/SNI y certificado válido. Registrar `T_FIRST_R3_HTTP_200` sólo con HTTP 200. Verificar app, TLS, propiedad única del storage y puertos, `.2`, IP cliente real, backend original y PostgreSQL/media intactos. Comparar imágenes, red, aliases, mounts y configuración del frontend restaurado con el snapshot. Su nuevo PID/`StartedAt` por stop/start es esperado. No recrear contenedores ni restaurar DB en la reversión normal. Retener Edge cerrado, el frontend-blue privado, estado y recibos para análisis. Escribir un recibo de rollback con los tiempos desde la decisión y el resultado real.

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

Si no se recupera R3 dentro de 30 s, registrar el tiempo real y escalar conforme al plan de recuperación aprobado. El timeout no autoriza reintentos automáticos, cambios de producto, restauraciones de datos ni el uso de Phase 4.
