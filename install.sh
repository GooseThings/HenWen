#!/bin/bash
# ASL3-EZ - Installer
# https://www.github.com/GooseThings/ASL3-EZ/
# Run as root: sudo bash install.sh
set -e

INSTALL_DIR="/opt/ASL3-EZ"
SERVICE_NAME="ASL3-EZ"
PORT="${PORT:-5000}"

echo ""
echo "============================================"
echo "  ASL3-EZ AllStarLink 3 Node Manager"
echo "  Installer  -  by N8GMZ"
echo "============================================"
echo ""

# ── Root check ────────────────────────────────────────────
if [ "$EUID" -ne 0 ]; then
    echo "ERROR: Please run as root: sudo bash install.sh"
    exit 1
fi

# ── Python check ─────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    echo "[1/7] Installing Python 3..."
    apt-get install -y python3 python3-pip python3-venv python3-full
else
    echo "[1/7] Python 3 found: $(python3 --version)"
fi

apt-get install -y python3-venv python3-full 2>/dev/null || true

# ── Copy files ────────────────────────────────────────────
echo "[2/7] Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r . "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR"          # standard app dir: owner rwx, group rx, others rx
chmod +x "$INSTALL_DIR/"*.sh 2>/dev/null || true

# ── Virtual environment ───────────────────────────────────
echo "[3/7] Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet flask gunicorn flask-wtf flask-limiter

# ── rpt_backups directory ─────────────────────────────────
echo "[4/7] Creating backup directory..."
mkdir -p /etc/asterisk/rpt_backups
chown asterisk:asterisk /etc/asterisk/rpt_backups
chmod 750 /etc/asterisk/rpt_backups

# Fix ownership of the database so the service can write it as the asterisk user
if [ -f /etc/asterisk/asl3ez.db ]; then
    chown asterisk:asterisk /etc/asterisk/asl3ez.db
fi

# ── Verify rpt.conf accessible ────────────────────────────
echo "[5/7] Checking rpt.conf..."
if [ -f /etc/asterisk/rpt.conf ]; then
    echo "      Found: /etc/asterisk/rpt.conf"
    ls -la /etc/asterisk/rpt.conf
else
    echo "      WARNING: /etc/asterisk/rpt.conf not found."
    echo "      The editor will still start but rpt.conf must exist to edit."
fi

# ── Systemd service ───────────────────────────────────────
echo "[6/7] Installing systemd service ($SERVICE_NAME)..."

# Remove any old service under the previous name to avoid duplicates
if [ -f /etc/systemd/system/asl3-rpt-editor.service ]; then
    echo "      Removing old asl3-rpt-editor service..."
    systemctl stop asl3-rpt-editor 2>/dev/null || true
    systemctl disable asl3-rpt-editor 2>/dev/null || true
    rm -f /etc/systemd/system/asl3-rpt-editor.service
fi

cp "$INSTALL_DIR/ASL3-EZ.service" /etc/systemd/system/
systemctl daemon-reload

# ── Firewall ──────────────────────────────────────────────
echo "      Opening firewall port $PORT..."
if command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port=${PORT}/tcp 2>/dev/null && firewall-cmd --reload 2>/dev/null || true
elif command -v ufw &>/dev/null; then
    ufw allow ${PORT}/tcp 2>/dev/null || true
fi

# ── Start service ─────────────────────────────────────────
echo "[7/7] Enabling and starting $SERVICE_NAME..."
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    IP=$(hostname -I | awk '{print $1}')
    echo ""
    echo "============================================"
    echo "  Installation complete!"
    echo ""
    echo "  Open your browser:"
    echo "    http://${IP}:${PORT}"
    echo ""
    echo "  rpt.conf:  /etc/asterisk/rpt.conf"
    echo "  Backups:   /etc/asterisk/rpt_backups/"
    echo "  Logs:      journalctl -u $SERVICE_NAME -f"
    echo "============================================"
    echo ""
    echo "  Running AMI setup now..."
    bash "$INSTALL_DIR/ami-setup.sh" || true
else
    echo ""
    echo "WARNING: Service may not have started. Check:"
    echo "  journalctl -u $SERVICE_NAME -n 50"
    echo "  systemctl status $SERVICE_NAME"
fi
