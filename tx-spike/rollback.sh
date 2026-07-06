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
cp "$BACKUP_DIR/henwen-ssl.conf" /etc/apache2/sites-enabled/henwen-ssl.conf
if [ -f "$BACKUP_DIR/custom-extensions.conf" ]; then
  cp "$BACKUP_DIR/custom-extensions.conf" /etc/asterisk/custom/extensions.conf
else
  rm -f /etc/asterisk/custom/extensions.conf
fi
rm -f /etc/asterisk/henwen-tx.secret

asterisk -rx "core reload" >/dev/null || true
apache2ctl configtest 2>&1 | grep -q "Syntax OK" && systemctl reload apache2
echo "Done. (PJSIP modules remain loaded until Asterisk's next natural restart.)"
