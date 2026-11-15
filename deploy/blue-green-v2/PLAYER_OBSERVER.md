# Observación pasiva de Players

El colector lee sólo el enlace HTTP/1.1 IPv4 entre Edge y la API candidata. El adjudicador asocia la captura con los logs de acceso de Edge y con los heartbeats confirmados por PostgreSQL. Ninguno hace peticiones Player, consulta hashes de tokens ni recibe credenciales Player. Los emuladores que generan tráfico en QA se ejecutan por separado.

## Límites de acceso

El proceso `player_capture.py` necesita Linux x86_64 y acceso al namespace de red de Edge. Ejecútalo en un proceso dedicado, con sistema de archivos en solo lectura, `no-new-privileges`, sólo `CAP_NET_RAW` y sin montar el socket de Docker, claves, credenciales, directorios de aplicación ni volúmenes de datos. `SO_ATTACH_FILTER` selecciona la interfaz y los dos extremos de la ruta aprobada, incluido TCP 8000. `SO_LOCK_FILTER` fija ese filtro. El proceso elimina todas las capacidades después del `bind` y restringe operaciones de red y escritura del socket mediante seccomp. No inicia conexiones ni produce pcap. El proceso sigue teniendo acceso en memoria a los paquetes del enlace permitido, incluidos encabezados y payloads. Sólo emite un identificador de solicitud generado por Edge, clase de endpoint, estado, tiempo y los dos campos mínimos de ACK por su pipe privado. No persistir ni copiar a logs el stdout del colector.

El adjudicador corre como parte del tooling autorizado, en el host del Engine local, con el socket Docker de control y la clave protegida. Sólo inspecciona identidades ya conocidas, lee los logs sanitizados de Edge y ejecuta `SELECT` dentro de `BEGIN READ ONLY` con rollback en PostgreSQL. No imprime filas ni respuestas. Conserva el recibo HMAC con referencias seudónimas y un archivo hermano privado `<receipt>.docker-logs.json` con los bytes de los logs sanitizados, separados por stream. Ese archivo registra el ID exacto, el intervalo y el hash del comando. La vista de adjudicación usa `docker container logs --timestamps` y ordena por timestamp con precisión de nanosegundos. La clave del firmante nunca entra al colector.

## Secuencia del controlador

1. Ejecutar `player_observer.py … baseline` antes del switch. Toma el baseline de los dispositivos activos elegibles del predecesor con heartbeat en los últimos 120 segundos, usando la hora de PostgreSQL. El recibo se liga a la generación anterior y a la identidad de la API candidata.
2. Iniciar `player_capture.py <interfaz> <ip_edge> <ip_api> <segundos>` **antes** del switch, desde un supervisor de confianza que controle sus descriptores. Usar los IP verificados de la identidad de red del slot. Mantener stdout en pipe privado no persistente. Confirmar el primer registro `ready`, sin perderlo al entregar la captura al adjudicador. No utilizar tee, archivo temporal, journald, `docker logs`, copia a pantalla ni una terminal compartida para ese flujo.
3. Cambiar sólo API con `routectl` y el recibo de baseline. Registrar el inicio UTC de la ventana a partir de la transición confirmada. Fijar el fin UTC al menos 120 segundos después. El tiempo del colector incluye la preparación previa y un margen para completar las respuestas en curso. Un padre supervisor puede retener únicamente las líneas acotadas en memoria o encadenar un pipe. Si alguno termina sin `done`, o hay drops, abortar la prueba. El colector requiere haber visto el SYN. Una solicitud pertinente sobre una conexión anterior sin captura válida produce falta de evidencia al contrastar con Edge.
4. Ejecutar `player_observer.py … observe --edge-id … --baseline … --started-at … --finished-at …` en el host, con el pipe completo como stdin. Revalida que el estado permanezca en la transición API-only e inspecciona las identidades de Edge y PostgreSQL antes y después. Correlaciona la generación, el destino y HTTP con la captura y une cada ACK con exactamente una fila de PostgreSQL. Los eventos anteriores al cambio y de la generación anterior quedan excluidos. HTTP 401 no acredita un heartbeat. HTTP 409 y 5xx fallan el gate. Un playback HTTP 201 aporta evidencia sólo si se observa. Una ausencia queda `UNVERIFIED`.
5. Entregar únicamente el recibo sellado al gate de frontend. El adjudicador devuelve 0 en PASS, 2 cuando conserva un recibo de rechazo y 1 ante una observación incompleta. Si la observación no produce recibo, la ventana pierde integridad o falta un dispositivo, detener promoción y aplicar el rollback de API del controlador, manteniendo el frontend en el predecesor. Un nuevo dispositivo y una repetición del mismo dispositivo no sustituyen un ID faltante.

