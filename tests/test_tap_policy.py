"""Policy around the AudioSocket tap: which channels may be tapped, the
per-node circuit breaker, and refusing audio capture for the DVSwitch bridge
node. The underlying fact (a ChanSpy leg on a silent channel can't be hung up)
was reproduced against a throwaway Asterisk, which can't run in pytest -- these
tests pin the *decisions* made from it.
"""
import pytest

import app


@pytest.fixture(autouse=True)
def _policy_state(monkeypatch):
    app._tap_breaker.clear()
    app._tap_skip_logged.clear()
    monkeypatch.setattr(app, "_asterisk_pid", lambda: 4242)
    monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: None)
    yield
    app._tap_breaker.clear()
    app._tap_skip_logged.clear()


def _no_side_effects(monkeypatch):
    """Fail loudly if the gate lets a call through to AMI or a subprocess."""
    def boom(*a, **k): raise AssertionError("gate should have returned before this")
    monkeypatch.setattr(app, "ami_send_command", boom)
    monkeypatch.setattr(app.subprocess, "Popen", boom)


def _try(channel, node="643930"):
    return app._try_audiosocket_tap(node, channel, "-", "gen1", {})


class TestChannelTech:
    @pytest.mark.parametrize("channel,tech", [
        ("SimpleUSB/643930", "simpleusb"),
        ("usrp/127.0.0.1:34001:32001", "usrp"),
        ("Local/pseudo@default-00000000;1", "local"),
        ("IAX2/12.17.28.195:4569-1392", "iax2"),
        ("", ""), (None, ""),
    ])
    def test_tech_parsing(self, channel, tech):
        assert app._tap_channel_tech(channel) == tech

    def test_default_allowlist_is_simpleusb_only(self):
        assert app.TAP_CHANNEL_TECHS == {"simpleusb"}


class TestAllowlistGate:
    @pytest.mark.parametrize("channel", [
        "usrp/127.0.0.1:34001:32001", "Local/pseudo@default-0;1", "IAX2/1.2.3.4:4569-1", "USBRadio/1999", "",
    ])
    def test_non_allowlisted_tech_falls_back_before_touching_asterisk(self, monkeypatch, channel):
        _no_side_effects(monkeypatch)
        assert _try(channel) is None

    def test_allowlisted_tech_passes_the_gate(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app, "ami_send_command", lambda fn: calls.append(1) or {"ok": False})
        assert _try("SimpleUSB/643930") is None     # modules "not loaded" -> None, but it got that far
        assert calls == [1]

    def test_tech_match_is_case_insensitive(self, monkeypatch):
        calls = []
        monkeypatch.setattr(app, "ami_send_command", lambda fn: calls.append(1) or {"ok": False})
        _try("simpleusb/643930")
        assert calls == [1]

    def test_allowlist_can_be_extended(self, monkeypatch):
        monkeypatch.setattr(app, "TAP_CHANNEL_TECHS", {"simpleusb", "usbradio"})
        calls = []
        monkeypatch.setattr(app, "ami_send_command", lambda fn: calls.append(1) or {"ok": False})
        _try("USBRadio/1999")
        assert calls == [1]

    def test_skip_is_logged_once_per_node_and_tech(self, monkeypatch):
        _no_side_effects(monkeypatch)
        logs = []
        monkeypatch.setattr(app, "log", lambda lvl, msg, *a, **k: logs.append(msg))
        _try("usrp/127.0.0.1:34001:32001"); _try("usrp/127.0.0.1:34001:32001")
        assert len([m for m in logs if "allowlist" in m]) == 1


