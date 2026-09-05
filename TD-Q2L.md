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

## Hardware constraint: Bluetooth audio and BLE PTT sniffing may conflict

Confirmed live and repeatedly reproduced: once the above audio fix is in
place and a call is actually using the Tidradio's mic/speaker over classic
Bluetooth (SCO), the BLE GATT PTT characteristic's notifications stop being
delivered — `characteristicvaluechanged` simply never fires for a press,
even though `device.gatt.connected` still reports `true` (the connection
object never sees a formal disconnect event). Before the call goes active,
the exact same BLE connection reliably delivers every press/release.

This looks like a genuine radio-sharing limit of this device's (likely
single, combined BLE + Classic Bluetooth) chip, not a browser or app bug —
it can apparently maintain a nominal GATT connection while a Classic SCO
audio link is active, but can't actually get notification packets through
under that load. No software-side retry/reconnect logic was found to help,
since the connection never reports itself as broken.

**Practical implication:** on this device, you likely cannot have both
"automatic Bluetooth audio routing" and "reliable physical PTT button"
active at the same time. Pick one:
- Reliable PTT button → keep `autoGainControl: false` (or otherwise avoid
  triggering Bluetooth SCO) so audio stays on the phone's own mic/speaker,
  leaving the BLE radio uncontended.
- Bluetooth audio routing → accept that the physical PTT button may stop
  responding once a call/transmission is actually using the Bluetooth audio
  path; an on-screen/software PTT control is needed as the reliable
  fallback in that mode.

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

It deliberately does not attempt manual audio device selection; mic/speaker
routing is left entirely to the browser. The radio-sharing conflict above is
surfaced as an explicit user choice rather than silently picked: a "Route
audio through Bluetooth headset" checkbox in the TX bar (`_txBtAudioPref()`,
persisted in `localStorage`) is what decides the `autoGainControl` constraint
in `_txBuildMicChain()`, i.e. whether Chrome engages SCO at all.

- **Unchecked (default)** — `autoGainControl: false`, no SCO, BLE PTT keeps
  working through a transmission. Audio stays on the phone's own
  mic/speaker; the manual gain slider is the level control.
- **Checked** — `autoGainControl: true`, Bluetooth headset mic/speaker
  routing, and the physical PTT button goes deaf once transmit audio is
  live. The on-screen PTT button and the Space key still work.

Default is unchecked because a physical button that works is the reason to
pair one at all. Ticking the box while a BLE PTT device is connected paints
a warning in the TX bar rather than letting the button just quietly stop
responding, since the failure mode has no disconnect event to report. The
constraint is only read when the mic chain is built, so a change applies on
the next arm, which the UI also says out loud.

## Open questions / untested

- Whether other Tidradio PTT mic models/units share the same service and
  characteristic UUIDs, or whether these are per-unit/per-batch.
- Whether the BLE-advertising-window flakiness and the audio/BLE radio
  conflict are related symptoms of the same underlying combo-chip
  limitation, or independent issues.
- Exact byte layout/keycodes for the CH+/CH−/Volume/Power media keys — only
  their functional effect was observed, not sniffed at the protocol level
  (unnecessary, since they already work via standard OS media-key APIs).
