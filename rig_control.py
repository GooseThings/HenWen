"""Radio frequency control for HenWen's kiosk VFO, via Hamlib's rigctld.

Standalone module: no Flask, DB or app.py import (same independence story as
irc_relay.py / recording.py). app.py owns config, scheduling, auth and the
routes; this file only knows how to talk to a radio and how to validate a
requested frequency.

Hamlib is the only radio-specific layer. HenWen speaks rigctld's plain-text
TCP protocol, so supporting another radio later is a Manager setting (a
Hamlib model number + serial port for rigctld), not new code here.

Deliberately NOT here: any way to key the transmitter. PTT belongs to the
Asterisk node (chan_simpleusb / the DRA-50's GPIO). HenWen only tunes, and
reads PTT state (so a QSY can be refused mid-transmission).

Not validated against a real radio at the time of writing -- written from
rigctld's documented text protocol and exercised against a fake rigctld in
tests/test_rig_control.py.
"""

import socket
import threading
import time

DEFAULT_RIGCTLD_HOST = "127.0.0.1"
DEFAULT_RIGCTLD_PORT = 4532
RIGCTLD_TIMEOUT_SEC = 3.0

# Hamlib mode names HenWen will set. A conservative subset: FM is what a
# repeater node needs; the rest are accepted so a non-FM Hamlib radio added
# later doesn't need a code change here.
ALLOWED_MODES = ("FM", "WFM", "AM", "USB", "LSB", "CW", "PKTFM")

# Plausible-frequency sanity bounds for ANY request, before band limits are
# even consulted (rejects 0, negatives and absurd values). Band limits from
# Manager are the real TX gate; this is just a type/range check.
MIN_FREQ_HZ = 100_000
MAX_FREQ_HZ = 6_000_000_000


class RigError(Exception):
    """Any failure talking to the radio / rigctld. Message is operator-safe."""


# ── Pure helpers (no I/O) ──────────────────────────────────────────────────

def parse_freq_hz(value):
    """Accept '146.520', '146520000', 146.52 or 146520000 and return integer
    Hz. Values under 1e5 are taken as MHz (nobody tunes 99 kHz here), the
    rest as Hz. Raises ValueError on anything unparseable or non-positive."""
    if isinstance(value, bool):
        raise ValueError("Invalid frequency")
    try:
        f = float(str(value).strip().replace(",", ""))
    except (TypeError, ValueError):
        raise ValueError("Invalid frequency")
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        raise ValueError("Invalid frequency")
    hz = int(round(f * 1_000_000)) if f < 100_000 else int(round(f))
    if not (MIN_FREQ_HZ <= hz <= MAX_FREQ_HZ):
        raise ValueError("Frequency out of range")
    return hz


def format_mhz(hz):
    """146520000 -> '146.520000'."""
    return f"{hz / 1_000_000:.6f}"


def parse_band_limits(spec):
    """Parse the Manager's TX-limit text into [(lo_hz, hi_hz), ...].

    One band per line or comma-separated, each 'LOW-HIGH' in MHz, e.g.
    '144.0-148.0, 420.0-450.0'. Raises ValueError with a message naming the
    bad entry. Empty spec -> [] (which allow_frequency() treats as 'no band
    allowed', i.e. fail closed, not 'anything goes')."""
    bands = []
    for raw in str(spec or "").replace("\n", ",").split(","):
        part = raw.strip()
        if not part:
            continue
        if "-" not in part:
            raise ValueError(f"Bad band '{part}' (expected LOW-HIGH in MHz)")
        lo_s, hi_s = part.split("-", 1)
        try:
            lo, hi = parse_freq_hz(lo_s), parse_freq_hz(hi_s)
        except ValueError:
            raise ValueError(f"Bad band '{part}' (expected LOW-HIGH in MHz)")
        if lo >= hi:
            raise ValueError(f"Bad band '{part}' (low must be below high)")
        bands.append((lo, hi))
    return bands


def allow_frequency(hz, bands):
    """True only if hz falls inside one of the configured bands. An empty
    band list allows nothing: a misconfigured/blank limit must never
    translate into an unrestricted transmitter."""
    return any(lo <= hz <= hi for lo, hi in bands)


def validate_mode(mode):
    m = str(mode or "").strip().upper()
    if m not in ALLOWED_MODES:
        raise ValueError(f"Mode must be one of {', '.join(ALLOWED_MODES)}")
    return m


def validate_ctcss_tenths(value):
    """CTCSS tone in tenths of a Hz (rigctld's unit): 0 = off, else 670-2541.
    Accepts Hz ('100.0') or tenths ('1000')."""
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("Invalid CTCSS tone")
    tenths = int(round(f * 10)) if f < 300 else int(round(f))
    if tenths != 0 and not (670 <= tenths <= 2541):
        raise ValueError("CTCSS tone out of range (67.0-254.1 Hz, or 0 for off)")
    return tenths


