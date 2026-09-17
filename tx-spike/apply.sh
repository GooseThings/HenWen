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
#           Every Apache vhost apache-common.sh's henwen_discover_vhosts()
#           finds actually fronting HenWen (WSS proxy line added to each —
#           see that file for why this is a scan, not two hardcoded names)
#           /etc/asterisk/henwen-tx.secret        (generated SIP password)
# Does NOT restart Asterisk — modules are loaded live; app_rpt keeps running.
set -euo pipefail

SPIKE_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=../apache-common.sh
. "$SPIKE_DIR/../apache-common.sh"
MARKER="HenWen browser transmitter"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/root/henwen-browsertx-backup-$STAMP"
RPT_CONF_PATH="${RPT_CONF_PATH:-/etc/asterisk/rpt.conf}"

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

# apply.sh only wires up Asterisk PJSIP/WebRTC + the WSS proxy — it assumes
# Apache is already fronting HenWen. henwen_discover_vhosts() (see
# apache-common.sh) finds *every* Apache vhost currently proxying HenWen's
# own Flask port, however many there are and whatever they're named —
# confirmed live that a real install can front HenWen through more than
# one vhost at once (one per hostname), which a fixed two-filename
# candidate list silently only ever found one of.
FLASK_PORT="$(henwen_flask_port)"
mapfile -t APACHE_CONFS < <(henwen_discover_vhosts "$FLASK_PORT")
if [ "${#APACHE_CONFS[@]}" -eq 0 ]; then
  echo "ERROR: no HenWen Apache vhost found (scanned $(henwen_apache_sites_dir)/*.conf"
  echo "for a ProxyPass to 127.0.0.1:${FLASK_PORT}/)."
  echo ""
  echo "Browser TX needs Apache fronting HenWen first:"
  echo "  sudo bash $SPIKE_DIR/setup-https.sh <hostname> <email>   (full HTTPS via Let's Encrypt)"
  echo "  sudo bash $SPIKE_DIR/setup-https.sh --http-only          (if you front HTTPS yourself —"
  echo "                                                             Tailscale Serve/Funnel,"
  echo "                                                             Cloudflare Tunnel, another box)"
  exit 1
fi
echo "== Found ${#APACHE_CONFS[@]} HenWen Apache vhost(s):"
printf '     %s\n' "${APACHE_CONFS[@]}"

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
cp /etc/asterisk/modules.conf /etc/asterisk/http.conf /etc/asterisk/pjsip.conf /etc/asterisk/rtp.conf "$BACKUP_DIR/"
[ -f /etc/asterisk/custom/extensions.conf ] && cp /etc/asterisk/custom/extensions.conf "$BACKUP_DIR/custom-extensions.conf"
henwen_backup_vhosts "$BACKUP_DIR" "${APACHE_CONFS[@]}"
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
  echo "   already patched, skipping"
  # A prior run can be interrupted between patching pjsip.conf and writing
  # the secret file (the two used to be assumed atomic together) -- confirmed
  # live: pjsip.conf had a real password already patched in while
  # /etc/asterisk/henwen-tx.secret was missing, which left TX_SECRET_PATH's
  # "missing file = feature off" switch stuck off even though PJSIP itself
  # was fully configured and working. Recover the already-configured
  # password straight from pjsip.conf rather than generating a new one --
  # a fresh secret here wouldn't match the endpoint's actual auth.
  if [ ! -s /etc/asterisk/henwen-tx.secret ]; then
    echo "   secret file missing/empty -- recovering existing password from pjsip.conf"
    TXSECRET=$(sed -n '/^\[henwen-tx-auth\]$/,/^$/p' /etc/asterisk/pjsip.conf | sed -n 's/^password=//p' | head -1)
    [ -n "$TXSECRET" ] || { echo "   FAILED: could not find password= under [henwen-tx-auth] in pjsip.conf"; exit 1; }
    printf '%s\n' "$TXSECRET" > /etc/asterisk/henwen-tx.secret
    chown asterisk:asterisk /etc/asterisk/henwen-tx.secret
    chmod 600 /etc/asterisk/henwen-tx.secret
  else
    echo "   existing secret kept"
  fi
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
INSERT_TEXT="    # ${MARKER}: SIP-over-WebSocket signaling to the loopback-only
    # Asterisk builtin HTTP server; Apache terminates WSS with the same cert.
    ProxyPass /asterisk-ws ws://127.0.0.1:8088/ws retry=0
