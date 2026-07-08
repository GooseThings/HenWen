#!/bin/bash
# HenWen browser-transmitter spike — apply script.
#
# Enables the PJSIP/WebRTC stack in the local ASL3 Asterisk and wires the
# Apache WSS proxy, so a browser can register and call into this machine's
# own node in phone-control mode (PTT = *99, unkey = #). Everything is
# additive and marker-guarded (safe to re-run); backups of every touched
# file are taken first. Companion: rollback.sh restores them.
#
# Touches:  /etc/asterisk/modules.conf   (append module loads)
#           /etc/asterisk/http.conf      (enabled=yes on loopback bind)
#           /etc/asterisk/pjsip.conf     (append transport/endpoint/auth/aor)
#           /etc/asterisk/custom/extensions.conf  (create context)
#           /etc/asterisk/rtp.conf       (stunaddr, for remote WebRTC ICE)
#           /etc/apache2/sites-enabled/henwen-ssl.conf (WSS proxy line)
#           /etc/asterisk/henwen-tx.secret        (generated SIP password)
# Does NOT restart Asterisk — modules are loaded live; app_rpt keeps running.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="HenWen browser transmitter"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/root/henwen-browsertx-backup-$STAMP"
APACHE_CONF="/etc/apache2/sites-enabled/henwen-ssl.conf"
RPT_CONF_PATH="${RPT_CONF_PATH:-/etc/asterisk/rpt.conf}"

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

# apply.sh only wires up Asterisk PJSIP/WebRTC + the WSS proxy — it assumes
# setup-https.sh already gave this box HTTPS and left $APACHE_CONF in place.
# Check that explicitly and fail with a clear pointer rather than letting
# the "== Backing up" step below die on a bare `cp: cannot stat` a few
# lines later.
if [ ! -f "$APACHE_CONF" ]; then
  echo "ERROR: $APACHE_CONF not found."
  echo ""
  echo "Browser TX needs HTTPS set up first, before apply.sh:"
  echo "  sudo bash $SPIKE_DIR/setup-https.sh <hostname> <email>"
  exit 1
fi

# Local node number: same convention app.py's get_node_numbers() uses
# (first top-level [NNNN] stanza in rpt.conf, 4-7 digits) so the TX feature
# always keys the same node HenWen itself treats as primary. Pass it
# explicitly as $1 to override (e.g. multiple node stanzas and you want a
# specific one, or rpt.conf isn't in the default place).
NODENUM="${1:-}"
if [ -z "$NODENUM" ]; then
  [ -f "$RPT_CONF_PATH" ] || { echo "rpt.conf not found at $RPT_CONF_PATH — pass the node number explicitly: $0 <node>"; exit 1; }
  NODENUM=$(grep -oE '^\[[0-9]{4,7}\]' "$RPT_CONF_PATH" | head -1 | tr -d '[]')
fi
[[ "$NODENUM" =~ ^[0-9]{4,7}$ ]] || { echo "Could not determine a valid node number from $RPT_CONF_PATH — pass it explicitly: $0 <node>"; exit 1; }
echo "== Using node $NODENUM"

echo "== Backing up to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp /etc/asterisk/modules.conf /etc/asterisk/http.conf /etc/asterisk/pjsip.conf "$APACHE_CONF" "$BACKUP_DIR/"
[ -f /etc/asterisk/custom/extensions.conf ] && cp /etc/asterisk/custom/extensions.conf "$BACKUP_DIR/custom-extensions.conf"
echo "$BACKUP_DIR" > /root/henwen-browsertx-last-backup

echo "== modules.conf"
if grep -q "$MARKER" /etc/asterisk/modules.conf; then
  echo "   already patched, skipping"
else
  cat "$SPIKE_DIR/modules.snippet" >> /etc/asterisk/modules.conf
fi

echo "== http.conf"
if grep -qE "^enabled=yes" /etc/asterisk/http.conf; then
  echo "   already enabled, skipping"
else
  sed -i "s|^bindaddr=127\.0\.0\.1$|bindaddr=127.0.0.1\nenabled=yes ; $MARKER: SIP-over-WebSocket, loopback only, WSS terminated by Apache|" /etc/asterisk/http.conf
  grep -qE "^enabled=yes" /etc/asterisk/http.conf || { echo "   FAILED to enable builtin HTTP server"; exit 1; }
fi

echo "== pjsip.conf + secret"
if grep -q "$MARKER" /etc/asterisk/pjsip.conf; then
  echo "   already patched, skipping (existing secret kept)"
else
  TXSECRET=$(openssl rand -hex 16)
  sed -e "s/__TXSECRET__/$TXSECRET/" -e "s/__NODENUM__/$NODENUM/g" "$SPIKE_DIR/pjsip.snippet" >> /etc/asterisk/pjsip.conf
  printf '%s\n' "$TXSECRET" > /etc/asterisk/henwen-tx.secret
  chown asterisk:asterisk /etc/asterisk/henwen-tx.secret
  chmod 600 /etc/asterisk/henwen-tx.secret
fi

echo "== custom/extensions.conf"
mkdir -p /etc/asterisk/custom
if [ -f /etc/asterisk/custom/extensions.conf ] && grep -q "$MARKER" /etc/asterisk/custom/extensions.conf; then
  echo "   already patched, skipping"
else
  sed "s/__NODENUM__/$NODENUM/g" "$SPIKE_DIR/extensions-custom.snippet" >> /etc/asterisk/custom/extensions.conf
  chown asterisk:asterisk /etc/asterisk/custom/extensions.conf
fi

echo "== rtp.conf (STUN for remote WebRTC operators)"
cp /etc/asterisk/rtp.conf "$BACKUP_DIR/" 2>/dev/null || true
if grep -qE "^stunaddr" /etc/asterisk/rtp.conf; then
  echo "   stunaddr already set, skipping"
