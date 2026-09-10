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
    echo "[1/11] Missing required package(s): ${NEED_PKGS[*]}"
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
    echo "[1/11] Python 3 found: $(python3 --version), venv module available."
fi

# ── Copy files ────────────────────────────────────────────
echo "[2/11] Installing to $INSTALL_DIR..."
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
echo "[3/11] Creating Python virtual environment..."
python3 -m venv "$INSTALL_DIR/venv"
"$INSTALL_DIR/venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/venv/bin/pip" install --quiet flask gunicorn flask-wtf flask-limiter piper-tts

# ── rpt_backups directory ─────────────────────────────────
echo "[4/11] Creating backup directory..."
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
echo "[5/11] Checking rpt.conf..."
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
echo "[6/11] Verifying Asterisk MixMonitor module..."
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

# ── AudioSocket tap (low-latency Listen audio capture) ────
# Purely additive and self-falling-back (see audiosocket-tap/README.md) --
# safe to apply unconditionally on every fresh install so new installs get
# low-latency Listen audio without a manual Settings-page step. Needs
# Asterisk actually running (apply.sh issues live "module load"/"dialplan
# reload" AMI-CLI commands), same precondition as the MixMonitor check
# above. Never fatal to the install -- Listen still works via MixMonitor
# if this fails or is skipped.
echo "[7/11] Applying AudioSocket tap (low-latency Listen audio)..."
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

# ── Low-latency RX audio: Apache + ws-audio proxy ─────────
# Reachable low-latency RX (the browser-facing counterpart to the
# AudioSocket tap above) needs Apache fronting HenWen -- gunicorn's
# --worker-class gthread doesn't do WebSocket upgrades, so
# ws-audio/apply.sh's /ws-audio proxy line needs somewhere to live (see
# CLAUDE.md's "Audio streaming"/"Background threads" sections). Merged with
# the (still fully opt-in) "set up HTTPS for browser TX" question so there's
# one Apache-provisioning pass covering both features rather than two.
#
# An interactive run asks two questions, in order: first, whether HenWen is
# already sitting behind the operator's OWN reverse proxy on this box (nginx,
# Caddy, another Apache instance) -- answering yes skips Apache entirely, no
# apache2 install, no vhost, nothing (this used to be folded into the HTTPS
# question's own "skip this if..." wording, which was wrong: declining HTTPS
# there still ran setup-https.sh --http-only underneath, which would fight
# with a real existing reverse proxy exactly like the old wording warned
# against -- the two are now genuinely separate questions, not one question
# doing double duty). Answering no (or a non-interactive install, which can't
# ask either question) moves on to the actual HTTPS question: yes runs the
# full HTTPS flow (Apache comes along for free), no still gets a minimal
# plain-HTTP vhost via setup-https.sh --http-only so browser TX and the
# capture-side AudioSocket tap improvement stay reachable either way. Self-
# falling-back and non-fatal throughout, exactly like the AudioSocket tap
# step above -- Listen keeps working over the legacy WebM path (and TX just
# stays hidden) regardless of what happens here.
#
# HTTPS_SUCCEEDED (distinct from HAVE_APACHE_VHOST) tracks specifically
# whether the FULL HTTPS flow succeeded, not just plain HTTP -- this matters
# below for whether rx_audio_config.path gets defaulted to lowlatency at all.
# The low-latency path's browser side uses WebCodecs (AudioDecoder), which
# is a secure-context-only API: on a plain-HTTP vhost it's simply undefined
# for every real remote visitor, so _doStartListen() always falls through to
# the legacy MSE pipeline regardless of the saved rx_audio_config.path value
# -- defaulting to lowlatency there would just be a misleading label with no
# behavioral difference from legacy. Found live: a fresh plain-HTTP install's
# Listen button failed for a remote browser and needed this traced end to end
# before the actual constraint surfaced.
echo "[8/11] Setting up Apache for low-latency RX audio..."
WS_AUDIO_DEFAULT_OK=0
HAVE_APACHE_VHOST=0
HTTPS_SUCCEEDED=0
if [ -f /etc/apache2/sites-enabled/henwen-ssl.conf ]; then
    echo "      Apache vhost already present (HTTPS) — leaving it as-is."
    HAVE_APACHE_VHOST=1
    HTTPS_SUCCEEDED=1
