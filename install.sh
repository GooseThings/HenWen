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
    echo "[1/9] Installing Python 3..."
    apt-get install -y python3 python3-pip python3-venv python3-full
else
    echo "[1/9] Python 3 found: $(python3 --version)"
fi

apt-get install -y python3-venv python3-full 2>/dev/null || true

# ── Copy files ────────────────────────────────────────────
echo "[2/9] Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
cp -r . "$INSTALL_DIR/"
chmod 755 "$INSTALL_DIR"          # standard app dir: owner rwx, group rx, others rx
chmod +x "$INSTALL_DIR/"*.sh 2>/dev/null || true

# ── Virtual environment ───────────────────────────────────
echo "[3/9] Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet flask gunicorn flask-wtf flask-limiter piper-tts

# ── rpt_backups directory ─────────────────────────────────
echo "[4/9] Creating backup directory..."
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
echo "[5/9] Checking rpt.conf..."
if [ -f /etc/asterisk/rpt.conf ]; then
    echo "      Found: /etc/asterisk/rpt.conf"
    ls -la /etc/asterisk/rpt.conf
else
    echo "      WARNING: /etc/asterisk/rpt.conf not found."
    echo "      The editor will still start but rpt.conf must exist to edit."
fi

# ── Ensure app_mixmonitor is loaded ───────────────────────
# HenWen's Listen/broadcast feature (audio_relay.py pipeline) depends on
# Asterisk's MixMonitor app. Some ASL3 installs carry a stale
# 'noload => app_mixmonitor.so' in modules.conf, or the module simply
# hasn't been loaded yet on a freshly installed system. Fix what we can
# here rather than making the user discover it later via a silent Listen
# button.
echo "[6/9] Verifying Asterisk MixMonitor module..."
MODULES_CONF="/etc/asterisk/modules.conf"
if [ -f "$MODULES_CONF" ] && grep -qE '^\s*noload\s*=>\s*app_mixmonitor\.so' "$MODULES_CONF"; then
    echo "      Found 'noload => app_mixmonitor.so' in modules.conf — disabling that line."
    sed -i -E 's/^(\s*)noload(\s*=>\s*app_mixmonitor\.so)/\1;noload\2/' "$MODULES_CONF"
fi

if command -v asterisk &>/dev/null && systemctl is-active --quiet asterisk 2>/dev/null; then
    if asterisk -rx "module show like mixmonitor" 2>/dev/null | grep -qi "app_mixmonitor.so"; then
        echo "      app_mixmonitor.so already loaded."
    else
        LOAD_OUT=$(asterisk -rx "module load app_mixmonitor.so" 2>&1)
        if echo "$LOAD_OUT" | grep -qi "not found\|failed\|error"; then
            echo "      WARNING: app_mixmonitor.so could not be loaded ($LOAD_OUT)."
            echo "      It may not be installed on this system. Audio streaming (Listen)"
            echo "      in HenWen will not work until this is fixed. Check:"
            echo "        find / -xdev -name app_mixmonitor.so"
            echo "      and reinstall/repair the Asterisk modules package if it's missing."
        else
            echo "      app_mixmonitor.so loaded."
        fi
    fi
else
    echo "      Asterisk not running — skipping live load. It will autoload on next"
    echo "      Asterisk start unless the module is missing entirely (checked above"
    echo "      only covers the modules.conf blacklist, not a missing .so file)."
fi

# ── Systemd service ───────────────────────────────────────
echo "[7/9] Installing systemd service ($SERVICE_NAME)..."

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
echo "[8/9] Installing sudoers rule for restart/reload/update actions..."
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
echo "[9/9] Enabling and starting $SERVICE_NAME..."
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

    # ── Optional: HTTPS for the browser TX button ─────────
    # Only relevant to the browser-transmit feature (getUserMedia/WebRTC
    # need a secure context) — the kiosk and everything else work fine
    # over plain HTTP. Requires a public hostname pointed at this box, so
    # it's opt-in and skipped entirely on a non-interactive install.
    if [ -t 0 ]; then
        echo ""
        read -p "  Set up HTTPS now for the browser TX button? Requires a public hostname pointed at this box. [y/N]: " SETUP_HTTPS
        if [[ "$SETUP_HTTPS" =~ ^[Yy] ]]; then
            bash "$INSTALL_DIR/tx-spike/setup-https.sh" || echo "  HTTPS setup failed — you can re-run it later: sudo bash $INSTALL_DIR/tx-spike/setup-https.sh"
        else
            echo "  Skipped. Run 'sudo bash $INSTALL_DIR/tx-spike/setup-https.sh' later if you want browser TX."
        fi
    fi
else
    echo ""
    echo "WARNING: Service may not have started. Check:"
    echo "  journalctl -u $SERVICE_NAME -n 50"
    echo "  systemctl status $SERVICE_NAME"
fi
