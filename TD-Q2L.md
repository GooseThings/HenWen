# Tidradio TD-Q2L Bluetooth PTT mic — reverse-engineered notes

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
