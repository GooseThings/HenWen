"""Route-level tests for Phase 1 of the audio recording feature: the
recording_config / stream_relay_config settings routes (both owner-only),
the can_record per-user flag, and /api/recording/permission. See
CLAUDE.md and the plan for why recording.py and stream_relay.py are
deliberately independent modules/config tables -- these tests exercise
only the schema/config/permission surface, not the (not yet built)
audio pipelines themselves.
"""
import app


def _login(client, username, password=None):
    """Stamp the session directly the way check_auth() expects, instead of
    POSTing to /login. The real login route carries an explicit
    @limiter.limit("10 per minute") that RATELIMIT_ENABLED=False (set in
    conftest.py) doesn't suppress -- Limiter reads that flag once, at
    construction time, which happens at `import app` in conftest.py
    *before* conftest gets a chance to set RATELIMIT_ENABLED=False on the
    already-built extension. This file logs in far more than 10 times, all
    from the test client's shared "127.0.0.1" key, and would flake with 429s
    if it went through the real route. `password` is accepted for call-site
    readability but unused now that this never re-derives a password hash."""
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["idle_timeout"] = (row["session_idle_timeout"] if row["session_idle_timeout"] is not None
                                 else app.SESSION_IDLE_TIMEOUT)
        sess["sid"]          = "test-sid-" + username


