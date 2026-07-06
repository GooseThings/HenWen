#!/bin/bash
# HenWen browser-transmitter — network/port readiness check.
#
# Verifies every network requirement the feature has, from this box, and
# reports PASS/FAIL per item. Run any time; changes nothing.
#
# What the browser TX feature needs:
#   TCP 443  in   → Apache (HTTPS kiosk + WSS signaling proxy)   [router forward]
#   UDP 10000-10100 in → Asterisk RTP media                      [router forward]
#   UDP out  → STUN (stun.l.google.com) for both sides' ICE candidates
# Asterisk's builtin HTTP server (8088) must be loopback-ONLY — it is checked
# as a negative requirement.
set -u
PASS=0; FAIL=0; WARN=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
warn() { echo "  WARN  $1"; WARN=$((WARN+1)); }

echo "== Local services"
if ss -tln 2>/dev/null | grep -q ":443 "; then ok "Apache listening on TCP 443"; else bad "nothing listening on TCP 443"; fi

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
if grep -qE "^stunaddr" /etc/asterisk/rtp.conf; then
  ok "stunaddr set ($(grep -E '^stunaddr' /etc/asterisk/rtp.conf | cut -d= -f2))"
else
  bad "stunaddr not set in rtp.conf — remote operators will get no audio"
fi

echo "== WSS signaling path (through Apache, as a browser would)"
WSHOST=$(grep -h "ServerName" /etc/apache2/sites-enabled/henwen-ssl.conf 2>/dev/null | awk '{print $2}' | head -1)
if [ -n "$WSHOST" ]; then
  WSKEY=$(head -c16 /dev/urandom | base64)
  CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 6 \
    -H "Connection: Upgrade" -H "Upgrade: websocket" \
    -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: $WSKEY" \
    -H "Sec-WebSocket-Protocol: sip" "https://$WSHOST/asterisk-ws" 2>/dev/null)
  if [ "$CODE" = "101" ]; then
    ok "WSS handshake to https://$WSHOST/asterisk-ws answered 101 (SIP websocket up)"
  else
    bad "WSS handshake to https://$WSHOST/asterisk-ws returned '$CODE' (expected 101)"
  fi
else
  warn "could not determine ServerName from henwen-ssl.conf; skipped WSS probe"
fi

echo "== NAT / router forward (STUN probe from inside the RTP range)"
python3 - <<'EOF'
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

echo "== Existing AllStarLink ports (unchanged by browser TX, for reference)"
if ss -uln 2>/dev/null | grep -q ":4569 "; then ok "IAX2 on UDP 4569 listening (needs its existing router forward)"; else warn "IAX2 4569 not listening"; fi

echo
echo "Summary: $PASS pass, $FAIL fail, $WARN warn"
echo "Router forwards required for browser TX:  TCP 443,  UDP ${RTPSTART:-10000}-${RTPEND:-10100}"
[ "$FAIL" -eq 0 ]
