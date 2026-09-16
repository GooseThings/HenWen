#!/bin/bash
# HenWen low-latency RX audio — rollback.
# Restores the Apache vhost(s) apply.sh backed up (most recent run by
# default, or pass a backup dir). Does not touch audio_ws_relay.py itself —
# app.py keeps spawning/supervising that process regardless (it's cheap idle
# and unrelated to whether Apache can reach it); this only removes the
# network path to it, and the Manager "RX Audio Path" setting should be
# switched back to Legacy separately if it had been changed.
set -euo pipefail
SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../apache-common.sh
. "$SPIKE_DIR/../apache-common.sh"
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

BACKUP_DIR="${1:-$(cat /root/henwen-ws-audio-last-backup 2>/dev/null || true)}"
[ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ] || { echo "Backup dir not found: '$BACKUP_DIR'"; exit 1; }

# apply.sh patches every HenWen Apache vhost henwen_discover_vhosts() finds
# -- restore all of them via the manifest it wrote alongside the backups.
# Older backup dirs (from before this hardening) have no manifest, since
# apply.sh used to only ever touch one of two hardcoded filenames -- fall
# back to that same two-name restore for those.
RESTORED=()
mapfile -t RESTORED < <(henwen_restore_vhosts_from_manifest "$BACKUP_DIR" 2>/dev/null || true)
if [ "${#RESTORED[@]}" -eq 0 ]; then
  for name in henwen-ssl.conf henwen.conf; do
    if [ -f "$BACKUP_DIR/$name" ]; then
      cp "$BACKUP_DIR/$name" "/etc/apache2/sites-enabled/$name"
      RESTORED+=("/etc/apache2/sites-enabled/$name")
      break
    fi
  done
fi
[ "${#RESTORED[@]}" -gt 0 ] || { echo "No backed-up vhost file found in $BACKUP_DIR"; exit 1; }

echo "== Restored:"
printf '     %s\n' "${RESTORED[@]}"
apache2ctl configtest 2>&1 | grep -q "Syntax OK" || { echo "Restored config failed configtest — investigate before reloading"; exit 1; }
systemctl reload apache2
echo "Done. /ws-audio is no longer proxied by Apache."
