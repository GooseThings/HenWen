"""Tests for the /internal/audio/* control-plane API that audio_ws_relay.py
(the low-latency RX audio path's own process) uses to drive capture
lifecycle and authorize browser WebSocket connections. See app.py's
"Low-latency RX audio" section and the "Low-Latency Listen Path" plan.

These routes are never meant to be reachable by an ordinary browser
session -- ensure-capture/release-capture are gated purely by
_check_internal_audio_request() (loopback address + shared secret,
independent of any Flask session), and authorize-ws is gated by the real
check_auth() session logic (it's in _USER_OR_ABOVE) *in addition to* the
same internal secret check, since its whole purpose is answering "is this
forwarded browser cookie a valid logged-in session" for a caller that isn't
itself a logged-in user.
"""
import pytest

import app


def _login(client, username, password=None):
    """Stamp the session directly, mirroring tests/test_recording_config.py's
    own _login() helper."""
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


def _secret_headers():
    return {"X-Internal-Secret": app._INTERNAL_AUDIO_SECRET}


class TestCheckInternalAudioRequest:
    """Flask's test client sends requests with REMOTE_ADDR=127.0.0.1 by
    default, so these exercise the secret check specifically -- the
    loopback check is exercised via environ_overrides below."""

    def test_missing_secret_rejected(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post("/internal/audio/ensure-capture", json={"node": "628280"})
        assert resp.status_code == 403

    def test_wrong_secret_rejected(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post("/internal/audio/ensure-capture",
                            json={"node": "628280"},
                            headers={"X-Internal-Secret": "not-the-real-secret"})
        assert resp.status_code == 403

    def test_non_loopback_remote_addr_rejected_even_with_correct_secret(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post(
            "/internal/audio/ensure-capture", json={"node": "628280"},
            headers=_secret_headers(),
            environ_overrides={"REMOTE_ADDR": "203.0.113.5"},
        )
        assert resp.status_code == 403

    def test_correct_secret_and_loopback_passes_the_gate(self, client, create_user):
        # Passes the internal gate, then hits real business logic (no
        # lowlatency mode selected yet -> 409, not 403) -- proves the gate
        # itself let this through rather than blocking it.
        create_user("owner1", role="owner")
        resp = client.post("/internal/audio/ensure-capture",
                            json={"node": "628280"}, headers=_secret_headers())
        assert resp.status_code == 409


class TestEnsureCapture:
    def test_rejects_when_rx_audio_path_is_legacy(self, client, create_user):
        create_user("owner1", role="owner")
        # rx_audio_config defaults to 'legacy' -- no save needed.
        resp = client.post("/internal/audio/ensure-capture",
                            json={"node": "628280"}, headers=_secret_headers())
        assert resp.status_code == 409
        assert "lowlatency" in resp.get_json()["error"]

    def test_rejects_invalid_node(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        resp = client.post("/internal/audio/ensure-capture",
                            json={"node": "not-a-node"}, headers=_secret_headers())
        assert resp.status_code == 400

    def test_lowlatency_mode_with_no_live_asterisk_fails_cleanly(self, client, create_user):
        # No live AMI in the test environment (HENWEN_SKIP_STARTUP=1) --
        # _find_node_channel() degrades to "channel not found" rather than
        # raising, so this should be a clean 500 with a readable message,
        # not an unhandled exception.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        resp = client.post("/internal/audio/ensure-capture",
                            json={"node": "628280"}, headers=_secret_headers())
        assert resp.status_code == 500
        assert "no active asterisk channel" in resp.get_json()["error"].lower()

    def test_success_response_includes_current_agc_setting(self, client, create_user, monkeypatch):
        # _ensure_capture_only() needs live Asterisk (_find_node_channel())
        # -- stubbed here specifically to reach the success response and
        # verify audio_ws_relay.py's own _ensure_capture() gets the AGC
        # flag it needs at the exact moment it's about to spawn an encoder.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "lowlatency", "agc_enabled": False})
        monkeypatch.setattr(app, "_ensure_capture_only", lambda node: None)
        resp = client.post("/internal/audio/ensure-capture",
                            json={"node": "628280"}, headers=_secret_headers())
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["agc_enabled"] is False

    def test_success_response_reflects_agc_enabled_true(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "lowlatency", "agc_enabled": True})
        monkeypatch.setattr(app, "_ensure_capture_only", lambda node: None)
        resp = client.post("/internal/audio/ensure-capture",
                            json={"node": "628280"}, headers=_secret_headers())
        assert resp.status_code == 200
        assert resp.get_json()["agc_enabled"] is True


class TestReleaseCapture:
    def test_release_of_unknown_node_is_a_no_op(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post("/internal/audio/release-capture",
                            json={"node": "628280"}, headers=_secret_headers())
        assert resp.status_code == 200

    def test_rejects_invalid_node(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post("/internal/audio/release-capture",
                            json={"node": "not-a-node"}, headers=_secret_headers())
        assert resp.status_code == 400


class TestCsrfExemption:
    """ensure-capture/release-capture are server-to-server calls from
    audio_ws_relay.py, authenticated by _check_internal_audio_request()'s
    loopback+secret check -- never by a browser session/CSRF token, so they
    must be @csrf.exempt. The rest of this file's tests can't catch a
    regression here: conftest.py sets WTF_CSRF_ENABLED=False for the whole
    suite (deliberately -- exercising CSRF plumbing isn't those tests'
    point), so a request with no token passes regardless of the exemption.
    This was a real bug caught only by an actual end-to-end browser run
    against a real Flask process with CSRF genuinely enabled: every
    ensure-capture/release-capture call 400'd until @csrf.exempt was added.
    These tests re-enable CSRF for just this one class to guard against it
    coming back."""

    @pytest.fixture(autouse=True)
    def _csrf_enabled(self):
        app.app.config['WTF_CSRF_ENABLED'] = True
        try:
            yield
        finally:
            app.app.config['WTF_CSRF_ENABLED'] = False

    def test_ensure_capture_exempt_from_csrf(self, client, create_user):
        create_user("owner1", role="owner")
        # No X-CSRFToken header at all -- exactly what audio_ws_relay.py's
        # urllib-based POST looks like. A non-exempt route would 400 here
        # before ever reaching _check_internal_audio_request(); getting the
        # real 409 (rx_audio_path is legacy) instead proves CSRF didn't
        # block it.
        resp = client.post("/internal/audio/ensure-capture",
                            json={"node": "628280"}, headers=_secret_headers())
        assert resp.status_code == 409

    def test_release_capture_exempt_from_csrf(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post("/internal/audio/release-capture",
                            json={"node": "628280"}, headers=_secret_headers())
        assert resp.status_code == 200

    def test_ordinary_post_route_still_requires_csrf(self, client, create_user):
        # Control: confirms this test class's CSRF-enabled fixture is
        # actually doing something, by checking a normal (non-exempt)
        # mutating route still rejects a request with no token.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        assert resp.status_code == 400


class TestAuthorizeWs:
    def test_requires_valid_internal_secret(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.get("/internal/audio/authorize-ws?node=628280")
        assert resp.status_code == 403

    def test_no_session_cookie_is_unauthenticated(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/internal/audio/authorize-ws?node=628280", headers=_secret_headers())
        assert resp.status_code == 401

    def test_valid_session_forwarded_is_authorized(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.get("/internal/audio/authorize-ws?node=628280", headers=_secret_headers())
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["username"] == "owner1"
        assert body["role"] == "owner"

    def test_any_logged_in_role_is_authorized_not_just_admin(self, client, create_user):
        # Mirrors api_audio_stream's own _USER_OR_ABOVE membership -- a
        # plain 'user'/kiosk account can Listen today, so it must be able
        # to authorize the low-latency path too.
        create_user("owner1", role="owner")
        create_user("bob", password="password12345", role="user")
        _login(client, "bob")
        resp = client.get("/internal/audio/authorize-ws?node=628280", headers=_secret_headers())
        assert resp.status_code == 200
        assert resp.get_json()["role"] == "user"
