"""Tests for the rx_audio_config setting: `path` (which RX pipeline serves
the Listen feature -- 'legacy' WebM/MSE or 'lowlatency' WebSocket/
AudioWorklet) and `agc_enabled` (AGC on/off, unified across both paths --
see the "Unified AGC Toggle" plan). `path` is deliberately independent of
recording_config/stream_relay_config -- both keep using the existing WebM
_AudioBroadcast regardless of its value. `agc_enabled` is NOT independent
of them: it's baked into that same shared WebM ffmpeg
(_start_broadcast()'s _webm_af_filter()), so toggling it affects Listen
(legacy mode), recording.py, and stream_relay.py together -- a deliberate
scope decision, not an oversight. These tests exercise the config/
validation/permission surface, not the audio pipelines themselves.
"""
import pytest

import app


def _login(client, username, password=None):
    """Stamp the session directly, mirroring tests/test_recording_config.py's
    own _login() helper (see that file for why this bypasses the real /login
    route)."""
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"] = (row["session_idle_timeout"] if row["session_idle_timeout"] is not None
                                 else app.SESSION_IDLE_TIMEOUT)
        sess["sid"]          = "test-sid-" + username


class TestValidateRxAudioPath:
    """Pure-function tests -- no DB/Flask context needed."""

    def test_accepts_legacy(self):
        assert app._validate_rx_audio_path("legacy") == "legacy"

    def test_accepts_lowlatency(self):
        assert app._validate_rx_audio_path("lowlatency") == "lowlatency"

    def test_normalizes_case_and_whitespace(self):
        assert app._validate_rx_audio_path("  LowLatency  ") == "lowlatency"

    def test_rejects_unknown_value(self):
        with pytest.raises(ValueError):
            app._validate_rx_audio_path("webrtc")

    def test_rejects_empty_string(self):
        with pytest.raises(ValueError):
            app._validate_rx_audio_path("")


