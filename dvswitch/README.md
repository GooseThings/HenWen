# DVSwitch bridge (owner-only, opt-in)

Bridges one local AllStarLink node to a DMR network (BrandMeister, TGIF, or
a custom master) via the [DVSwitch](https://dvswitch.org/) suite
(`dvswitch-server`: Analog_Bridge + MMDVM_Bridge, plus `stfu` for
BrandMeister — see below). Off by default on every HenWen install —
`install.sh` never touches this directory. Everything here is triggered
from Manager > DVSwitch (Owner role only).

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
4. **The bridge node is not automatically linked to your real repeater
   node.** Connect them yourself from the Status Board (check
   "Permanent"). That link does not survive Asterisk reloading rpt.conf —
   which step 2 above does on every single run — so it needs reconnecting
   after every Guided Setup re-run, not just the first one.

## BrandMeister via STFU, not MMDVM_Bridge

For `dmr_network: "brandmeister"`, `apply.sh` does **not** use
MMDVM_Bridge's generic Homebrew `[DMR Network]` gateway — it configures and
runs `stfu.service` instead, a separate DVSwitch package/binary
implementing BrandMeister's own dedicated ODMRT protocol ("STFU" = Simple
Terminal Feature Update, port 54006 instead of Homebrew's 62031).
`mmdvm_bridge.service` is disabled entirely for a BrandMeister config; it's
only used for TGIF/custom, where its Homebrew gateway works fine.

**Why**: this exact `mmdvm-bridge` build (`1.6.8-20241231-94`) reproducibly
segfaults connecting `[DMR Network]` to a real BrandMeister master — a null
`CDMRNetwork*` dereference inside `CDMRControl`'s constructor /
`CDMRSlot::init`, confirmed via `coredumpctl`/`gdb` against a live core
dump, not inferred. Ruled out before concluding it's an upstream bug: bad
`Options` syntax (fixed, still crashed), a misplaced `[Info]` Callsign/
DMRId pair (fixed, still crashed), a wrong `[DMR Network] Id` override
(fixed, still crashed), stale BrandMeister login state from repeated crash-
restarts (waited ~50 minutes, still crashed on the very next clean attempt,
in under a second — too fast to be a real network timeout), and the
BrandMeister credentials themselves (confirmed correct against the owner's
own SelfCare account). The same DMR ID/password connects to `stfu.service`
instantly, with real TG traffic flowing within a second — confirming this
is a bug in that specific compiled binary's BrandMeister handling, not a
HenWen config issue, and not fixable by patching ini values further.
`dvswitch.sh tune <tg>` (talkgroup switching — see below) works identically
against STFU, since the underlying `txTg=` remote command rides Analog_
Bridge's TLV control channel to whichever partner is currently routed, not
a DMR-Network-specific mechanism.

STFU's own config lives in `DVSwitch.ini`'s `[STFU]` section (not
`MMDVM_Bridge.ini`), patched with the same DMR ID/callsign/password/static-
talkgroup fields as the Homebrew path. `BMAddress` is derived from
`network_host`'s leading master-number digits (e.g.
`3104.master.brandmeister.network` → `3104.repeater.net`) — BrandMeister's
own STFU/ODMRT hostname convention (confirmed live against master 3104;
not verified across every master number — override `DVSwitch.ini`'s
`[STFU] BMAddress` by hand if a particular master doesn't follow this
pattern).

If a future `mmdvm-bridge` package release fixes this, switching back to
the Homebrew path for BrandMeister would just mean re-enabling that branch
in `apply.sh` — nothing about the STFU integration is a one-way door.

## What `apply.sh` does

- Adds the DVSwitch apt repository for this box's Debian codename
  (`dvswitch.org/<codename>` — DVSwitch's own documented install method)
  and installs `analog-bridge`, `mmdvm-bridge`, and `stfu`.
- Patches (not replaces) `Analog_Bridge.ini`, and either `MMDVM_Bridge.ini`
  (TGIF/custom) or `DVSwitch.ini`'s `[STFU]` section (BrandMeister) — every
  key it doesn't mention keeps the package's own shipped default.
- Enables and starts `analog_bridge.service` plus whichever of
  `mmdvm_bridge.service` (TGIF/custom) or `stfu.service` (BrandMeister) the
  configured network actually needs — the other one is explicitly stopped
  and disabled, not left in whatever state it was previously in. The
  package ships several other mode gateways too (D-Star, P25, NXDN, YSF) —
  out of scope for this DMR-only feature, so those are also stopped and
  disabled regardless of network.
- Points Analog_Bridge's active audio routing at the right partner
  (`dvswitch.sh mode STFU` or `dvswitch.sh mode DMR`) — best-effort; a
  failure here is logged but doesn't fail the whole run, since it's easy to
  redo by hand once the services are confirmed up.

**Not verified against a real install on real hardware**: the exact ini
file paths (`/opt/Analog_Bridge/Analog_Bridge.ini`,
`/opt/MMDVM_Bridge/MMDVM_Bridge.ini`, `/opt/MMDVM_Bridge/DVSwitch.ini`)
come from DVSwitch's published systemd unit files and this session's own
live inspection of one real install, not an exhaustive survey — override
via `ANALOG_BRIDGE_INI`/`MMDVM_BRIDGE_INI`/`DVSWITCH_INI` env vars if they
differ. Same for whether the DVSwitch apt repo actually ships arm64/armhf
builds (a Raspberry Pi target) — confirm before relying on this on Pi
hardware.

## Static vs. dynamic talkgroups

**BrandMeister (STFU)**: `static_talkgroups`' first entry becomes
`DVSwitch.ini`'s `[STFU] StartTG` — confirmed live this puts the bridge on
that TG immediately and receives real traffic without any BrandMeister
self-care configuration needed. STFU is single-TG-at-a-time by design (not
Homebrew's TS1/TS2 multi-static-TG model): whatever TG it's tuned to is the
one being relayed, full stop. `dvswitch.sh tune <tg>` (what the Kiosk's
talkgroup preset buttons call via `POST /api/dvswitch/tune`) changes it
live.

**TGIF/custom (MMDVM_Bridge Homebrew gateway)**: `static_talkgroups` only
ever becomes `MMDVM_Bridge.ini`'s `StartupTG` key, which seeds Analog_
Bridge/the USRP tag's own idea of "current TG" at boot — cosmetic only, it
does not register a static link with the network. Without a real static
entry the bridge is dynamic-TG-only: your own transmissions register fine
on the network's talker list, but nothing plays back locally afterward,
since you were never statically linked to the TG. `apply.sh` clears
`[DMR Network] Options` (the package ships it live and uncommented as
`StartRef=3100;RelinkTime=15;`, DMR+/XLX reflector syntax meaningless to a
Homebrew-protocol connection) but does not attempt to fill it with a real
static-TG value for these networks — not investigated for TGIF/custom, and
the BrandMeister-specific `TS2_1=<tg>;` syntax that was tried here crashed
`mmdvm-bridge` outright (see above) rather than just failing to register.
If TGIF/custom has an equivalent to BrandMeister's self-care static-TG
config, use that.

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

`sudo bash dvswitch/rollback.sh` stops/disables all three units
(`analog_bridge`, `mmdvm_bridge`, `stfu`) and restores the ini files from
the most recent `apply.sh` backup (`/root/henwen-dvswitch-backup-
<timestamp>/`). It deliberately does **not** remove the rpt.conf bridge
node (app.py created it, not this script) or uninstall the packages — use
the Manager raw rpt.conf editor (superuser) and `apt-get remove analog-
bridge mmdvm-bridge stfu` respectively if a full teardown is wanted.
