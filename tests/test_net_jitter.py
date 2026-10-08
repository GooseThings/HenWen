"""Tests for the footer's network jitter monitor.

NETSTATS_LIVE below is the verbatim output of `iax2 show netstats` on this
project's own box (ASL3 Asterisk, DVSwitch bridge up, no remote links at the
time) rather than invented -- the exact column count and the loopback channel
naming are what the parser keys on. The rows with real remote peers are that
same shape with the host and figures changed, since a genuinely bad remote
link isn't something a test box can be made to produce on demand.

get_network_jitter() needs only AMIClient.command() to return lines, so the
AMI is stubbed rather than stood up.
"""
import pytest

import app


# Verbatim from a live node: two loopback channels (the DVSwitch/Analog_Bridge
# bridge), the two header lines, and the trailing summary line.
NETSTATS_LIVE = [
    "                                -------- LOCAL ---------------------  "
    "-------- REMOTE --------------------",
    "Channel                    RTT  Jit  Del  Lost   %  Drop  OOO  Kpkts  "
    "Jit  Del  Lost   %  Drop  OOO  Kpkts FirstMsg    LastMsg",
    "IAX2/127.0.0.1:4569-1412    3    0   40     0   0     0    0     32    "
    "0   40     1   0     0  662   1517 Rx:NEW      Rx:ACK",
    "IAX2/127.0.0.1:4569-6469    3    0   40     1   0     0  662   1538    "
    "0   40     0   0     0    0      1 Tx:NEW      Tx:ACK",
    "2 active IAX channels",
]

# Same shape, two real remote peers: 12ms and 47ms local jitter.
NETSTATS_REMOTE = NETSTATS_LIVE[:4] + [
    "IAX2/71.12.34.56:4569-2001  28   12   40     3   0     0    0    412    "
    "9   40     2   0     0    0    400 Rx:NEW      Rx:ACK",
    "IAX2/203.0.113.9:4569-2002  95   47   60    88   4     1    7    901   "
    "51   60    12   1     0    3    880 Tx:NEW      Tx:ACK",
    "4 active IAX channels",
]


class FakeAMI:
    """Minimal stand-in: get_network_jitter() only calls .command()."""

    def __init__(self, lines=None, raises=None):
        self._lines = lines or []
        self._raises = raises
        self.sent = []

    def command(self, cmd, log_level="INFO"):
        self.sent.append(cmd)
        if self._raises:
            raise self._raises
        return list(self._lines)


@pytest.fixture(autouse=True)
def _fresh_jitter_cache():
    """get_network_jitter() is TTL-memoized, so without this the first test's
    reading would be served to every later one."""
    app.get_network_jitter.cache_clear()
    yield
    app.get_network_jitter.cache_clear()


def _stub_ami(monkeypatch, ami, connected=True):
    monkeypatch.setattr(app, "_ami_connected", connected)
    monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(ami))


# ── _parse_iax_netstats ────────────────────────────────────────────────────

class TestParseNetstats:
    def test_parses_live_loopback_rows(self):
        rows = app._parse_iax_netstats(NETSTATS_LIVE)
        assert len(rows) == 2, "the 2 header lines and summary line must be skipped"
        assert rows[0] == {
            "channel": "IAX2/127.0.0.1:4569-1412",
            "rtt_ms": 3, "jitter_ms": 0, "lost": 0, "loss_pct": 0,
        }

    def test_reads_local_columns_not_remote(self):
        """The LOCAL block (what this node hears) comes before REMOTE. The
        remote row below deliberately differs in every column so reading the
        wrong block can't accidentally pass."""
        rows = app._parse_iax_netstats(NETSTATS_REMOTE)
        worst = [r for r in rows if r["channel"].startswith("IAX2/203.0.113.9")][0]
        assert worst["rtt_ms"] == 95
        assert worst["jitter_ms"] == 47     # LOCAL Jit, not REMOTE's 51
        assert worst["lost"] == 88          # LOCAL Lost, not REMOTE's 12
        assert worst["loss_pct"] == 4       # LOCAL %, not REMOTE's 1

    def test_skips_malformed_and_foreign_rows(self):
        assert app._parse_iax_netstats([
            "",
            "0 active IAX channels",
            "IAX2/1.2.3.4:4569-1  3  0",                      # truncated
            "SIP/henwen-tx  3  0  40  0  0  0  0  32  0  40  1  0  0  0  5",
            "IAX2/1.2.3.4:4569-1  x  0  40  0  0  0  0  32  0  40  1  0  0  9",
        ]) == []

    def test_tolerates_missing_trailing_message_columns(self):
        """FirstMsg/LastMsg are text and not required: a row carrying only the
        16 numeric-table columns still parses."""
        row = ("IAX2/198.51.100.7:4569-77  10   5   40     0   0     0    0    "
               "100    4   40     0   0     0    0     99")
        rows = app._parse_iax_netstats([row])
        assert len(rows) == 1 and rows[0]["jitter_ms"] == 5