class TestCircuitBreaker:
    def test_blocks_only_the_tripped_node(self, monkeypatch):
        app._tap_breaker_trip("643930", "test")
        assert app._tap_breaker_remaining("643930") > 0
        assert app._tap_breaker_remaining("1999") == 0

    def test_open_breaker_falls_back_before_touching_asterisk(self, monkeypatch):
        app._tap_breaker_trip("643930", "test")
        _no_side_effects(monkeypatch)
        assert _try("SimpleUSB/643930") is None

    def test_other_nodes_still_tap_while_one_is_tripped(self, monkeypatch):
        app._tap_breaker_trip("643930", "test")
        calls = []
        monkeypatch.setattr(app, "ami_send_command", lambda fn: calls.append(1) or {"ok": False})
        _try("SimpleUSB/1999", node="1999")
        assert calls == [1]

    def test_expires_after_the_cooldown(self, monkeypatch):
        now = [1_000_000.0]
        monkeypatch.setattr(app.time, "time", lambda: now[0])
        app._tap_breaker_trip("643930", "test")
        now[0] += app.TAP_BREAKER_COOLDOWN_SEC - 1
        assert app._tap_breaker_remaining("643930") == 1
        now[0] += 2
        assert app._tap_breaker_remaining("643930") == 0
        assert "643930" not in app._tap_breaker

    def test_default_cooldown_is_thirty_minutes(self):
        assert app.TAP_BREAKER_COOLDOWN_SEC == 1800

    def test_clears_when_asterisk_restarts(self, monkeypatch):
        app._tap_breaker_trip("643930", "test")            # recorded pid 4242
        assert app._tap_breaker_remaining("643930") > 0
        monkeypatch.setattr(app, "_asterisk_pid", lambda: 9999)   # restarted: stranded leg is gone
        assert app._tap_breaker_remaining("643930") == 0

    def test_unknown_pid_does_not_clear_it(self, monkeypatch):
        app._tap_breaker_trip("643930", "test")
        monkeypatch.setattr(app, "_asterisk_pid", lambda: None)
        assert app._tap_breaker_remaining("643930") > 0

    def test_status_route_lists_open_breakers(self, client, create_user):
        from tests.test_rx_diagnostics import _login
        create_user("owner1", role="owner")
        _login(client, "owner1")
        app._tap_breaker_trip("643930", "test")
        body = client.get("/api/leak-monitor").get_json()
        assert body["tap_breaker"]["643930"] > 0


class TestBridgeNodeGuard:
    def test_reason_only_for_the_enabled_bridge_node(self, monkeypatch):
        assert app._audio_node_block_reason("1999") is None
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        assert "DVSwitch bridge" in app._audio_node_block_reason("1999")
        assert app._audio_node_block_reason("643930") is None

    @pytest.mark.parametrize("starter", ["_start_broadcast", "_start_capture_only"])
    def test_starters_refuse_the_bridge_node_before_any_work(self, monkeypatch, starter):
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        def boom(*a, **k): raise AssertionError("must not look for a channel")
        monkeypatch.setattr(app, "_find_node_channel", boom)
        with pytest.raises(app.AudioNodeBlocked):
            getattr(app, starter)("1999")

    @pytest.mark.parametrize("starter", ["_start_broadcast", "_start_capture_only"])
    def test_other_nodes_are_not_blocked(self, monkeypatch, starter):
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        monkeypatch.setattr(app, "_find_node_channel", lambda n: None)
        with pytest.raises(RuntimeError) as e:
            getattr(app, starter)("643930")
        assert not isinstance(e.value, app.AudioNodeBlocked)

    def test_listen_route_returns_403_for_the_bridge_node(self, client, create_user, monkeypatch):
        from tests.test_rx_diagnostics import _login
        create_user("owner1", role="owner")      # first-run state 503s every page until an owner exists
        create_user("user1", role="user")
        _login(client, "user1")
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        r = client.get("/api/audio/stream/1999")
        assert r.status_code == 403 and "DVSwitch bridge" in r.get_json()["error"]

    def test_stream_relay_cannot_target_the_bridge_node(self, client, create_user, monkeypatch):
        from tests.test_rx_diagnostics import _login
        create_user("owner1", role="owner")
        _login(client, "owner1")
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        r = client.post("/api/stream-relay/config",
                        json={"broadcastify_enabled": 1, "broadcastify_host": "audio.example.org",
                              "broadcastify_port": 80, "broadcastify_mount": "/m",
                              "broadcastify_user": "u", "broadcastify_pass": "p",
                              "target_node": "1999"})
        assert r.status_code == 400 and "DVSwitch bridge" in r.get_json()["error"]
