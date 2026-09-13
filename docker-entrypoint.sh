#!/bin/sh
# Run the app as PUID:PGID (Unraid convention: 99:100). Started as root only to fix
# ownership of /data, then privileges are dropped for good.
set -e

PUID="${PUID:-1000}"
PGID="${PGID:-1000}"

if [ "$(id -u)" = "0" ]; then
    mkdir -p "$TB_DATA_DIR"
    # Only chown when the owner differs — avoids walking a large backup tree on every start.
    if [ "$(stat -c %u:%g "$TB_DATA_DIR")" != "$PUID:$PGID" ]; then
        echo "Setting owner of $TB_DATA_DIR to $PUID:$PGID"
        chown -R "$PUID:$PGID" "$TB_DATA_DIR"
    fi
    exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi

# Already started unprivileged (e.g. docker run --user): just run.
exec "$@"
