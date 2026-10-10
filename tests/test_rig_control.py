"""Tests for rig_control.py: pure validators plus the rigctld client against
a fake rigctld speaking its documented text protocol over a real socket.

Not a substitute for a real radio -- see rig_control.py's module docstring."""

import socket
import threading

import pytest

import rig_control as rc


# ── pure helpers ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("raw,hz", [
    ("146.520", 146_520_000), (146.52, 146_520_000),
    ("146520000", 146_520_000), (446_000_000, 446_000_000),
    ("146.5200", 146_520_000),
])
def test_parse_freq_hz(raw, hz):
    assert rc.parse_freq_hz(raw) == hz


@pytest.mark.parametrize("bad", ["", "abc", "0", "-5", "nan", "inf", None, True,
                                 "99999999999"])
def test_parse_freq_hz_rejects(bad):
    with pytest.raises(ValueError):
        rc.parse_freq_hz(bad)


def test_band_limits_parse_and_allow():
    bands = rc.parse_band_limits("144.0-148.0, 420-450\n902-928")
    assert bands[0] == (144_000_000, 148_000_000)
    assert len(bands) == 3
    assert rc.allow_frequency(146_520_000, bands)
    assert rc.allow_frequency(148_000_000, bands)          # inclusive edge
    assert not rc.allow_frequency(149_000_000, bands)
    assert not rc.allow_frequency(146_520_000, [])         # empty = fail closed


@pytest.mark.parametrize("spec", ["144", "148-144", "a-b", "144-148-150x"])
def test_band_limits_reject_bad(spec):
    with pytest.raises(ValueError):
        rc.parse_band_limits(spec)


def test_validate_mode():
    assert rc.validate_mode("fm") == "FM"
    with pytest.raises(ValueError):
        rc.validate_mode("FM; rm -rf")
    with pytest.raises(ValueError):
        rc.validate_mode("")


@pytest.mark.parametrize("raw,tenths", [("100.0", 1000), ("1000", 1000),
                                        ("0", 0), ("67.0", 670)])
def test_validate_ctcss(raw, tenths):
    assert rc.validate_ctcss_tenths(raw) == tenths


@pytest.mark.parametrize("bad", ["x", "5", "300.1", "-1"])
def test_validate_ctcss_rejects(bad):
    with pytest.raises(ValueError):
        rc.validate_ctcss_tenths(bad)


# ── fake rigctld ──────────────────────────────────────────────────────────

class FakeRigctld:
    """Speaks just enough of rigctld's default protocol for the client."""

    def __init__(self):
        self.freq = 146_520_000
        self.mode = "FM"
        self.ptt = 0
        self.tone = 1000
        self.shift = "None"
        self.offs = 0
        self.fail_set_freq = False
        self.received = []
        self.drop_next = False
        self._srv = socket.socket()
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(("127.0.0.1", 0))
        self._srv.listen(5)
        self.port = self._srv.getsockname()[1]
        self._stop = False
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self):
        while not self._stop:
            try:
                c, _ = self._srv.accept()
            except OSError:
                return
            threading.Thread(target=self._serve, args=(c,), daemon=True).start()

    def _serve(self, c):
        f = c.makefile("rwb", buffering=0)
        try:
            for raw in f:
                line = raw.decode().strip()
                self.received.append(line)
                if self.drop_next:
                    self.drop_next = False
                    c.close()
                    return
                f.write(self._reply(line).encode())
        except OSError:
            pass

    def _reply(self, line):
        cmd, _, arg = line.partition(" ")
        if cmd == "f":
            return f"{self.freq}\n"
        if cmd == "m":
            return f"{self.mode}\n15000\n"
        if cmd == "t":
            return f"{self.ptt}\n"
        if cmd == "\\get_ctcss_tone":
            return f"{self.tone}\n"
        if cmd == "\\get_rptr_shift":
            return f"{self.shift}\n"
        if cmd == "\\get_rptr_offs":
            return f"{self.offs}\n"
        if cmd == "\\set_rptr_shift":
            self.shift = arg
            return "RPRT 0\n"
        if cmd == "\\set_rptr_offs":
            self.offs = int(arg)
            return "RPRT 0\n"
        if cmd == "F":
            if self.fail_set_freq:
                return "RPRT -1\n"
            self.freq = int(arg)
            return "RPRT 0\n"
        if cmd == "M":
            self.mode = arg.split()[0]
            return "RPRT 0\n"
        if cmd == "\\set_ctcss_tone":
            self.tone = int(arg)
            return "RPRT 0\n"
        return "RPRT -11\n"

    def stop(self):
        self._stop = True
        self._srv.close()


