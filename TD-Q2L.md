# Tidradio TD-Q2L Bluetooth PTT mic — reverse-engineered notes

No public protocol documentation exists for this device (checked as of 2026-09).
Tidradio markets it as compatible with "most PTT applications" without a
per-app pairing step, which implied — before this was actually tested against
real hardware — that it might ride standard Bluetooth media-remote commands.
That turned out to be only half true: see below.

## Buttons

| Button | Behavior |
|---|---|
| CH+ | Standard AVRCP media key — previous track |
| CH− | Standard AVRCP media key — next track |
| Volume + / − | Standard AVRCP media key — volume up/down |
| Power (momentary press) | Standard AVRCP media key — play/pause (toggle) |
| Power (long press) | Powers the mic off |
| **PTT** | **Not a standard signal.** Does nothing in normal Android media apps (confirmed against YouTube Music). Only reachable via the device's proprietary BLE GATT characteristic below. |

Everything except PTT is a real, OS-recognized hardware media key — any app
using the standard media-key APIs (e.g. the Web `MediaSession` API in a
browser) will see those presses with zero device-specific code.

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

HenWen (`/opt/HenWen`, this repo) implements the PTT-button path for its
browser TX feature in `templates/status.html` — see `_txBleConnect()` /
`_txBleOnValue()` (search for `TX_BLE_SERVICE`). A "BLE PTT" button next to
TX on the Status Board does the user-gesture pairing; it sits outside the
TX bar so the link can be established before TX is armed, and every
press/release is reported to the server log as `tx-ble-value` via
`/api/audio/client-log`, so `journalctl -u HenWen -f` alone is enough to
confirm the button is being seen — no browser devtools needed.

It deliberately does not attempt manual audio device selection, and does not
manipulate `getUserMedia` constraints to steer routing either. Pairing the
phone to the Q2L already makes Android use it for mic and speaker; HenWen
requests the plain WebRTC defaults (the AEC+NS+AGC trio any calling site
asks for) and lets that happen.

A checkbox briefly existed to switch between routed-Bluetooth and own-mic
capture, built on the mutual-exclusion premise above. When that premise
turned out to be wrong the checkbox went with it — it made the operator
reason about a tradeoff that isn't reliably there, and the constraint
fiddling behind it caused its own problems (opting out of `echoCancellation`
lands Chrome on Android's heavily-processed VOICE_COMMUNICATION capture
path; a local compressor added to compensate for the resulting level drop
then overdrove the input by ~3x once the processing came back out).

The lesson worth keeping: on this device, don't fight the OS's own audio
routing. Ask for the defaults, leave routing alone, and treat the BLE PTT
link as something to reconnect when it drops rather than something to
protect by starving the audio path.

## Open questions / untested

- Whether other Tidradio PTT mic models/units share the same service and
  characteristic UUIDs, or whether these are per-unit/per-batch.
- Whether the BLE-advertising-window flakiness and the audio/BLE radio
  conflict are related symptoms of the same underlying combo-chip
  limitation, or independent issues.
- Exact byte layout/keycodes for the CH+/CH−/Volume/Power media keys — only
  their functional effect was observed, not sniffed at the protocol level
  (unnecessary, since they already work via standard OS media-key APIs).
