"""Tests for the Alerts feature's provider fan-out: ntfy / Pushover / Discord /
Kiosk Chat are independent enable toggles (not a single-select `provider`), so
more than one can be configured at once and a failure in one provider must
not block the others. The actual outbound HTTP calls are monkeypatched -- no
live network is touched, consistent with this project's testing boundary.
"""
import app


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["idle_timeout"] = app.SESSION_IDLE_TIMEOUT
        sess["sid"]          = "test-sid-" + username


class TestAlertsConfigRoute:
    def test_get_defaults_have_no_provider_enabled(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/alerts/config").get_json()
        assert body["ntfy_enabled"] == 0
        assert body["pushover_enabled"] == 0
        assert body["discord_enabled"] == 0
        assert body["chat_enabled"] == 0

    def test_save_can_enable_multiple_providers_at_once(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/alerts/config", json={
            "enabled": True,
            "ntfy_enabled": True, "pushover_enabled": True, "discord_enabled": True,
            "chat_enabled": True,
            "ntfy_topic": "my-topic",
            "pushover_token": "tok", "pushover_user": "usr",
        })
        assert resp.status_code == 200
        cfg = app._get_alert_config()
        assert cfg["ntfy_enabled"] == 1
        assert cfg["pushover_enabled"] == 1
        assert cfg["discord_enabled"] == 1
        assert cfg["chat_enabled"] == 1

    def test_save_can_enable_just_discord(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/alerts/config", json={
            "enabled": True, "discord_enabled": True,
        })
        assert resp.status_code == 200
        cfg = app._get_alert_config()
        assert cfg["ntfy_enabled"] == 0
        assert cfg["pushover_enabled"] == 0
        assert cfg["discord_enabled"] == 1


class TestSendAlertFanOut:
    def test_disabled_master_switch_sends_nothing(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        calls = []
        monkeypatch.setattr(app.urlreq, "urlopen", lambda *a, **k: calls.append(1))
        app._send_alert("Title", "Message")
        assert calls == []

    def test_fans_out_to_every_enabled_provider(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute(
            """INSERT OR REPLACE INTO alert_config
               (id, enabled, ntfy_enabled, pushover_enabled, discord_enabled, chat_enabled,
                ntfy_topic, pushover_token, pushover_user)
               VALUES (1, 1, 1, 1, 1, 1, 'topic', 'tok', 'usr')"""
        )
        db.execute(
            "INSERT OR REPLACE INTO discord_relay_config (id, enabled, webhook_url) "
            "VALUES (1, 1, 'https://discord.com/api/webhooks/1/abc')"
        )
        db.commit()

        calls = []

        class _FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""

        def _fake_urlopen(req, timeout=None):
            calls.append(req.full_url)
            return _FakeResp()

        monkeypatch.setattr(app.urlreq, "urlopen", _fake_urlopen)
        app._send_alert("Title", "Message")

        assert any("ntfy.sh" in u for u in calls)
        assert any("pushover.net" in u for u in calls)
        # Exactly one Discord POST -- the chat branch inserts straight into
        # chat_messages rather than going through the normal chat-post route,
        # specifically so it can't also enqueue a second Discord delivery via
        # the chat relay.
        assert len([u for u in calls if "discord.com" in u]) == 1

        row = db.execute("SELECT username, role, message FROM chat_messages").fetchone()
        assert row["username"] == app.ALERT_CHAT_USERNAME
        assert row["role"] == "system"
        assert row["message"] == "Title: Message"

    def test_one_provider_failing_does_not_block_the_others(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute(
            """INSERT OR REPLACE INTO alert_config
               (id, enabled, ntfy_enabled, pushover_enabled, discord_enabled,
                ntfy_topic, pushover_token, pushover_user)
               VALUES (1, 1, 1, 1, 1, 'topic', 'tok', 'usr')"""
        )
        db.execute(
            "INSERT OR REPLACE INTO discord_relay_config (id, enabled, webhook_url) "
            "VALUES (1, 1, 'https://discord.com/api/webhooks/1/abc')"
        )
        db.commit()

        calls = []

        class _FakeResp:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *a): return False
            def read(self): return b""

        def _fake_urlopen(req, timeout=None):
            if "ntfy.sh" in req.full_url:
                raise OSError("simulated ntfy failure")
            calls.append(req.full_url)
            return _FakeResp()

        monkeypatch.setattr(app.urlreq, "urlopen", _fake_urlopen)
        app._send_alert("Title", "Message")  # must not raise

        assert any("pushover.net" in u for u in calls)
        assert any("discord.com" in u for u in calls)

    def test_discord_enabled_without_saved_webhook_logs_warning_not_exception(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute(
            """INSERT OR REPLACE INTO alert_config (id, enabled, discord_enabled)
               VALUES (1, 1, 1)"""
        )
        db.commit()
        calls = []
        monkeypatch.setattr(app.urlreq, "urlopen", lambda *a, **k: calls.append(1))
        app._send_alert("Title", "Message")  # must not raise
        assert calls == []


class TestSendAlertChatDestination:
    def test_chat_only_posts_to_chat_and_not_discord_queue(self, client, create_user):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute(
            "INSERT OR REPLACE INTO alert_config (id, enabled, chat_enabled) VALUES (1, 1, 1)"
        )
        db.commit()
        app._send_alert("AMI Offline", "Connection to Asterisk lost")
        row = db.execute("SELECT user_id, username, role, message FROM chat_messages").fetchone()
        assert row["user_id"] == 0
        assert row["username"] == "HenWen Alert"
        assert row["role"] == "system"
        assert row["message"] == "AMI Offline: Connection to Asterisk lost"
        # Straight DB insert, not the enqueueing api_chat_messages_post() path.
        assert app._discord_relay_queue.empty()
