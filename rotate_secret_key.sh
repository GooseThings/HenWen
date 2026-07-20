#!/bin/bash
# HenWen - SECRET_KEY rotation helper
#
# HenWen.service runs as User=asterisk, which cannot write the root-owned
# unit file at /etc/systemd/system/HenWen.service directly. This script is
# the sole piece of code granted sudo rights to make that one edit — see
# install.sh's henwen-systemctl sudoers rule — invoked by app.py's
# /api/settings/secret_key route (superuser only) as a fallback when the
# direct write fails with PermissionError.
#
# The new key is read from stdin, never argv, so it never appears in `ps`
# output. Scope is deliberately narrow: this script does exactly one thing
# (rewrite the Environment="SECRET_KEY=..." line) and nothing else — it does
# not reload or restart anything; app.py already holds separate sudo rights
# for `systemctl daemon-reload` / `restart HenWen` and does that itself
# after this succeeds.
set -euo pipefail

SERVICE_NAME="${SERVICE_NAME:-HenWen}"
SERVICE_FILE_PATH="${SERVICE_FILE_PATH:-/etc/systemd/system/${SERVICE_NAME}.service}"
TAG="[HENWEN-SECRET-ROTATE]"

read -r NEW_KEY

# Strict allowlist charset: must match app.py's own validation. This is the
# only thing standing between an untrusted value and a raw regex substitution
# into a systemd unit file — a quote, newline, or '[' could otherwise inject
# an unrelated directive (e.g. a new ExecStart= or User=) into the file.
if [[ ! "$NEW_KEY" =~ ^[A-Za-z0-9_-]{16,128}$ ]]; then
    echo "$TAG ERROR: rejected key — must be 16-128 chars of [A-Za-z0-9_-]" >&2
    exit 1
fi

if [ ! -f "$SERVICE_FILE_PATH" ]; then
    echo "$TAG ERROR: service file not found: $SERVICE_FILE_PATH" >&2
    exit 1
fi

TMP_PATH=$(mktemp "$(dirname "$SERVICE_FILE_PATH")/.henwen_svc_XXXXXXXX")
trap 'rm -f "$TMP_PATH"' EXIT

NEW_LINE="Environment=\"SECRET_KEY=${NEW_KEY}\""
if grep -qE '^Environment="?SECRET_KEY=' "$SERVICE_FILE_PATH"; then
    sed -E "0,/^Environment=\"?SECRET_KEY=[^\"]*\"?[[:space:]]*$/s||${NEW_LINE}|" \
        "$SERVICE_FILE_PATH" > "$TMP_PATH"
else
    awk -v line="$NEW_LINE" '
        {print}
        !done && /^\[Service\]/ { print line; done=1 }
    ' "$SERVICE_FILE_PATH" > "$TMP_PATH"
fi

chown --reference="$SERVICE_FILE_PATH" "$TMP_PATH"
chmod --reference="$SERVICE_FILE_PATH" "$TMP_PATH"
mv "$TMP_PATH" "$SERVICE_FILE_PATH"
trap - EXIT

echo "$TAG SECRET_KEY updated in $SERVICE_FILE_PATH"