# ── host extraction / remote-link filtering ───────────────────────────────

class TestRemoteLinkFilter:
    @pytest.mark.parametrize("channel,host", [
        ("IAX2/127.0.0.1:4569-1412", "127.0.0.1"),
        ("IAX2/71.12.34.56:4569-2001", "71.12.34.56"),
        ("IAX2/[::1]:4569-9", "::1"),
        ("IAX2/peer-1234", "peer-1234"),      # no port in the name
    ])
    def test_host_extraction(self, channel, host):
        assert app._iax_channel_host(channel) == host

    def test_loopback_links_are_not_counted(self):
        """A DVSwitch bridge is an IAX2 channel to 127.0.0.1 that always
        reports 0ms. Counting it would report a reassuring 0ms on a node whose
        only real link is in trouble."""
        rows = app._parse_iax_netstats(NETSTATS_LIVE)
        assert rows, "sanity: the loopback rows do parse"
        assert app._iax_netstats_remote_links(rows) == []

    def test_keeps_only_remote_links(self):
        links = app._iax_netstats_remote_links(app._parse_iax_netstats(NETSTATS_REMOTE))
        assert sorted(r["jitter_ms"] for r in links) == [12, 47]

    def test_negative_jitter_means_no_reading_yet(self):
        rows = [{"channel": "IAX2/71.12.34.56:4569-1", "jitter_ms": -1,
                 "rtt_ms": 0, "lost": 0, "loss_pct": 0}]
        assert app._iax_netstats_remote_links(rows) == []


# ── get_network_jitter ────────────────────────────────────────────────────

class TestGetNetworkJitter:
    def test_reports_the_worst_remote_link(self, monkeypatch):
        ami = FakeAMI(NETSTATS_REMOTE)
        _stub_ami(monkeypatch, ami)
        assert app.get_network_jitter() == {
            "jitter_ms": 47, "links": 2, "rtt_ms": 95, "loss_pct": 4,
        }
        assert ami.sent == ["iax2 show netstats"]

    def test_no_remote_links_is_a_reading_not_a_failure(self, monkeypatch):
        """links=0 with a null jitter, distinct from {} -- the footer says
        "no remote nodes linked" rather than "unavailable"."""
        _stub_ami(monkeypatch, FakeAMI(NETSTATS_LIVE))
        assert app.get_network_jitter() == {
            "jitter_ms": None, "links": 0, "rtt_ms": None, "loss_pct": None,
        }

    def test_ami_down_reports_unavailable(self, monkeypatch):
        called = []
        monkeypatch.setattr(app, "_ami_connected", False)
        monkeypatch.setattr(app, "ami_send_command",
                            lambda fn: called.append(1) or {})
        assert app.get_network_jitter() == {}
        assert not called, "must not issue an AMI command while AMI is down"

    def test_ami_error_reports_unavailable(self, monkeypatch):
        _stub_ami(monkeypatch, FakeAMI(raises=OSError("broken pipe")))
        assert app.get_network_jitter() == {}

    def test_result_is_memoized(self, monkeypatch):
        """One AMI command per TTL no matter how many kiosk tabs poll the
        board -- the whole reason this is cheap enough for the hot path."""
        ami = FakeAMI(NETSTATS_REMOTE)
        _stub_ami(monkeypatch, ami)
        for _ in range(5):
            app.get_network_jitter()
        assert ami.sent == ["iax2 show netstats"]

    def test_peer_identity_is_not_exposed(self, monkeypatch):
        """/api/status/board is public, so the payload must not carry peer IPs
        (see get_network_jitter()'s docstring)."""
        _stub_ami(monkeypatch, FakeAMI(NETSTATS_REMOTE))
        blob = repr(app.get_network_jitter())
        assert "71.12.34.56" not in blob and "203.0.113.9" not in blob


# ── board payload ─────────────────────────────────────────────────────────

class TestBoardPayload:
    """The footer reads this straight off the board payload, so the key going
    missing would silently blank the monitor with nothing else failing."""

    def test_board_carries_net_key_when_ami_is_down(self, client, create_user,
                                                    monkeypatch):
        """_ami_connected is pinned rather than left at its default: earlier
        tests in a full-suite run reach this box's real AMI, which leaves the
        flag True, and this case would then quietly measure live Asterisk
        instead of the state it means to assert."""
        create_user("owner1", role="owner")
        monkeypatch.setattr(app, "_ami_connected", False)
        app.get_network_jitter.cache_clear()
        resp = client.get("/api/status/board")
        assert resp.status_code == 200
        assert resp.get_json()["net"] == {}

    def test_board_carries_the_reading(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _stub_ami(monkeypatch, FakeAMI(NETSTATS_REMOTE))
        app.get_network_jitter.cache_clear()
        net = client.get("/api/status/board").get_json()["net"]
        assert net["jitter_ms"] == 47 and net["links"] == 2
