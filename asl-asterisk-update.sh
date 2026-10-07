#!/bin/bash
# HenWen - AllStarLink Asterisk package updater (root helper)
#
# Run by the Manager > Asterisk Updates page through two narrowly-scoped
# NOPASSWD sudoers rules (see provision-sudoers.sh):
#
#   asl-asterisk-update.sh check     refresh apt's package lists (apt-get update)
#   asl-asterisk-update.sh install   upgrade the installed asl3-asterisk* packages
#
# `install` is launched via `systemd-run --unit=henwen-asl-upgrade` so it runs
# outside HenWen.service's cgroup. Upgrading asl3-asterisk restarts Asterisk,
# which drops every link and any transmission in progress, and the script has
# to keep running (and logging) while that happens.
#
# Everything it does is written to $HENWEN_ASL_UPDATE_LOG (one run per file,
# the previous run is kept as <log>.1). The Manager page tails that file live,
# so every line is a plain "[HH:MM:SS] [LEVEL] text" line, and apt's own output
# is passed through unchanged. The last line of a finished run is always
# "=== RESULT: SUCCESS ===" or "=== RESULT: FAILED ===".
#
# Config files are never overwritten: dpkg is told to keep whatever is on disk
# (--force-confold), so a changed package default lands next to the real file as
# <name>.dpkg-dist instead of replacing rpt.conf and friends. /etc/asterisk is
# also tarred up first (minus HenWen's own DB and the TX SIP secret) so there is
# something to restore from.
set -u

MODE="${1:-}"
LOG="${HENWEN_ASL_UPDATE_LOG:-/var/log/henwen-asl-update.log}"
BACKUP_DIR="${HENWEN_ASL_UPDATE_BACKUP_DIR:-/var/backups/henwen-asterisk}"
CONF_DIR="${HENWEN_ASL_UPDATE_CONF_DIR:-/etc/asterisk}"
PKG_GLOB="asl3-asterisk*"

export DEBIAN_FRONTEND=noninteractive
export LC_ALL=C

# Packages from the glob that are actually installed (status "ii"), one per line.
installed_pkgs() {
    dpkg-query -W -f='${db:Status-Abbrev} ${Package}\n' "$PKG_GLOB" 2>/dev/null \
        | awk '$1 == "ii" {print $2}'
}

ts() { date +%H:%M:%S; }
say() { echo "[$(ts)] [$1] $2"; }

do_check() {
    echo "Refreshing package lists (apt-get update)..."
    apt-get update -o DPkg::Lock::Timeout=120
}

