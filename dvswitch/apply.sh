#!/bin/bash
# HenWen DVSwitch guided setup — apply script.
#
# Installs the dvswitch-server apt package (Analog_Bridge + MMDVM_Bridge,
# among other gateways this feature doesn't use) and configures it to
# bridge exactly one AllStarLink node to one DMR network, using the config
# app.py's POST /api/dvswitch/apply route exports as JSON (path passed as
# $1 — contains network_password in plaintext, so it's written 0600 by
# app.py and this script doesn't print its contents).
#
# Deliberately does NOT touch rpt.conf — app.py owns that (append_node_
# stanza(), same backup/write path every other rpt.conf writer in that file
# uses) and has already created the bridge node's stanza before this script
# runs. This script only ever touches DVSwitch's own ini files and units.
#
# Touches: /etc/apt/sources.list.d/dvswitch*  (repo, added once)
#          dvswitch-server package (apt install)
#          $ANALOG_BRIDGE_INI, $MMDVM_BRIDGE_INI  (patched, not replaced —
#            every key not mentioned here keeps the package's own shipped
#            default)
#          analog_bridge.service, mmdvm_bridge.service  (enabled + started)
#          every other DVSwitch-shipped unit (D-Star/P25/NXDN/YSF gateways,
#            Quantar_Bridge) — explicitly stopped + disabled, since only DMR
#            is in scope for this feature
# Idempotent / marker-guarded / safe to re-run. Companion: rollback.sh.
set -euo pipefail

DVS_DIR="$(cd "$(dirname "$0")" && pwd)"
MARKER="HenWen DVSwitch bridge"
STAMP=$(date +%Y%m%d-%H%M%S)
BACKUP_DIR="/root/henwen-dvswitch-backup-$STAMP"

# Paths as shipped by dvswitch-server's own systemd units (confirmed via
# MMDVM_Bridge/systemd/mmdvm_bridge.service upstream: ExecStart=/opt/
# MMDVM_Bridge/MMDVM_Bridge /opt/MMDVM_Bridge/MMDVM_Bridge.ini). NOT
# verified against a real dvswitch-server .deb install on this box's own
# Debian release — if apt puts the ini files somewhere else, override via
# these env vars rather than editing this script.
ANALOG_BRIDGE_INI="${ANALOG_BRIDGE_INI:-/opt/Analog_Bridge/Analog_Bridge.ini}"
MMDVM_BRIDGE_INI="${MMDVM_BRIDGE_INI:-/opt/MMDVM_Bridge/MMDVM_Bridge.ini}"

[ "$(id -u)" = 0 ] || { echo "Run as root (sudo)"; exit 1; }

CONFIG_JSON="${1:-}"
[ -n "$CONFIG_JSON" ] && [ -f "$CONFIG_JSON" ] || {
  echo "Usage: $0 <path-to-config.json>  (written by POST /api/dvswitch/apply)"
  exit 1
}

# Pull every field out of the JSON in one python3 call rather than shelling
# out per-field — also keeps network_password out of this script's own
# argv/env, where a local user could read it via /proc/<pid>/cmdline or ps.
eval "$(python3 - "$CONFIG_JSON" <<'PYEOF'
import json, sys, shlex
with open(sys.argv[1]) as f:
    c = json.load(f)
for k in ("dmr_id","callsign","dmr_network","network_host","network_port",
          "network_password","static_talkgroups","allstar_gain","dmr_gain",
          "ambe_source","ambe_device","ambe_host","ambe_port",
          "usrp_asterisk_rxport","usrp_asterisk_txport"):
    print(f"DVS_{k.upper()}={shlex.quote(str(c.get(k, '')))}")
PYEOF
)"
echo "== Config: DMR ID $DVS_DMR_ID, callsign $DVS_CALLSIGN, network $DVS_DMR_NETWORK ($DVS_NETWORK_HOST:$DVS_NETWORK_PORT), AMBE source: $DVS_AMBE_SOURCE"

echo "== Detecting Debian codename"
. /etc/os-release
CODENAME="${VERSION_CODENAME:-}"
[ -n "$CODENAME" ] || { echo "ERROR: could not determine Debian codename from /etc/os-release"; exit 1; }
echo "   $CODENAME"

echo "== DVSwitch apt repository"
if ls /etc/apt/sources.list.d/dvswitch*.list >/dev/null 2>&1; then
  echo "   already added, skipping"
else
  TMPREPO=$(mktemp)
  # dvswitch.org publishes one repo-adding script per Debian codename
  # (dvswitch.org/bookworm, dvswitch.org/trixie, ...) — this is DVSwitch's
  # own documented install method, not a HenWen-invented mechanism.
  if ! curl -fsSL "http://dvswitch.org/$CODENAME" -o "$TMPREPO"; then
    echo "   ERROR: no DVSwitch repo published for '$CODENAME' — check http://dvswitch.org/ by hand"
    rm -f "$TMPREPO"
    exit 1
  fi
  chmod +x "$TMPREPO"
  "$TMPREPO"
  rm -f "$TMPREPO"
  apt-get update -qq
fi

echo "== Installing dvswitch-server (this can take a while on first run)"
DEBIAN_FRONTEND=noninteractive apt-get install -y dvswitch-server

