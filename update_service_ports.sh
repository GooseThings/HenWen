#!/bin/bash
# HenWen - service-file port rotation helper
#
# Sibling to rotate_secret_key.sh: HenWen.service runs as User=asterisk,
# which cannot write the root-owned unit file at
# /etc/systemd/system/HenWen.service directly, so app.py's
# /api/settings/ports route (superuser only) falls back to this script when
# its own direct write hits PermissionError. Same design constraint as the
# secret-key helper applies here too — this independently re-validates and
# re-derives the substitutions rather than accepting precomputed file
# content from its caller, because accepting arbitrary content would let a
# compromised app process write anything (including a new ExecStart=/User=)
# into a root-owned unit file this same sudoers rule can then
# daemon-reload+restart into effect.
#
# manager.conf's AMI `port =` line is NOT handled here — that file is
# asterisk:asterisk and already writable directly by the service, so app.py
# never needs to fall back for it.
#
# Reads two lines from stdin: new flask_port (or blank to leave it alone),
# then new ami_port (or blank). Never argv, so values never appear in `ps`.
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-HenWen}"
SERVICE_FILE_PATH="${SERVICE_FILE_PATH:-/etc/systemd/system/${SERVICE_NAME}.service}"
PORT_MIN=1024
PORT_MAX=65535
TAG="[HENWEN-PORTS-ROTATE]"

read -r FLASK_PORT
read -r AMI_PORT

_valid_port() {
    local p="$1" label="$2"
    [ -z "$p" ] && return 0
    if [[ ! "$p" =~ ^[0-9]{1,5}$ ]] || [ "$p" -lt "$PORT_MIN" ] || [ "$p" -gt "$PORT_MAX" ]; then
        echo "$TAG ERROR: $label must be $PORT_MIN-$PORT_MAX, got: $p" >&2
        exit 1
    fi
    if [ "$p" -eq 8088 ]; then
        echo "$TAG ERROR: $label can't be 8088 (Asterisk's builtin HTTP/WS server)" >&2
        exit 1
    fi
}
_valid_port "$FLASK_PORT" "flask_port"
_valid_port "$AMI_PORT" "ami_port"

if [ -z "$FLASK_PORT" ] && [ -z "$AMI_PORT" ]; then
    echo "$TAG ERROR: nothing to change — both ports blank" >&2
    exit 1
fi
if [ -n "$FLASK_PORT" ] && [ -n "$AMI_PORT" ] && [ "$FLASK_PORT" = "$AMI_PORT" ]; then
    echo "$TAG ERROR: flask_port and ami_port can't be the same" >&2
    exit 1
fi

if [ ! -f "$SERVICE_FILE_PATH" ]; then
    echo "$TAG ERROR: service file not found: $SERVICE_FILE_PATH" >&2
    exit 1
fi

TMP_PATH=$(mktemp "$(dirname "$SERVICE_FILE_PATH")/.henwen_svc_XXXXXXXX")
trap 'rm -f "$TMP_PATH"' EXIT
cp "$SERVICE_FILE_PATH" "$TMP_PATH"

if [ -n "$FLASK_PORT" ]; then
    if ! grep -qE '^Environment="?PORT=[0-9]+"?[[:space:]]*$' "$TMP_PATH"; then
        echo "$TAG ERROR: PORT environment line not found in $SERVICE_FILE_PATH" >&2
        exit 1
    fi
    if ! grep -qE -- '--bind[[:space:]]+[^:[:space:]]*:[0-9]+' "$TMP_PATH"; then
        echo "$TAG ERROR: gunicorn --bind flag not found in $SERVICE_FILE_PATH" >&2
        exit 1
    fi
    sed -i -E "s/^Environment=\"?PORT=[0-9]+\"?[[:space:]]*\$/Environment=PORT=${FLASK_PORT}/" "$TMP_PATH"
    sed -i -E "s/(--bind[[:space:]]+[^:[:space:]]*:)[0-9]+/\1${FLASK_PORT}/" "$TMP_PATH"
fi

if [ -n "$AMI_PORT" ]; then
    if ! grep -qE '^Environment="?AMI_PORT=[0-9]+"?[[:space:]]*$' "$TMP_PATH"; then
        echo "$TAG ERROR: AMI_PORT environment line not found in $SERVICE_FILE_PATH" >&2
        exit 1
    fi
    sed -i -E "s/^Environment=\"?AMI_PORT=[0-9]+\"?[[:space:]]*\$/Environment=AMI_PORT=${AMI_PORT}/" "$TMP_PATH"
fi

chown --reference="$SERVICE_FILE_PATH" "$TMP_PATH"
chmod --reference="$SERVICE_FILE_PATH" "$TMP_PATH"
mv "$TMP_PATH" "$SERVICE_FILE_PATH"
trap - EXIT

echo "$TAG Ports updated in $SERVICE_FILE_PATH (flask=${FLASK_PORT:-unchanged}, ami=${AMI_PORT:-unchanged})"
