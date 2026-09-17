# DVSwitch bridge (owner-only, opt-in)

Bridges one local AllStarLink node to a DMR network (BrandMeister, TGIF, or
a custom master) via the [DVSwitch](https://dvswitch.org/) suite
(`dvswitch-server`: Analog_Bridge + MMDVM_Bridge). Off by default on every
HenWen install — `install.sh` never touches this directory. Everything here
is triggered from Manager > DVSwitch (Owner role only).

## How it fits together

1. Owner fills in DMR ID, callsign, DMR network host/port/password, a
   bridge node number, and (optionally) AMBE gain/hardware settings in
   Manager > DVSwitch. Saved to the `dvswitch_config` table via
   `GET/POST /api/dvswitch/config`.
2. Once the required fields are complete, a "Run Guided Setup" button
   appears. Clicking it hits `POST /api/dvswitch/apply`, which:
   - creates the bridge node's rpt.conf stanza in Python
     (`append_node_stanza()` in app.py) if it doesn't already exist —
     `apply.sh` never touches rpt.conf itself;
   - writes the saved config to a root-only JSON file
     (`/etc/asterisk/henwen-dvswitch-config.json` by default);
   - runs `apply.sh` as root via the same narrowly-scoped passwordless sudo
     rule every other guided-setup script in this repo uses
     (`tx-spike/apply.sh`, `audiosocket-tap/apply.sh`, `ws-audio/apply.sh`);
   - reloads rpt.conf live (`asterisk -rx "rpt restart"`).
3. `check.sh` (read-only) verifies the result — package/service state,
   USRP ports, AMBE source reachability — surfaced via
   `GET /api/dvswitch/diagnostics`.

## What `apply.sh` does

- Adds the DVSwitch apt repository for this box's Debian codename
  (`dvswitch.org/<codename>` — DVSwitch's own documented install method)
  and installs the `dvswitch-server` package.
- Patches (not replaces) `Analog_Bridge.ini` and `MMDVM_Bridge.ini` —
  every key it doesn't mention keeps the package's own shipped default.
- Enables and starts exactly two systemd units: `analog_bridge.service`
  and `mmdvm_bridge.service`. The package ships several other mode
  gateways (D-Star, P25, NXDN, YSF) — this feature is DMR-only, so those
  are explicitly stopped and disabled, not left in whatever state the
  package defaulted to.

**Not verified against a real install on real hardware**: the exact ini
file paths (`/opt/Analog_Bridge/Analog_Bridge.ini`,
`/opt/MMDVM_Bridge/MMDVM_Bridge.ini`) come from DVSwitch's published
systemd unit files, not a live `dvswitch-server` package inspection on
this box's own Debian release — override via `ANALOG_BRIDGE_INI`/
`MMDVM_BRIDGE_INI` env vars if they differ. Same for whether the DVSwitch
apt repo actually ships arm64/armhf builds (a Raspberry Pi target) —
confirm before relying on this on Pi hardware.

## AMBE vocoder

**No hardware dongle is required.** Analog_Bridge ships a bundled software
AMBE/IMBE codec (mbelib for decode, an op25-derived encoder) and uses it
automatically (`decoderFallBack = true` is Analog_Bridge's own shipped
default) with zero hardware present. `ambe_source: "software"` in the
Manager config is the default for this reason. A real hardware dongle
(ThumbDV/DVstick, serial) or a network `AMBEServer` is an optional
quality/CPU-offload upgrade, set via `ambe_source: "hardware"` /
`"network"`.

**Unverified**: the software AMBE codec's CPU cost on a Raspberry Pi Zero
2 W (this project's hardware floor — see CLAUDE.md's Hardware Target
section) has not been benchmarked on real hardware. The Manager page
carries a warning about this next to the guided-setup button; don't
remove it without a real measurement.

## Rollback

`sudo bash dvswitch/rollback.sh` stops/disables the two units and restores
the ini files from the most recent `apply.sh` backup
(`/root/henwen-dvswitch-backup-<timestamp>/`). It deliberately does **not**
remove the rpt.conf bridge node (app.py created it, not this script) or
uninstall the `dvswitch-server` package — use the Manager raw rpt.conf
editor (superuser) and `apt-get remove dvswitch-server` respectively if a
full teardown is wanted.