else
  sed -i "s|^\[general\]\$|[general]\n; $MARKER: learn our public (server-reflexive) ICE candidate so\n; remote WebRTC operators can reach us — without it, media from any\n; non-LAN browser never flows (SIP signaling rides TCP 443 via Apache,\n; but RTP is direct UDP).\nstunaddr=stun.l.google.com:3478|" /etc/asterisk/rtp.conf
  grep -qE "^stunaddr" /etc/asterisk/rtp.conf || { echo "   FAILED to set stunaddr"; exit 1; }
fi

echo "== rtp.conf (narrow RTP port range)"
# 100 ports = 50 concurrent RTP sessions; browser TX needs ~2. Forwarding
# 10000-20000/udp from the internet is 100x more surface than required —
# narrow Asterisk's range so the router forward can be equally narrow.
# (Nothing else in ASL3 uses RTP: IAX2 media rides its own port 4569.)
if grep -qE "^rtpend=10100" /etc/asterisk/rtp.conf; then
  echo "   already narrowed, skipping"
else
  sed -i "s|^rtpend=20000$|rtpend=10100 ; $MARKER: narrowed from 20000 — forward only 10000-10100/udp|" /etc/asterisk/rtp.conf
  grep -qE "^rtpend=10100" /etc/asterisk/rtp.conf || echo "   NOTE: rtpend was not 20000; narrow it manually if desired"
fi

echo "== Loading Asterisk modules (live, no restart)"
grep -E "^load = " "$SPIKE_DIR/modules.snippet" | awk '{print $3}' | while read -r m; do
  out=$(asterisk -rx "module load $m" 2>&1) || true
  case "$out" in
    # Covers every phrasing Asterisk has used for "this is already resident,
    # nothing to do" across versions (e.g. "already loaded and running.") —
    # a module hot-loading fine at boot via modules.conf but refusing a
    # second live "module load" here is normal, not a failure.
    *"Loaded $m"*|*"Already loaded"*|*"already loaded"*|*"is already loaded"*) : ;;
    *) echo "   $m: $out" ;;
  esac
done

echo "== Reloading Asterisk config (http server, dialplan, pjsip)"
asterisk -rx "core reload" >/dev/null
sleep 2

echo "== Apache WSS proxy"
if grep -q "asterisk-ws" "$APACHE_CONF"; then
  echo "   already patched, skipping"
else
  # Insert the websocket proxy just above the HenWen catch-all ProxyPass.
  sed -i 's|^    ProxyPass        / http://127.0.0.1:5000/ retry=0 timeout=120$|    # '"$MARKER"': SIP-over-WebSocket signaling to the loopback-only\n    # Asterisk builtin HTTP server; Apache terminates WSS with the same cert.\n    ProxyPass /asterisk-ws ws://127.0.0.1:8088/ws retry=0\n\n    ProxyPass        / http://127.0.0.1:5000/ retry=0 timeout=120|' "$APACHE_CONF"
  grep -q "asterisk-ws" "$APACHE_CONF" || { echo "   FAILED to insert proxy line"; exit 1; }
fi
apache2ctl configtest 2>&1 | grep -q "Syntax OK" || { echo "   Apache configtest FAILED — restoring backup"; cp "$BACKUP_DIR/henwen-ssl.conf" "$APACHE_CONF"; exit 1; }
# `reload` requires an already-active service. Config just passed
# configtest, so if apache2 isn't running, start it fresh instead of
# failing outright — and if that *also* fails, print the actual log
# instead of just systemd's bare "not active, cannot reload".
if systemctl is-active --quiet apache2; then
  systemctl reload apache2
elif ! systemctl start apache2; then
  echo "   ERROR: apache2 failed to start. Recent log:"
  journalctl -u apache2 --no-pager -n 15 | sed 's/^/     /'
  echo "   Browser TX's WSS proxy and the HTTPS kiosk both depend on Apache — fix this before testing TX."
  exit 1
fi

echo "== Verification"
asterisk -rx "http show status" | head -6
asterisk -rx "pjsip show endpoints" | head -12
echo

# Read back whatever hostname/port setup-https.sh (or the operator, by
# hand) recorded, rather than assuming any particular domain or that it's
# always 443. Fall back to grepping the vhost directly for installs set up
# before these marker files existed.
WSHOST=$(cat /etc/asterisk/henwen-https-hostname 2>/dev/null || true)
if [ -z "$WSHOST" ]; then
    WSHOST=$(grep -h "ServerName" "$APACHE_CONF" 2>/dev/null | awk '{print $2}' | head -1)
fi
WSHOST="${WSHOST:-$(hostname -f 2>/dev/null || hostname)}"
WSPORT=$(cat /etc/asterisk/henwen-https-port 2>/dev/null || echo 443)
WSPORT_SUFFIX=""
[ "$WSPORT" != "443" ] && WSPORT_SUFFIX=":${WSPORT}"

echo "Done. SIP credentials for the test page:"
echo "  WSS URL:  wss://${WSHOST}${WSPORT_SUFFIX}/asterisk-ws"
echo "  Username: henwen-tx"
echo "  Password: $(cat /etc/asterisk/henwen-tx.secret)"
echo "  Dial:     2$NODENUM   (node $NODENUM, phone-control mode: *99 = PTT, # = unkey)"
echo "Port check: sudo bash $SPIKE_DIR/check-ports.sh   (verifies forwards/NAT/WSS)"
echo "Required router forwards: TCP ${WSPORT}, UDP 10000-10100 -> this machine"
echo "Rollback:  sudo bash $SPIKE_DIR/rollback.sh"