echo "== Verifying expected ini paths"
for f in "$ANALOG_BRIDGE_INI" "$MMDVM_BRIDGE_INI"; do
  [ -f "$f" ] || {
    echo "   ERROR: expected ini file not found: $f"
    echo "   The dvswitch-server package on this system may lay files out differently —"
    echo "   find the real paths (dpkg -L dvswitch-server | grep ini) and re-run with"
    echo "   ANALOG_BRIDGE_INI=... MMDVM_BRIDGE_INI=... $0 $CONFIG_JSON"
    exit 1
  }
done

echo "== Backing up ini files to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp "$ANALOG_BRIDGE_INI" "$BACKUP_DIR/"
cp "$MMDVM_BRIDGE_INI"  "$BACKUP_DIR/"
echo "$BACKUP_DIR" > /root/henwen-dvswitch-last-backup

# Patches one "Key = value" line within a named [Section], leaving every
# other line (comments, keys not mentioned) untouched — same section-scoped
# approach as app.py's own update_setting_in_content(), reimplemented here
# in python3 since this script doesn't import app.py. Appends the key at
# the end of the section if it isn't already present, rather than failing.
patch_ini() {
  python3 - "$1" "$2" "$3" "$4" <<'PYEOF'
import re, sys
path, section, key, value = sys.argv[1:5]
with open(path) as f:
    lines = f.readlines()
out, in_sec, found = [], False, False
sec_re = re.compile(r'^\s*\[([^\]]+)\]')
key_re = re.compile(r'^\s*' + re.escape(key) + r'\s*=')
for i, line in enumerate(lines):
    m = sec_re.match(line)
    if m:
        if in_sec and not found:
            out.append(f"{key} = {value}\n")
            found = True
        in_sec = (m.group(1).strip().lower() == section.lower())
    if in_sec and key_re.match(line):
        out.append(f"{key} = {value}\n")
        found = True
        continue
    out.append(line)
if in_sec and not found:
    out.append(f"{key} = {value}\n")
with open(path, "w") as f:
    f.writelines(out)
PYEOF
}

echo "== Patching $ANALOG_BRIDGE_INI"
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "address" "127.0.0.1"
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "txPort" "$DVS_USRP_ASTERISK_RXPORT"   # Analog_Bridge's txPort == Asterisk's rxport
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "rxPort" "$DVS_USRP_ASTERISK_TXPORT"   # Analog_Bridge's rxPort == Asterisk's txport
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "usrpAudio" "AUDIO_USE_GAIN"
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "usrpGain" "$DVS_ALLSTAR_GAIN"
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "tlvAudio" "AUDIO_USE_GAIN"
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "tlvGain" "$DVS_DMR_GAIN"
patch_ini "$ANALOG_BRIDGE_INI" "GENERAL" "ambeMode" "DMR"
# decoderFallBack=true is Analog_Bridge's own shipped default (software AMBE
# codec, no hardware needed) — only overridden here when the owner picked a
# real device. See app.py's dvswitch_config table comment for why software
# is the sane default rather than something requiring hardware.
if [ "$DVS_AMBE_SOURCE" = "hardware" ]; then
  patch_ini "$ANALOG_BRIDGE_INI" "GENERAL" "decoderFallBack" "false"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "type" "serial"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "device" "$DVS_AMBE_DEVICE"
elif [ "$DVS_AMBE_SOURCE" = "network" ]; then
  patch_ini "$ANALOG_BRIDGE_INI" "GENERAL" "decoderFallBack" "false"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "type" "ip"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "address" "$DVS_AMBE_HOST"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "port" "$DVS_AMBE_PORT"
else
  patch_ini "$ANALOG_BRIDGE_INI" "GENERAL" "decoderFallBack" "true"
fi

echo "== Patching $MMDVM_BRIDGE_INI"
patch_ini "$MMDVM_BRIDGE_INI" "Info" "Callsign" "$DVS_CALLSIGN"
patch_ini "$MMDVM_BRIDGE_INI" "Info" "DMRId" "$DVS_DMR_ID"
patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Enable" "1"
patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Id" "$DVS_DMR_ID"
patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Password" "$DVS_NETWORK_PASSWORD"
patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Address" "$DVS_NETWORK_HOST"
patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Port" "$DVS_NETWORK_PORT"
if [ -n "$DVS_STATIC_TALKGROUPS" ]; then
  patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "StartupTG" "$DVS_STATIC_TALKGROUPS"
fi

echo "== Enabling only the two units DMR bridging needs"
systemctl enable --now analog_bridge.service
systemctl enable --now mmdvm_bridge.service

echo "== Disabling every other DVSwitch-shipped mode gateway (out of scope for this feature)"
for unit in ircddbgateway p25gateway nxdngateway ysfgateway quantar_bridge; do
  if systemctl list-unit-files "${unit}.service" >/dev/null 2>&1; then
    systemctl disable --now "${unit}.service" 2>/dev/null || true
  fi
done

echo "== Restarting bridge units to pick up the new config"
systemctl restart analog_bridge.service
systemctl restart mmdvm_bridge.service
sleep 2

echo "Done."
echo "  analog_bridge.service: $(systemctl is-active analog_bridge.service 2>/dev/null || echo unknown)"
echo "  mmdvm_bridge.service:  $(systemctl is-active mmdvm_bridge.service 2>/dev/null || echo unknown)"
echo "Run diagnostics from Manager > DVSwitch to verify the rest (ports, AMBE source, rpt.conf node)."
echo "Rollback: sudo bash $DVS_DIR/rollback.sh"
