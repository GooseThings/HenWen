#!/bin/bash
# HenWen browser-transmitter spike — rollback.
# Restores the config files apply.sh backed up (most recent run by default,
# or pass a backup dir). Already-loaded Asterisk modules stay resident until
# Asterisk next restarts — harmless, nothing references them once the config
# is restored — so this deliberately does not restart the live repeater.
set -euo pipefail
SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../apache-common.sh
. "$SPIKE_DIR/../apache-common.sh"
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

BACKUP_DIR="${1:-$(cat /root/henwen-browsertx-last-backup 2>/dev/null || true)}"
[ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ] || { echo "Backup dir not found: '$BACKUP_DIR'"; exit 1; }

echo "== Restoring from $BACKUP_DIR"
cp "$BACKUP_DIR/modules.conf" /etc/asterisk/modules.conf
cp "$BACKUP_DIR/http.conf"    /etc/asterisk/http.conf
cp "$BACKUP_DIR/pjsip.conf"   /etc/asterisk/pjsip.conf

# apply.sh patches every HenWen Apache vhost henwen_discover_vhosts() finds
# (however many there are, whatever they're named) -- restore all of them
# via the manifest apply.sh wrote alongside the backups. Older backup dirs
# (from before this hardening) have no manifest, since apply.sh used to
# only ever touch one of two hardcoded filenames -- fall back to that same
# two-name restore for those so a pre-upgrade backup dir still rolls back
# correctly.
RESTORED_VHOSTS=()
mapfile -t RESTORED_VHOSTS < <(henwen_restore_vhosts_from_manifest "$BACKUP_DIR" 2>/dev/null || true)
if [ "${#RESTORED_VHOSTS[@]}" -eq 0 ]; then
  for name in henwen-ssl.conf henwen.conf; do
    if [ -f "$BACKUP_DIR/$name" ]; then
      cp "$BACKUP_DIR/$name" "/etc/apache2/sites-enabled/$name"
      RESTORED_VHOSTS+=("/etc/apache2/sites-enabled/$name")
      break
    fi
  done
fi
if [ "${#RESTORED_VHOSTS[@]}" -eq 0 ]; then
  echo "   NOTE: no backed-up Apache vhost found in $BACKUP_DIR — nothing to restore there."
else
  echo "   Restored vhost(s):"
  printf '     %s\n' "${RESTORED_VHOSTS[@]}"
fi

[ -f "$BACKUP_DIR/rtp.conf" ] && cp "$BACKUP_DIR/rtp.conf" /etc/asterisk/rtp.conf
if [ -f "$BACKUP_DIR/custom-extensions.conf" ]; then
  cp "$BACKUP_DIR/custom-extensions.conf" /etc/asterisk/custom/extensions.conf
else
  rm -f /etc/asterisk/custom/extensions.conf
fi
rm -f /etc/asterisk/henwen-tx.secret

asterisk -rx "core reload" >/dev/null || true
apache2ctl configtest 2>&1 | grep -q "Syntax OK" && systemctl reload apache2
echo "Done. (PJSIP modules remain loaded until Asterisk's next natural restart.)"
