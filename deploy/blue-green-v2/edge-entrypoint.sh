#!/bin/sh
set -eu
umask 077

for directory in /run/bgv2 /config /config/caddy; do
    test -d "$directory" && test ! -L "$directory"
    test "$(stat -c '%u:%a' "$directory")" = "0:700"
done

exec 9>>/run/bgv2/admin.sock.writer.lock
flock -xn 9
rm -f -- /run/bgv2/ingress-ready
if test -e /run/bgv2/admin.sock || test -L /run/bgv2/admin.sock; then
    test -S /run/bgv2/admin.sock && test ! -L /run/bgv2/admin.sock
    if curl --silent --max-time 1 --unix-socket /run/bgv2/admin.sock http://localhost/config/ >/dev/null; then
        exit 1
    fi
    rm -f -- /run/bgv2/admin.sock
fi
boot_id_file=$(mktemp /run/bgv2/.boot-id.XXXXXX)
cat /proc/sys/kernel/random/uuid > "$boot_id_file"
mv -f -- "$boot_id_file" /run/bgv2/boot-id
flock -u 9
exec 9>&-
exec caddy run --config /opt/bgv2/edge-bootstrap.json