elif [ -f /etc/apache2/sites-enabled/henwen.conf ]; then
    echo "      Apache vhost already present (plain HTTP) — leaving it as-is."
    HAVE_APACHE_VHOST=1
elif [ -t 0 ]; then
    echo ""
    echo "  By default this installs its own Apache on this box (if not already"
    echo "  present) to front HenWen, which is what lets the low-latency RX audio"
    echo "  path's WebSocket proxy work out of the box, and optionally sets up"
    echo "  HTTPS on it for the browser TX button too."
    read -p "  Is HenWen already running behind YOUR OWN reverse proxy on this box (nginx, Caddy, another Apache instance)? [y/N]: " EXISTING_PROXY
    if [[ "$EXISTING_PROXY" =~ ^[Yy] ]]; then
        echo "      Skipping Apache setup entirely so this doesn't fight with your"
        echo "      existing reverse proxy (e.g. a port-80 bind conflict). Low-latency"
        echo "      RX audio and browser TX both still work -- point your own reverse"
        echo "      proxy at this box's Flask port ($PORT) and, for either feature, add"
        echo "      its own WebSocket proxy rule for /ws-audio (and /asterisk-ws for TX)"
        echo "      -- see ws-audio/README.md and tx-spike/README.md for exactly what"
        echo "      those need to point at."
    else
        read -p "  Set up HTTPS now for the browser TX button? Requires a public hostname pointed at this box. [y/N]: " SETUP_HTTPS
        if [[ "$SETUP_HTTPS" =~ ^[Yy] ]]; then
            if bash "$INSTALL_DIR/tx-spike/setup-https.sh"; then
                HAVE_APACHE_VHOST=1
                HTTPS_SUCCEEDED=1
            else
                echo "      HTTPS setup failed — you can re-run it later:"
                echo "        sudo bash $INSTALL_DIR/tx-spike/setup-https.sh"
            fi
        else
            echo "      Skipped HTTPS. Run 'sudo bash $INSTALL_DIR/tx-spike/setup-https.sh' later if you want browser TX and default low-latency RX audio."
            if bash "$INSTALL_DIR/tx-spike/setup-https.sh" --http-only; then
                HAVE_APACHE_VHOST=1
            else
                echo "      WARNING: plain-HTTP Apache setup failed — TX and low-latency RX"
                echo "      audio won't be reachable by default. Apply later from Manager >"
                echo "      Audio, or: sudo bash $INSTALL_DIR/tx-spike/setup-https.sh --http-only"
            fi
        fi
    fi
else
    if bash "$INSTALL_DIR/tx-spike/setup-https.sh" --http-only; then
        HAVE_APACHE_VHOST=1
    else
        echo "      WARNING: plain-HTTP Apache setup failed — TX and low-latency RX audio"
        echo "      won't be reachable by default. Apply later from Manager > Audio,"
        echo "      or: sudo bash $INSTALL_DIR/tx-spike/setup-https.sh --http-only"
    fi
fi

