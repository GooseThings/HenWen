#!/bin/bash
# HenWen browser-transmitter spike — rollback.
# Restores the config files apply.sh backed up (most recent run by default,
# or pass a backup dir). Already-loaded Asterisk modules stay resident until
# Asterisk next restarts — harmless, nothing references them once the config
# is restored — so this deliberately does not restart the live repeater.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

BACKUP_DIR="${1:-$(cat /root/henwen-browsertx-last-backup 2>/dev/null || true)}"
[ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ] || { echo "Backup dir not found: '$BACKUP_DIR'"; exit 1; }

echo "== Restoring from $BACKUP_DIR"
cp "$BACKUP_DIR/modules.conf" /etc/asterisk/modules.conf
cp "$BACKUP_DIR/http.conf"    /etc/asterisk/http.conf
cp "$BACKUP_DIR/pjsip.conf"   /etc/asterisk/pjsip.conf
# apply.sh patches whichever of henwen-ssl.conf/henwen.conf was present at
# the time (see its own candidate-loop comment) -- restore whichever one it
# actually backed up, mirroring ws-audio/rollback.sh's identical loop.
RESTORED_VHOST=""
for name in henwen-ssl.conf henwen.conf; do
  if [ -f "$BACKUP_DIR/$name" ]; then
    cp "$BACKUP_DIR/$name" "/etc/apache2/sites-enabled/$name"
    RESTORED_VHOST="$name"
    break
  fi
done
[ -n "$RESTORED_VHOST" ] || echo "   NOTE: no backed-up Apache vhost found in $BACKUP_DIR — nothing to restore there."
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
