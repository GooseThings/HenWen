#!/bin/bash
# HenWen browser-transmitter — network/port readiness check.
#
# Verifies every network requirement the feature has, from this box, and
# reports PASS/FAIL per item. Run any time; changes nothing.
#
# Reads /etc/asterisk/henwen-https-{port,mode,hostname} (written by
# setup-https.sh or setup-tailscale.sh) so this adapts to whichever HTTPS
# setup was actually used — a custom port, or Tailscale (no forwarding at
# all) — instead of assuming the public/443 path unconditionally. Falls
# back to public/443 defaults if those markers don't exist yet.
#
# What the browser TX feature needs (public/certbot mode):
#   TCP <https-port> in → Apache (HTTPS kiosk + WSS signaling proxy)  [router forward]
#   UDP 10000-10100  in → Asterisk RTP media                          [router forward]
#   UDP out  → STUN (stun.l.google.com) for both sides' ICE candidates
# In Tailscale mode, none of the above need a router forward — everything
# rides the tailnet directly.
# Asterisk's builtin HTTP server (8088) must be loopback-ONLY — it is checked
# as a negative requirement in both modes.
set -u
PASS=0; FAIL=0; WARN=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
warn() { echo "  WARN  $1"; WARN=$((WARN+1)); }

HTTPS_PORT=$(cat /etc/asterisk/henwen-https-port 2>/dev/null || echo 443)
HTTPS_MODE=$(cat /etc/asterisk/henwen-https-mode 2>/dev/null || echo public)
WSHOST=$(cat /etc/asterisk/henwen-https-hostname 2>/dev/null || true)
if [ -z "$WSHOST" ]; then
  WSHOST=$(grep -h "ServerName" /etc/apache2/sites-enabled/henwen-ssl.conf 2>/dev/null | awk '{print $2}' | head -1)
fi

echo "== Configuration"
echo "        HTTPS mode: ${HTTPS_MODE}   port: ${HTTPS_PORT}   host: ${WSHOST:-?}"

echo "== Local services"
if ss -tln 2>/dev/null | grep -q ":${HTTPS_PORT} "; then ok "Apache listening on TCP ${HTTPS_PORT}"; else bad "nothing listening on TCP ${HTTPS_PORT}"; fi

HTTP8088=$(ss -tln 2>/dev/null | grep ":8088 ")
if [ -z "$HTTP8088" ]; then
  bad "Asterisk HTTP server not listening on 8088 (WSS signaling will fail)"
elif echo "$HTTP8088" | grep -q "127.0.0.1:8088"; then
  ok "Asterisk HTTP server on 8088 is loopback-only (not directly exposed)"
else
  bad "Asterisk HTTP server on 8088 is bound beyond loopback: $HTTP8088"
fi

if asterisk -rx "http show status" 2>/dev/null | grep -q "/ws"; then
  ok "Asterisk WebSocket URI /ws registered"
else
  bad "Asterisk /ws URI not registered (pjsip websocket modules not loaded?)"
fi

echo "== Asterisk RTP/ICE config"
RTPSTART=$(grep -E "^rtpstart=" /etc/asterisk/rtp.conf | head -1 | cut -d= -f2 | awk '{print $1}')
RTPEND=$(grep -E "^rtpend=" /etc/asterisk/rtp.conf | head -1 | cut -d= -f2 | awk '{print $1}')
echo "        RTP range: ${RTPSTART:-?}-${RTPEND:-?} udp"
if [ -n "${RTPEND:-}" ] && [ "$RTPEND" -le 10200 ] 2>/dev/null; then
  ok "RTP range is narrowed (forward only ${RTPSTART}-${RTPEND}/udp on the router)"
else
  warn "RTP range is wide (${RTPSTART:-?}-${RTPEND:-?}) — apply.sh narrows it to 10000-10100"
fi
if [ "$HTTPS_MODE" = "tailscale" ]; then
  # No port-forwarded public IP in this mode, so stunaddr isn't the right
  # check — apply.sh instead remaps local addresses to the Tailscale IP via
  # [ice_host_candidates] (see rtp.conf's own comments on that section).
  TS_IP=$(tailscale ip -4 2>/dev/null || true)
  ICE_MAP=$(grep -E "^[0-9.]+[[:space:]]*=>" /etc/asterisk/rtp.conf 2>/dev/null | head -1)
  if [ -n "$TS_IP" ] && echo "$ICE_MAP" | grep -qE "=> *${TS_IP}(,|\$)"; then
    ok "ice_host_candidates advertises the Tailscale IP ($ICE_MAP)"
  elif [ -z "$TS_IP" ]; then
    bad "could not determine this box's Tailscale IP ('tailscale ip -4' returned nothing) — is Tailscale up?"
  elif [ -n "$ICE_MAP" ]; then
    bad "ice_host_candidates is set but doesn't map to this box's current Tailscale IP ($TS_IP): $ICE_MAP — re-run apply.sh (IP may have changed)"
  else
    bad "no ice_host_candidates mapping in rtp.conf — remote tailnet operators will see your private LAN address in ICE candidates and get no audio. Re-run apply.sh"
  fi
  if grep -qE "^stunaddr" /etc/asterisk/rtp.conf; then
    warn "stunaddr is also set in rtp.conf — its own docs say not to combine that with ice_host_candidates"
  fi
