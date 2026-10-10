# Rig control (kiosk VFO)

Lets the Status Board change a radio's frequency, mode and CTCSS tone through
[Hamlib](https://hamlib.github.io/)'s `rigctld`. Owner-configured in
**Manager > Rig Control**; off by default.

**Status: not verified against real hardware.** The client was written from
`rigctld`'s documented text protocol and tested against a fake `rigctld`
(`tests/test_rig_control.py`). Expect to debug the first real connection.

## What it does and doesn't do

- Reads frequency / mode / CTCSS / PTT state every 2s into a shared in-process
  cache (one `rigctld` connection however many kiosks are open).
- Tunes: frequency, mode, PL (CTCSS TX tone), repeater shift (+/-/simplex) and offset. Nothing else. A receive-side tone squelch / DCS is not controlled.
- **Never keys the transmitter.** There is no PTT setter in the code, on
  purpose. PTT belongs to the Asterisk node (chan_simpleusb / the audio
  interface's GPIO).
- Does not start or supervise `rigctld` for you.

## Guardrails (enforced server-side in `POST /api/rig/tune`)

| Rule | Detail |
|---|---|
| TX bands | Owner-set list, applies to **every** role. Empty list allows nothing. Checked against both the dial frequency **and the transmit frequency** (dial plus/minus the repeater offset), so 147.9 MHz with a +0.6 MHz shift is refused. |
| Who can tune | Any logged-in user: recall a saved memory channel. Admin+: any frequency inside the bands. |
| Radio transmitting | Refused (read from the rig's PTT state). |
| Node keyed | Refused by default (needs the node number set). |
| Nodes linked | Optional refusal. |
| Node lockout | Non-owner tuning is blocked while the owner has locked a node. |

The tuning rules are about preventing mistakes; keeping within your license
privileges is still on the owner (set the TX bands accordingly).

## Running rigctld

Kenwood TM-D710G on its **PC port** (not the DATA port, not the head-unit
port), e.g.:

```bash
rigctld -m 2034 -r /dev/serial/by-id/<your-cable> -s 57600 -t 4532
```

- Model number and baud: confirm with `rigctl -l | grep -i 710` and the
  radio's PC-port baud menu setting. Hamlib 4.6.4+ fixed a Kenwood backend
  bug that affects the D710/V71 command terminator; apt's candidate here was
  4.6.2, so a newer build may be needed.
- If CAT times out, a reported cause on Digirig cables is a missing RTS/CTS
  loopback on the MiniDin8 connector.
- Bind `rigctld` to loopback (the default) -- it has no authentication.

## Proxmox LXC

An LXC container only sees devices the host passes through. You need the
serial adapter (`/dev/ttyUSB*` or `/dev/serial/by-id/...`) in the container,
plus `/dev/snd` and `/dev/hidraw*` for the audio interface (e.g. a DRA-50) that
the node itself uses. Use a stable `by-id` path for the serial device.

## Audio / PTT is separate

Audio and PTT are the Asterisk node's business (e.g. DRA-50 on the D710's
DATA port with `chan_simpleusb`). This feature only changes frequency. DRA-50
jumper settings and the D710's data-port menu options are not covered here and
were not verified.

## Adding another radio

Run `rigctld` with that radio's Hamlib model and set the host/port. Anything
Hamlib exposes through the standard `f/F/m/M/t/get_ctcss_tone` commands works;
a backend that lacks one (CTCSS is the usual gap) degrades to a blank field
rather than an error.

## Repeater shift, offset and PL

Uses rigctld's `\set_rptr_shift` / `\set_rptr_offs` / `\set_ctcss_tone` plus `\set_func TONE` (setting a tone frequency alone does not switch encoding on -- seen against rigctld's dummy rig; "No PL" turns the function off instead of writing a 0 Hz tone). Whether
the TM-D710G backend implements all three is **unverified**; a backend that
lacks one reads back as simplex / 0 / no PL, and a set against it will return
an error you'll see in the kiosk. "No PL" sends tone 0, which a radio may also
reject. Memory-channel format in Manager:
`label, MHz, mode, PL Hz, shift, offset MHz`, e.g. `Local Rptr, 147.000, FM, 100.0, +, 0.600`.
Leaving shift/offset out of a free-form tune keeps whatever the radio has now
(and the band check uses that), and a tune is refused until HenWen has read the
radio's state at least once.

## What has been checked against real Hamlib

`tests/test_rig_control.py` includes a test against a real `rigctld -m 1` (the
dummy radio) that runs automatically wherever Hamlib is installed. It confirmed
the wire format and found two bugs that a hand-written fake had hidden (PTT
readback is not universal; a PL tone needs the TONE function). Hamlib 4.6.2
lists model 2034 as `TM-D710(G)`. The dummy rig is not a D710, so none of this
says how the D710 backend itself behaves.

## First-connection checklist

1. `rigctl -m 2034 -r /dev/serial/by-id/<cable> -s <baud> f` should print the
   frequency. Try `m`, `t`, `\get_rptr_shift`, `\get_rptr_offs`,
   `\get_ctcss_tone`, `\get_func TONE` and note which ones error.
2. Put the radio in VFO mode (not memory recall) on the band the node uses.
3. Start `rigctld`, then watch **Manager > Rig Control**: the status line shows
   the connection state and, for the owner, the exact error.
4. Try a tune with the transmitter disconnected or on a dummy load first.
