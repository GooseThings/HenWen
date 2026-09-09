#!/bin/bash
# HenWen low-latency RX audio — Apache WebSocket proxy apply script.
#
# Adds the /ws-audio ProxyPass Apache needs to reach audio_ws_relay.py's
# browser-facing WebSocket listener (see audio_ws_relay.py and app.py's
# "Low-latency RX audio" section) from outside this box — mirrors
# tx-spike/apply.sh's own /asterisk-ws proxy line exactly, reusing the same
# proxy_wstunnel Apache module. Unlike tx-spike, this does NOT require
# HTTPS first: Listen already works over plain HTTP today (unlike browser
# TX's getUserMedia, which needs a secure context), so this patches
# whichever HenWen Apache vhost is actually present — the HTTPS one
# (henwen-ssl.conf) if setup-https.sh has been run, else the plain-HTTP one
# (henwen.conf) if Apache is fronting HenWen at all. Everything is additive
# and marker-guarded (safe to re-run); a backup is taken first. Companion:
# rollback.sh restores it.
#
# This only wires the *network path* to the WebSocket server, which app.py
# already spawns/supervises unconditionally (audio_ws_relay.py is always
# running at near-zero idle cost, regardless of whether this script has
# been applied or the low-latency path is selected). Actually selecting the
# low-latency RX audio path is a separate step, from Manager > Audio's
# "RX Audio Path" setting (near TX Diagnostics) — this script only makes
# that choice reachable from outside this box once made.
#
# Touches:  /etc/apache2/sites-enabled/henwen-ssl.conf, OR
#           /etc/apache2/sites-enabled/henwen.conf (whichever is present)
# Does NOT touch Asterisk, and does NOT restart HenWen.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="HenWen low-latency RX audio"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/root/henwen-ws-audio-backup-$STAMP"

# Must match AUDIO_WS_PORT in HenWen.service (default 8098 if that's unset
# there too — see app.py's own AUDIO_WS_PORT constant).
WS_PORT="${AUDIO_WS_PORT:-8098}"

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

APACHE_CONF=""
for candidate in /etc/apache2/sites-enabled/henwen-ssl.conf /etc/apache2/sites-enabled/henwen.conf; do
  if [ -f "$candidate" ]; then
    APACHE_CONF="$candidate"
    break
  fi
done
if [ -z "$APACHE_CONF" ]; then
  echo "ERROR: no HenWen Apache vhost found"
  echo "  (checked /etc/apache2/sites-enabled/henwen-ssl.conf and henwen.conf)."
  echo ""
  echo "This feature needs Apache fronting HenWen to reach audio_ws_relay.py's"
  echo "WebSocket port from outside this box — same requirement browser TX"
  echo "already has for its own WSS proxy. Provision Apache first:"
  echo "  sudo bash $SPIKE_DIR/../tx-spike/setup-https.sh <hostname> <email>"
  echo "(A LAN-only install with no Apache in front of HenWen at all isn't"
  echo "supported by this script — same limitation tx-spike/apply.sh has.)"
  exit 1
fi
echo "== Using Apache vhost: $APACHE_CONF"

echo "== Backing up to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp "$APACHE_CONF" "$BACKUP_DIR/"
echo "$BACKUP_DIR" > /root/henwen-ws-audio-last-backup

echo "== Apache WebSocket proxy"
if grep -q "$MARKER" "$APACHE_CONF"; then
  echo "   already patched, skipping"
else
  # Insert just above the catch-all ProxyPass line, same placement
  # tx-spike/apply.sh uses for /asterisk-ws — matched literally rather than
  # with a variable port, mirroring that script's own existing (accepted)
  # assumption that HenWen listens on the default port 5000 here.
  sed -i 's|^    ProxyPass        / http://127.0.0.1:5000/ retry=0 timeout=120$|    # '"$MARKER"': low-latency Listen audio, proxied to\n    # audio_ws_relay.py'"'"'s own loopback-only WebSocket listener\n    # (app.py spawns/supervises that process unconditionally; this line\n    # is what makes it reachable from outside this box).\n    ProxyPass /ws-audio ws://127.0.0.1:'"$WS_PORT"'/ retry=0\n\n    ProxyPass        / http://127.0.0.1:5000/ retry=0 timeout=120|' "$APACHE_CONF"
  grep -q "$MARKER" "$APACHE_CONF" || {
    echo "   FAILED to insert proxy line — is $APACHE_CONF using the expected"
    echo "   'ProxyPass        / http://127.0.0.1:5000/ retry=0 timeout=120' line?"
    echo "   (unmodified from what install.sh/setup-https.sh write). Nothing was"
    echo "   changed; restoring the backup just in case."
    cp "$BACKUP_DIR/$(basename "$APACHE_CONF")" "$APACHE_CONF"
    exit 1
  }
fi

echo "== proxy_wstunnel module"
if apache2ctl -M 2>/dev/null | grep -q proxy_wstunnel_module; then
  echo "   already enabled, skipping"
else
  a2enmod proxy_wstunnel >/dev/null
  echo "   enabled"
fi

apache2ctl configtest 2>&1 | grep -q "Syntax OK" || {
  echo "   Apache configtest FAILED — restoring backup"
  cp "$BACKUP_DIR/$(basename "$APACHE_CONF")" "$APACHE_CONF"
  exit 1
}
# `reload` requires an already-active service — start fresh if it isn't
# running yet, and surface the real log rather than systemd's bare
# "not active, cannot reload" if that also fails. Mirrors tx-spike/apply.sh.
if systemctl is-active --quiet apache2; then
  systemctl reload apache2
elif ! systemctl start apache2; then
  echo "   ERROR: apache2 failed to start. Recent log:"
  journalctl -u apache2 --no-pager -n 15 | sed 's/^/     /'
  exit 1
fi

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
echo "Done. The low-latency RX audio path is now reachable through Apache at /ws-audio."
echo "Select it in Manager > Audio's 'RX Audio Path' card (near TX Diagnostics) to"
echo "actually switch Listen over to it — this script only wires the network path."
echo "Rollback:  sudo bash $SPIKE_DIR/rollback.sh"
