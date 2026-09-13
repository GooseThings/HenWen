#!/bin/bash
# HenWen - sudoers rule provisioning
#
# Writes/refreshes /etc/sudoers.d/henwen-systemctl, the NOPASSWD rule that
# lets the unprivileged `asterisk` service account run the specific
# privileged commands the Manager UI's Restart/Launch Updater/Apply buttons
# need (see the rule file's own header comment below for the full list).
#
# Shared by install.sh (fresh install / re-run) and update.sh (in-place
# self-update, via "Launch Updater") so the two can never drift apart: a
# self-updated install picks up any new NOPASSWD line a future PR adds here
# on its very next update, without needing install.sh re-run by hand.
#
# Expects INSTALL_DIR and SERVICE_NAME as env vars from the caller; defaults
# match a standard install for a standalone run.
set -e

INSTALL_DIR="${INSTALL_DIR:-/opt/HenWen}"
SERVICE_NAME="${SERVICE_NAME:-HenWen}"

SUDOERS_FILE=/etc/sudoers.d/henwen-systemctl
SYSTEMCTL_BIN=$(command -v systemctl || echo /bin/systemctl)
SYSTEMD_RUN_BIN=$(command -v systemd-run || echo /usr/bin/systemd-run)

if ! command -v visudo >/dev/null 2>&1; then
    echo "      sudo is not installed — skipping this rule."
    echo "      HenWen installs and runs normally without it. The only things"
    echo "      that stop working are the Manager UI buttons needing root:"
    echo "      Restart Asterisk/HenWen, Launch Updater, rotate SECRET_KEY,"
    echo "      change ports. Do those from a root shell instead, or install"
    echo "      sudo and re-run install.sh."
    exit 0
fi

cat > "${SUDOERS_FILE}.tmp" <<EOF
# Installed by HenWen's install.sh / update.sh. Lets the unprivileged
# service account restart the units it manages, reload systemd unit
# definitions, rotate SECRET_KEY/PORT/AMI_PORT in its own unit file, and
# launch the self-updater as its own transient unit.
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} daemon-reload
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart asterisk
asterisk ALL=(root) NOPASSWD: ${SYSTEMCTL_BIN} restart ${SERVICE_NAME}
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/rotate_secret_key.sh
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/update_service_ports.sh
asterisk ALL=(root) NOPASSWD: ${SYSTEMD_RUN_BIN} --unit=henwen-updater --collect ${INSTALL_DIR}/update.sh
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/audiosocket-tap/apply.sh
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/ws-audio/apply.sh
asterisk ALL=(root) NOPASSWD: ${INSTALL_DIR}/tx-spike/apply.sh
EOF
# visudo ships as part of the sudo package, so its absence is already
# handled above as a legitimate "not installed" case, not an error path.
if visudo -c -f "${SUDOERS_FILE}.tmp" &>/dev/null; then
    chmod 440 "${SUDOERS_FILE}.tmp"
    mv "${SUDOERS_FILE}.tmp" "$SUDOERS_FILE"
    echo "      Installed $SUDOERS_FILE"
else
    echo "      WARNING: generated sudoers rule failed validation — not installed."
    echo "      Restart/reload actions from the Manager UI will not work until"
    echo "      this is fixed manually. See $SUDOERS_FILE.tmp for the rejected content."
fi
