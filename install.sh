#!/bin/bash
# HenWen - Installer
# https://www.github.com/GooseThings/HenWen/
# Run as root — either `sudo bash install.sh`, or `bash install.sh` from a
# root shell. sudo is a convenience here, not a dependency: this script only
# ever requires EUID 0 and never invokes sudo itself.
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
    echo "ERROR: install.sh must run as root."
    echo ""
    echo "  With sudo:     sudo bash install.sh"
    echo "  Without sudo:  su -   then   bash install.sh"
    echo ""
    echo "  (sudo is not required to install or run HenWen — see the"
    echo "   Installation guide's \"Does HenWen need sudo?\" section.)"
    exit 1
fi

# ── Required system packages ──────────────────────────────
# python3 itself, plus the venv module (python3-venv/python3-full) needed
# to create the app's virtualenv -- Debian/Ubuntu ship python3 without
# ensurepip by default, so `python3 -m venv` fails outright without it.
# Rather than silently apt-get installing whatever's missing, state what's
# needed and ask before touching the system -- and always apt-get update
# first, so a stale package list doesn't 404 mid-install (as it did on an
# out-of-date Debian 12 box: idle-python3.11/python3.11-venv/etc. all
# 404'd, `python3 -m venv` then failed for real, and the venv module's own
# error message told the user to re-run the exact apt-get install that had
# already just failed).
NEED_PKGS=()
if ! command -v python3 &>/dev/null; then
    NEED_PKGS+=(python3 python3-pip)
fi
if ! python3 -c "import ensurepip" &>/dev/null; then
    NEED_PKGS+=(python3-venv python3-full)
fi

