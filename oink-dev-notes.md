# Oink dev notes (HenWen side)

HenWen's own breadcrumbs for whoever is building "Oink" — a separate,
independently-maintained handheld/appliance hardware project
(`GooseThings/HenWen-Oink`, multiple contributors, its own repo) that
pairs with a HenWen-run node. **This file lives in the HenWen repo on
purpose and stays HenWen-only**: it records what's true on the server
side that a remote hardware appliance has to work with or around, not
Oink's own firmware/hardware design. Oink's architecture, BOM, and build
status belong in Oink's own repo — don't grow this file into a mirror of
that project's docs, and don't expect it to track Oink's day-to-day
progress. The two projects are deliberately kept separable: HenWen ships
and works standalone with zero Oink awareness, and this file existing
doesn't change that.

## The one hard constraint any such device is built around

Verified against `app_rpt`'s own source
(`apps/app_rpt/rpt_functions.c`, `function_cop()`) and confirmed live: PTT
in app_rpt's phone-control mode (`cop,6`) hard-gates on
`command_source == SOURCE_PHONE`, which is only ever set inside app_rpt's
own DTMF handling for a real `Rpt(<node>,P)` phone-control channel. There
is **no way to key a node's transmitter except DTMF sent inside an actual
phone-control call** — not via AMI (`rpt fun <node> cop,6` over AMI/CLI
does not key), not via any other channel type. This is an Asterisk/app_rpt
fact, not a HenWen design choice, so it constrains *any* remote-PTT design
against a HenWen (or any ASL3) node, regardless of what hardware or
language is on the other end. It's the same reason HenWen's own browser TX
(`tx-spike/`, see CLAUDE.md's "Browser transmit (TX)") drives PTT via
DTMF `*99`/unkey `#` into `Rpt(<node>,P)` rather than any more direct
mechanism — there isn't a more direct mechanism.

Practical upshot for a remote device: DTMF-over-a-live-call is not a
shortcut being taken for convenience, it's the only path that exists at
all, and it inherits every reliability problem that implies (RTP is
lossy, a dropped DTMF packet is a dropped keypress). A device relying on
this should assume it needs: redundant/spec-compliant DTMF delivery,
closed-loop confirmation against real AMI-observed keyed state
(`RPT_TXKEYED`/`RPT_RXKEYED` via `rpt show variables <node>`) rather than
trusting "I sent the tone" as proof of anything, and a local
release-on-timeout default so a wedged client can't hold a node keyed
indefinitely. app_rpt's own Time-Out Timer (`totime=` in `rpt.conf`,
**milliseconds** despite the name — see `parse_stanza_settings()` and the
TX Diagnostics config route for how HenWen resolves it through
node/template inheritance, falling back to app_rpt's documented 180s
default rather than treating an absent value as "no timeout") is an
existing, independent backstop worth confirming is sanely set on any node
a remote PTT device pairs with — that's a per-install checklist item, not
something either HenWen or a client device can enforce from outside.

## The existing reference pattern: `tx-spike/`

HenWen's own browser TX feature is the closest thing to prior art for
"an external client keys this node's transmitter," even though its
transport (WebRTC/WSS through Apache, browser-mic audio) doesn't match a
native embedded SIP/RTP client. What's reusable conceptually:

- **A dedicated, narrowly-scoped, revocable PJSIP endpoint per device
  class** — not reusing HenWen's own browser-TX credential (`TX_SECRET_PATH`,
  `/etc/asterisk/henwen-tx.secret`) for a different kind of client. A new
  device class gets its own endpoint/context/credential, generated fresh,
  stored with tight permissions, rollback-able independently. `tx-spike/`
  is additive and marker-guarded specifically so a second, unrelated
  `apply.sh`-style script can coexist on the same box without either one
  stepping on the other's endpoint/context names.
- **`Rpt(<node>,P)` phone-control mode is the whole contract.** Whatever
  dials in, however it dials in, ends up in the same dialplan destination
  HenWen's browser TX and a plain analog phone patch both use. There's
  nothing endpoint-specific about the PTT mechanism itself once a call is
  established — see the constraint above.
