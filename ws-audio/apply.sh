#!/bin/bash
# HenWen low-latency RX audio — Apache WebSocket proxy apply script.
#
# Adds the /ws-audio ProxyPass Apache needs to reach audio_ws_relay.py's
# browser-facing WebSocket listener (see audio_ws_relay.py and app.py's
# "Low-latency RX audio" section) from outside this box — mirrors
# tx-spike/apply.sh's own /asterisk-ws proxy line exactly, reusing the same
# proxy_wstunnel Apache module. Unlike tx-spike, this does NOT require
# HTTPS first: Listen already works over plain HTTP today (unlike browser
# TX's getUserMedia, which needs a secure context), so this patches every
# HenWen Apache vhost apache-common.sh's henwen_discover_vhosts() finds —
# however many there are, and whatever they're named (see that file for
# why this is a live scan rather than two hardcoded filenames; confirmed
# live that a real install can front HenWen through more than one vhost
# at once, one per hostname, and a fixed candidate list only ever patched
# the first). Everything is additive and marker-guarded (safe to re-run);
# a backup of every touched vhost is taken first. Companion: rollback.sh
# restores them.
#
# This only wires the *network path* to the WebSocket server, which app.py
# already spawns/supervises unconditionally (audio_ws_relay.py is always
# running at near-zero idle cost, regardless of whether this script has
# been applied or the low-latency path is selected). Actually selecting the
# low-latency RX audio path is a separate step, from Manager > Audio's
# "RX Audio Path" setting (near TX Diagnostics) — this script only makes
# that choice reachable from outside this box once made.
#
# Touches: every Apache vhost currently proxying HenWen's own Flask port
#          (see apache-common.sh's henwen_discover_vhosts())
# Does NOT touch Asterisk, and does NOT restart HenWen.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../apache-common.sh
. "$SPIKE_DIR/../apache-common.sh"
MARKER="HenWen low-latency RX audio"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/root/henwen-ws-audio-backup-$STAMP"

# Must match AUDIO_WS_PORT in HenWen.service (default 8098 if that's unset
# there too — see app.py's own AUDIO_WS_PORT constant).
WS_PORT="${AUDIO_WS_PORT:-8098}"

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

FLASK_PORT="$(henwen_flask_port)"
mapfile -t APACHE_CONFS < <(henwen_discover_vhosts "$FLASK_PORT")
if [ "${#APACHE_CONFS[@]}" -eq 0 ]; then
  echo "ERROR: no HenWen Apache vhost found"
  echo "  (scanned $(henwen_apache_sites_dir)/*.conf for a ProxyPass to 127.0.0.1:${FLASK_PORT}/)."
  echo ""
  echo "This feature needs Apache fronting HenWen to reach audio_ws_relay.py's"
  echo "WebSocket port from outside this box — same requirement browser TX"
  echo "already has for its own WSS proxy. Provision Apache first:"
  echo "  sudo bash $SPIKE_DIR/../tx-spike/setup-https.sh <hostname> <email>"
  echo "(A LAN-only install with no Apache in front of HenWen at all isn't"
  echo "supported by this script — same limitation tx-spike/apply.sh has.)"
  exit 1
fi
echo "== Found ${#APACHE_CONFS[@]} HenWen Apache vhost(s):"
printf '     %s\n' "${APACHE_CONFS[@]}"

echo "== Backing up to $BACKUP_DIR"
henwen_backup_vhosts "$BACKUP_DIR" "${APACHE_CONFS[@]}"
echo "$BACKUP_DIR" > /root/henwen-ws-audio-last-backup

echo "== Apache WebSocket proxy"
INSERT_TEXT="    # ${MARKER}: low-latency Listen audio, proxied to
    # audio_ws_relay.py's own loopback-only WebSocket listener
    # (app.py spawns/supervises that process unconditionally; this line
    # is what makes it reachable from outside this box).
    ProxyPass /ws-audio ws://127.0.0.1:${WS_PORT}/ retry=0
"
for conf in "${APACHE_CONFS[@]}"; do
  echo "   $conf"
  rc=0
  henwen_insert_before_proxypass "$conf" "$FLASK_PORT" "$MARKER" "$INSERT_TEXT" || rc=$?
  case "$rc" in
    0) echo "      patched" ;;
    1) echo "      already patched, skipping" ;;
    *) echo "      FAILED to insert proxy line — is $conf using the expected"
       echo "      'ProxyPass        / http://127.0.0.1:${FLASK_PORT}/ retry=0 timeout=120' line"
       echo "      (or its quoted-directive equivalent)? Restoring every vhost touched this run."
       henwen_restore_vhosts_from_manifest "$BACKUP_DIR" >/dev/null || true
       exit 1 ;;
  esac
  # Belt-and-suspenders regardless of which branch above ran (also heals a
  # box that already hit the stale-permissions bug from a previous version
  # of this script): Apache vhosts should always be world-readable.
  chmod 644 "$conf"
done

echo "== proxy_wstunnel module"
if apache2ctl -M 2>/dev/null | grep -q proxy_wstunnel_module; then
  echo "   already enabled, skipping"
else
  a2enmod proxy_wstunnel >/dev/null
  echo "   enabled"
fi

apache2ctl configtest 2>&1 | grep -q "Syntax OK" || {
  echo "   Apache configtest FAILED — restoring every vhost touched this run"
  henwen_restore_vhosts_from_manifest "$BACKUP_DIR" >/dev/null || true
  exit 1
}
henwen_apache_reload_or_start || exit 1

echo
echo "== Verification"
if ss -tln 2>/dev/null | grep -q ":${WS_PORT} "; then
  echo "   audio_ws_relay.py is listening on 127.0.0.1:${WS_PORT} (good)"
else
  echo "   WARNING: nothing is listening on 127.0.0.1:${WS_PORT} yet."
  echo "   audio_ws_relay.py should already be running (app.py spawns it at"
  echo "   startup) — check: journalctl -u HenWen | grep AUDIO-WS"
fi

echo
echo "Done. The low-latency RX audio path is now reachable through Apache at /ws-audio"
echo "on every hostname listed above."
echo "Select it in Manager > Audio's 'RX Audio Path' card (near TX Diagnostics) to"
echo "actually switch Listen over to it — this script only wires the network path."
echo "Rollback:  sudo bash $SPIKE_DIR/rollback.sh"
