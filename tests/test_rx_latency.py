"""Tests for /api/audio/rx-latency, which sizes browser TX's post-unkey RX
mute hold (the self-echo guard: users reported hearing themselves on the
Listen stream right after ending a transmission, because the mute was
released the instant the PTT came up while the operator's own audio was
still in the RX pipeline).

The one property worth pinning is the fail-safe direction: when nothing is
capturing yet, or the running capture is MixMonitor, the estimate must be
the slow one. Under-reporting puts the echo back; over-reporting only
trims a little of the repeater tail.
"""
import app


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]      = True
        sess["username"]       = username
        sess["role"]           = row["role"]
        sess["user_id"]        = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"]   = app.SESSION_IDLE_TIMEOUT
        sess["sid"]            = "test-sid-" + username


class FakeBroadcast:
    def __init__(self, tap_mode):
        self._tap_mode = tap_mode


class FakeCapture:
    def __init__(self, tap_mode):
        self.tap_mode = tap_mode


class TestRxLatencyEstimate:
    def test_audiosocket_is_much_faster_than_mixmonitor(self):
        fast = app._rx_latency_estimate_ms("audiosocket", "lowlatency")
        slow = app._rx_latency_estimate_ms("mixmonitor", "lowlatency")
        assert slow - fast >= 1500, (
            "MixMonitor's ~2s of Asterisk stdio buffering is the whole reason "
            "the hold has to be path-dependent")

    def test_unknown_capture_is_treated_as_the_slow_path(self):
        assert (app._rx_latency_estimate_ms(None, "lowlatency")
                == app._rx_latency_estimate_ms("mixmonitor", "lowlatency"))

    def test_legacy_path_adds_the_webm_cluster_granularity(self):
        legacy = app._rx_latency_estimate_ms("audiosocket", "legacy")
        low    = app._rx_latency_estimate_ms("audiosocket", "lowlatency")
        assert legacy == low + app.RX_LATENCY_WEBM_CLUSTER_MS


class TestRxCaptureMode:
    def test_none_when_nothing_is_capturing(self):
        assert app._rx_capture_mode("546054") is None

    def test_reads_a_live_broadcast(self, monkeypatch):
        monkeypatch.setitem(app._audio_active, "546054", FakeBroadcast("audiosocket"))
        assert app._rx_capture_mode("546054") == "audiosocket"

    def test_reads_a_live_capture_only_relay(self, monkeypatch):
        monkeypatch.setitem(app._capture_only_active, "546054", FakeCapture("mixmonitor"))
        assert app._rx_capture_mode("546054") == "mixmonitor"

    def test_the_slower_of_two_live_captures_wins(self, monkeypatch):
        """A legacy broadcast and a capture-only relay can both be up for one
        node and disagree about the capture path. Which one feeds a given
        listener isn't knowable from here, so the slow one has to win: an
        over-estimate costs a little repeater tail, an under-estimate puts
        the operator's own voice back in their ears."""
        monkeypatch.setitem(app._audio_active, "546054", FakeBroadcast("audiosocket"))
        monkeypatch.setitem(app._capture_only_active, "546054", FakeCapture("mixmonitor"))
        assert app._rx_capture_mode("546054") == "mixmonitor"

        monkeypatch.setitem(app._audio_active, "546055", FakeBroadcast("mixmonitor"))
        monkeypatch.setitem(app._capture_only_active, "546055", FakeCapture("audiosocket"))
        assert app._rx_capture_mode("546055") == "mixmonitor", (
            "order of the two lookups must not decide the answer")


class TestRxLatencyRoute:
    def test_requires_a_login(self, client, create_user):
        create_user("owner1", role="owner")
        assert client.get("/api/audio/rx-latency/546054").status_code in (302, 401, 403)

    def test_rejects_a_non_node(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("user1", role="user")
        _login(client, "user1")
        assert client.get("/api/audio/rx-latency/not-a-node").status_code in (400, 404)

    def test_any_logged_in_role_gets_an_estimate(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        create_user("user1", role="user")
        _login(client, "user1")
        monkeypatch.setitem(app._audio_active, "546054", FakeBroadcast("audiosocket"))
        d = client.get("/api/audio/rx-latency/546054").get_json()
        assert d["capture_mode"] == "audiosocket"
        assert d["capture_known"] is True
        assert d["est_latency_ms"] > 0

    def test_idle_node_reports_unknown_and_the_slow_estimate(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("user1", role="user")
        _login(client, "user1")
        d = client.get("/api/audio/rx-latency/546054").get_json()
        assert d["capture_known"] is False
        assert d["est_latency_ms"] >= app.RX_LATENCY_CAPTURE_MS["mixmonitor"]