class TestRxAudioConfigGating:
    def test_get_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/api/rx-audio/config")
        assert resp.status_code == 401

    def test_admin_cannot_view(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.get("/api/rx-audio/config").status_code == 403

    def test_superuser_cannot_view(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("su1", password="password12345", role="superuser")
        _login(client, "su1")
        assert client.get("/api/rx-audio/config").status_code == 403

    def test_admin_cannot_save(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        assert resp.status_code == 403

    def test_owner_gets_legacy_default_before_any_save(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.get("/api/rx-audio/config")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["path"] == "legacy"
        # Explicit owner decision: AGC defaults off everywhere (Listen-legacy,
        # recording, stream relay, low-latency alike), a deliberate behavior
        # change from AGC having always been on before this setting existed.
        assert bool(body["agc_enabled"]) is False


class TestRxAudioConfigValidation:
    def test_owner_save_and_reload_round_trips(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        assert resp.status_code == 200
        assert client.get("/api/rx-audio/config").get_json()["path"] == "lowlatency"

    def test_can_switch_back_to_legacy(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        resp = client.put("/api/rx-audio/config", json={"path": "legacy"})
        assert resp.status_code == 200
        assert client.get("/api/rx-audio/config").get_json()["path"] == "legacy"

    def test_rejects_unknown_path(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/rx-audio/config", json={"path": "webrtc"})
        assert resp.status_code == 400

    def test_missing_path_defaults_to_legacy(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/rx-audio/config", json={})
        assert resp.status_code == 200
        body = client.get("/api/rx-audio/config").get_json()
        assert body["path"] == "legacy"
        assert bool(body["agc_enabled"]) is False   # missing agc_enabled also defaults False


class TestWebmAfFilter:
    """Pure-function tests for _webm_af_filter() -- the legacy WebM
    broadcast's -af filter builder, shared by Listen (legacy mode),
    recording.py, and stream_relay.py (all three attach to the same
    _AudioBroadcast/ffmpeg for a node). No AMI/subprocess involved, mirrors
    audio_relay.py's _fade_frame() in being kept pure for exactly this
    reason."""

    def test_agc_disabled_is_alimiter_only(self):
        af = app._webm_af_filter(False)
        assert af == "alimiter=limit=0.85:attack=5:release=50:level=false"
        assert "dynaudnorm" not in af

    def test_agc_enabled_prepends_dynaudnorm(self):
        af = app._webm_af_filter(True)
        assert af == ("dynaudnorm=f=50:g=5:p=0.95:m=4:r=0.2,"
                       "alimiter=limit=0.85:attack=5:release=50:level=false")

    def test_alimiter_always_present_either_way(self):
        assert "alimiter=limit=0.85:attack=5:release=50:level=false" in app._webm_af_filter(False)
        assert "alimiter=limit=0.85:attack=5:release=50:level=false" in app._webm_af_filter(True)


class TestAgcEnabledConfig:
    def test_can_disable_agc(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/rx-audio/config", json={"path": "legacy", "agc_enabled": False})
        assert resp.status_code == 200
        assert bool(client.get("/api/rx-audio/config").get_json()["agc_enabled"]) is False

    def test_can_re_enable_agc(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "legacy", "agc_enabled": False})
        resp = client.put("/api/rx-audio/config", json={"path": "legacy", "agc_enabled": True})
        assert resp.status_code == 200
        assert bool(client.get("/api/rx-audio/config").get_json()["agc_enabled"]) is True

    def test_agc_setting_independent_of_path(self, client, create_user):
        # Confirms the "unified" design: AGC off + lowlatency path selected
        # is a valid, savable combination (AGC off is also the low-latency
        # path's own long-standing default behavior).
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/rx-audio/config", json={"path": "lowlatency", "agc_enabled": False})
        assert resp.status_code == 200
        body = client.get("/api/rx-audio/config").get_json()
        assert body["path"] == "lowlatency"
        assert bool(body["agc_enabled"]) is False


class TestForceTeardownOnChange:
    """A settings change is supposed to take effect immediately -- forcibly
    interrupting any active Listen session, recording, or the stream
    relay's connection, rather than waiting for each to end naturally (see
    api_rx_audio_config_save()). _force_teardown_all_broadcasts() and
    _signal_audio_ws_relay_reload() are stubbed here with call-tracking
    spies rather than exercised for real -- the former needs real
    _AudioBroadcast instances, the latter a real audio_ws_relay.py
    subprocess, neither of which belong in this test's scope. What's under
    test is the *decision* of when to call them: only on an actual change,
    never on a no-op save."""

    def _spy(self, monkeypatch, return_value=0):
        calls = {"teardown": 0, "signal": 0}
        monkeypatch.setattr(app, "_force_teardown_all_broadcasts",
                             lambda: (calls.__setitem__("teardown", calls["teardown"] + 1), return_value)[1])
        monkeypatch.setattr(app, "_signal_audio_ws_relay_reload",
                             lambda: calls.__setitem__("signal", calls["signal"] + 1))
        return calls

    def test_no_op_save_does_not_force_teardown(self, client, create_user, monkeypatch):
        # Saving exactly the current (default) values back -- nothing
        # actually changed, so no one should be interrupted.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        calls = self._spy(monkeypatch)
        resp = client.put("/api/rx-audio/config", json={"path": "legacy", "agc_enabled": False})
        assert resp.status_code == 200
        assert resp.get_json()["sessions_interrupted"] == 0
        assert calls["teardown"] == 0
        assert calls["signal"] == 0

    def test_changing_path_forces_teardown(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        calls = self._spy(monkeypatch, return_value=3)
        resp = client.put("/api/rx-audio/config", json={"path": "lowlatency", "agc_enabled": False})
        assert resp.status_code == 200
        assert resp.get_json()["sessions_interrupted"] == 3
        assert calls["teardown"] == 1
        assert calls["signal"] == 1

    def test_changing_agc_only_forces_teardown(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        calls = self._spy(monkeypatch)
        resp = client.put("/api/rx-audio/config", json={"path": "legacy", "agc_enabled": True})
        assert resp.status_code == 200
        assert calls["teardown"] == 1
        assert calls["signal"] == 1

    def test_second_identical_save_does_not_force_teardown_again(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        calls = self._spy(monkeypatch)
        client.put("/api/rx-audio/config", json={"path": "lowlatency", "agc_enabled": True})
        assert calls["teardown"] == 1
        # Saving the exact same values again -- already in that state, no
        # active session should be interrupted a second time for nothing.
        resp = client.put("/api/rx-audio/config", json={"path": "lowlatency", "agc_enabled": True})
        assert resp.get_json()["sessions_interrupted"] == 0
        assert calls["teardown"] == 1
        assert calls["signal"] == 1


class TestWsAudioStatus:
    """/api/ws-audio/status and /apply -- the Manager-triggered installer for
    the Apache WebSocket proxy (ws-audio/apply.sh), mirroring
    /api/audiosocket-tap/status's own existing precedent exactly."""

    def test_status_requires_owner(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.get("/api/ws-audio/status").status_code == 403

    def test_status_reports_script_installed_in_this_checkout(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/ws-audio/status").get_json()
        assert body["installed"] is True

    def test_status_includes_agc_enabled_default(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        assert client.get("/api/ws-audio/status").get_json()["agc_enabled"] is False

    def test_status_reflects_saved_agc_setting(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "legacy", "agc_enabled": False})
        assert client.get("/api/ws-audio/status").get_json()["agc_enabled"] is False

    def test_status_reports_not_applied_when_no_apache_vhost_present(self, client, create_user):
        # This sandbox has no /etc/apache2/sites-enabled/henwen*.conf files.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/ws-audio/status").get_json()
        assert body["applied"] is False
        assert body["apache_conf"] is None

    def test_status_includes_current_rx_audio_path(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        body = client.get("/api/ws-audio/status").get_json()
        assert body["rx_audio_path"] == "lowlatency"

    def test_apply_requires_owner(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.post("/api/ws-audio/apply").status_code == 403

    def test_apply_fails_cleanly_without_passwordless_sudo(self, client, create_user):
        # No sudoers rule exists in the test environment -- confirms this
        # degrades to a clean 500 with a readable error rather than hanging
        # or raising an unhandled exception.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/ws-audio/apply")
        assert resp.status_code == 500
        assert "error" in resp.get_json()
