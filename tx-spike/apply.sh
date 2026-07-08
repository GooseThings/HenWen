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
#           /etc/asterisk/rtp.conf       (stunaddr in public mode, or
#                                          [ice_host_candidates] mappings to
#                                          this box's Tailscale IP in
#                                          Tailscale mode — see
#                                          henwen-https-mode below)
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

echo "== rtp.conf (ICE addressing for remote WebRTC operators)"
cp /etc/asterisk/rtp.conf "$BACKUP_DIR/" 2>/dev/null || true
RTP_CONF=/etc/asterisk/rtp.conf
HTTPS_MODE=$(cat /etc/asterisk/henwen-https-mode 2>/dev/null || echo public)
STUN_BEGIN="; BEGIN $MARKER: stunaddr"
STUN_END="; END $MARKER: stunaddr"
ICE_BEGIN="; BEGIN $MARKER: ice_host_candidates"
ICE_END="; END $MARKER: ice_host_candidates"

# Strip any block a previous run of this script added, so re-running always
# reflects the *current* mode instead of accumulating stale config from a
# prior public<->Tailscale switch.
sed -i "/^${STUN_BEGIN}\$/,/^${STUN_END}\$/d" "$RTP_CONF"
sed -i "/^${ICE_BEGIN}\$/,/^${ICE_END}\$/d" "$RTP_CONF"

if [ "$HTTPS_MODE" = "tailscale" ]; then
  # No port-forwarded public IP exists to discover via STUN in this mode —
  # the address that's actually reachable by a remote tailnet browser is
  # this box's Tailscale IP. rtp.conf's [ice_host_candidates] section exists
  # for exactly this (see its comments further up in the file): remap each
  # local interface's real address to the Tailscale IP so it's what gets
  # advertised in ICE, instead of a private LAN address no tailnet peer can
  # reach. include_local_address keeps LAN-direct clients working too.
  TS_IP=$(tailscale ip -4 2>/dev/null || true)
  if [ -z "$TS_IP" ]; then
    echo "   WARNING: 'tailscale ip -4' returned nothing (is Tailscale up?) — skipping ICE candidate override; TX will only work for LAN-local browsers until this is fixed and apply.sh is re-run"
  else
    LOCAL_IPS=$(ip -4 -o addr show scope global 2>/dev/null | awk '$2 != "tailscale0" {print $4}' | cut -d/ -f1 | grep -vxF "$TS_IP" || true)
    if [ -z "$LOCAL_IPS" ]; then
      echo "   WARNING: found no non-Tailscale local IPv4 address to remap — skipping"
    else
      # [ice_host_candidates] ships as a commented-out example section in
      # stock rtp.conf; appending our own active lines only lands under it
      # correctly if the header itself is actually present.
      grep -qE "^\[ice_host_candidates\]" "$RTP_CONF" || echo -e "\n[ice_host_candidates]" >> "$RTP_CONF"
      {
        echo "$ICE_BEGIN"
        for ip in $LOCAL_IPS; do
          echo "${ip} => ${TS_IP},include_local_address"
        done
        echo "$ICE_END"
      } >> "$RTP_CONF"
      echo "   advertising Tailscale IP $TS_IP in place of: $(echo "$LOCAL_IPS" | tr '\n' ' ')"
    fi
  fi
  RAW_STUN=$(grep -E "^stunaddr=" "$RTP_CONF" || true)
  if [ "$RAW_STUN" = "stunaddr=stun.l.google.com:3478" ]; then
    # Exactly the value earlier (pre-marker) versions of this script wrote —
    # safe to retire automatically now that ice_host_candidates covers
    # Tailscale mode; a hand-customized value is left alone (below).
    sed -i "s|^stunaddr=stun\.l\.google\.com:3478\$|; stunaddr=stun.l.google.com:3478 ; disabled by $MARKER: superseded by ice_host_candidates in Tailscale mode|" "$RTP_CONF"
    echo "   disabled a pre-existing stunaddr line from an earlier apply.sh run"
  elif [ -n "$RAW_STUN" ]; then
    echo "   NOTE: a pre-existing (non-HenWen-managed) stunaddr line is also set ($RAW_STUN) —"
    echo "   rtp.conf's own docs say not to combine that with ice_host_candidates; consider removing it"
  fi
else
  if grep -qE "^stunaddr" "$RTP_CONF"; then
    echo "   stunaddr already set, skipping"
  else
    # Must land inside [general] (stunaddr is a [general]-only option, and
    # rtp.conf has [ice_host_candidates] later in the file — appending at
    # EOF would silently scope it under that section instead).
    sed -i "s|^\[general\]\$|[general]\n${STUN_BEGIN}\n; learn our public (server-reflexive) ICE candidate so remote WebRTC\n; operators can reach us — without it, media from any non-LAN browser\n; never flows (SIP signaling rides TCP 443 via Apache, but RTP is\n; direct UDP).\nstunaddr=stun.l.google.com:3478\n${STUN_END}|" "$RTP_CONF"
    grep -qE "^stunaddr" "$RTP_CONF" || { echo "   FAILED to set stunaddr"; exit 1; }
  fi
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
    *"Loaded $m"*|*"Already loaded"*|*"is already loaded"*) : ;;
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
systemctl reload apache2

echo "== Verification"
asterisk -rx "http show status" | head -6
asterisk -rx "pjsip show endpoints" | head -12
echo

# Read back whatever hostname/port setup-https.sh or setup-tailscale.sh (or
# the operator, by hand) recorded, rather than assuming any particular
# domain or that it's always 443. Fall back to grepping the vhost directly
# for installs set up before these marker files existed.
WSHOST=$(cat /etc/asterisk/henwen-https-hostname 2>/dev/null || true)
if [ -z "$WSHOST" ]; then
    WSHOST=$(grep -h "ServerName" "$APACHE_CONF" 2>/dev/null | awk '{print $2}' | head -1)
fi
WSHOST="${WSHOST:-$(hostname -f 2>/dev/null || hostname)}"
WSPORT=$(cat /etc/asterisk/henwen-https-port 2>/dev/null || echo 443)
WSPORT_SUFFIX=""
[ "$WSPORT" != "443" ] && WSPORT_SUFFIX=":${WSPORT}"
HTTPS_MODE=$(cat /etc/asterisk/henwen-https-mode 2>/dev/null || echo public)

echo "Done. SIP credentials for the test page:"
echo "  WSS URL:  wss://${WSHOST}${WSPORT_SUFFIX}/asterisk-ws"
echo "  Username: henwen-tx"
echo "  Password: $(cat /etc/asterisk/henwen-tx.secret)"
echo "  Dial:     2$NODENUM   (node $NODENUM, phone-control mode: *99 = PTT, # = unkey)"
echo "Port check: sudo bash $SPIKE_DIR/check-ports.sh   (verifies forwards/NAT/WSS)"
if [ "$HTTPS_MODE" = "tailscale" ]; then
    echo "Tailscale mode: no router forwards needed — reachable only from devices on your tailnet."
else
    echo "Required router forwards: TCP ${WSPORT}, UDP 10000-10100 -> this machine"
fi
echo "Rollback:  sudo bash $SPIKE_DIR/rollback.sh"