@pytest.fixture
def fake():
    r = FakeRigctld()
    yield r
    r.stop()


@pytest.fixture
def client(fake):
    c = rc.RigctldClient("127.0.0.1", fake.port, timeout=2)
    yield c
    c.close()


def test_read_state(client):
    st = client.read_state()
    assert st["freq_hz"] == 146_520_000
    assert st["mode"] == "FM"
    assert st["ptt"] is False
    assert st["ctcss_tenths"] == 1000


def test_set_freq_and_mode_and_tone(client, fake):
    client.set_freq(147_000_000)
    client.set_mode("fm")
    client.set_ctcss_tone(885)
    assert (fake.freq, fake.mode, fake.tone) == (147_000_000, "FM", 885)


def test_ptt_state_is_read(client, fake):
    fake.ptt = 1
    assert client.get_ptt() is True


def test_radio_error_raises(client, fake):
    fake.fail_set_freq = True
    with pytest.raises(rc.RigError):
        client.set_freq(147_000_000)


def test_unreachable_rigctld_raises():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()                                  # nothing listening there
    with pytest.raises(rc.RigError):
        rc.RigctldClient("127.0.0.1", port, timeout=1).get_freq()


def test_reconnects_after_dropped_connection(client, fake):
    assert client.get_freq() == 146_520_000
    fake.drop_next = True                      # server hangs up mid-command
    assert client.get_freq() == 146_520_000    # one transparent retry


def test_command_injection_refused(client):
    with pytest.raises(rc.RigError):
        client._command("F 1\nF 2", 1)


def test_client_has_no_ptt_setter():
    # Tuning only: keying the transmitter is the node's job, never HenWen's.
    assert not hasattr(rc.RigctldClient, "set_ptt")
    assert not hasattr(rc.SimRig, "set_ptt")


def test_sim_rig_matches_client_surface():
    sim = rc.SimRig()
    sim.set_freq(147_000_000)
    assert sim.read_state()["freq_hz"] == 147_000_000


# ── repeater shift / offset ───────────────────────────────────────────────

@pytest.mark.parametrize("raw,want", [("+", "+"), ("-", "-"), ("\u2212", "-"), ("", "None"),
                                      (None, "None"), ("simplex", "None"), ("None", "None")])
def test_validate_shift(raw, want):
    assert rc.validate_shift(raw) == want


@pytest.mark.parametrize("bad", ["sideways", "++", "1"])
def test_validate_shift_rejects(bad):
    with pytest.raises(ValueError):
        rc.validate_shift(bad)


@pytest.mark.parametrize("raw,hz", [("0.600", 600_000), ("5", 5_000_000), ("5.0", 5_000_000),
                                    (600000, 600_000), ("0", 0), ("7.6", 7_600_000)])
def test_parse_offset_hz(raw, hz):
    assert rc.parse_offset_hz(raw) == hz


@pytest.mark.parametrize("bad", ["", "abc", "-1", "nan", "inf", True, "999999999999"])
def test_parse_offset_rejects(bad):
    with pytest.raises(ValueError):
        rc.parse_offset_hz(bad)


def test_tx_frequency():
    assert rc.tx_frequency_hz(147_000_000, "+", 600_000) == 147_600_000
    assert rc.tx_frequency_hz(147_000_000, "-", 600_000) == 146_400_000
    assert rc.tx_frequency_hz(146_520_000, "None", 600_000) == 146_520_000


def test_client_reads_and_sets_shift_offset(client, fake):
    client.set_rptr_offs(600_000)
    client.set_rptr_shift("+")
    assert (fake.shift, fake.offs) == ("+", 600_000)
    st = client.read_state()
    assert st["shift"] == "+" and st["offset_hz"] == 600_000


def test_missing_backend_support_degrades_to_simplex(client, fake):
    # A backend lacking these commands answers an RPRT error -> reads as simplex/0.
    fake._reply = lambda line: ("RPRT -11\n" if "rptr" in line else FakeRigctld._reply(fake, line))
    st = client.read_state()
    assert st["shift"] == "None" and st["offset_hz"] == 0
