#!/bin/bash
# HenWen Apache helpers, shared by tx-spike/{apply,rollback}.sh,
# ws-audio/{apply,rollback}.sh, tx-spike/check-ports.sh, tx-spike/setup-https.sh,
# and install.sh. Sourced only -- this file does nothing if executed directly.
#
# Why this exists: every one of those scripts used to carry its own copy of
# "check for exactly /etc/apache2/sites-enabled/henwen-ssl.conf or
# henwen.conf, first one found wins" -- which breaks the moment an install
# fronts HenWen through a vhost with any other name, or through more than
# one vhost at once (e.g. one per hostname). Confirmed live on a production
# box: it already runs HenWen behind *two* independently-named vhosts
# (one per hostname), and the old candidate-loop only ever found one of
# them, silently leaving the other's visitors without the WSS/ws-audio
# proxy line no matter how many times apply.sh was re-run. This file
# replaces "guess two filenames" with "ask Apache's own config what's
# actually proxying to HenWen's real Flask port," and every touched script
# now loops over *all* of what that finds instead of stopping at the first.
#
# set -euo pipefail is deliberately NOT set here -- this is a library of
# functions, not a script with its own flow; each caller keeps its own
# shell options, and a `local` failing under a caller's `set -e` while this
# file is mid-source would be a confusing place to die.

# The directory Apache actually loads vhosts from. Overridable so a test
# suite (or an operator with a non-default Apache layout) can point this
# somewhere else entirely -- production installs never need to set it.
henwen_apache_sites_dir() {
    printf '%s\n' "${HENWEN_APACHE_SITES_DIR:-/etc/apache2/sites-enabled}"
}

# HenWen's own Flask/gunicorn port, read from the same place
# update_service_ports.sh keeps authoritative when an operator changes it
# from Manager > Settings: the systemd unit's `Environment=PORT=` line (or,
# for a unit written by hand before that env var existed, the gunicorn
# `--bind ...:PORT` flag it's passed on ExecStart=). This is what lets vhost
# discovery work *without* already knowing a vhost to sed-scrape a port out
# of -- the old scripts had that backwards, assuming a vhost first and only
# reading its port as an afterthought.
#
# PORT itself (not SERVICE_FILE_PATH/SERVICE_NAME) is the one explicit
# override callers already use (setup-https.sh, ws-audio/apply.sh's
# AUDIO_WS_PORT sibling) -- honored first so a manual `PORT=8080 ...`
# invocation still works with no systemd unit installed at all, e.g. in CI.
henwen_flask_port() {
    if [ -n "${PORT:-}" ]; then
        printf '%s\n' "$PORT"
        return 0
    fi
    local svc="${SERVICE_FILE_PATH:-/etc/systemd/system/${SERVICE_NAME:-HenWen}.service}"
    local p=""
    if [ -f "$svc" ]; then
        p=$(grep -oE '^Environment="?PORT=[0-9]+' "$svc" 2>/dev/null | grep -oE '[0-9]+$' | head -1)
        if [ -z "$p" ]; then
            p=$(grep -oE -- '--bind[[:space:]]+[^:[:space:]]*:[0-9]+' "$svc" 2>/dev/null | grep -oE '[0-9]+$' | head -1)
        fi
    fi
    printf '%s\n' "${p:-5000}"
}

# Resolve a sites-enabled entry to the real file Apache is actually reading
# -- almost always a symlink into sites-available. This is the exact
# resolution every apply.sh already did individually before this file
# existed (see their own comments): `sed -i` doesn't edit *through* a
# symlink, it silently replaces the symlink itself with a fresh regular
# file, which both breaks the sites-available/sites-enabled split and can
# leave the result with the wrong permissions. Editing the real file
# directly sidesteps that regardless of which tool a caller uses to edit it.
henwen_resolve_vhost() {
    local f="$1"
    if [ -L "$f" ]; then
        readlink -f "$f"
    else
        printf '%s\n' "$f"
    fi
}