if [ "$HAVE_APACHE_VHOST" = "1" ]; then
    if bash "$INSTALL_DIR/ws-audio/apply.sh"; then
        echo "      /ws-audio Apache proxy applied."
        # Wired up regardless (harmless, and ready for whenever HTTPS gets
        # added later), but only actually switch Listen's default to
        # lowlatency when HTTPS succeeded -- see the comment above this
        # whole step for why plain HTTP can't use the low-latency path at
        # all (WebCodecs/AudioDecoder needs a secure context), so defaulting
        # to it there would just mislabel what's actually still the legacy
        # pipeline under the hood.
        if [ "$HTTPS_SUCCEEDED" = "1" ]; then
            WS_AUDIO_DEFAULT_OK=1
        else
            echo "      Not defaulting RX Audio Path to Low-Latency -- it needs HTTPS to"
            echo "      actually work in a browser (WebCodecs requires a secure context)."
            echo "      Listen still works fine over the legacy path. Set up HTTPS later"
            echo "      (sudo bash $INSTALL_DIR/tx-spike/setup-https.sh) to make Low-Latency usable."
        fi
    else
        echo "      WARNING: ws-audio/apply.sh failed — Listen will use the legacy"
        echo "      WebM path. Re-run manually later: sudo bash $INSTALL_DIR/ws-audio/apply.sh"
    fi
fi

# Recorded by setup-https.sh's full flow only -- used below to pick the
# right URL for the final banner and to open the right firewall port(s).
HTTPS_HOSTNAME=""
HTTPS_PORT_VAL=""
if [ -f /etc/asterisk/henwen-https-hostname ] && [ -f /etc/asterisk/henwen-https-port ]; then
    HTTPS_HOSTNAME=$(cat /etc/asterisk/henwen-https-hostname 2>/dev/null || true)
    HTTPS_PORT_VAL=$(cat /etc/asterisk/henwen-https-port 2>/dev/null || true)
fi

# ── Systemd service ───────────────────────────────────────
echo "[9/11] Installing systemd service ($SERVICE_NAME)..."

# Remove any old service under the previous name to avoid duplicates
if [ -f /etc/systemd/system/asl3-rpt-editor.service ]; then
    echo "      Removing old asl3-rpt-editor service..."
    systemctl stop asl3-rpt-editor 2>/dev/null || true
    systemctl disable asl3-rpt-editor 2>/dev/null || true
    rm -f /etc/systemd/system/asl3-rpt-editor.service
fi

SERVICE_FILE_DEST="/etc/systemd/system/${SERVICE_NAME}.service"

# Every checked-in HenWen.service ships the same placeholder
# SECRET_KEY=henwen-change-me-in-production -- a plain `cp` would leave
# every fresh install signing session cookies with that identical,
# publicly-known key until an admin happens to notice the Dashboard's
# warning and rotates it by hand (issue #115). Generate a real random one
# here instead, the same way the in-app rotation route does
# (secrets.token_hex(32)), so a fresh install is secure by default. If
# this is a reinstall over an already-rotated key, preserve that existing
# key rather than clobbering it back to the placeholder -- same
# preserve-across-reinstall reasoning as henwen.db above, and it avoids
# silently invalidating every logged-in session on a routine reinstall.
PREV_SECRET_KEY=""
if [ -f "$SERVICE_FILE_DEST" ]; then
    PREV_SECRET_KEY=$(sed -n 's/^Environment="\?SECRET_KEY=\([^"]*\)"\?[[:space:]]*$/\1/p' "$SERVICE_FILE_DEST" | head -1)
fi

cp "$INSTALL_DIR/HenWen.service" "$SERVICE_FILE_DEST"

if [ -n "$PREV_SECRET_KEY" ] && [ "$PREV_SECRET_KEY" != "henwen-change-me-in-production" ]; then
    echo "      Preserving existing SECRET_KEY from previous install."
    NEW_SECRET_KEY="$PREV_SECRET_KEY"
else
    echo "      Generating a random SECRET_KEY for this install."
    NEW_SECRET_KEY=$(python3 -c "import secrets; print(secrets.token_hex(32))")
fi
sed -i "s|^Environment=\"\?SECRET_KEY=.*|Environment=\"SECRET_KEY=${NEW_SECRET_KEY}\"|" "$SERVICE_FILE_DEST"