El supervisor debe atar la ejecución a los IDs de Edge, PostgreSQL y slot de la intervención. Los parámetros de red y el pipe forman parte de esa autoridad. El adjudicador no debe aceptar un archivo o una cadena de evidencia aportados por un cliente. La prueba desechable usa un pipe privado desde `docker exec`, las imágenes ya selladas y el Engine local. Su resultado no afirma compatibilidad física de Players productivos.

## Continuidad durante la adopción inicial

Phase 3 usa el mismo colector y adjudicador con `phase3-baseline` y `phase3-observe`. La autoridad es la generación inicial `0` confirmada, API y frontend `BLUE`, y el endpoint IPv4 exacto del backend legacy. La toma de baseline ocurre con el estado preparado y el frontend histórico todavía sirviendo. Su vigencia máxima es 120 segundos hasta abrir el ingreso nuevo. El colector se inicia antes de abrir y la ventana observada comienza con el ingreso verificado, después de la transferencia. El recibo `PHASE3_PLAYER` acredita continuidad de BLUE y queda separado del recibo que autoriza una promoción de frontend.

El adjudicador verifica antes y después el mismo estado, epoch del backend, identidad saludable de PostgreSQL, configuración efectiva y autosave de Edge. Se aplica la misma ventana mínima de 120 segundos y la misma unión de ACK. Un baseline vacío produce `PASS_NO_ACTIVE_BASELINE` y evidencia física sin verificar. Un dispositivo faltante, un 409/5xx o evidencia incompleta requiere la reversión de infraestructura descrita en el runbook Phase 3.

## Evidencia al fallar

El CLI emite `PLAYER_OBSERVER_RESULT`, `PLAYER_OBSERVER_REASON`, `PLAYER_OBSERVER_STAGE`, `PLAYER_OBSERVER_EXIT_CODE` y `PLAYER_OBSERVER_EVIDENCE_PATH` en stderr. Primero intenta crear `<receipt>.failure.json` con modo `0600`, sin reemplazar archivos. Si falla, intenta un directorio temporal privado con un nombre nuevo. Si tampoco puede escribir allí, emite el JSON saneado en una línea de stderr que el supervisor puede preservar. Una terminación del proceso sin oportunidad de ejecutar código requiere evidencia del supervisor.

El diagnóstico registra el tipo de excepción y los marcos del tooling por archivo, función y línea, las etapas actual y anterior, y los conteos que ya se pudieron verificar. Registra UTC para correlación y tiempo monotónico para la duración del proceso. Los límites UTC del gate y el orden de los logs siguen formando parte de la prueba. La captura cruda, los argumentos, la clave, los IDs de Players y sus referencias HMAC quedan fuera del diagnóstico.

| Ruta de salida | Evidencia adicional antes de terminar |
| --- | --- |
| Argumentos inválidos, código 2 | Diagnóstico de argumentos sin repetir valores recibidos. |
| Estado, clave, identidad inicial, salud, transición o ingreso inválidos, código 1 | Causa y etapa. Se conserva el baseline existente. |
| Query del baseline, decodificación, HMAC, revalidación o escritura, código 1 | Causa y etapa. Si se generó un baseline en memoria, conserva su conteo. |
| Lectura del pipe, límite o fallo externo, código 1 | Diagnóstico y conteo de registros si terminó la lectura. El pipe permanece en memoria. |
| Ventana o colección de logs, timeout o escritura de evidencia, código 1 | Diagnóstico y metadatos de cada stream disponible. Los bytes de ambos streams se conservan en el archivo de logs ya previsto si se pudo escribir. |
| Firma del baseline, correlación de logs y captura, o unión de ACK con PostgreSQL, código 1 | Causa, etapa y conteo del baseline si quedó autenticado. |
| Revalidación, validación del fin o publicación del recibo, código 1 | Conteos adjudicados si están disponibles. La durabilidad del recibo se marca como no confirmada si falla la escritura. |
| Gate rechazado, código 2 | Recibo firmado de rechazo y diagnóstico privado con conteos. |

El recibo diagnóstico complementa el código de salida. El supervisor debe exigir una salida exitosa y el recibo HMAC válido antes de acreditar el gate.
