#!/bin/bash
# HenWen - Uninstaller
set -e

if [ "$EUID" -ne 0 ]; then
    echo "Please run as root: sudo bash uninstall.sh"
    exit 1
fi

echo ""
echo "============================================"
echo "  HenWen Uninstaller"
echo "============================================"
echo ""

echo "Stopping and disabling HenWen service..."
systemctl stop    HenWen 2>/dev/null || true
systemctl disable HenWen 2>/dev/null || true
rm -f /etc/systemd/system/HenWen.service

# Also clean up old service name if present
systemctl stop    asl3-rpt-editor 2>/dev/null || true
systemctl disable asl3-rpt-editor 2>/dev/null || true
rm -f /etc/systemd/system/asl3-rpt-editor.service

systemctl daemon-reload

echo "Removing installation directory /opt/HenWen..."
rm -rf /opt/HenWen

echo ""
echo "Uninstall complete."
echo "Your rpt.conf and backups in /etc/asterisk/ were NOT removed."
echo ""