SHIFTS = ("None", "+", "-")
# Standard EIA CTCSS tones in tenths of a Hz, for the UI's picker. The server
# only range-checks (see validate_ctcss_tenths) so a radio's off-list tones
# still work; this list is what the kiosk offers.
CTCSS_TONES_TENTHS = (
    670, 693, 719, 744, 770, 797, 825, 854, 885, 915, 948, 974, 1000, 1035,
    1072, 1109, 1148, 1188, 1230, 1273, 1318, 1365, 1413, 1462, 1514, 1567,
    1622, 1679, 1738, 1799, 1862, 1928, 2035, 2107, 2181, 2257, 2336, 2418,
    2503)
MAX_OFFSET_HZ = 100_000_000


def validate_shift(value):
    """Repeater shift as Hamlib names it: '+', '-' or 'None' (simplex).
    Accepts the obvious spellings; raises ValueError on anything else."""
    v = str(value if value is not None else "").strip().lower()
    if v in ("", "none", "simplex", "off", "0"):
        return "None"
    if v in ("+", "plus"):
        return "+"
    if v in ("-", "minus", "\u2212"):
        return "-"
    raise ValueError("Shift must be +, - or simplex")


def parse_offset_hz(value):
    """Repeater offset to integer Hz. Values below 1000 are MHz ('0.600' ->
    600000, '5' -> 5000000); 1000 and up are already Hz. 0 is allowed (it
    means 'no offset'). Raises ValueError on junk or an absurd size."""
    if isinstance(value, bool):
        raise ValueError("Invalid offset")
    try:
        f = float(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("Invalid offset")
    if f != f or f < 0 or f == float("inf"):
        raise ValueError("Invalid offset")
    hz = int(round(f * 1_000_000)) if f < 1000 else int(round(f))
    if hz > MAX_OFFSET_HZ:
        raise ValueError("Offset out of range")
    return hz


def tx_frequency_hz(rx_hz, shift, offset_hz):
    """The frequency the radio will actually transmit on: the dial frequency
    moved by the repeater offset. This -- not the dial frequency -- is what
    the TX band limits must be checked against."""
    shift = validate_shift(shift)
    if shift == "+":
        return rx_hz + int(offset_hz)
    if shift == "-":
        return rx_hz - int(offset_hz)
    return rx_hz


# ── rigctld client ─────────────────────────────────────────────────────────

class RigctldClient:
    """Minimal client for rigctld's default text protocol.

    One persistent TCP connection, serialized by a lock (rigctld handles one
    command at a time anyway), reconnected lazily after any failure. Every
    method raises RigError rather than leaking socket exceptions.

    Protocol facts relied on (Hamlib rigctl(1)): 'f' replies one line (Hz);
    'm' replies two lines (mode, passband); 't' replies one line (0/1);
    's'/'S'... unused. Every 'set' command replies 'RPRT <n>' with n==0 on
    success, negative on error. A get that fails also replies 'RPRT <-n>'.
    """

    def __init__(self, host=DEFAULT_RIGCTLD_HOST, port=DEFAULT_RIGCTLD_PORT,
                 timeout=RIGCTLD_TIMEOUT_SEC, log_fn=None):
        self.host = host
        self.port = int(port)
        self.timeout = timeout
        self._log = log_fn or (lambda msg: None)
        self._sock = None
        self._buf = b""
        self._lock = threading.Lock()

    def close(self):
        with self._lock:
            self._close_locked()

    def _close_locked(self):
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._buf = b""

    def _connect_locked(self):
        try:
            s = socket.create_connection((self.host, self.port), timeout=self.timeout)
        except OSError as e:
            raise RigError(f"Cannot reach rigctld at {self.host}:{self.port} ({e})")
        s.settimeout(self.timeout)
        self._sock = s
        self._buf = b""

    def _readline_locked(self):
        while b"\n" not in self._buf:
            try:
                chunk = self._sock.recv(4096)
            except OSError as e:
                raise RigError(f"rigctld read failed ({e})")
            if not chunk:
                raise RigError("rigctld closed the connection")
            self._buf += chunk
            if len(self._buf) > 65536:
                raise RigError("rigctld sent an oversized reply")
        line, _, self._buf = self._buf.partition(b"\n")
        return line.decode("ascii", "replace").strip()

    def _command(self, line, reply_lines):
        """Send one command; return its reply lines. reply_lines is how many
        lines a successful reply has (1 for sets: 'RPRT 0')."""
        if "\n" in line or "\r" in line:
            raise RigError("Invalid command")
        with self._lock:
            for attempt in (1, 2):
                try:
                    if self._sock is None:
                        self._connect_locked()
                    self._sock.sendall(line.encode("ascii") + b"\n")
                    first = self._readline_locked()
                    if first.startswith("RPRT"):
                        lines = [first]
                    else:
                        lines = [first] + [self._readline_locked()
                                           for _ in range(reply_lines - 1)]
                    break
                except RigError:
                    self._close_locked()
                    if attempt == 2:
                        raise
                except OSError as e:
                    self._close_locked()
                    if attempt == 2:
                        raise RigError(f"rigctld write failed ({e})")
        if lines[0].startswith("RPRT"):
            try:
                code = int(lines[0].split()[1])
            except (IndexError, ValueError):
                raise RigError(f"Unexpected rigctld reply '{lines[0]}'")
            if code != 0:
                raise RigError(f"Radio/rigctld reported error {code}")
            return []          # a successful set
        return lines

    # -- reads
    def get_freq(self):
        try:
            return int(float(self._command("f", 1)[0]))
        except (IndexError, ValueError):
            raise RigError("Unreadable frequency reply")

    def get_mode(self):
        lines = self._command("m", 2)
        if not lines:
            raise RigError("Unreadable mode reply")
        return lines[0]

    def get_ptt(self):
        try:
            return self._command("t", 1)[0].strip() not in ("0", "")
        except IndexError:
            raise RigError("Unreadable PTT reply")

    def get_ctcss_tone(self):
        """Tenths of a Hz, 0 = off/unsupported. Not every Hamlib backend
        implements this; an error here is not fatal to a status read."""
        try:
            return int(self._command("\\get_ctcss_tone", 1)[0])
        except (IndexError, ValueError):
            return 0

    def get_rptr_shift(self):
        """'+', '-' or 'None'. Not every backend implements this."""
        try:
            return validate_shift(self._command("\\get_rptr_shift", 1)[0])
        except (IndexError, ValueError):
            return "None"

    def get_rptr_offs(self):
        """Offset in Hz, 0 if unset/unsupported."""
        try:
            return int(float(self._command("\\get_rptr_offs", 1)[0]))
        except (IndexError, ValueError):
            return 0

    # -- writes (frequency/mode/tone only -- never PTT)
    def set_freq(self, hz):
        self._command(f"F {int(hz)}", 1)

    def set_mode(self, mode, passband=0):
        self._command(f"M {validate_mode(mode)} {int(passband)}", 1)

    def set_ctcss_tone(self, tenths):
        self._command(f"\\set_ctcss_tone {int(tenths)}", 1)

    def set_rptr_shift(self, shift):
        self._command(f"\\set_rptr_shift {validate_shift(shift)}", 1)

    def set_rptr_offs(self, offset_hz):
        self._command(f"\\set_rptr_offs {int(offset_hz)}", 1)

    def read_state(self):
        """One consistent snapshot for the kiosk. Frequency + PTT are the
        load-bearing reads; mode/tone are best-effort (a backend that lacks
        one still yields a usable VFO display)."""
        state = {"freq_hz": self.get_freq(), "ptt": self.get_ptt()}
        try:
            state["mode"] = self.get_mode()
        except RigError:
            state["mode"] = ""
        try:
            state["ctcss_tenths"] = self.get_ctcss_tone()
        except RigError:
            state["ctcss_tenths"] = 0
        try:
            state["shift"] = self.get_rptr_shift()
            state["offset_hz"] = self.get_rptr_offs()
        except RigError:
            state["shift"], state["offset_hz"] = "None", 0
        state["ts"] = time.time()
        return state


# ── Simulator ──────────────────────────────────────────────────────────────

class SimRig:
    """In-memory stand-in with the same method surface as RigctldClient, for
    tests and for trying the UI with no radio attached."""

    def __init__(self, freq_hz=146_520_000, mode="FM", ctcss_tenths=0):
        self.freq_hz = freq_hz
        self.mode = mode
        self.ctcss_tenths = ctcss_tenths
        self.ptt = False
        self.shift = "None"
        self.offset_hz = 0
        self.calls = []

    def close(self):
        pass

    def get_freq(self):
        return self.freq_hz

    def get_mode(self):
        return self.mode

    def get_ptt(self):
        return self.ptt

    def get_ctcss_tone(self):
        return self.ctcss_tenths

    def set_freq(self, hz):
        self.calls.append(("set_freq", hz))
        self.freq_hz = int(hz)

    def set_mode(self, mode, passband=0):
        self.calls.append(("set_mode", mode))
        self.mode = validate_mode(mode)

    def set_ctcss_tone(self, tenths):
        self.calls.append(("set_ctcss_tone", tenths))
        self.ctcss_tenths = int(tenths)

    def get_rptr_shift(self):
        return self.shift

    def get_rptr_offs(self):
        return self.offset_hz

    def set_rptr_shift(self, shift):
        self.calls.append(("set_rptr_shift", shift))
        self.shift = validate_shift(shift)

    def set_rptr_offs(self, offset_hz):
        self.calls.append(("set_rptr_offs", offset_hz))
        self.offset_hz = int(offset_hz)

    def read_state(self):
        return {"freq_hz": self.freq_hz, "ptt": self.ptt, "mode": self.mode,
                "ctcss_tenths": self.ctcss_tenths, "shift": self.shift,
                "offset_hz": self.offset_hz, "ts": time.time()}
