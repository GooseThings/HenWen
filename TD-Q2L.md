# Tidradio TD-Q2L Bluetooth PTT mic — reverse-engineered notes

> **Status 2026-09-05 — likely not a permanent bug: looks like BLE connection
> warm-up / connection-interval variability.**
> Everything earlier in this file describes the PTT characteristic
> (`894c8042-...`) as unreadable from Chrome: `readValue()` resolving with
> **zero bytes** and `characteristicvaluechanged` never firing, reproduced
> identically on Android and desktop Linux Chrome. That description was
> accurate for every session tested that day — until the same evening, the
> exact same characteristic, on the exact same desktop setup, delivered
> **150+ consecutive press/release notifications with almost no misses**.
> Nothing in the code changed between the bad sessions and the good one.
> The leading theory is a BLE **connection-interval warm-up**: a fresh
> connection can start on a slow, power-saving interval where a quick
> press-and-release falls entirely between polls and is never seen at all,
> and either settles into a fast interval after sustained traffic or gets a
> fast interval from the start, seemingly by luck of the connection. Native
> apps like nRF Connect can request a high-priority interval immediately (an
> API Web Bluetooth does not expose to JavaScript at all) — which would
> explain why nRF Connect has been reliable from the very first press in
> every test, while Chrome's reliability has varied session to session.
> See [Follow-up testing: connection warm-up, not a permanent bug](#follow-up-testing-2026-09-05-connection-warm-up-not-a-permanent-bug)
> for the full sequence (a stale-GATT-cache race fix, a Wi-Fi-off test that
> looked conclusive and then got contradicted by a Wi-Fi-on test that worked
> even better). [Open problem](#open-problem-chrome-reads-return-zero-bytes)
> and [Desktop Chrome testing results](#desktop-chrome-linux-testing-results-2026-09-05)
> below are kept as-is — they're real data, just not the final word — and
> [Troubleshooting on a desktop](#troubleshooting-on-a-desktop) has the
> standalone harness.
>
> Meanwhile HenWen ships a working-but-imperfect fallback: PTT on the **Rev**
> media key, which is a toggle rather than hold-to-talk. Removing that
> compromise is the entire point of getting the BLE read working — and this
> new finding means it may actually be reachable, with the right connection
> warm-up strategy in HenWen's own connect code.


No public protocol documentation exists for this device (checked as of 2026-09).
Tidradio markets it as compatible with "most PTT applications" without a
per-app pairing step, which implied — before this was actually tested against
real hardware — that it might ride standard Bluetooth media-remote commands.
That turned out to be only half true: see below.

## Buttons

| Button | Behavior |
|---|---|
| Fwd (CH+) | Standard AVRCP media key — arrives as **`previoustrack`** (labelling is inverted vs. the media semantics). Long press is the mic's own Noise Reduction toggle, so it is not a free key. |
| Rev (CH−) | Standard AVRCP media key — arrives as **`nexttrack`**. Not tied to any on-device function; the one genuinely free key. |
| Volume + / − | Standard AVRCP media key — volume up/down |
| Power (momentary press) | Standard AVRCP media key — play/pause (toggle) |
| Power (long press) | Powers the mic off |
| **PTT** | **Not a standard signal — exhaustively confirmed, see below.** Emits nothing any web page can observe. Only reachable via the device's proprietary BLE GATT characteristic below. |

Everything except PTT is a real, OS-recognized hardware media key — any app
using the standard media-key APIs (e.g. the Web `MediaSession` API in a
browser) will see those presses with zero device-specific code.

### The PTT button emits nothing observable — exhaustive scan, 2026-09-05

Worth recording properly, because the first version of this claim rested on a
scan that could not have found most of the possibilities: it registered only
the Media Session `play`/`pause` actions, and a handler fires *only* for an
action explicitly registered, so anything else the button might send was
indistinguishable from silence.

Re-run against every input surface a web page has:

- **all 15 Media Session actions** — `play`, `pause`, `stop`, `seekbackward`,
  `seekforward`, `seekto`, `skipad`, `previoustrack`, `nexttrack`,
  `togglemicrophone`, `togglecamera`, `hangup`, `previousslide`, `nextslide`,
  `enterpictureinpicture` (all accepted by Chrome on this phone)
- **raw DOM `keydown`/`keyup`** on the window, capture phase
- **the Gamepad API**, polled at 100ms

Result — PTT pressed repeatedly, short and long, produced **zero events on
every channel**. The same run captured, seconds apart:

```
16:57:28  scan-mediakey: pause          <- Power button
16:57:30  scan-mediakey: previoustrack  <- Fwd
16:57:31  scan-mediakey: nexttrack      <- Rev
```

That control matters: the channel was demonstrably live at the moment PTT
was pressed. This is a real negative, not a null result from a broken test.

Vol+/Vol- also produced nothing, as expected - those are absolute-volume
commands the phone handles itself and never routes to a page.

**Consequence:** hold-to-talk is unreachable through any standard API on this
device. A media key is one semantic action with no down/up pair, so PTT bound
to Rev is necessarily a toggle. The BLE characteristic below is the *only*
path to genuine press-and-hold, since it alone reports live button state.

The scan itself is kept in `status.html` behind `?btscan=1` (see
`_txMediaKeyMode()`), so it can be re-run against a different accessory
without rebuilding it.

## The LE instance — full GATT service list

The Q2L appears **twice** in Android's Bluetooth menu: the Classic BR/EDR
instance (headset audio + AVRCP media keys) and a separate LE instance,
`TID-MIC-Q2L-1a8d` / `29:D7:1F:9C:4E:58`. The LE one cannot be paired from
Android's menu, and does not need to be — it reports **NOT BONDED** while
fully connected and serving GATT. That is normal for a GATT-only peripheral;
a failed pairing attempt there is not a fault and not worth chasing.

Services, read off nRF Connect while connected (2026-09-05):

| UUID | Notes |
|---|---|
| `0x1800` | Generic Access (standard) |
| `0xAE30` | Vendor-specific, unexplored |
| `0xFF00` | Vendor-specific, unexplored |
| `89a8591d-bb19-485b-9f59-58492bc33e24` | **The PTT service.** Characteristic `894c8042-…` NOTIFY+READ, value `0x00` at rest, CCCD `0x2902` reads "Notifications enabled" |
| `0xFFE0` | Almost certainly the HM-10-style BLE serial/UART passthrough (its `0xFFE1` characteristic is the usual notify/write pair). Unexplored, and the most promising backup if the PTT service proves unreliable |

**No Human Interface Device service (`0x1812`).** This rules out the
otherwise-attractive theory that bonding the LE instance would make the PTT
button arrive as an ordinary BLE-HID keyboard key, giving OS-level
press/release for free. It would not — there is no HID service to bond to,
which is consistent with the exhaustive input scan above finding nothing on
any standard channel.

**Only one central can hold the LE link at a time.** nRF Connect and Chrome
cannot both be connected, and whichever gets there first locks the other
out — a likely cause of `Connection Error: Connection attempt failed.` in
HenWen's log. Disconnect nRF Connect before testing the browser, and vice
versa.

## PTT button — BLE GATT protocol

Sniffed with **nRF Connect for Mobile** (Nordic Semiconductor, free Android/iOS
app): scan for the device, connect, browse GATT services for a non-standard
128-bit UUID, find its Notify-capable characteristic, subscribe, then press
the PTT button and watch the notified value.

- **Service UUID:** `89a8591d-bb19-485b-9f59-58492bc33e24`
- **Characteristic UUID:** `894c8042-e841-461c-a5c9-5a73d25db08e`
- **Properties:** `NOTIFY`, `READ`
- **Value:** a single byte reflecting *live* physical button state, not a
  toggle/event — `0x01` while the button is physically held down, `0x00`
  when released. This means a consumer gets true press/hold semantics for
  free, not just "pressed" pulses.
- **CCCD (`0x2902`):** must be written to enable notifications (standard BLE
  "notifications enabled" descriptor) — any Web Bluetooth / GATT client
  library's `startNotifications()`-equivalent call handles this.

### Minimal Web Bluetooth consumer (JavaScript)

```js
const SERVICE_UUID = '89a8591d-bb19-485b-9f59-58492bc33e24';
const CHAR_UUID    = '894c8042-e841-461c-a5c9-5a73d25db08e';

/* Filtering requestDevice() by services only matches a device that
   advertises that UUID in its BLE advertisement packet. This device's
   custom PTT service is only visible AFTER connecting (that's how nRF
   Connect found it) — it is not in the advertisement — so a services
   filter here returns an empty picker every time, even with the mic right
   next to the phone. acceptAllDevices + optionalServices is the fix: list
   every nearby BLE device and grant access to the PTT service once
   connected. */
const device  = await navigator.bluetooth.requestDevice({ acceptAllDevices: true, optionalServices: [SERVICE_UUID] });
const server  = await device.gatt.connect();
const service = await server.getPrimaryService(SERVICE_UUID);
const char    = await service.getCharacteristic(CHAR_UUID);

char.addEventListener('characteristicvaluechanged', (e) => {
  const pressed = e.target.value.getUint8(0) === 1;
  // pressed === true  -> PTT physically held down
  // pressed === false -> PTT released
});
await char.startNotifications();
```

Web Bluetooth is Chrome-only (Android + desktop) — no Safari/Firefox support,
and it requires a secure context (HTTPS) plus a fresh user-gesture pairing
each page load (no silent auto-reconnect on plain page load without also
using the newer, less broadly supported `navigator.bluetooth.getDevices()`
persistent-permission API).

The device's BLE advertising also appears to only run in narrow windows
(observed: sometimes visible right as the mic powers on or off, often
invisible in between) rather than continuously — if the picker comes back
empty, try clicking Connect at the moment of a power-cycle rather than
assuming the mic is out of range or the code is wrong. Also check Android's
own "Nearby devices" runtime permission for the browser (distinct from
Location, on Android 12+) and, as a last resort, `chrome://bluetooth-internals`
→ Devices → Start Scanning to confirm the browser's Bluetooth stack sees
*any* device at all, independent of this page's code.

## Audio routing gotcha (Chrome/WebRTC, not device-specific)

The mic works as a real Bluetooth headset — verified with an actual cellular
phone call, both directions of audio correctly routed through it while paired
and connected, and separately confirmed the user's regular in-Chrome Zoom/
Google Meet calls already use it fine with zero special handling. But a
**web page's** plain `getUserMedia()` call did **not** automatically get the
same routing in HenWen initially, even with the device already paired and
shown as "Connected" in Android's Bluetooth settings.

Root cause, confirmed empirically (not just theorized): HenWen's
`getUserMedia()` call requested `autoGainControl: false` (turned off
elsewhere to fix a clipping problem on a different mic). Chromium's Android
audio backend appears to tie its automatic "route to whatever Bluetooth
device is currently active" behavior to requesting the **full default**
echoCancellation + noiseSuppression + autoGainControl trio — the same
constraints Zoom/Meet request by default. Opting any one of them out seems
to drop Chrome into a plain capture path that never engages Bluetooth SCO
routing at all, regardless of what's paired/connected. Setting
`autoGainControl: true` immediately fixed it — mic and speaker both
confirmed working through the Tidradio with zero device-picker UI, exactly
matching native call behavior.

An earlier, now-abandoned approach tried working around this by manually
enumerating devices (`navigator.mediaDevices.enumerateDevices()`, filtering
`audioinput`/`audiooutput`, passing an explicit `deviceId` to `getUserMedia`
and `audioElement.setSinkId()` for output) — this technically works as a
fallback if the constraint-based fix ever stops applying, but it added a
device-picker UI the actual fix made unnecessary. The constraint fix is
simpler and matches how any other WebRTC site on this browser already
behaves, so it's the one actually in use.

## Radio contention between Bluetooth audio and BLE PTT — revised 2026-09-05

An earlier revision of this file concluded that Bluetooth audio and the BLE
PTT button were **mutually exclusive** on this device: that once a call was
using the mic/speaker over classic Bluetooth, GATT notifications stopped
arriving entirely, with `device.gatt.connected` still `true` and no
disconnect event ever fired.

**Field use contradicts that.** With the phone simply paired to the Q2L and
Android routing mic and speaker to it as it does for any other app, the
mic, the speaker and the BLE PTT button have all been observed working at
the same time. So the mutual exclusion is not a fixed property of the
device, and code should not be built around assuming it.

What *is* real is intermittent contention. In one session the link connected
four times, delivered zero press notifications, and dropped twice on its
own after 14s and 39s. Two corrections to the original writeup fall out of
that:

- the drops raise a genuine `gattserverdisconnected`, where the original
  writeup recorded none — which is what makes automatic reconnection
  possible at all;
- they happen with Bluetooth *capture* routing off too, so pinning it on
  SCO capture specifically was wrong. Anything keeping the classic radio
  busy is a candidate, including the board's own Listen playback going to
  the headset over A2DP.

Still unexplained: why the link sometimes delivers every press reliably and
sometimes none at all, with no visible difference in configuration. The
`tx-ble-connected` / `tx-ble-disconnected` / `tx-ble-value` trail in
`journalctl -u HenWen` is there to pin that down — `tx-ble-connected` is
logged only after `startNotifications()` resolves, so that line appearing
without any `tx-ble-value` following it means the subscription was accepted
and the notifications themselves are being lost.

Not confirmed: whether a different (pricier / different chipset) Bluetooth
PTT accessory would avoid this entirely, or whether some other combination
of Android/Chrome versions handles the radio-sharing better.

## Open problem: Chrome reads return zero bytes

The BLE characteristic is the only route to genuine press-and-hold on this
device (every standard input API was ruled out — see the exhaustive scan
above). It works from nRF Connect and not from Chrome.

**Confirmed working, Android + nRF Connect:**
- connect to the LE instance (no bonding needed — it reports NOT BONDED while
  fully serving GATT)
- subscribe: pressing PTT flips the value `0x00` → `0x01` → `0x00`, every time
- read while holding the button down: returns `0x01`

**Confirmed failing, Android + Chrome (same phone, same device, same session):**
- `requestDevice()` + `gatt.connect()` + `getPrimaryService()` +
  `getCharacteristic()` all succeed
- `startNotifications()` resolves — the CCCD write is accepted — and then
  **no `characteristicvaluechanged` event ever fires** for a run of presses
- `readValue()` resolves successfully but with a **zero-length DataView**, at
  every one of ~7 reads/sec for minutes at a time

### Ruled out, with the evidence

| Theory | Killed by |
|---|---|
| The button emits some standard signal we never registered for | Exhaustive scan: all 15 Media Session actions, DOM `keydown`/`keyup`, Gamepad API. Nothing, while Power/Fwd/Rev logged correctly seconds apart in the same run |
| Bonding the LE instance would expose it as a BLE-HID keyboard | No HID service (`0x1812`) on the device at all |
| The device stops notifying under Bluetooth-audio load | nRF Connect receives every press reliably, including with audio active |
| Chrome answers reads from an empty notification cache | Reads still return zero-length with notifications never enabled (`ble-read-empty … (notifications off)`) |
| Our poll was wedged / not running | `ble-poll-tick` and ~7 reads/sec confirmed; failures are real resolved-but-empty values, not a stuck in-flight guard |
| Bug is Android-specific | Desktop Linux Chrome (BlueZ) reproduces the identical zero-byte-read / silent-notify signature — see below |
| Bug is permanent / present on every connection | Same characteristic, same desktop setup, later the same day: 150+ consecutive press/release notifications delivered with almost no misses. See [connection warm-up](#follow-up-testing-2026-09-05-connection-warm-up-not-a-permanent-bug) |
| Wi-Fi/Bluetooth radio coexistence (AX200 shared antenna) is the cause | Looked confirmed by one Wi-Fi-off test, then contradicted by an even better result with Wi-Fi back on — see [connection warm-up](#follow-up-testing-2026-09-05-connection-warm-up-not-a-permanent-bug) |

### Not yet tried

- **Reading with a longer MTU / after a delay**, in case the zero-length
  response is a negotiation artefact.
- **A different OS's Web Bluetooth stack** (Windows/macOS use their native
  BLE stacks, not BlueZ) — would tell us whether this is BlueZ-specific
  rather than Chrome-specific.
- **Filing a Chromium bug** about the empty-`properties` finding below, since
  that reproduces on every characteristic on every service, not just the PTT
  one, and looks like a GATT-discovery bug independent of the read/notify
  problem.

## Desktop Chrome (Linux) testing results, 2026-09-05

Tested on a Debian 13 laptop, Intel AX200 adapter, Google Chrome 151, BlueZ
5.82. Three platform-setup issues had to be cleared before the actual
zero-byte question could even be tested:

1. **`navigator.bluetooth` was `undefined`** out of the box. Web Bluetooth is
   not on by default in Linux Chrome the way it is on Windows/macOS/ChromeOS/
   Android — it needed `chrome://flags/#enable-experimental-web-platform-features`
   set to Enabled, then a relaunch, before `requestDevice()` existed at all.
2. **`getPrimaryService(SERVICE_UUID)` threw `NotFoundError`** on the first
   connect after that, even though the device genuinely has that service.
   Cause: this device's LE address had already been touched once by
   `bluetoothctl` (for an unrelated pairing test) and disconnected quickly,
   leaving BlueZ with a **stale, incomplete cached GATT attribute table** for
   that address — `bluetoothctl info` showed only `0xFFE0` cached, not the
   PTT service. Fix: `bluetoothctl remove <addr>` to forget the device
   entirely, then reconnect fresh from the page so BlueZ redoes full
   discovery. This is the desktop-Linux equivalent of the "clearing Android's
   GATT attribute cache" theory from the list above — confirmed as a real
   failure mode, just triggered here by a `bluetoothctl` session rather than
   normal use.
3. **The classic BR/EDR audio instance is a separate device** from the LE
   GATT one (confirmed: different MAC, `29:D7:1F:7A:E3:47` vs
   `29:D7:1F:9C:4E:58`) and needed its own pairing to stop the mic's
   unconnected-blink LED — via `bluetoothctl`, this required switching the
   agent to `NoInputNoOutput` (Just Works) first; the default `DisplayYesNo`
   agent produced a numeric-comparison passkey prompt the device doesn't
   actually support, which failed with `Authentication Failed (0x05)`. Not
   relevant to the PTT bug itself, just a prerequisite for a clean setup.

With a fresh device object and full discovery, the actual test:

- **`byteLength` came back `0`** holding PTT and reading `894c8042-...` —
  identical to the Android result. This rules out "Android-specific bug" per
  the doc's own decision tree above.
- **Subscribing to `894c8042-...` and pressing PTT produced zero
  `characteristicvaluechanged` events**, again matching Android exactly.
- **`chrome://bluetooth-internals`** turned out not to be the clean
  bypass the original plan assumed: its Devices list is populated by its own
  scan, and a device already GATT-connected via a page (and therefore not
  currently advertising) doesn't show up there to Inspect. It disappeared
  from the list entirely when attempting to inspect it, even though
  `bluetoothctl info` confirmed the LE link was still `Connected: yes` the
  whole time. Not a disconnect — a limitation of that internals page for
  this use case. Untried: connecting *from* `bluetooth-internals` directly
  (its own "New Connection" flow) rather than expecting it to show a
  page-initiated connection.
- **New finding, not previously documented:** `chr.properties` reports
  **empty (`{}` / no flags true) for every characteristic on every service**,
  not just the PTT one — `894c8042`, `ffe1`, `ff01`, `ff02`, `ff21`, `ff22`,
  `ae01`, `ae02` all came back with an empty properties object via both
  `getCharacteristic()` and `getCharacteristics()`. A real device does not
  have zero properties on every characteristic; this looks like Chrome/BlueZ
  on Linux failing to surface the GATT characteristic property flags at all,
  a distinct bug from the zero-byte read/notify problem. Doesn't fully
  explain the read/notify failures (Chrome does not appear to gate
  `readValue()`/`startNotifications()` on client-side property flags), but
  is a second, independently reproducible platform bug worth reporting
  upstream.
- **`0xFFE0` backup service — partial success.** Its notify characteristic is
  `0xFFE1` (confirmed via `bluetoothctl`, matching the doc's original guess).
  Subscribing to it and pressing PTT **did** produce real notifications
  twice in one session — `byteLength=1 value=01` then, half a second later,
  `byteLength=1 value=00` (each logged twice, i.e. delivered as a duplicate
  pair) — out of roughly 50 button presses attempted. This is the first time
  *any* characteristic on this device has delivered button state to Chrome
  on any platform. But the loss rate (~1 full cycle in 50 presses) is severe,
  consistent with the radio-contention flakiness already documented above
  rather than a clean working channel. Not yet retried as a longer, isolated
  session (i.e. subscribe once and leave it running, without intermixing
  Enumerate-services calls, which may themselves be disruptive to an active
  GATT session).

**Net conclusion at this point in the day:** the zero-byte/silent-notify
behavior on the documented PTT characteristic looked like a genuine
cross-platform (Android + Linux desktop) Chrome/Web-Bluetooth-stack problem,
not a phone-specific quirk, and `0xFFE0`/`0xFFE1` looked like the most
promising lead, despite lossy delivery. **Superseded a few hours later** —
see the follow-up section immediately below, where the primary characteristic
turned out to work fine under the right connection conditions. Left in place
because the empty-`properties` finding and the `chrome://bluetooth-internals`
limitation are still real and unexplained, independent of the main mystery.

## Follow-up testing, 2026-09-05: connection warm-up, not a permanent bug

Same day, same desktop setup, continued testing turned up a race-condition
fix and then a result that overturned the conclusion above.

**1. `getPrimaryService()` can race GATT discovery on a never-before-seen
device.** After a `bluetoothctl remove` (to clear a stale cache, see above),
reconnecting produced the same `NotFoundError` — but the timestamps told a
different story than "stale cache" this time: `gatt.connect()` resolved at
`22:57:07.987` and the `NotFoundError` landed at `22:57:08.036`, **49ms**
later. Real over-the-air GATT discovery of a whole attribute table takes
hundreds of milliseconds at minimum. Chrome's `getPrimaryService()` call was
simply racing BlueZ's own discovery on a device with zero prior cache to
serve from. Fix: `getPrimaryServiceRetry()` wraps `getPrimaryService()` in retries with a
500ms backoff (6 attempts) instead of failing on the first miss. This is a
harness/client-code bug, not a device or platform bug. (The fix was described
here before it was actually committed — the harness was still calling
`getPrimaryService()` directly at all three call sites. Now genuinely present
in `TD-Q2L-test.html`, and carried into HenWen as `_txBleService()`.)

**2. Wi-Fi/Bluetooth coexistence looked like the answer, then wasn't.** With
the classic BR/EDR audio link disconnected, comparing quick taps vs. held
presses on `0xFFE1` showed **zero** deliveries during ~76 seconds of quick
taps, then sparse hits, in a session with Wi-Fi on. Turning the laptop's
Wi-Fi off entirely (`nmcli radio wifi off` — the Intel AX200 shares one
antenna between Wi-Fi and Bluetooth, a known coexistence weak point) and
re-running produced a dramatic change: dozens of clean press/release pairs
delivered back-to-back with almost no gaps. At the time this looked like
strong, direct confirmation that Wi-Fi contention on the shared AX200 antenna
was the dominant cause of lost notifications.

**3. That conclusion didn't survive the next test.** Wi-Fi was turned back
on, and a fresh connection was made subscribing to **both** the primary PTT
characteristic (`894c8042-...`) and `0xFFE1` at the same time. Result: **over
150 consecutive press/release cycles delivered on both characteristics**,
matching each other within single-digit milliseconds, for a full two-minute
test — the best result of the entire day, obtained with Wi-Fi back on. This
directly contradicts Wi-Fi state as the deciding factor: the exact condition
blamed for the earlier failures was present during the best result of the
day.

**Revised theory: BLE connection-interval warm-up (or plain per-connection
luck), not radio coexistence.** A BLE central and peripheral negotiate a
"connection interval" — how often they exchange packets. A slow interval
(commonly used to save the peripheral's battery when idle) means a quick
press-and-release can complete entirely between two polling windows and
never be seen by either side, not just delayed. Some stacks tighten the
interval adaptively once sustained traffic starts flowing; separately, the
initial interval a given connection lands on may simply vary run to run.
Evidence this fits better than the Wi-Fi theory:

- The Wi-Fi-off, `0xFFE1`-only session took **68 seconds** after
  `startNotifications()` before the first successful delivery, then improved
  gradually — consistent with a slow-to-fast interval transition taking time
  to kick in.
- The final, best session (subscribing to both characteristics, Wi-Fi on)
  delivered its first success only **5 seconds** after subscribing, then
  stayed reliable throughout — consistent with that particular connection
  landing on a fast interval quickly, for reasons not yet isolated (more
  simultaneous GATT traffic? plain variance?).
- **Native BLE clients can request a high-priority connection interval
  immediately after connecting** (e.g. Android's
  `BluetoothGatt.requestConnectionPriority()`) — an API **Web Bluetooth does
  not expose to JavaScript at all**. This cleanly explains why nRF Connect
  has been 100% reliable in every test in this document, from the very first
  press, on the same hardware where Chrome has ranged from 0% to
  near-perfect: nRF Connect can force a fast interval on connect; a Chrome
  page has no equivalent lever and is at the mercy of whatever interval the
  connection happens to negotiate or drift into.

**This means the primary PTT characteristic is not permanently broken in
Chrome** — every earlier "zero bytes / silent notify" result in this
document was real, but apparently connection-state-dependent rather than an
unconditional platform limitation. The practical question for HenWen is no
longer "is this characteristic readable from Chrome," but "how do we get (or
wait for) a good connection interval before relying on it."

**Not yet done:**
- Capture the actual negotiated connection interval per session (e.g. via
  `btmon`/`hcidump`) to confirm the interval-length theory directly instead
  of inferring it from delivery timing alone.
- Test whether deliberately generating GATT traffic immediately after
  connect (e.g. a burst of reads on some characteristic, before the user's
  first real PTT press) reliably shortens the time-to-fast-interval, which
  would turn this into an actionable warm-up step in HenWen's own connect
  code rather than a wait-and-hope.
- Repeat the two-characteristics-at-once test a few more times to see how
  often a fresh connection lands on a fast interval immediately vs. needs
  time to get there — the sample size so far is one good session against
  several bad ones.

## Carried into HenWen, 2026-09-05

Acting on the follow-up findings, `templates/status.html` now:

- **retries service discovery** (`_txBleService()`, 6 tries / 500ms) rather
  than trusting the first `NotFoundError`;
- **subscribes again** — `TX_BLE_USE_NOTIFY` is back on, since the good
  session proved notifications do work on this characteristic;
- **subscribes to `0xFFE1` as a redundant second source**, not a fallback:
  both delivered every press within single-digit milliseconds of each other
  in the best session, and `_txBleApply()` de-duplicates, so whichever
  arrives first wins. Non-`0x00`/`0x01` values are ignored, since a serial
  passthrough can carry other traffic. Its characteristic is picked by UUID
  rather than by `.notify`, because properties come back empty on Chrome/BlueZ;
- **polls hard for the first 3 seconds** after connect (40ms, then settling to
  150ms) as a **warm-up attempt**. This is the untested item from the list
  above, now live: sustained GATT traffic is the only lever a page has, since
  Web Bluetooth exposes no equivalent of
  `BluetoothGatt.requestConnectionPriority()`. Whether it actually shortens
  time-to-first-delivery is what the `ble-poll-start` → first `ble-value`
  interval in `journalctl -u HenWen` will show, across several connects.

Log lines to watch: `ble-svc-retry`, `ble-serial`, `ble-warmup-done`, and
`ble-value … via poll|notify|ffe1` — the source tag says which transport
actually delivered each press.

## Phone result, 2026-09-05 evening: fast polling correlates with delivery

First delivery of button state to Chrome **on the phone**, and it arrived
with a sharp correlation attached. Connect at `19:47:27` started the poll at
40ms as a 3-second warm-up:

```
19:47:27  ble-poll-start: 40ms warm-up for 3000ms
19:47:28  ble-value: press via notify
19:47:28  ble-value: release via notify
19:47:29  ble-value: press via notify
19:47:29  ble-value: release via notify
19:47:30  ble-warmup-done: settled to 150ms
   ...nothing further, for the rest of the session
```

Two clean press/release pairs while polling at 40ms; nothing at all after
the poll relaxed to 150ms. This is the first time the phone has produced
button state through Chrome by any route, and it supports the
connection-interval theory directly rather than by inference from timing.

**Caveat, and it is a real one:** TX was armed at `19:47:32`, one second
after the warm-up ended. So this single run cannot separate "the poll rate
dropped" from "Bluetooth audio started" — the original radio-contention
theory predicts the same silence. The two are distinguishable by testing:
with the fast poll now permanent, presses that keep working *after* arming
point at the interval; presses that stop again at the moment of arming point
at audio contention.

Note also that the reads themselves stayed useless throughout —
`ble-read-empty` at #1, #200, #400 — so the poll is not a data source on
this device. It is a keep-alive whose only purpose is generating enough GATT
traffic to hold a fast connection interval. **Notifications are what deliver
the button; the poll is what appears to keep them flowing.**

Acted on: the 40ms rate is no longer a warm-up that settles, it is the
operating rate for as long as the link is up. The link only exists while an
operator has deliberately connected a PTT button, so the cost is bounded.

## Troubleshooting on a desktop

`TD-Q2L-test.html` at the repo root is a standalone harness — no server-side
component, nothing to do with HenWen. Pair the mic to the laptop, then:

```bash
python3 -m http.server 8000
# open http://localhost:8000/TD-Q2L-test.html in Chrome
```

`http://localhost` counts as a secure context, so Web Bluetooth works;
`file://` does not. **Only one central can hold the LE link** — quit nRF
Connect and close any HenWen tab before connecting, or the connect attempt
fails with `Connection Error: Connection attempt failed.`

Buttons, in order: **Connect**, **Enumerate services** (lists every
characteristic under the PTT service and the three extras, with the
properties Chrome sees — a second characteristic under the same UUID, or
properties differing from nRF's, would explain the empty read), **Read once**,
**Start poll**, **Subscribe**. The log shows `byteLength` and raw hex for
every read and notification, and a PTT indicator turns red on `0x01`.

The single question to answer first: **hold the button down and read — does
`byteLength` come back 1 or 0?**

- **1, value `0x01`** → desktop Chrome can read it and the fault is
  Android-specific. HenWen's polling approach is correct as written and the
  fix is a platform workaround (cache clear, different Chrome version) rather
  than a code change.
- **0** → Chrome cannot read this characteristic on any platform, and the
  remaining avenues are `chrome://bluetooth-internals`, then the `0xFFE0`
  serial service.

**Answered 2026-09-05: it's `0` on desktop too** — see
[Desktop Chrome (Linux) testing results](#desktop-chrome-linux-testing-results-2026-09-05)
above for the full readout and what to try next (`0xFFE0`/`0xFFE1`, longer
sustained subscribe sessions, other OS's native BLE stacks).

## Known-working reference implementation

HenWen (`/opt/HenWen`, this repo) drives PTT from the **Rev media key**, not
from the proprietary BLE characteristic — see `_txMediaKeySync()` /
`_txMediaKeyAction()` in `templates/status.html`. Confirmed working on real
hardware 2026-09-05: repeated presses alternate cleanly, every press
delivered.

The BLE GATT path documented above is real and correctly implemented
(`_txBleConnect()`), but in field use it never once delivered a press —
it connects, subscribes successfully, then drops on its own. Since the Q2L
is a plain Bluetooth headset to Android and its Fwd/Rev/Vol±/Play-Pause
buttons are ordinary AVRCP keys the phone already handles, riding a media
key is both simpler and far more reliable. The BLE code is kept for
explicit, manual use and for anyone wanting to pursue the PTT-labelled
button, but it is not the path in use.

### Two non-obvious requirements for media keys to arrive

An earlier attempt (commit 2d374a3, bound to play/pause) concluded media
keys never reach the page. They do, but only under both of these:

1. **The page must own the system media session**, which on Android means it
   must actually be producing audible audio. HenWen's Listen stream through
   its `<audio>` element earns that, and arming TX ensures Listen is running.
   Setting `navigator.mediaSession.metadata` as well is what makes Android
   surface it as the active media notification, rather than leaving the
   headset's keys attached to whatever app played audio last.

2. **The page must stay audible for the whole time you need keys.** This one
   cost a day. HenWen muted its Listen element while transmitting (a
   radio-style RX mute). Muting makes the page inaudible, Android hands the
   media session away, and *no further media-key events are delivered* — so
   the first press keyed the transmitter and nothing could release it. The
   symptom reads exactly like a stuck button; the log showed no second event
   arriving at all. Fix: duck to a near-zero volume (0.0001) instead of
   muting. Inaudible in practice, still audible as far as the media session
   is concerned.

### Toggle semantics, and the safety that needs

A media key is a single semantic action with no press/hold pair, so this is
necessarily press-to-key / press-again-to-unkey, not hold-to-talk — the one
thing the BLE characteristic would have done better, since it reports live
button state. That makes a latched transmitter possible, so HenWen binds
`previoustrack`/`play`/`pause` as **release-only**: any other button on the
mic clears a stuck transmit, none of them can start one. A watchdog also
unkeys at the node's own TOT, or 120s if none is configured.

Binding play/pause has a second purpose: it stops the Power button pausing
Listen while armed, which would drop the media session for the same reason
muting did.

## Open questions / untested

- Whether other Tidradio PTT mic models/units share the same service and
  characteristic UUIDs, or whether these are per-unit/per-batch.
- Whether the BLE-advertising-window flakiness and the audio/BLE radio
  conflict are related symptoms of the same underlying combo-chip
  limitation, or independent issues.
- Exact byte layout/keycodes for the Fwd/Rev/Volume/Power media keys — only
  their functional effect was observed, not sniffed at the protocol level
  (unnecessary, since they already work via standard OS media-key APIs).
- Why the BLE GATT link connects and subscribes successfully but delivers no
  notifications in field use, when nRF Connect sees every press. Moot for
  HenWen now that PTT rides a media key, but unexplained.
- Why Chrome/BlueZ on Linux reports empty `properties` for every
  characteristic on this device (2026-09-05 finding, see desktop testing
  results) — whether that's specific to this BlueZ version, this device, or
  a broader Chromium-on-Linux Web Bluetooth bug worth reporting upstream.
  Still true even in the session where notifications worked perfectly, so
  it's independent of the connection-interval question.
- Whether the connection-interval warm-up theory (see follow-up testing
  section) is actually correct, and if so, whether HenWen can reliably force
  or speed up a fast interval (e.g. a burst of GATT traffic right after
  connect) rather than getting one only by chance.
- What made the one good session land on a fast interval in 5 seconds while
  other sessions took over a minute or never got there in the test window —
  sample size is currently one good session against several bad ones.
