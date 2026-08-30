"""Tests for the Discord chat relay: the payload-building helper (pure, no
network) and the owner-gating on /api/discord-relay/config, mirroring
tests/test_meshtastic.py's pattern for an owner-only config route. The actual
webhook POST (_discord_relay_post) and worker thread are not covered here,
consistent with this project's documented testing boundary -- no live-network
background loops under pytest.
"""
import app


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"] = app.SESSION_IDLE_TIMEOUT
        sess["sid"]          = "test-sid-" + username


class TestDiscordRelayBuildPayload:
    def test_formats_username_and_message(self):
        payload = app._discord_relay_build_payload("alice", "hello world")
        assert payload["content"] == "**alice**: hello world"

    def test_always_suppresses_mentions(self):
        # The one real security property of this feature: a kiosk user
        # typing "@everyone" must never be able to ping the linked Discord
        # server through the webhook.
        payload = app._discord_relay_build_payload("alice", "@everyone hi @here <@123>")
        assert payload["allowed_mentions"] == {"parse": []}
        assert "@everyone" in payload["content"]  # present in content, just not parsed as a mention


class TestDiscordRelayConfigRoute:
    def test_get_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        assert client.get("/api/discord-relay/config").status_code == 401

    def test_get_rejects_admin(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.get("/api/discord-relay/config").status_code == 403

    def test_get_rejects_plain_user(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        assert client.get("/api/discord-relay/config").status_code == 403

    def test_get_allows_owner_and_returns_defaults(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/discord-relay/config").get_json()
        assert body["enabled"] == 0
        assert body["webhook_url"] == ""

    def test_save_rejects_non_owner(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.put("/api/discord-relay/config",
                           json={"enabled": True, "webhook_url": "https://discord.com/api/webhooks/1/abc"})
        assert resp.status_code == 403

    def test_owner_can_save_and_it_persists(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/discord-relay/config",
                           json={"enabled": True, "webhook_url": "https://discord.com/api/webhooks/1/abc"})
        assert resp.status_code == 200
        cfg = app._get_discord_relay_config()
        assert cfg["enabled"] == 1
        assert cfg["webhook_url"] == "https://discord.com/api/webhooks/1/abc"

    def test_save_rejects_enabled_without_webhook_url(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/discord-relay/config", json={"enabled": True, "webhook_url": ""})
        assert resp.status_code == 400

    def test_save_rejects_non_discord_webhook_url(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/discord-relay/config",
                           json={"enabled": True, "webhook_url": "https://evil.example/hook"})
        assert resp.status_code == 400

    def test_save_allows_disabling_with_blank_url(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/discord-relay/config", json={"enabled": False, "webhook_url": ""})
        assert resp.status_code == 200