else
  if grep -qE "^stunaddr" /etc/asterisk/rtp.conf; then
    ok "stunaddr set ($(grep -E '^stunaddr' /etc/asterisk/rtp.conf | cut -d= -f2))"
  else
    bad "stunaddr not set in rtp.conf — remote (non-tailnet) operators will get no audio"
  fi
fi

echo "== WSS signaling path (through Apache, as a browser would)"
if [ -n "$WSHOST" ]; then
  WSKEY=$(head -c16 /dev/urandom | base64)
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 \
    -H "Connection: Upgrade" -H "Upgrade: websocket" \
    -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: $WSKEY" \
    -H "Sec-WebSocket-Protocol: sip" "https://${WSHOST}:${HTTPS_PORT}/asterisk-ws" 2>/dev/null)
  if [ "$CODE" = "101" ]; then
    ok "WSS handshake to https://${WSHOST}:${HTTPS_PORT}/asterisk-ws answered 101 (SIP websocket up)"
  else
    bad "WSS handshake to https://${WSHOST}:${HTTPS_PORT}/asterisk-ws returned '$CODE' (expected 101)"
  fi
else
  warn "could not determine the HTTPS hostname (no henwen-https-hostname marker and no ServerName in henwen-ssl.conf); skipped WSS probe"
fi

if [ "$HTTPS_MODE" = "tailscale" ]; then
  echo "== NAT / router forward"
  echo "        Tailscale mode — nothing is forwarded through your router, so the public-NAT/STUN"
  echo "        probe below doesn't apply and is skipped. RTP should reach directly over the"
  echo "        tailnet between this box's Tailscale interface and any tailnet-joined browser."
  echo "        If TX has no audio from a tailnet device, that's the first thing to check --"
  echo "        not port forwarding."
else
  echo "== NAT / router forward (STUN probe from inside the RTP range)"
  STUN_OUT=$(python3 - <<'EOF'
import socket, os, struct, sys

def stun_probe(local_port):
    txid = os.urandom(12)
    req  = struct.pack('!HHI', 0x0001, 0, 0x2112A442) + txid
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(4)
    try:
        s.bind(('0.0.0.0', local_port))
    except OSError:
        s.close(); return None  # port in use (active call) — try another
    try:
        s.sendto(req, ('stun.l.google.com', 19302))
        data, _ = s.recvfrom(2048)
    except Exception:
        s.close(); return 'timeout'
    s.close()
    if len(data) < 20 or data[4:8] != b'\x21\x12\xa4\x42':
        return 'bad-response'
    # walk attributes for XOR-MAPPED-ADDRESS (0x0020)
    i, alen = 20, struct.unpack('!H', data[2:4])[0]
    end = 20 + alen
    while i + 4 <= end:
        atype, asize = struct.unpack('!HH', data[i:i+4])
        if atype == 0x0020 and asize >= 8:
            port = struct.unpack('!H', data[i+6:i+8])[0] ^ 0x2112
            ip   = bytes(b ^ m for b, m in zip(data[i+8:i+12], b'\x21\x12\xa4\x42'))
            return ('.'.join(map(str, ip)), port)
        i += 4 + asize + ((4 - asize % 4) % 4)
    return 'no-mapped-address'

tried = 0
for lp in (10002, 10014, 10036, 10058):
    r = stun_probe(lp)
    if r is None:
        continue  # port busy with a live call
    tried += 1
    if isinstance(r, tuple):
        wan_ip, mapped = r
        print(f"        WAN address via STUN: {wan_ip} (local udp {lp} -> public {mapped})")
        if mapped == lp:
            print(f"  PASS  NAT preserves the port ({lp} -> {mapped}) — the forwarded range will work")
        else:
            print(f"  WARN  NAT rewrote the port ({lp} -> {mapped}) — symmetric-ish NAT; media may still")
            print(  "        work via Asterisk's outbound ICE checks, but verify with a real remote TX test")
        break
    else:
        print(f"  FAIL  STUN probe from udp {lp}: {r} (outbound UDP blocked?)")
        break
if tried == 0:
    print("  WARN  all probe ports busy (active calls) — re-run when idle")
EOF
)
  echo "$STUN_OUT"
  # Fold the embedded probe's own PASS/FAIL/WARN lines into this script's
  # running totals so the final summary below actually reflects them.
  while IFS= read -r line; do
    case "$line" in
      *"  PASS  "*) PASS=$((PASS+1)) ;;
      *"  FAIL  "*) FAIL=$((FAIL+1)) ;;
      *"  WARN  "*) WARN=$((WARN+1)) ;;
    esac
  done <<< "$STUN_OUT"
fi

echo "== Existing AllStarLink ports (unchanged by browser TX, for reference)"
if ss -uln 2>/dev/null | grep -q ":4569 "; then ok "IAX2 on UDP 4569 listening (needs its existing router forward)"; else warn "IAX2 4569 not listening"; fi

echo
echo "Summary: $PASS pass, $FAIL fail, $WARN warn"
if [ "$HTTPS_MODE" = "tailscale" ]; then
  echo "Tailscale mode: no router forwards required."
else
  echo "Router forwards required for browser TX:  TCP ${HTTPS_PORT},  UDP ${RTPSTART:-10000}-${RTPEND:-10100}"
fi
[ "$FAIL" -eq 0 ]