"
for conf in "${APACHE_CONFS[@]}"; do
  echo "   $conf"
  rc=0
  henwen_insert_before_proxypass "$conf" "$FLASK_PORT" "asterisk-ws" "$INSERT_TEXT" || rc=$?
  case "$rc" in
    0) echo "      patched" ;;
    1) echo "      already patched, skipping" ;;
    *) echo "      FAILED to insert proxy line — restoring every vhost touched this run"
       henwen_restore_vhosts_from_manifest "$BACKUP_DIR" >/dev/null || true
       exit 1 ;;
  esac
  # Belt-and-suspenders regardless of which branch above ran (also heals a
  # box that already hit the stale-permissions bug from a previous version
  # of this script): Apache vhosts should always be world-readable.
  chmod 644 "$conf"
done
apache2ctl configtest 2>&1 | grep -q "Syntax OK" || {
  echo "   Apache configtest FAILED — restoring every vhost touched this run"
  henwen_restore_vhosts_from_manifest "$BACKUP_DIR" >/dev/null || true
  exit 1
}
henwen_apache_reload_or_start || {
  echo "   Browser TX's WSS proxy and the HTTPS kiosk both depend on Apache — fix this before testing TX."
  exit 1
}

echo "== Verification"
asterisk -rx "http show status" | head -6
asterisk -rx "pjsip show endpoints" | head -12
echo

# Print one set of SIP credentials per host:port this feature is now
# actually reachable on -- every VirtualHost block, in every discovered
# vhost, that both declares a ServerName and really does proxy
# /asterisk-ws, rather than the old single global guess (a marker file
# recorded by setup-https.sh, or the first ServerName found anywhere).
HOSTPORTS_FILE=$(mktemp)
for conf in "${APACHE_CONFS[@]}"; do
  henwen_hostports_with_marker "$conf" "asterisk-ws"
done | sort -u > "$HOSTPORTS_FILE"
PORTS_SEEN=()
while IFS=: read -r host port; do
  [ -n "$host" ] || continue
  echo "  WSS URL:  wss://${host}$([ "$port" != "443" ] && echo ":${port}")/asterisk-ws"
  case " ${PORTS_SEEN[*]:-} " in *" $port "*) ;; *) PORTS_SEEN+=("$port") ;; esac
done < "$HOSTPORTS_FILE"
if [ ! -s "$HOSTPORTS_FILE" ]; then
  WSHOST=$(cat /etc/asterisk/henwen-https-hostname 2>/dev/null || true)
  [ -z "$WSHOST" ] && WSHOST="$(hostname -f 2>/dev/null || hostname)"
  WSPORT=$(cat /etc/asterisk/henwen-https-port 2>/dev/null || echo 443)
  echo "NOTE: could not find a ServerName+asterisk-ws pair in any discovered vhost."
  echo "If you're fronting HTTPS with your own reverse proxy/tunnel, use ITS public"
  echo "hostname instead of \"$WSHOST\" below."
  echo "  WSS URL:  wss://${WSHOST}$([ "$WSPORT" != "443" ] && echo ":${WSPORT}")/asterisk-ws"
  PORTS_SEEN=("$WSPORT")
fi
rm -f "$HOSTPORTS_FILE"

echo "Done. SIP credentials for the test page:"
echo "  Username: henwen-tx"
echo "  Password: $(cat /etc/asterisk/henwen-tx.secret)"
echo "  Dial:     2$NODENUM   (node $NODENUM, phone-control mode: *99 = PTT, # = unkey)"
echo "Port check: sudo bash $SPIKE_DIR/check-ports.sh   (verifies forwards/NAT/WSS)"
echo "Required router forwards: TCP ${PORTS_SEEN[*]}, UDP 10000-10100 -> this machine"
echo "Rollback:  sudo bash $SPIKE_DIR/rollback.sh"
