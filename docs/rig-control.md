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

An LXC container only sees devices the host passes through, and this one is
**unprivileged** (`/proc/self/uid_map` is `0 100000 65536`), so there are two
parts: expose the device nodes, then make them usable by the container's
`asterisk` user.

**Blocker found in practice: OSS sound emulation.** `chan_simpleusb` as shipped
in ASL3 opens its sound card through OSS (`/dev/dsp<N>`), not ALSA -- ASL3
ships `/etc/modules-load.d/asl3-oss.conf` to load the `snd_pcm_oss` kernel
module on a normal install. A container cannot load kernel modules, so the
**host kernel must provide `snd_pcm_oss`** and the `/dev/dsp*` nodes must then
be passed through. Tested on Proxmox VE with kernel `7.0.14-5-pve`: it is built
with `CONFIG_SND_PCM_OSS` not set, so `/dev/dsp*` cannot exist at all, and
`chan_simpleusb` logs `Unable to open DSP device 1: No such file or directory`
every 20 ms (a log flood -- roll back the `rxchannel` change if you see it).
USB (libusb, for the PTT/COS lines) and ALSA both worked through the
passthrough; the missing OSS emulation is the only failure.

So with a stock PVE kernel, **`SimpleUSB` cannot run inside an LXC container**.
Options: run the node in a **VM** (own kernel; USB passthrough is native in
Proxmox -- Debian's stock kernel ships `snd_pcm_oss`), use a host kernel that
has it, or put the radio on a separate machine. Everything below that applies
to the device nodes is still correct, but it is not sufficient on its own.

What else the node needs, checked against the installed modules:
`chan_simpleusb` uses **libusb-0.1** (`/dev/bus/usb`) for the dongle's PTT/COS
lines; `res_usbradio` uses **ALSA** only for the mixer controls. The rig
control serial adapter needs `/dev/ttyUSB*` or `/dev/ttyACM*` (use a
`/dev/serial/by-id/...` path).

| Device | Char major | Passed as |
|---|---|---|
| ALSA | 116 | bind `/dev/snd` |
| USB (libusb) | 189 | bind the directory `/dev/bus/usb` (survives replug) |
| ttyUSB / ttyACM | 188 / 166 | bind the node, or `/dev/serial` |

via `lxc.cgroup2.devices.allow` and `lxc.mount.entry` in
`/etc/pve/lxc/<id>.conf`. Applying them needs a container restart.

Unprivileged: bind-mounted nodes keep their *host* numeric owner, which the
container sees as `nobody`. Give the specific devices (by vendor/product ID)
a host udev `MODE`/`GROUP` the mapped container user can use. Confirm numbers
on your own host rather than copying the table.

## Audio / PTT: DRA-50 to the D710 DATA port

Audio and PTT are the Asterisk node's business (`chan_simpleusb`); this feature
only changes frequency. This section records the research for wiring a Masters
Communications **DRA-50** to a **Kenwood TM-D710G**. It comes from the
manufacturers' documents, **not a bench test** -- the items under "Not verified"
need checking on the real hardware.

### Cable

Kenwood's manual lists the DATA terminal pins as a-f; reading them as pins 1-6
in order (the manual doesn't state that outright), they line up with the
DRA-50's mini-DIN-6 by function:

| Pin | D710 DATA terminal | DRA-50 mini-DIN-6 |
|---|---|---|
| 1 | PKD -- transmit audio in | TX audio (JU5 selects left/right channel) |
| 2 | ground | ground |
| 3 | PKS -- transmit control; low = transmit, mic muted | PTT |
| 4 | PR9 -- detected 9600 bps data out | RX audio, 9600 position (JU7 = B) |
| 5 | PR1 -- detected 1200 bps data out | RX audio, 1200 position (JU7 = A) |
| 6 | SQC -- squelch control; closed = low, open = high | COS |

Digirig's forum reports the D710's data port matches the Yaesu FT-8xx pinout, so
FT-8xx-style audio/PTT cables fit. The D710 DATA port is a 6-pin mini-DIN (no
10-pin adapter). A plain mini-DIN-6 cable works **only if all six pins are wired
straight through** -- check continuity on every pin; without pin 6 the node
gets no carrier-detect (COS).

The older TM-V7A was measured with its 1200 and 9600 outputs on pins 4 and 5
the opposite way round from the D710 manual, so confirm which JU7 position
sounds right.

### DRA-50 jumpers (per Masters' jumper text -- check against your board's silkscreen)

| Jumper | Setting | Why |
|---|---|---|
| JU7 | B (9600 pin, default); try A if audio is quiet or wrong | picks pin 4 vs pin 5 |
| JU3 | **installed** (off by default) | AllStar COS: routes the SQC line (pin 6) to the node |
| JU4 | removed | CTCSS input -- not on the mini-DIN-6 connector |
| JU5 | A (default) | TX audio channel |
| JU2 | A (default) | amps on USB 5 V; the D710 needs ~2 Vp-p at 9600 |
| JU6 | removed (default) | PTT LED protection |
| H1/H2 | shunt over the center two pins | as shipped |

Masters' own pages disagree slightly on what JU1-JU4 do, and the jumper text
mentioned a "DRA-45" (a different board), so treat the table as a starting point.
Masters generally recommends the DRA-50M for Kenwood radios; a DRA-50 should work.

### D710 menu settings (Kenwood manual)

- **Menu 918, External Data Band** -- the band the node uses (A, B, TX:A RX:B,
  RX:A TX:B). **HenWen's rig control must tune this same band.**
- **Menu 919, Data Speed** -- 1200 or 9600 bps. Transmit input sensitivity is
  ~40 mVp-p at 1200 and ~2 Vp-p at 9600. 9600 is the likely choice for voice;
  the audio bandwidth in each mode is unverified.
- **Menu 921, SQC Output** -- what the COS line means: OFF, BUSY (signal on the
  data band), SQL (CTCSS/DCS must match; carrier if no tone is set), TX,
  BUSY.TX, SQL.TX. The activation logic can also be inverted with Kenwood's
  MCP-6A software.
- **Menu 920, PC Port Speed** -- 9600 / 19200 / 38400 / 57600; must match
  `rigctld -s`. Power-cycle the radio after changing it.

### Node side (`/etc/asterisk/simpleusb.conf`)

- `carrierfrom`: `usb` (active high) or `usbinvert` (active low, the default).
  The SQC line is high when squelch is open, but whether the DRA-50 inverts it
  is unknown -- pick by testing.
- `ctcssfrom`: `simpleusb` only offers `no` / `usb` / `usbinvert`, and the DRA-50
  has no CTCSS pin on the mini-DIN-6, so use `no` and let the D710 do tone
  squelch (Menu 921 = SQL).
- `deemphasis`: the data-port outputs are probably flat discriminator audio, so
  try `yes`. `preemphasis` on the transmit side may be needed too.

### Not verified

The a-f to pin 1-6 order; which output is flat vs de-emphasized; the COS
polarity through the DRA-50; the best TX level and whether the 9600 TX path
needs pre-emphasis; and the DRA-50 jumper meanings above. A first session:
`susb tune` on the node, adjusting the DRA-50's R14 trimmer, `txmixa` and
`rxmixerset` while watching deviation.

Sources: Masters Communications
([pinout](https://www.masterscommunications.com/products/radio-adapter/dra/txt/dra50-DIN-pinout.txt),
[jumpers](https://www.masterscommunications.com/products/radio-adapter/dra/txt/dra50-jumpers.txt),
[DRA-50 vs 50M](https://masterscommunications.com/products/radio-adapter/dra/dra50-vs-dra50m.html)),
[Kenwood TM-D710G manual](https://kasc.kenwood.com/files/prod/2681/5/TM-D710GE_GA_Instruction_Manual_CD-ROM_V1.01.pdf),
[Febo TM-V7A measurements](https://www.febo.com/packet/layer-one/kenwood-tmv7a.html),
[Digirig forum](https://forum.digirig.net/t/cables-for-kenwood-tm-d710/136).

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