do_install() {
    local start_marker start_ts rc pkgs sim n before after
    start_ts=$(date '+%Y-%m-%d %H:%M:%S')
    start_marker=$(mktemp) || start_marker=""
    [ -n "$start_marker" ] && trap 'rm -f "$start_marker"' RETURN

    say INFO "Asterisk package update started (run as $(id -un))"

    pkgs=$(installed_pkgs | tr '\n' ' ')
    if [ -z "${pkgs// /}" ]; then
        say ERROR "No installed $PKG_GLOB packages found - nothing to update."
        return 1
    fi
    before=$(dpkg-query -W -f='${Package} ${Version}\n' $pkgs 2>/dev/null)
    say INFO "Installed before:"
    echo "$before" | sed 's/^/        /'

    # --- pre-flight ---------------------------------------------------------
    if [ -n "$(dpkg --audit 2>&1)" ]; then
        say ERROR "dpkg reports half-installed or unconfigured packages. Fix with"
        say ERROR "'dpkg --configure -a' from a root shell, then run this again."
        dpkg --audit 2>&1 | sed 's/^/        /'
        return 1
    fi
    local free_kb
    free_kb=$(df -Pk / | awk 'NR==2 {print $4}')
    if [ -n "$free_kb" ] && [ "$free_kb" -lt 150000 ]; then
        say ERROR "Only $((free_kb / 1024)) MB free on / - need at least ~150 MB. Aborting."
        return 1
    elif [ -n "$free_kb" ] && [ "$free_kb" -lt 400000 ]; then
        say WARN "Low disk space on /: $((free_kb / 1024)) MB free."
    fi
    say INFO "Pre-flight OK (dpkg state clean, $((free_kb / 1024)) MB free on /)"

    # --- config backup ------------------------------------------------------
    if [ -d "$CONF_DIR" ]; then
        mkdir -p "$BACKUP_DIR" && chmod 700 "$BACKUP_DIR"
        local tarball="$BACKUP_DIR/asterisk-conf-$(date +%Y%m%d-%H%M%S).tar.gz"
        if tar -czf "$tarball" \
                --exclude='henwen.db*' --exclude='henwen-tx.secret' \
                -C "$(dirname "$CONF_DIR")" "$(basename "$CONF_DIR")" 2>/dev/null; then
            chmod 600 "$tarball"
            say INFO "Config backup: $tarball"
            # keep the 5 newest, drop the rest
            ls -1t "$BACKUP_DIR"/asterisk-conf-*.tar.gz 2>/dev/null | tail -n +6 | xargs -r rm -f
        else
            say WARN "Could not back up $CONF_DIR - continuing without a backup."
        fi
    fi

    # --- what will change ---------------------------------------------------
    sim=$(apt-get -s install --only-upgrade $pkgs 2>&1)
    n=$(echo "$sim" | grep -c '^Inst ')
    if [ "$n" -eq 0 ]; then
        say INFO "Already up to date - nothing to install."
        return 0
    fi
    say INFO "Will upgrade $n package(s):"
    echo "$sim" | grep '^Inst ' | sed 's/^/        /'

    # --- the upgrade --------------------------------------------------------
    say WARN "Asterisk will restart during this step: links drop and any active transmission ends."
    say INFO "Running apt-get install --only-upgrade ..."
    apt-get install --only-upgrade -y \
        -o DPkg::Lock::Timeout=120 \
        -o Dpkg::Options::=--force-confold \
        -o Dpkg::Options::=--force-confdef \
        $pkgs 2>&1
    rc=${PIPESTATUS[0]}
    if [ "$rc" -ne 0 ]; then
        say ERROR "apt-get exited with status $rc."
        say ERROR "If it stopped mid-install, run 'dpkg --configure -a' then 'apt-get -f install' from a root shell."
        return 1
    fi
    say INFO "apt-get finished OK."

    # --- verify -------------------------------------------------------------
    say INFO "Waiting for Asterisk to come back up (up to 90s)..."
    local i up=0
    for i in $(seq 1 45); do
        if systemctl is-active --quiet asterisk && \
           asterisk -rx 'core show uptime' >/dev/null 2>&1; then
            up=1
            break
        fi
        sleep 2
    done
    if [ "$up" -eq 1 ]; then
        say INFO "Asterisk is running and answering on its console."
        asterisk -rx 'core show version' 2>/dev/null | sed 's/^/        /'
    else
        say ERROR "Asterisk is NOT answering after the upgrade (systemctl is-active: $(systemctl is-active asterisk 2>&1))."
        say ERROR "Check: journalctl -u asterisk -n 80 --no-pager"
    fi

    after=$(dpkg-query -W -f='${Package} ${Version}\n' $pkgs 2>/dev/null)
    say INFO "Installed after:"
    echo "$after" | sed 's/^/        /'

    # New package defaults that dpkg parked next to a kept config file.
    local dist
    if [ -n "$start_marker" ]; then
        dist=$(find "$CONF_DIR" -name '*.dpkg-dist' -newer "$start_marker" 2>/dev/null)
        if [ -n "$dist" ]; then
            say WARN "The package shipped new defaults for config files you have customised."
            say WARN "Your files were kept as-is. Review these when convenient:"
            echo "$dist" | sed 's/^/        /'
        fi
    fi

    local warns
    warns=$(journalctl -u asterisk --since "$start_ts" -p warning --no-pager -q 2>/dev/null | tail -n 25)
    if [ -n "$warns" ]; then
        say WARN "Asterisk warnings/errors since this run began (last 25):"
        echo "$warns" | cut -c1-220 | sed 's/^/        /'
    fi

    [ "$up" -eq 1 ] || return 1
    return 0
}

case "$MODE" in
    check)
        do_check
        exit $?
        ;;
    install)
        if [ "$(id -u)" -ne 0 ]; then
            echo "Must run as root (use the Manager page, or sudo)." >&2
            exit 1
        fi
        mkdir -p "$(dirname "$LOG")"
        [ -f "$LOG" ] && mv -f "$LOG" "$LOG.1"
        ( umask 022; : > "$LOG" )
        chmod 644 "$LOG"
        {
            do_install
            rc=$?
            if [ "$rc" -eq 0 ]; then
                say INFO "Update complete."
                echo "=== RESULT: SUCCESS ==="
            else
                say ERROR "Update did not complete cleanly - see the messages above."
                echo "=== RESULT: FAILED ==="
            fi
            exit "$rc"
        } 2>&1 | tee -a "$LOG"
        exit "${PIPESTATUS[0]}"
        ;;
    *)
        echo "usage: $0 check|install" >&2
        exit 2
        ;;
esac