if [ ${#NEED_PKGS[@]} -gt 0 ]; then
    echo "[1/10] Missing required package(s): ${NEED_PKGS[*]}"
    DO_INSTALL=1
    if [ -t 0 ]; then
        read -p "      Install via apt-get now? [Y/n]: " REPLY
        [[ "$REPLY" =~ ^[Nn] ]] && DO_INSTALL=0
    fi
    if [ "$DO_INSTALL" -ne 1 ]; then
        echo "      Skipped. Install these manually, then re-run this script:"
        echo "        sudo apt-get update && sudo apt-get install -y ${NEED_PKGS[*]}"
        exit 1
    fi
    echo "      Running apt-get update..."
    apt-get update
    echo "      Installing: ${NEED_PKGS[*]}"
    apt-get install -y "${NEED_PKGS[@]}"
else
    echo "[1/10] Python 3 found: $(python3 --version), venv module available."
fi

# ── Copy files ────────────────────────────────────────────
echo "[2/10] Installing to $INSTALL_DIR..."
mkdir -p "$INSTALL_DIR"
# Re-running this script from inside a checkout that already *is*
# INSTALL_DIR (e.g. /opt/HenWen's own live git checkout, used to pick up a
# sudoers rule added after the initial install) makes `cp -r .
# "$INSTALL_DIR/"` a same-file no-op that cp refuses to do — under `set -e`
# that would otherwise abort the whole script before reaching the later
# steps (sudoers rule, firewall, service enable). Skip the copy in that one
# case; every other file/permission/service step below still runs.
if [ "$(pwd -P)" = "$(cd "$INSTALL_DIR" && pwd -P)" ]; then
    echo "      Already running from $INSTALL_DIR — skipping copy."
else
    cp -r . "$INSTALL_DIR/"
fi
chmod 755 "$INSTALL_DIR"          # standard app dir: owner rwx, group rx, others rx
chmod +x "$INSTALL_DIR/"*.sh 2>/dev/null || true

# ── Virtual environment ───────────────────────────────────
echo "[3/10] Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet flask gunicorn flask-wtf flask-limiter piper-tts

# ── rpt_backups directory ─────────────────────────────────
echo "[4/10] Creating backup directory..."
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

# ── Announcement sound directory ───────────────────────────
# Uploaded/TTS-synthesized announcement audio (SOUNDS_DIR). Must live under
# Asterisk's own sounds dir so "rpt localplay <node> henwen/<slug>" resolves,
# but that parent dir is root:root 0755 — the asterisk user can't create the
# henwen/ subdirectory itself, so it has to exist before the service starts.
mkdir -p /usr/share/asterisk/sounds/henwen
chown asterisk:asterisk /usr/share/asterisk/sounds/henwen
chmod 750 /usr/share/asterisk/sounds/henwen

# Fix ownership of the database so the service can write it as the asterisk user
if [ -f /etc/asterisk/henwen.db ]; then
    chown asterisk:asterisk /etc/asterisk/henwen.db
fi

# ── Verify rpt.conf accessible ────────────────────────────
echo "[5/10] Checking rpt.conf..."
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
echo "[6/10] Verifying Asterisk MixMonitor module..."
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

# ── Optional: AudioSocket tap (low-latency Listen audio) ──
# Purely additive and self-falling-back (see audiosocket-tap/README.md) --
# safe to apply unconditionally on every fresh install so new installs get
# low-latency Listen audio without a manual Settings-page step. Needs
# Asterisk actually running (apply.sh issues live "module load"/"dialplan
# reload" AMI-CLI commands), same precondition as the MixMonitor check
# above. Never fatal to the install -- Listen still works via MixMonitor
# if this fails or is skipped.
echo "[7/10] Applying AudioSocket tap (low-latency Listen audio)..."
if command -v asterisk &>/dev/null && systemctl is-active --quiet asterisk 2>/dev/null; then
    if bash "$INSTALL_DIR/audiosocket-tap/apply.sh"; then
        echo "      Applied."
    else
        echo "      WARNING: audiosocket-tap/apply.sh failed — Listen will use"
        echo "      MixMonitor instead (higher latency, still fully functional)."
        echo "      Re-run manually later: sudo bash $INSTALL_DIR/audiosocket-tap/apply.sh"
    fi
else
    echo "      Asterisk not running — skipping. Apply later from Manager >"
    echo "      Settings, or: sudo bash $INSTALL_DIR/audiosocket-tap/apply.sh"
fi

# ── Systemd service ───────────────────────────────────────
echo "[8/10] Installing systemd service ($SERVICE_NAME)..."

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
# the Dashboard's "Restart Asterisk" button, secret-key rotation, port
# rotation, the "Launch Updater" button, and the Settings page's "Apply"
# button for the optional AudioSocket tap need to run `systemctl restart
# asterisk`, `systemctl restart HenWen`, `systemctl daemon-reload`,
# rotate_secret_key.sh / update_service_ports.sh (the only code allowed to
# edit the root-owned unit file's SECRET_KEY/PORT/AMI_PORT lines — see
# app.py's api_set_secret_key / api_set_ports), (via systemd-run, so it
# survives outside HenWen.service's own cgroup) update.sh,
# audiosocket-tap/apply.sh (edits /etc/asterisk/modules.conf and
# custom/extensions.conf, loads Asterisk modules live — see
# audiosocket-tap/README.md), and ws-audio/apply.sh (edits the Apache vhost
# to add the low-latency RX audio path's WebSocket proxy — see
# ws-audio/README.md). Without this rule those actions fail with
# "Interactive authentication required" since there's no session for
# polkit to prompt. Scope is intentionally limited to these exact commands
# — do not broaden with wildcards. The updater rule only works if
# $INSTALL_DIR is itself a git checkout of the HenWen repo — update.sh
# no-ops with an error otherwise.
echo "[9/10] Installing sudoers rule for restart/reload/update actions..."
SUDOERS_FILE=/etc/sudoers.d/henwen-systemctl
SYSTEMCTL_BIN=$(command -v systemctl || echo /bin/systemctl)
SYSTEMD_RUN_BIN=$(command -v systemd-run || echo /usr/bin/systemd-run)
cat > "${SUDOERS_FILE}.tmp" <<EOF
# Installed by HenWen's install.sh. Lets the unprivileged service account
# restart the units it manages, reload systemd unit definitions, rotate
# SECRET_KEY/PORT/AMI_PORT in its own unit file, and launch the
# self-updater as its own transient unit.
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} daemon-reload
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart asterisk
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart ${SERVICE_NAME}
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/rotate_secret_key.sh
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/update_service_ports.sh
asterisk ALL=(root) NOPASSWD: ${SYSTEMD_RUN_BIN} --unit=henwen-updater --collect ${INSTALL_DIR}/update.sh
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/audiosocket-tap/apply.sh
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/ws-audio/apply.sh
EOF
# visudo ships as part of the sudo package, so "no visudo" means sudo simply
# isn't installed on this box — a legitimate choice, not an error. Say so
# plainly instead of reporting it as a failed validation, which is what the
# single else-branch used to do and which read like something had gone wrong
# with the install (issue #70).
if ! command -v visudo >/dev/null 2>&1; then
    rm -f "${SUDOERS_FILE}.tmp"
    echo "      sudo is not installed — skipping this rule."
    echo "      HenWen installs and runs normally without it. The only things"
    echo "      that stop working are the Manager UI buttons needing root:"
    echo "      Restart Asterisk/HenWen, Launch Updater, rotate SECRET_KEY,"
    echo "      change ports. Do those from a root shell instead, or install"
    echo "      sudo and re-run install.sh."
elif visudo -c -f "${SUDOERS_FILE}.tmp" &>/dev/null; then
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
echo "[10/10] Enabling and starting $SERVICE_NAME..."
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
        echo "  Note: skip this if HenWen will run behind an existing reverse proxy"
        echo "  (e.g. nginx, Caddy, or another Apache instance) that already terminates"
        echo "  HTTPS for you -- this step provisions its own standalone Apache +"
        echo "  Let's Encrypt HTTPS listener, which isn't what you want in that case."
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