- **Codec is `ulaw` only, always** — app_rpt is native µ-law; HenWen's own
  `tx-spike` endpoint disallows everything else (`disallow=all` /
  `allow=ulaw`) specifically so there's never a transcoding stage. Any new
  endpoint definition for a different device class should do the same
  unless there's a concrete reason not to.
- **LAN-adjacency is the easy case, NAT/remote is real extra work.**
  HenWen's own TX stack needs HTTPS/WSS specifically because it's a
  browser reaching in from anywhere; a device on the same LAN as Asterisk
  can skip that entire layer (plain UDP transport, no ICE/DTLS/SRTP, no
  Apache proxy) — but the moment a design needs to work off-LAN, it picks
  up the same problems HenWen already solved for TX/low-latency-audio
  (see CLAUDE.md "Browser transmit (TX)" and "Low-latency delivery"): a
  secure-context/HTTPS requirement for anything browser-based, and for a
  raw-UDP SIP client specifically, NAT traversal (STUN at minimum, TURN
  if symmetric NAT is in play) plus firewall/port-forwarding for both the
  signaling port and the RTP range. HenWen doesn't currently solve that
  RTP/NAT problem for anyone — it's out of scope for any browser-based
  flow since WebRTC/ICE handles it there, so there's no existing HenWen
  code to lean on if a device needs it.

## What HenWen does *not* currently offer a remote device

- **No HTTP/AMI proxy for third-party hardware.** A remote PTT/audio
  appliance talks to Asterisk directly (its own AMI login in
  `manager.conf`, its own PJSIP registration) — it does not, today, go
  through HenWen's Flask/gunicorn process at all for either audio or
  control. HenWen's public `GET /api/*` routes (board status, favorites,
  etc.) are available as supplementary read-only data if a device's UI
  wants them, but the core status/PTT path bypasses HenWen entirely.
- **No device-credential management UI.** Every scoped credential HenWen
  itself manages (browser TX's secret, the AMI connection, SECRET_KEY) is
  either a single systemd-env value or a single file on disk — there's no
  Manager-page concept of "register/revoke one of several hardware
  devices." If a design ever needs multiple independently-revocable
  device credentials at any real scale, that's new HenWen surface (closer
  to the invite/TOTP-recovery-code machinery in "Auth and security" than
  to anything TX-related today), not something to bolt onto
  `tx-spike/`'s single-secret-file pattern.
- **No ARI/`externalMedia` app.** Confirmed (see constraint above) that
  this wouldn't have a working PTT path anyway without further app_rpt-side
  work, so this isn't a gap being left open by omission — it was evaluated
  and doesn't currently buy anything for a PTT device specifically. It
  might still be relevant for a hypothetical audio-only (no PTT) listen
  device, which is a different problem than what Oink is solving.

## Operational notes that affect any external Asterisk client

- **AMI connections from other clients don't contend with HenWen's own.**
  `AMIClient`/`_poll_loop` is a single persistent connection HenWen itself
  owns; a separate `manager.conf` user for an external device is a wholly
  independent TCP connection to `asterisk`, not something routed through
  or sharing state with HenWen's connection pool. The gunicorn
  `--workers 1 --threads 8` constraint noted in CLAUDE.md ("Background
  threads") is about HenWen's own process and its in-process caches — it
  has no bearing on how many external AMI/PJSIP clients Asterisk itself
  can carry.
- **Pi Zero 2 W hardware-floor guidance doesn't apply to a device like
  Oink.** That constraint (CLAUDE.md "Hardware target") is about code
  that runs *inside HenWen's own process* on the box running Asterisk. An
  ESP32-class (or any other) remote appliance runs its own firmware on
  its own hardware and never executes inside HenWen at all — it's a
  distinct question with its own constraints (battery, Wi-Fi, whatever
  MCU it's built on), decided entirely on the Oink side, not something
  this file should try to govern.
- **Any future HenWen-side change made *because* a remote-appliance class
  needs it** (a new AMI-scoped user pattern, a new `apply.sh`-style
  install script, a Manager diagnostics page mirroring TX Diagnostics)
  should still land as a generic HenWen feature usable by any compatible
  device, the same way `tx-spike/`, `ws-audio/`, and `audiosocket-tap/`
  are each generic install-time capabilities rather than named after one
  specific consumer. Keep Oink-specific naming, defaults, and assumptions
  out of HenWen's own code even if Oink is the reason a change gets made.
