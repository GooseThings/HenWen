#!/bin/bash
# HenWen - Uninstaller
#
# Run as root — either `sudo bash uninstall.sh`, or `bash uninstall.sh` from a
# root shell. sudo is a convenience here, not a dependency: this script only
# ever requires EUID 0 and never invokes sudo itself.
set -e

usage() {
    cat <<USAGE
Usage: bash uninstall.sh [--purge | --keep-data]

  --purge      Also delete HenWen's database, credentials, recordings, TTS
               voices and uploaded sounds. No prompt.
  --keep-data  Remove the application only, leaving all data in place. No
               prompt.

With neither flag an interactive run asks. A non-interactive run keeps the
data and says so — an unattended uninstall never destroys data silently.
USAGE
}

PURGE=""      # "" = ask, 1 = purge, 0 = keep
while [ $# -gt 0 ]; do
    case "$1" in
        --purge)      PURGE=1 ;;
        --keep-data)  PURGE=0 ;;
        -h|--help)    usage; exit 0 ;;
        *) echo "Unknown option: $1"; echo; usage; exit 1 ;;
    esac
    shift
done

if [ "$EUID" -ne 0 ]; then
    echo "ERROR: uninstall.sh must run as root."
    echo ""
    echo "  With sudo:     sudo bash uninstall.sh"
    echo "  Without sudo:  su -   then   bash uninstall.sh"
    exit 1
fi

echo ""
echo "============================================"
echo "  HenWen Uninstaller"
echo "============================================"
echo ""

# Everything holding account credentials or user data. The database is the
# important one and the reason this prompt exists at all (issue #80): it holds
# every account's password hash, TOTP secret and recovery codes, plus the
# credentials for every integration configured through the Manager —
# Broadcastify password, YouTube stream key, Discord webhook URLs, IRC NickServ
# password, Meshtastic channel PSK, ntfy/Pushover tokens. install.sh
# deliberately preserves it so an in-place reinstall doesn't wipe the operator's
# accounts and configuration; the consequence nobody expected is that
# uninstall-then-reinstall silently restored working admin logins from before.
DATA_PATHS=(
    "/etc/asterisk/henwen.db|database: all accounts, 2FA secrets and saved integration credentials"
    "/etc/asterisk/henwen-tx.secret|SIP secret for the browser TX button"
    "/var/lib/asterisk/henwen_recordings|saved audio recordings"
    "/var/lib/asterisk/henwen_tts_voices|downloaded Piper TTS voice models"
    "/usr/share/asterisk/sounds/henwen|uploaded announcement and node-ID audio"
)

present_paths() {
    local entry path
    for entry in "${DATA_PATHS[@]}"; do
        path="${entry%%|*}"
        [ -e "$path" ] && printf '%s\n' "$entry"
    done
}

FOUND=()
while IFS= read -r line; do [ -n "$line" ] && FOUND+=("$line"); done < <(present_paths)

if [ "${#FOUND[@]}" -gt 0 ]; then
    echo "HenWen data and credentials found on this system:"
    echo ""
    for entry in "${FOUND[@]}"; do
        printf '    %-42s %s\n' "${entry%%|*}" "${entry#*|}"
    done
    echo ""
    if [ -z "$PURGE" ]; then
        if [ -t 0 ]; then
            echo "Keeping these means a future reinstall comes back with the same"
            echo "accounts and passwords still working."
            read -p "Delete them? [y/N]: " REPLY
            [[ "$REPLY" =~ ^[Yy] ]] && PURGE=1 || PURGE=0
        else
            # Never destroy data in an unattended run that didn't ask for it.
            PURGE=0
            echo "Non-interactive run and no --purge given — keeping all of the above."
        fi
    fi
else
    PURGE=0
    echo "No HenWen data files found on this system."
    echo ""
fi

echo "Stopping and disabling HenWen service..."
systemctl stop    HenWen 2>/dev/null || true
systemctl disable HenWen 2>/dev/null || true
rm -f /etc/systemd/system/HenWen.service

# Also clean up old service name if present
systemctl stop    asl3-rpt-editor 2>/dev/null || true
systemctl disable asl3-rpt-editor 2>/dev/null || true
rm -f /etc/systemd/system/asl3-rpt-editor.service

systemctl daemon-reload

# The sudoers rule grants the asterisk user passwordless root for a handful of
# systemctl commands and two HenWen scripts. Leaving it behind for an app that
# no longer exists is a standing grant pointing at paths anyone able to write
# /opt/HenWen could recreate — so it goes unconditionally, not as part of the
# data prompt.
echo "Removing sudoers rule and journald cap..."
rm -f /etc/sudoers.d/henwen-systemctl
rm -f /etc/systemd/journald.conf.d/99-henwen-cap.conf
systemctl restart systemd-journald 2>/dev/null || true

echo "Removing installation directory /opt/HenWen..."
rm -rf /opt/HenWen

if [ "$PURGE" = "1" ]; then
    echo "Removing HenWen data and credentials..."
    for entry in "${FOUND[@]}"; do
        path="${entry%%|*}"
        rm -rf "$path"
        echo "      removed $path"
    done
fi

echo ""
echo "============================================"
echo "  Uninstall complete."
echo "============================================"
echo ""
if [ "$PURGE" = "1" ]; then
    echo "  All HenWen accounts and stored credentials were deleted."
    echo "  A future install will start from the first-run setup screen."
else
    if [ "${#FOUND[@]}" -gt 0 ]; then
        echo "  KEPT: HenWen's database and credentials are still on this system."
        echo "        A reinstall will reuse them — the same accounts and"
        echo "        passwords will work again. Re-run with --purge to remove"
        echo "        them, or delete them yourself:"
        echo ""
        for entry in "${FOUND[@]}"; do
            echo "          rm -rf ${entry%%|*}"
        done
        echo ""
    fi
fi
echo "  Left alone (not HenWen's to remove):"
echo "    /etc/asterisk/rpt.conf and /etc/asterisk/rpt_backups/"
echo "    Apache vhosts and any Let's Encrypt certificate from setup-https.sh"
echo "      (/etc/apache2/sites-*/henwen*.conf — check 'apache2ctl -S')"
echo "    The firewall rule opening the web port, if install.sh added one"
echo "    Asterisk's own AudioSocket dialplan, if audiosocket-tap was applied"
echo ""
