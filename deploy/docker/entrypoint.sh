#!/bin/sh
set -u
umask 077

# Take the final sample while the native statistics socket still exists.
# The host collector and this helper share the same SQLite accounting lock.
shutdown() {
    trap '' TERM INT
    stopping=1
    /usr/local/sbin/zivpn-native-save-traffic || \
        echo 'Final traffic sample failed; inspect container logs.' >&2
    kill -TERM "$server_pid" 2>/dev/null || true
}

"$@" &
server_pid=$!
stopping=0
trap shutdown TERM INT
status=0
wait "$server_pid" || status=$?
# A trapped signal interrupts wait before the child has exited.
if [ "$stopping" -eq 1 ]; then
    status=0
    wait "$server_pid" || status=$?
fi
exit "$status"
