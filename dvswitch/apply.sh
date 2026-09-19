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
DVSWITCH_INI="${DVSWITCH_INI:-/opt/MMDVM_Bridge/DVSwitch.ini}"
DVSWITCH_SH="${DVSWITCH_SH:-/opt/MMDVM_Bridge/dvswitch.sh}"

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

# BrandMeister only: derived from network_host's leading master-number
# digits (e.g. "3104.master.brandmeister.network" or "3104.repeater.net"
# -> "3104") plus ".repeater.net" -- BrandMeister's own STFU/ODMRT hostname
# convention (confirmed live: DVSwitch.ini's own shipped [STFU] example
# uses this exact "<master#>.repeater.net" shape, and it connected
# successfully on the first try against this box's real master). Not
# independently verified across every BrandMeister master number --
# override by hand-editing DVSwitch.ini's [STFU] BMAddress afterward if a
# particular master doesn't follow this pattern.
DVS_STFU_BMADDRESS=$(python3 -c "
import re, sys
m = re.match(r'^(\d+)\.', sys.argv[1])
print(m.group(1) + '.repeater.net' if m else sys.argv[1])
" "$DVS_NETWORK_HOST")
# TalkerAlias is capped at 27 chars by STFU itself; callsign alone is
# always well under that, so no separate truncation is needed.
DVS_STFU_STARTTG=$(echo "$DVS_STATIC_TALKGROUPS" | cut -d',' -f1 | xargs)

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
  # own documented install method, not a HenWen-invented mechanism. Fetched
  # over HTTPS (not the plain-HTTP URL DVSwitch's own docs show) since this
  # script is executed as root immediately after download -- plain HTTP here
  # would be a MITM-to-RCE vector. No pinned checksum: the script differs
  # per codename and DVSwitch updates it over time, so a hardcoded hash
  # would just break every future install rather than catch tampering.
  if ! curl -fsSL "https://dvswitch.org/$CODENAME" -o "$TMPREPO"; then
    echo "   ERROR: no DVSwitch repo published for '$CODENAME' — check https://dvswitch.org/ by hand"
    rm -f "$TMPREPO"
    exit 1
  fi
  chmod +x "$TMPREPO"
  "$TMPREPO"
  rm -f "$TMPREPO"
  apt-get update -qq
fi

echo "== Installing analog-bridge + mmdvm-bridge + stfu (this can take a while on first run)"
# Deliberately NOT `apt-get install dvswitch-server` -- that's a pure
# metapackage with everything as Recommends, not Depends (confirmed via
# `dpkg -s dvswitch-server`: Recommends: dvswitch, dvswitch-monit,
# dvswitch-dashboard, dvswitch-menu). Installing it pulled in dvswitch-
# dashboard, a PHP control panel with NO LOGIN, which installed its own
# Apache config (Alias /dvswitch ...) globally across every vhost --
# confirmed live that this made an unauthenticated DMR-bridge control panel
# reachable on this box's real public HTTPS hostnames. --no-install-
# recommends installs exactly the packages this feature needs and nothing
# else DVSwitch ships (D-Star/P25/NXDN/YSF gateways, the dashboard,
# Quantar_Bridge, Analog_Reflector) -- their own hard Depends (actual
# libraries) still install normally, only the optional companion packages
# are skipped. `stfu` (confirmed via `apt-cache depends stfu`: only depends
# on the same dvswitch-base analog-bridge/mmdvm-bridge already pull in, no
# dvswitch-dashboard risk) is BrandMeister's dedicated ODMRT client -- see
# the "BrandMeister via STFU, not MMDVM_Bridge" section below for why this
# feature uses it instead of MMDVM_Bridge's generic Homebrew DMR gateway
# for BrandMeister specifically. Installed unconditionally (small, harmless
# when unused) rather than only for brandmeister configs, to keep this one
# apt-get line simple and idempotent regardless of which network is chosen.
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends analog-bridge mmdvm-bridge stfu

echo "== Verifying expected ini paths"
for f in "$ANALOG_BRIDGE_INI" "$MMDVM_BRIDGE_INI" "$DVSWITCH_INI"; do
  [ -f "$f" ] || {
    echo "   ERROR: expected ini file not found: $f"
    echo "   The dvswitch-server package on this system may lay files out differently —"
    echo "   find the real paths (dpkg -L dvswitch-server | grep ini) and re-run with"
    echo "   ANALOG_BRIDGE_INI=... MMDVM_BRIDGE_INI=... DVSWITCH_INI=... $0 $CONFIG_JSON"
    exit 1
  }
done

echo "== Backing up ini files to $BACKUP_DIR"
mkdir -p "$BACKUP_DIR"
cp "$ANALOG_BRIDGE_INI" "$BACKUP_DIR/"
cp "$MMDVM_BRIDGE_INI"  "$BACKUP_DIR/"
cp "$DVSWITCH_INI"      "$BACKUP_DIR/"
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
# These two used to be swapped (txPort got the RXPORT value and vice versa)
# -- harmless-looking since Analog_Bridge started and reported status fine
# either way (its TLV/remote-control ports are separate and unaffected),
# but it meant Analog_Bridge's rxPort collided with the port Asterisk's own
# USRP channel already binds (confirmed live via `ss -u -p`: Asterisk holds
# 32001, matching rpt.conf's own "32001 = UDP port ASL is listening on"
# comment on the node's rxchannel line), so Analog_Bridge could never
# actually bind its receiving port -- DMR audio reached STFU/Analog_Bridge
# fine (confirmed via STFU's own log showing real traffic) but never made
# it to Asterisk, with no error visible anywhere in this app because
# nothing here checks for it. Confirmed fixed against the same box's
# original (pre-DVSwitch-feature) Analog_Bridge.ini, which had these two
# values the other way around.
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "txPort" "$DVS_USRP_ASTERISK_TXPORT"   # Analog_Bridge's txPort == Asterisk's rxport
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "rxPort" "$DVS_USRP_ASTERISK_RXPORT"   # Analog_Bridge's rxPort == Asterisk's txport
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "usrpAudio" "AUDIO_USE_GAIN"
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "usrpGain" "$DVS_ALLSTAR_GAIN"
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "tlvAudio" "AUDIO_USE_GAIN"
patch_ini "$ANALOG_BRIDGE_INI" "USRP" "tlvGain" "$DVS_DMR_GAIN"
# ambeMode belongs to [AMBE_AUDIO], NOT [GENERAL] -- confirmed against a
# real installed Analog_Bridge.ini (patching it into [GENERAL] produced a
# stray line Analog_Bridge's parser treated as fatal: "parse error ... line
# 31", reproduced live on a real box). [AMBE_AUDIO]'s own shipped default is
# already "DMR", but set it explicitly rather than relying on that default
# holding across package versions, since this feature is DMR-only.
patch_ini "$ANALOG_BRIDGE_INI" "AMBE_AUDIO" "ambeMode" "DMR"
# decoderFallBack=true is Analog_Bridge's own shipped default (software AMBE
# codec, no hardware needed) — only overridden here when the owner picked a
# real device. See app.py's dvswitch_config table comment for why software
# is the sane default rather than something requiring hardware.
#
# [DV3000]'s real keys (confirmed against a real installed ini's own
# commented-out example block) are `serial` (true = local USB dongle, false
# = network AMBEServer) and `address` (doubles as either the serial device
# path or the AMBEServer's IP, depending on `serial`) plus `rxPort` for the
# AMBEServer's port -- NOT the `type`/`device`/`port` keys an earlier
# version of this script used, which Analog_Bridge would have silently
# ignored as unknown keys rather than actually configuring the device.
if [ "$DVS_AMBE_SOURCE" = "hardware" ]; then
  patch_ini "$ANALOG_BRIDGE_INI" "GENERAL" "decoderFallBack" "false"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "serial" "true"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "address" "$DVS_AMBE_DEVICE"
elif [ "$DVS_AMBE_SOURCE" = "network" ]; then
  patch_ini "$ANALOG_BRIDGE_INI" "GENERAL" "decoderFallBack" "false"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "serial" "false"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "address" "$DVS_AMBE_HOST"
  patch_ini "$ANALOG_BRIDGE_INI" "DV3000" "rxPort" "$DVS_AMBE_PORT"
else
  patch_ini "$ANALOG_BRIDGE_INI" "GENERAL" "decoderFallBack" "true"
fi

# BrandMeister via STFU, not MMDVM_Bridge -- MMDVM_Bridge's generic
# Homebrew [DMR Network] gateway reproducibly segfaults connecting to a
# real BrandMeister master on this exact mmdvm-bridge build (1.6.8-
# 20241231-94): a null CDMRNetwork* dereference inside CDMRControl's
# constructor / CDMRSlot::init, confirmed via coredumpctl/gdb, with correct
# credentials, open network reachability (verified via a raw UDP probe
# getting a real RPTACK back), and every ini field this script controls
# individually ruled out as the trigger -- it is a bug in that compiled
# binary talking to BrandMeister specifically, not a HenWen config issue
# (the exact same binary, exact same credentials, connects to TGIF fine).
# `stfu` is BrandMeister's own dedicated ODMRT client (a separate DVSwitch
# package/binary, "STFU" = Simple Terminal Feature Update, port 54006
# instead of Homebrew's 62031) -- confirmed live it connects instantly with
# the same DMR ID/password that crashed MMDVM_Bridge, receives real TG
# traffic, and `dvswitch.sh tune <tg>` works against it identically to the
# DMR path (the txTg remote command rides Analog_Bridge's TLV control
# channel to whichever partner is currently routed, not a DMR-specific
# mechanism). TGIF/custom still use MMDVM_Bridge's Homebrew gateway
# unchanged, since that path is proven working and STFU/ODMRT is a
# BrandMeister-specific protocol with no equivalent there.
if [ "$DVS_DMR_NETWORK" = "brandmeister" ]; then
  echo "== Patching $MMDVM_BRIDGE_INI (disabling MMDVM_Bridge's own DMR Network -- STFU handles BrandMeister instead)"
  patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Enable" "0"

  echo "== Patching $DVSWITCH_INI [STFU] (BrandMeister ODMRT)"
  patch_ini "$DVSWITCH_INI" "STFU" "BMAddress" "$DVS_STFU_BMADDRESS"
  patch_ini "$DVSWITCH_INI" "STFU" "BMPort" "54006"
  patch_ini "$DVSWITCH_INI" "STFU" "BMPassword" "$DVS_NETWORK_PASSWORD"
  patch_ini "$DVSWITCH_INI" "STFU" "UserID" "$DVS_DMR_ID"
  patch_ini "$DVSWITCH_INI" "STFU" "TalkerAlias" "$DVS_CALLSIGN"
  if [ -n "$DVS_STFU_STARTTG" ]; then
    patch_ini "$DVSWITCH_INI" "STFU" "StartTG" "$DVS_STFU_STARTTG"
  fi

  # stfu.service's own shipped unit lists exit code 255 (its own
  # documented ERROR_SERVER_TIMEOUT, per the comment block at the bottom of
  # /usr/lib/systemd/system/stfu.service) in RestartPreventExitStatus,
  # so systemd deliberately does NOT auto-restart it on that exit --
  # confirmed live this left the bridge silently dead (no audio, no error
  # anywhere) for hours after an idle-period BrandMeister connection
  # timeout, requiring a manual restart to notice and fix. A server
  # timeout is exactly the kind of transient condition that should be
  # retried, unlike the unit's other prevented codes (251 port-in-use, 253
  # ini-parse-error, 254 fatal-error) which really do need a human to look
  # at the config -- so this drop-in removes only 255 from the list rather
  # than clearing it entirely. A drop-in (not editing the package's own
  # unit file directly) survives a `stfu` package upgrade.
  echo "== Installing stfu.service systemd drop-in (auto-restart on server timeout)"
  mkdir -p /etc/systemd/system/stfu.service.d
  cat > /etc/systemd/system/stfu.service.d/henwen-restart-on-timeout.conf <<'DROPIN'
[Service]
RestartPreventExitStatus=
RestartPreventExitStatus=251 253 254
DROPIN
  systemctl daemon-reload

  echo "== Enabling analog_bridge.service + stfu.service (mmdvm_bridge.service not needed/used for BrandMeister)"
  systemctl enable --now analog_bridge.service
  systemctl enable --now stfu.service
  systemctl disable --now mmdvm_bridge.service 2>/dev/null || true
else
  echo "== Patching $MMDVM_BRIDGE_INI [DMR Network] ($DVS_DMR_NETWORK)"
  # NOT patching [Info] Callsign/DMRId -- those aren't real keys in that
  # section (confirmed against a real installed ini: [Info] is repeater
  # metadata -- RXFrequency/TXFrequency/Power/Latitude/Longitude/Location/...
  # -- Callsign+Id belong in [General], which the package's own shipped ini
  # already carries correctly and this script has never touched). An earlier
  # version of this script wrote them there anyway; removed as dead/wrong
  # config rather than left in as harmless-looking noise.
  #
  # NOT patching [DMR Network] Id either, for the same reason: that key
  # defaults to (falls back to) [General]'s own Id when absent -- which is
  # the DMR ID *with* the 2-digit repeater/hotspot SSID suffix these logins
  # actually require (e.g. DMR ID 3206012 -> 320601211), the same
  # convention this box's own [General] Id already correctly carries by
  # hand. Leaving [DMR Network] Id unset keeps the fallback-to-[General]
  # behavior already proven working under TGIF.
  patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Enable" "1"
  patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Password" "$DVS_NETWORK_PASSWORD"
  patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Address" "$DVS_NETWORK_HOST"
  patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Port" "$DVS_NETWORK_PORT"
  if [ -n "$DVS_STATIC_TALKGROUPS" ]; then
    patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "StartupTG" "$DVS_STATIC_TALKGROUPS"
  fi
  # dvswitch-server's own shipped MMDVM_Bridge.ini ships a live
  # (uncommented) `Options=StartRef=3100;RelinkTime=15;` -- DMR+/XLX
  # reflector syntax, meaningless to a Homebrew-protocol connection like
  # TGIF. Cleared rather than left in place.
  patch_ini "$MMDVM_BRIDGE_INI" "DMR Network" "Options" ""

  echo "== Enabling analog_bridge.service + mmdvm_bridge.service (stfu.service not needed/used for $DVS_DMR_NETWORK)"
  systemctl enable --now analog_bridge.service
  systemctl enable --now mmdvm_bridge.service
  systemctl disable --now stfu.service 2>/dev/null || true
fi

echo "== Disabling every other DVSwitch-shipped mode gateway (out of scope for this feature)"
for unit in ircddbgateway p25gateway nxdngateway ysfgateway quantar_bridge; do
  if systemctl list-unit-files "${unit}.service" >/dev/null 2>&1; then
    systemctl disable --now "${unit}.service" 2>/dev/null || true
  fi
done

echo "== Restarting bridge units to pick up the new config"
systemctl restart analog_bridge.service
if [ "$DVS_DMR_NETWORK" = "brandmeister" ]; then
  systemctl restart stfu.service
else
  systemctl restart mmdvm_bridge.service
fi
sleep 2

# Route Analog_Bridge's audio to whichever partner is actually configured --
# see dvswitch.sh's own setMode(): this just points Analog_Bridge's TLV
# ports at the right partner process, it doesn't touch either partner's own
# network connection. Best-effort (own error already visible in its output);
# a failure here just means the mode has to be set by hand afterward
# (Manager > DVSwitch > Diagnostics won't show audio flowing until it is).
echo "== Setting Analog_Bridge's active mode"
if [ "$DVS_DMR_NETWORK" = "brandmeister" ]; then
  "$DVSWITCH_SH" mode STFU || echo "   WARNING: mode switch failed -- run '$DVSWITCH_SH mode STFU' by hand once analog_bridge.service is confirmed up"
else
  "$DVSWITCH_SH" mode DMR || echo "   WARNING: mode switch failed -- run '$DVSWITCH_SH mode DMR' by hand once analog_bridge.service is confirmed up"
fi

echo "Done."
echo "  analog_bridge.service: $(systemctl is-active analog_bridge.service 2>/dev/null || echo unknown)"
if [ "$DVS_DMR_NETWORK" = "brandmeister" ]; then
  echo "  stfu.service:          $(systemctl is-active stfu.service 2>/dev/null || echo unknown)"
else
  echo "  mmdvm_bridge.service:  $(systemctl is-active mmdvm_bridge.service 2>/dev/null || echo unknown)"
fi
echo ""
echo "IMPORTANT: this only sets up the DVSwitch bridge node in rpt.conf and its"
echo "connection to the DMR network. It does NOT link that node to your real repeater"
echo "node -- connect them from the Status Board (check \"Permanent\") if you haven't"
echo "already. That link does not survive Asterisk reloading rpt.conf (which app.py"
echo "does right after this script runs, and which any future re-run of this script"
echo "also triggers), so it needs reconnecting each time."
echo ""
echo "Run diagnostics from Manager > DVSwitch to verify the rest (ports, AMBE source, rpt.conf node)."
echo "Rollback: sudo bash $DVS_DIR/rollback.sh"
