#!/bin/bash
# HenWen - Automated updater
#
# Launched via `sudo systemd-run --unit=henwen-updater --collect
# /opt/HenWen/update.sh` from the Manager's "Launch Updater" button
# (superuser only, see /api/update/launch in app.py). Runs as its own
# transient systemd unit, deliberately outside HenWen.service's cgroup,
# so the `systemctl restart HenWen` at the end doesn't get killed along
# with the very service it's restarting.
#
# Fetches over plain HTTPS from the public repo rather than the
# SSH-based `origin` remote (git@github.com:...) — this script runs as
# root, which may have no SSH key/agent configured, and the repo is
# public, so there's nothing to authenticate.
set -e

REPO_URL="https://github.com/GooseThings/HenWen.git"
INSTALL_DIR="/opt/HenWen"
SERVICE_NAME="HenWen"
TAG="[HENWEN-UPDATE]"

cd "$INSTALL_DIR"

if [ ! -d .git ]; then
    echo "$TAG ERROR: $INSTALL_DIR is not a git checkout — cannot self-update." >&2
    exit 1
fi

BEFORE=$(git rev-parse HEAD)
echo "$TAG Current commit: $BEFORE"
echo "$TAG Fetching latest main from $REPO_URL..."
git fetch --quiet "$REPO_URL" main
AFTER=$(git rev-parse FETCH_HEAD)

if [ "$BEFORE" = "$AFTER" ]; then
    echo "$TAG Already up to date ($BEFORE)."
    exit 0
fi

echo "$TAG Updating $BEFORE -> $AFTER"
git reset --hard FETCH_HEAD

echo "$TAG Installing dependencies..."
"$INSTALL_DIR/venv/bin/pip" install --quiet -r "$INSTALL_DIR/requirements.txt"

echo "$TAG Verifying app.py..."
if ! "$INSTALL_DIR/venv/bin/python3" -m py_compile "$INSTALL_DIR/app.py"; then
    echo "$TAG ERROR: app.py failed to compile after update — rolling back to $BEFORE." >&2
    git reset --hard "$BEFORE"
    exit 1
fi

echo "$TAG Reloading systemd and restarting $SERVICE_NAME..."
systemctl daemon-reload
systemctl restart "$SERVICE_NAME"

echo "$TAG Update complete: now on $AFTER"
