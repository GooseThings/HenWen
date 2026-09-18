#!/bin/bash
# HenWen DVSwitch — self-check. Verifies everything guided setup should
# have produced and reports PASS/FAIL/WARN per item. Run any time; changes
# nothing. Read by app.py's GET /api/dvswitch/diagnostics, same
# fold-stdout-lines-into-the-report pattern as tx-spike/check-ports.sh.
#
# What this can verify from inside the box:
#   package installed, apt repo present, expected systemd units in the
#   right active/inactive state, USRP UDP ports bound, AMBE source reachable
# What this explicitly CANNOT verify:
#   whether the DMR network (BrandMeister/TGIF/etc.) actually accepted the
#   DMR ID and password — that requires the network's own confirmation
#   (its dashboard), so that check is always a WARN, never a PASS/FAIL.
set -u
PASS=0; FAIL=0; WARN=0
ok()   { echo "  PASS  $1"; PASS=$((PASS+1)); }
bad()  { echo "  FAIL  $1"; FAIL=$((FAIL+1)); }
warn() { echo "  WARN  $1"; WARN=$((WARN+1)); }

CONFIG_JSON="${1:-/etc/asterisk/henwen-dvswitch-config.json}"
ANALOG_BRIDGE_INI="${ANALOG_BRIDGE_INI:-/opt/Analog_Bridge/Analog_Bridge.ini}"
MMDVM_BRIDGE_INI="${MMDVM_BRIDGE_INI:-/opt/MMDVM_Bridge/MMDVM_Bridge.ini}"

echo "== Package"
if dpkg -s analog-bridge >/dev/null 2>&1 && dpkg -s mmdvm-bridge >/dev/null 2>&1; then
  ok "analog-bridge + mmdvm-bridge packages installed"
else
  bad "analog-bridge/mmdvm-bridge not installed — run guided setup from Manager > DVSwitch"
fi

# apply.sh no longer installs the dvswitch-server metapackage (only the two
# packages actually needed -- see its own comment), specifically because
# that metapackage's Recommends pull in dvswitch-dashboard, a PHP control
# panel with no login that installs its own global Apache Alias -- confirmed
# live this made an unauthenticated DMR-bridge control panel reachable on
# real public HTTPS hostnames. Checked here so an install that ran an older
# apply.sh (or had it installed some other way) gets flagged, not silently
# left exposed.
if [ -e /etc/apache2/conf-enabled/dvswitch.conf ]; then
  bad "dvswitch-dashboard's Apache config is enabled (/etc/apache2/conf-enabled/dvswitch.conf) -- this is an UNAUTHENTICATED control panel reachable on every vhost fronting this box. Disable it: sudo a2disconf dvswitch && sudo systemctl reload apache2"
else
  ok "dvswitch-dashboard's Apache config is not enabled"
fi

if ls /etc/apt/sources.list.d/dvswitch*.list >/dev/null 2>&1; then
  ok "DVSwitch apt repository present"
else
  warn "No DVSwitch apt repository found under /etc/apt/sources.list.d/ — package may have been installed a different way, or not at all"
fi

echo "== Services"
for unit in analog_bridge mmdvm_bridge; do
  state=$(systemctl is-active "${unit}.service" 2>/dev/null || echo "inactive")
  if [ "$state" = "active" ]; then
    ok "${unit}.service is active"
  else
    bad "${unit}.service is $state"
  fi
done

echo "== Unused mode gateways (should be off — DMR-only in scope)"
for unit in ircddbgateway p25gateway nxdngateway ysfgateway quantar_bridge; do
  if systemctl list-unit-files "${unit}.service" >/dev/null 2>&1; then
    state=$(systemctl is-active "${unit}.service" 2>/dev/null || echo "inactive")
    if [ "$state" != "active" ]; then
      ok "${unit}.service correctly inactive"
    else
      warn "${unit}.service is active — guided setup only intends analog_bridge/mmdvm_bridge; "
           "stop it manually if you don't need this mode"
    fi
  fi
done

echo "== USRP loopback ports"
if [ -f "$ANALOG_BRIDGE_INI" ]; then
  TXPORT=$(grep -iE '^\s*txPort\s*=' "$ANALOG_BRIDGE_INI" | head -1 | cut -d= -f2 | tr -d ' \r')
  RXPORT=$(grep -iE '^\s*rxPort\s*=' "$ANALOG_BRIDGE_INI" | head -1 | cut -d= -f2 | tr -d ' \r')
  for p in "$TXPORT" "$RXPORT"; do
    [ -n "$p" ] || continue
    if ss -uln 2>/dev/null | grep -q ":${p} "; then
      ok "UDP ${p} bound (Analog_Bridge)"
    else
      warn "UDP ${p} not currently bound — normal if analog_bridge.service just started; "
           "re-check if this persists"
    fi
  done
else
  bad "$ANALOG_BRIDGE_INI not found"
fi

echo "== AMBE source"
if [ -f "$CONFIG_JSON" ]; then
  AMBE_SOURCE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('ambe_source','software'))" "$CONFIG_JSON" 2>/dev/null || echo "software")
  case "$AMBE_SOURCE" in
    software)
      ok "Using bundled software AMBE codec — no external device required"
      ;;
    hardware)
      AMBE_DEVICE=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('ambe_device',''))" "$CONFIG_JSON" 2>/dev/null || echo "")
      if [ -n "$AMBE_DEVICE" ] && [ -e "$AMBE_DEVICE" ]; then
        ok "AMBE hardware device present: $AMBE_DEVICE"
      else
        bad "AMBE hardware device not found: ${AMBE_DEVICE:-(none configured)}"
      fi
      ;;
    network)
      AMBE_HOST=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('ambe_host',''))" "$CONFIG_JSON" 2>/dev/null || echo "")
      AMBE_PORT=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1])).get('ambe_port',''))" "$CONFIG_JSON" 2>/dev/null || echo "")
      if [ -n "$AMBE_HOST" ] && timeout 3 bash -c "cat < /dev/null > /dev/tcp/$AMBE_HOST/$AMBE_PORT" 2>/dev/null; then
        ok "Network AMBEServer reachable at $AMBE_HOST:$AMBE_PORT"
      else
        bad "Network AMBEServer not reachable at ${AMBE_HOST:-?}:${AMBE_PORT:-?}"
      fi
      ;;
    *)
      warn "Unknown ambe_source '$AMBE_SOURCE' in $CONFIG_JSON"
      ;;
  esac
else
  warn "No exported config found at $CONFIG_JSON — run guided setup first"
fi

warn "DMR network authentication cannot be verified from this box — check your network's own dashboard (e.g. BrandMeister self-care, TGIF's site) after starting the bridge to confirm the DMR ID/password were actually accepted"

echo
echo "Summary: $PASS pass, $FAIL fail, $WARN warn"
[ "$FAIL" -eq 0 ]
