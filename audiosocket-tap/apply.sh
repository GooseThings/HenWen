#!/bin/bash
# HenWen AudioSocket tap — apply script.
#
# Installs the dialplan context and Asterisk modules needed to capture a
# node's live RX audio over a low-latency TCP socket (AudioSocket) instead
# of MixMonitor's buffered file writer, which was found to add ~2s of
# latency to the Listen audio stream (issue #30). Everything is additive
# and marker-guarded (safe to re-run); backups of every touched file are
# taken first. Companion: rollback.sh restores them.
#
# Touches:  /etc/asterisk/modules.conf              (append module loads)
#           /etc/asterisk/custom/extensions.conf     (append dialplan context)
# Does NOT restart Asterisk — modules are loaded live; app_rpt keeps running.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="HenWen AudioSocket tap"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/root/henwen-audiosocket-backup-$STAMP"

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

echo "== Backing up to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp /etc/asterisk/modules.conf "$BACKUP_DIR/"
[ -f /etc/asterisk/custom/extensions.conf ] && cp /etc/asterisk/custom/extensions.conf "$BACKUP_DIR/custom-extensions.conf"
echo "$BACKUP_DIR" > /root/henwen-audiosocket-last-backup

echo "== modules.conf"
if grep -q "$MARKER" /etc/asterisk/modules.conf; then
  echo "   already patched, skipping"
else
  cat "$SPIKE_DIR/modules.snippet" >> /etc/asterisk/modules.conf
fi

echo "== custom/extensions.conf"
mkdir -p /etc/asterisk/custom
if [ -f /etc/asterisk/custom/extensions.conf ] && grep -q "$MARKER" /etc/asterisk/custom/extensions.conf; then
  echo "   already patched, skipping"
else
  cat "$SPIKE_DIR/extensions-custom.snippet" >> /etc/asterisk/custom/extensions.conf
  chown asterisk:asterisk /etc/asterisk/custom/extensions.conf
fi

echo "== Loading Asterisk modules (live, no restart)"
# Order matters: app_audiosocket.so resolves a symbol from res_audiosocket.so
# at load time and fails outright ("undefined symbol") if loaded first.
#
# Asterisk's "module load" on an already-resident module prints the same
# "Unable to load module X" / command-failed text as a genuine failure
# (e.g. the missing-symbol case above) — there's no distinct "already
# loaded" phrasing to match on for these two modules. So check residency
# via "module show like" first (skip the load attempt entirely if already
# Running) and, when a load attempt is actually made, verify success the
# same way afterward rather than trusting stdout text.
for m in res_audiosocket.so app_audiosocket.so app_chanspy.so; do
  if asterisk -rx "module show like $m" 2>&1 | grep -q "^$m .*Running"; then
    echo "   $m: already loaded, skipping"
    continue
  fi
  out=$(asterisk -rx "module load $m" 2>&1) || true
  if asterisk -rx "module show like $m" 2>&1 | grep -q "^$m .*Running"; then
    echo "   $m: loaded"
  else
    echo "   $m: $out"
    echo "   FAILED to load $m — aborting"
    exit 1
  fi
done

echo "== Reloading Asterisk dialplan"
asterisk -rx "dialplan reload" >/dev/null

echo "== Verification"
asterisk -rx "module show like audiosocket"
asterisk -rx "module show like chanspy"
asterisk -rx "dialplan show henwen-audiosocket-tap" | head -5
echo
echo "Done. Rollback: sudo bash $SPIKE_DIR/rollback.sh"