# Only ever seeds rx_audio_config.path once, the first time the table is
# created (see get_db() in app.py) -- an owner's later Manager > Audio save
# always wins regardless of this env var. Added only when step [8/11] above
# got the Apache /ws-audio proxy wired up AND HTTPS succeeded -- WebCodecs
# needs a secure context, so defaulting to lowlatency without HTTPS would be
# a no-op label with no real behavior change from legacy. Otherwise the
# checked-in HenWen.service template (no RX_AUDIO_DEFAULT_PATH line at all)
# is left as-is and the DB falls back to today's 'legacy' default, same as
# ever.
if [ "$WS_AUDIO_DEFAULT_OK" = "1" ]; then
    sed -i '/^\[Install\]/i Environment="RX_AUDIO_DEFAULT_PATH=lowlatency"' "$SERVICE_FILE_DEST"
    echo "      RX_AUDIO_DEFAULT_PATH=lowlatency added to $SERVICE_FILE_DEST"
fi

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
# buttons for the AudioSocket tap and low-latency RX audio proxy need to run
# `systemctl restart asterisk`, `systemctl restart HenWen`, `systemctl daemon-reload`,
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
echo "[10/11] Installing sudoers rule for restart/reload/update actions..."
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
# Port 80 stays needed even in full-HTTPS mode (Let's Encrypt's HTTP-01
# challenge/renewal always uses it, --dns-manual aside), plus whichever
# HTTPS port setup-https.sh recorded, if any.
if [ "$HAVE_APACHE_VHOST" = "1" ]; then
    for _fw_port in 80 "${HTTPS_PORT_VAL:-}"; do
        [ -n "$_fw_port" ] || continue
        echo "      Opening firewall port $_fw_port (Apache)..."
        if command -v firewall-cmd &>/dev/null; then
            firewall-cmd --permanent --add-port=${_fw_port}/tcp 2>/dev/null && firewall-cmd --reload 2>/dev/null || true
        elif command -v ufw &>/dev/null; then
            ufw allow ${_fw_port}/tcp 2>/dev/null || true
        fi
    done
fi

# ── Start service ─────────────────────────────────────────
echo "[11/11] Enabling and starting $SERVICE_NAME..."
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"
sleep 2

if systemctl is-active --quiet "$SERVICE_NAME"; then
    IP=$(hostname -I | awk '{print $1}')
    # ami-setup.sh's own output (manager.conf dump, AMI login test, service
    # restart) runs BEFORE this summary, not after -- it's the noisiest part
    # of the whole install, and printing the URLs first just meant they
    # scrolled off screen before anyone could read them (issue found via a
    # live install run). This block is now deliberately the last thing
    # install.sh prints.
    echo ""
    echo "  Running AMI setup now..."
    bash "$INSTALL_DIR/ami-setup.sh" || true

    echo ""
    echo "============================================"
    echo "  Installation complete!"
    echo ""
    echo "  Open your browser:"
    if [ -n "$HTTPS_HOSTNAME" ]; then
        HTTPS_PORT_SUFFIX=""
        [ -n "$HTTPS_PORT_VAL" ] && [ "$HTTPS_PORT_VAL" != "443" ] && HTTPS_PORT_SUFFIX=":${HTTPS_PORT_VAL}"
        echo "    https://${HTTPS_HOSTNAME}${HTTPS_PORT_SUFFIX}"
        echo "    http://${IP}:${PORT}   (direct, bypasses Apache -- legacy RX audio only)"
    elif [ "$HAVE_APACHE_VHOST" = "1" ]; then
        echo "    http://${IP}/"
        echo "    http://${IP}:${PORT}   (direct, bypasses Apache -- legacy RX audio only)"
    else
        echo "    http://${IP}:${PORT}"
    fi
    echo ""
    echo "  rpt.conf:  /etc/asterisk/rpt.conf"
    echo "  Backups:   /etc/asterisk/rpt_backups/"
    echo "  Logs:      journalctl -u $SERVICE_NAME -f"
    echo "============================================"
else
    echo ""
    echo "WARNING: Service may not have started. Check:"
    echo "  journalctl -u $SERVICE_NAME -n 50"
    echo "  systemctl status $SERVICE_NAME"
fi