# Every distinct, real Apache vhost file that currently proxies HenWen's own
# Flask port at the root path -- i.e. every apply.sh's own former
# candidate-loop, generalized from "one of these two exact filenames" to
# "whichever files in the sites-enabled directory actually do this,
# however many there are." Matches both the bare ProxyPass syntax
# install.sh/setup-https.sh write (`ProxyPass / http://127.0.0.1:PORT/`)
# and Apache's equally-valid quoted form (`ProxyPass "/" "http://..."`),
# and tolerates whatever whitespace/trailing params (retry=, timeout=...)
# happen to be there -- the old scripts matched one exact literal string
# including its exact spacing, which a hand-edited or certbot-touched vhost
# doesn't reliably preserve.
#
# Prints one resolved absolute path per line, deduplicated (two
# sites-enabled entries can resolve to the same sites-available file, or a
# caller's explicit extra candidates can overlap with what's discovered
# here) and sorted for stable, deterministic script output.
henwen_discover_vhosts() {
    local port="${1:-$(henwen_flask_port)}"
    local dir; dir="$(henwen_apache_sites_dir)"
    local pat='ProxyPass[[:space:]]+"?/"?[[:space:]]+"?http://127\.0\.0\.1:'"${port}"'/"?'
    local f real
    local -A seen=()
    local extra=()
    # Extra caller-supplied candidates (app.py's tests, or an operator who
    # knows exactly which file to check) are honored in addition to the
    # directory scan -- never a substitute for it, since a box can have any
    # number of real vhosts the caller didn't explicitly name.
    if [ $# -gt 1 ]; then
        extra=("${@:2}")
    fi
    for f in "$dir"/*.conf "${extra[@]}"; do
        [ -e "$f" ] || continue
        real="$(henwen_resolve_vhost "$f")"
        [ -f "$real" ] || continue
        [ -n "${seen[$real]:-}" ] && continue
        grep -qE "$pat" "$real" 2>/dev/null || continue
        seen[$real]=1
        printf '%s\n' "$real"
    done | sort -u
}

# Insert `text` (may be multi-line) immediately before the first line
# matching HenWen's own catch-all ProxyPass in `file`, unless `marker` is
# already present (idempotent re-run). Returns 0 on success, 1 if already
# applied, 2 if the insertion point couldn't be found (caller should treat
# that as a failure, not a skip).
#
# Uses an awk regex *address* rather than the old approach of `sed -i
# s/<exact literal line>/<new lines><exact literal line>/` -- the old form
# silently did nothing on any vhost whose ProxyPass line didn't match that
# literal string byte-for-byte (different indentation, no trailing
# `retry=0 timeout=120`, the quoted directive form, ...). Matching by regex
# and inserting *before* the matched line, rather than rewriting it, means
# the line's own exact text never has to be reproduced or cared about.
henwen_insert_before_proxypass() {
    local file="$1" port="$2" marker="$3" text="$4"
    grep -qF "$marker" "$file" && return 1
    # Double backslashes, unlike henwen_discover_vhosts()'s own copy of this
    # pattern -- this one is handed to awk via -v, which runs its own
    # string-escape pass over -v values before the result is used as a
    # regex (same as a string literal in awk source). A single `\.` there
    # is an unrecognized escape that gawk warns about and silently reduces
    # to a bare `.` -- still matches in practice (a literal dot is a
    # subset of "any character"), but not what's intended and noisy.
    # `\\.` survives that pass as the intended literal-dot `\.`.
    local pat='ProxyPass[[:space:]]+"?/"?[[:space:]]+"?http://127\\.0\\.0\\.1:'"${port}"'/"?'
    local tmp; tmp=$(mktemp)
    awk -v ins="$text" -v pat="$pat" '
        BEGIN { done = 0 }
        !done && $0 ~ pat { print ins; done = 1 }
        { print }
    ' "$file" > "$tmp"
    if ! grep -qF "$marker" "$tmp"; then
        rm -f "$tmp"
        return 2
    fi
    mv "$tmp" "$file"
    # mktemp's default 600 would otherwise silently downgrade the vhost's
    # permissions -- Apache vhosts need to stay world-readable so the
    # `asterisk` user (gunicorn) can read them back for its own
    # applied-or-not diagnostics checks.
    chmod 644 "$file"
    return 0
}

# Every "host:port" this vhost file actually serves `marker` on -- i.e.
# only the <VirtualHost ...>...</VirtualHost> blocks that both declare a
# ServerName and contain `marker` somewhere in their body, paired with that
# same block's own listening port (from its own `<VirtualHost *:N>` line,
# not a global assumption). Used by check-ports.sh and apply.sh's own
# final printout to report the real reachable host:port pairs instead of
# just "the first ServerName found anywhere in the file" -- a vhost file
# routinely has more than one block (e.g. a :80 redirect-only stub plus the
# real :443 block), and only the block that actually proxies the feature
# being checked is the one worth reporting.
#
# Deliberately plain POSIX awk (default whitespace field-splitting, no
# gawk-only 3-arg match()) since Debian's default `awk` is mawk, not gawk.
henwen_hostports_with_marker() {
    local file="$1" marker="$2"
    [ -f "$file" ] || return 0
    awk -v marker="$marker" '
        function reset() { host = ""; port = ""; buf = "" }
        BEGIN { reset() }
        /<VirtualHost/ {
            reset()
            n = split($0, parts, ":")
            if (n > 1) { port = parts[n]; gsub(/[^0-9]/, "", port) }
        }
        $1 == "ServerName" { host = $2 }
        { buf = buf $0 "\n" }
        /<\/VirtualHost>/ {
            if (host != "" && port != "" && index(buf, marker) > 0) print host ":" port
        }
    ' "$file"
}

# Back up every vhost about to be touched, plus a manifest recording each
# one's real original path -- not just its basename, unlike the pre-
# hardening backups this replaces. A basename-only backup silently assumed
# every vhost lived in the one default sites-enabled directory, which broke
# restoring anything found via a non-default HENWEN_APACHE_SITES_DIR, and
# collided two files with the same basename living at different real paths
# (sites-enabled vs. sites-available, or two different custom layouts) into
# one backup slot. The manifest is what rollback.sh reads to know exactly
# where each backed-up file actually came from.
#
# $1 = backup dir (created if needed), $@ = real vhost paths to back up.
henwen_backup_vhosts() {
    local backup_dir="$1"; shift
    mkdir -p "$backup_dir"
    local manifest="$backup_dir/apache-manifest.txt"
    : > "$manifest"
    local f name i=0
    for f in "$@"; do
        i=$((i + 1))
        name="vhost-${i}-$(basename "$f")"
        cp "$f" "$backup_dir/$name"
        printf '%s\t%s\n' "$name" "$f" >> "$manifest"
    done
}

# The reverse of henwen_backup_vhosts(): restores every file the manifest
# in $1 (a backup dir) lists, to the exact original path it was backed up
# from. Prints each restored path on its own line so a caller can report
# what happened; returns 1 if the backup dir has no manifest at all (an
# older, pre-hardening backup -- callers fall back to their own legacy
# restore logic in that case rather than treating this as a hard error).
henwen_restore_vhosts_from_manifest() {
    local backup_dir="$1"
    local manifest="$backup_dir/apache-manifest.txt"
    [ -f "$manifest" ] || return 1
    local name path
    while IFS=$'\t' read -r name path; do
        [ -n "$name" ] || continue
        [ -f "$backup_dir/$name" ] || continue
        cp "$backup_dir/$name" "$path"
        printf '%s\n' "$path"
    done < "$manifest"
}

# The "reload if active, else start fresh, else print the real failure
# instead of systemd's bare 'not active, cannot reload'" dance every
# Apache-touching script here ended up needing independently. `reload`
# requires an already-active service -- true after `apt-get install
# apache2` in the common case, not guaranteed on a box where the package
# was already present but stopped.
henwen_apache_reload_or_start() {
    if systemctl is-active --quiet apache2; then
        systemctl reload apache2
    elif ! systemctl start apache2; then
        echo "   ERROR: apache2 failed to start. Recent log:"
        journalctl -u apache2 --no-pager -n 15 | sed 's/^/     /'
        return 1
    fi
}