class TestRecordingConfigGating:
    def test_get_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/api/recording/config")
        assert resp.status_code == 401

    def test_admin_cannot_view_recording_config(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.get("/api/recording/config")
        assert resp.status_code == 403

    def test_superuser_cannot_view_recording_config(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("su1", password="password12345", role="superuser")
        _login(client, "su1")
        resp = client.get("/api/recording/config")
        assert resp.status_code == 403

    def test_owner_can_view_and_gets_defaults_before_any_save(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.get("/api/recording/config")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["retention_days"] == 30
        assert body["tts_voice"] == app.DEFAULT_TTS_VOICE

    def test_admin_cannot_save_recording_config(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.put("/api/recording/config", json={"retention_days": 5})
        assert resp.status_code == 403


class TestRecordingConfigValidation:
    def test_owner_save_and_reload_round_trips(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/recording/config", json={
            "retention_days": 45, "max_recording_min": 90,
            "global_cap_gb": 10.5, "per_user_cap_gb": 2.5,
            "silence_rms_thresh": 250, "silence_min_gap_ms": 1500,
            "tts_interval_min": 5, "tts_voice": app.DEFAULT_TTS_VOICE,
            "tts_enabled": False, "output_format": "mp3",
        })
        assert resp.status_code == 200
        body = client.get("/api/recording/config").get_json()
        assert body["retention_days"] == 45
        assert body["output_format"] == "mp3"
        assert body["tts_enabled"] == 0

    def test_rejects_unknown_voice(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/recording/config", json={"tts_voice": "not-a-real-voice"})
        assert resp.status_code == 400

    def test_rejects_bad_output_format(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/recording/config", json={"output_format": "wav"})
        assert resp.status_code == 400

    def test_rejects_non_positive_caps(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/recording/config", json={"global_cap_gb": -1})
        assert resp.status_code == 400

    def test_rejects_non_numeric_input(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/recording/config", json={"retention_days": "not-a-number"})
        assert resp.status_code == 400


class TestStreamRelayConfigGating:
    def test_admin_cannot_view_or_save(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.get("/api/stream-relay/config").status_code == 403
        assert client.put("/api/stream-relay/config", json={}).status_code == 403

    def test_owner_gets_defaults_before_any_save(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/stream-relay/config").get_json()
        assert body["broadcastify_enabled"] is False
        assert body["target_node"] == ""


class TestStreamRelayConfigValidation:
    def test_enabling_broadcastify_requires_host_port_mount(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/stream-relay/config", json={"broadcastify_enabled": True})
        assert resp.status_code == 400

    def test_enabling_youtube_requires_url_and_key(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/stream-relay/config", json={"youtube_enabled": True})
        assert resp.status_code == 400

    def test_enabling_relay_requires_a_valid_target_node(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/stream-relay/config", json={
            "broadcastify_enabled": True, "broadcastify_host": "audio.broadcastify.com",
            "broadcastify_port": 8000, "broadcastify_mount": "/mymount",
            "target_node": "not-a-node",
        })
        assert resp.status_code == 400

    def test_full_valid_broadcastify_and_youtube_save_round_trips(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/stream-relay/config", json={
            "target_node": "546054",
            "broadcastify_enabled": True, "broadcastify_host": "audio.broadcastify.com",
            "broadcastify_port": 8000, "broadcastify_mount": "/mymount",
            "broadcastify_user": "src", "broadcastify_pass": "secret",
            "youtube_enabled": True, "youtube_rtmp_url": "rtmp://a.rtmp.youtube.com/live2",
            "youtube_stream_key": "abcd-1234",
        })
        assert resp.status_code == 200
        body = client.get("/api/stream-relay/config").get_json()
        assert body["target_node"] == "546054"
        assert body["broadcastify_port"] == 8000
        assert body["youtube_stream_key"] == "abcd-1234"

    def test_disabled_targets_do_not_require_credentials(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/stream-relay/config", json={"target_node": ""})
        assert resp.status_code == 200


class TestCanRecordFlag:
    def test_default_is_false_for_new_user(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="password12345", role="user")
        _login(client, "bob")
        resp = client.get("/api/recording/permission")
        assert resp.status_code == 200
        assert resp.get_json() == {"can_record": False}

    def test_owner_can_grant_can_record_to_any_role(self, client, create_user):
        owner = create_user("owner1", role="owner")
        bob = create_user("bob", password="password12345", role="user")
        _login(client, "owner1")
        resp = client.put(f"/api/users/{bob['id']}", json={"can_record": True})
        assert resp.status_code == 200
        # Owner can also grant it to their own account.
        resp = client.put(f"/api/users/{owner['id']}", json={"can_record": True})
        assert resp.status_code == 200

        bob_client_perm = app.get_db().execute(
            "SELECT can_record FROM users WHERE username='bob'"
        ).fetchone()
        assert bob_client_perm["can_record"] == 1

    def test_admin_cannot_grant_can_record_even_on_a_user_role_account(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        bob = create_user("bob", password="password12345", role="user")
        _login(client, "admin1")
        resp = client.put(f"/api/users/{bob['id']}", json={"can_record": True})
        assert resp.status_code == 403
        assert "owner" in resp.get_json()["error"].lower()

    def test_owner_can_create_user_with_can_record_set(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/users", json={
            "username": "carol", "password": "password12345",
            "role": "user", "can_record": True,
        })
        assert resp.status_code == 200
        row = app.get_db().execute("SELECT can_record FROM users WHERE username='carol'").fetchone()
        assert row["can_record"] == 1

    def test_admin_creating_user_cannot_set_can_record(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.post("/api/users", json={
            "username": "dave", "password": "password12345",
            "role": "user", "can_record": True,
        })
        assert resp.status_code == 403

    def test_users_list_includes_can_record(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/users").get_json()
        assert "can_record" in body["users"][0]

    def test_permission_endpoint_reflects_live_db_not_stale_session(self, client, create_user):
        """The frontend must never trust a cached can_record from login -- an
        owner revoking access mid-session should take effect on the very next
        permission check, since check_auth() never re-runs role-specific
        capability flags like this one."""
        create_user("owner1", role="owner")
        bob = create_user("bob", password="password12345", role="user")
        owner_client = app.app.test_client()
        _login(owner_client, "owner1")
        owner_client.put(f"/api/users/{bob['id']}", json={"can_record": True})

        bob_client = app.app.test_client()
        _login(bob_client, "bob")
        assert bob_client.get("/api/recording/permission").get_json()["can_record"] is True

        owner_client.put(f"/api/users/{bob['id']}", json={"can_record": False})
        assert bob_client.get("/api/recording/permission").get_json()["can_record"] is False
