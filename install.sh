#!/bin/bash
# HenWen - Installer
# https://www.github.com/GooseThings/HenWen/
# Run as root: sudo bash install.sh
set -e

INSTALL_DIR="/opt/HenWen"
SERVICE_NAME="HenWen"
PORT="${PORT:-5000}"

echo ""
echo "============================================"
echo "  HenWen AllStarLink 3 Node Manager"
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
    echo "[1/8] Installing Python 3..."
    apt-get install -y python3 python3-pip python3-venv python3-full
else
    echo "[1/8] Python 3 found: $(python3 --version)"
fi

apt-get install -y python3-venv python3-full 2>/dev/null || true

# ── Copy files ────────────────────────────────────────────
echo "[2/8] Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r . "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR"          # standard app dir: owner rwx, group rx, others rx
chmod +x "$INSTALL_DIR/"*.sh 2>/dev/null || true

# ── Virtual environment ───────────────────────────────────
echo "[3/8] Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet flask gunicorn flask-wtf flask-limiter piper-tts

# ── rpt_backups directory ─────────────────────────────────
echo "[4/8] Creating backup directory..."
mkdir -p /etc/asterisk/rpt_backups
chown asterisk:asterisk /etc/asterisk/rpt_backups
chmod 750 /etc/asterisk/rpt_backups

# ── TTS voice model directory ──────────────────────────────
# Piper voice models (.onnx/.onnx.json), downloaded on first use of each
# voice from the Manager UI. Not under $INSTALL_DIR (root:root, unwritable
# by the asterisk user the service runs as) and not under Asterisk's own
# sounds directory (these are Piper assets, not Asterisk sound files).
mkdir -p /var/lib/asterisk/henwen_tts_voices
chown asterisk:asterisk /var/lib/asterisk/henwen_tts_voices
chmod 750 /var/lib/asterisk/henwen_tts_voices

# Fix ownership of the database so the service can write it as the asterisk user
if [ -f /etc/asterisk/henwen.db ]; then
    chown asterisk:asterisk /etc/asterisk/henwen.db
fi

# ── Verify rpt.conf accessible ────────────────────────────
echo "[5/8] Checking rpt.conf..."
if [ -f /etc/asterisk/rpt.conf ]; then
    echo "      Found: /etc/asterisk/rpt.conf"
    ls -la /etc/asterisk/rpt.conf
else
    echo "      WARNING: /etc/asterisk/rpt.conf not found."
    echo "      The editor will still start but rpt.conf must exist to edit."
fi

# ── Systemd service ───────────────────────────────────────
echo "[6/8] Installing systemd service ($SERVICE_NAME)..."

# Remove any old service under the previous name to avoid duplicates
if [ -f /etc/systemd/system/asl3-rpt-editor.service ]; then
    echo "      Removing old asl3-rpt-editor service..."
    systemctl stop asl3-rpt-editor 2>/dev/null || true
    systemctl disable asl3-rpt-editor 2>/dev/null || true
    rm -f /etc/systemd/system/asl3-rpt-editor.service
fi

cp "$INSTALL_DIR/HenWen.service" /etc/systemd/system/
systemctl daemon-reload

# ── Cap systemd journal size ──────────────────────────────
# HenWen logs to stdout, which journald persists to /var/log/journal.
# Without a limit journald defaults to ~10% of the disk; cap it so logs
# can never crowd the disk on a small node. Applies to ALL services, not
# just HenWen. Only created if absent, so an operator's tuned value or a
# pre-existing site policy is left untouched.
JOURNALD_CAP=/etc/systemd/journald.conf.d/99-henwen-cap.conf
if [ ! -f "$JOURNALD_CAP" ]; then
    echo "      Capping systemd journal size (SystemMaxUse=1G)..."
    mkdir -p /etc/systemd/journald.conf.d
    cat > "$JOURNALD_CAP" <<'JCONF'
# Cap total systemd journal disk usage so logs can't fill the disk.
# Applies to ALL services' journald data, not just HenWen. Remove or
# raise these to keep more history.
[Journal]
SystemMaxUse=1G
SystemKeepFree=2G
JCONF
    systemctl restart systemd-journald 2>/dev/null || true
else
    echo "      Journal cap already present ($JOURNALD_CAP) — leaving as-is."
fi

# ── Sudoers rule for privileged systemctl actions ─────────
# The service runs unprivileged as User=asterisk (see HenWen.service), but
# the Dashboard's "Restart Asterisk" button, secret-key rotation, and the
# "Launch Updater" button need to run `systemctl restart asterisk`,
# `systemctl restart HenWen`, `systemctl daemon-reload`, and (via
# systemd-run, so it survives outside HenWen.service's own cgroup)
# update.sh. Without this rule those actions fail with "Interactive
# authentication required" since there's no session for polkit to prompt.
# Scope is intentionally limited to these exact commands — do not broaden
# with wildcards. The updater rule only works if $INSTALL_DIR is itself a
# git checkout of the HenWen repo — update.sh no-ops with an error otherwise.
echo "[7/8] Installing sudoers rule for restart/reload/update actions..."
SUDOERS_FILE=/etc/sudoers.d/henwen-systemctl
SYSTEMCTL_BIN=$(command -v systemctl || echo /bin/systemctl)
SYSTEMD_RUN_BIN=$(command -v systemd-run || echo /usr/bin/systemd-run)
cat > "${SUDOERS_FILE}.tmp" <<EOF
# Installed by HenWen's install.sh. Lets the unprivileged service account
# restart the units it manages, reload systemd unit definitions, and
# launch the self-updater as its own transient unit.
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} daemon-reload
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart asterisk
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart ${SERVICE_NAME}
asterisk ALL=(root) NOPASSWD: ${SYSTEMD_RUN_BIN} --unit=henwen-updater --collect ${INSTALL_DIR}/update.sh
EOF
if visudo -c -f "${SUDOERS_FILE}.tmp" &>/dev/null; then
    chmod 440 "${SUDOERS_FILE}.tmp"
    mv "${SUDOERS_FILE}.tmp" "$SUDOERS_FILE"
    echo "      Installed $SUDOERS_FILE"
else
    echo "      WARNING: generated sudoers rule failed validation — not installed."
    echo "      Restart/reload actions from the Manager UI will not work until"
    echo "      this is fixed manually. See $SUDOERS_FILE.tmp for the rejected content."
fi

# ── Firewall ──────────────────────────────────────────────
echo "      Opening firewall port $PORT..."
if command -v firewall-cmd &>/dev/null; then
    firewall-cmd --permanent --add-port=${PORT}/tcp 2>/dev/null && firewall-cmd --reload 2>/dev/null || true
elif command -v ufw &>/dev/null; then
    ufw allow ${PORT}/tcp 2>/dev/null || true
fi

# ── Start service ─────────────────────────────────────────
echo "[8/8] Enabling and starting $SERVICE_NAME..."
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
