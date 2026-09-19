#!/bin/bash
# HenWen DVSwitch guided setup — rollback.
# Restores the ini files apply.sh backed up (most recent run by default, or
# pass a backup dir) and stops/disables the two units it enabled. Does NOT
# remove the rpt.conf bridge node stanza (app.py created it, not this
# script) or apt-remove dvswitch-server — the package and its other config
# are left in place in case the owner just wants to pause the bridge rather
# than fully uninstall it. Does NOT restart Asterisk.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

ANALOG_BRIDGE_INI="${ANALOG_BRIDGE_INI:-/opt/Analog_Bridge/Analog_Bridge.ini}"
MMDVM_BRIDGE_INI="${MMDVM_BRIDGE_INI:-/opt/MMDVM_Bridge/MMDVM_Bridge.ini}"
DVSWITCH_INI="${DVSWITCH_INI:-/opt/MMDVM_Bridge/DVSwitch.ini}"

BACKUP_DIR="${1:-$(cat /root/henwen-dvswitch-last-backup 2>/dev/null || true)}"
[ -n "$BACKUP_DIR" ] && [ -d "$BACKUP_DIR" ] || { echo "Backup dir not found: '$BACKUP_DIR'"; exit 1; }

echo "== Stopping DVSwitch bridge units"
systemctl disable --now analog_bridge.service 2>/dev/null || true
systemctl disable --now mmdvm_bridge.service 2>/dev/null || true
systemctl disable --now stfu.service 2>/dev/null || true

echo "== Restoring ini files from $BACKUP_DIR"
[ -f "$BACKUP_DIR/$(basename "$ANALOG_BRIDGE_INI")" ] && \
  cp "$BACKUP_DIR/$(basename "$ANALOG_BRIDGE_INI")" "$ANALOG_BRIDGE_INI"
[ -f "$BACKUP_DIR/$(basename "$MMDVM_BRIDGE_INI")" ] && \
  cp "$BACKUP_DIR/$(basename "$MMDVM_BRIDGE_INI")" "$MMDVM_BRIDGE_INI"
[ -f "$BACKUP_DIR/$(basename "$DVSWITCH_INI")" ] && \
  cp "$BACKUP_DIR/$(basename "$DVSWITCH_INI")" "$DVSWITCH_INI"

echo "Done. dvswitch-server package and the rpt.conf bridge node are left in place."
echo "To remove the package entirely: sudo apt-get remove dvswitch-server"
echo "To remove the bridge node from rpt.conf, edit it in the Manager raw editor (superuser) —"
echo "this script deliberately doesn't touch rpt.conf, since app.py owns that file."
