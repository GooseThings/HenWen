"""Tests for the Meshtastic Discord relay: the payload-building helper (pure,
no network), the owner-gating on /api/meshtastic-discord-relay/config, and
the enqueue-on-decode hook in _meshtastic_on_message(). Mirrors
tests/test_discord_relay.py's pattern for the chat relay's own config route.
The actual webhook POST (_meshtastic_discord_relay_post) and worker thread
are not covered here, consistent with this project's documented testing
boundary -- no live-network background loops under pytest.
"""
import queue as _queue_mod

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import app
import meshtastic_mqtt
from meshtastic_proto import mesh_min_pb2, mqtt_min_pb2, portnums_pb2


def _build_envelope(psk_b64, portnum, payload_bytes, packet_id=123456, from_node=0xAABBCC11,
                     channel_id="Michigan"):
    """Hand-encrypt a synthetic packet the same way tests/test_meshtastic.py
    does, so _meshtastic_on_message() can be driven end-to-end without a
    live broker."""
    key = meshtastic_mqtt.derive_key(psk_b64)
    data = mesh_min_pb2.Data()
    data.portnum = portnum
    data.payload = payload_bytes
    plaintext = data.SerializeToString()

    nonce = meshtastic_mqtt._build_nonce(packet_id, from_node)
    enc = Cipher(algorithms.AES(key), modes.CTR(nonce)).encryptor()
    ciphertext = enc.update(plaintext) + enc.finalize()

    env = mqtt_min_pb2.ServiceEnvelope()
    setattr(env.packet, "id", packet_id)
    setattr(env.packet, "from", from_node)
    env.packet.encrypted = ciphertext
    env.channel_id = channel_id
    return env.SerializeToString()


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["idle_timeout"] = app.SESSION_IDLE_TIMEOUT
        sess["sid"]          = "test-sid-" + username


class TestMeshtasticDiscordRelayBuildPayload:
    def test_formats_display_name_and_text(self):
        payload = app._meshtastic_discord_relay_build_payload("N8G", "hello mesh")
        assert payload["content"] == "**N8G**: hello mesh"

    def test_falls_back_to_raw_node_id_when_no_name_known(self):
        payload = app._meshtastic_discord_relay_build_payload("!aabbcc11", "hello mesh")
        assert payload["content"] == "**!aabbcc11**: hello mesh"

    def test_always_suppresses_mentions(self):
        # This text comes straight from unauthenticated public mesh radios,
        # not logged-in HenWen users -- mention suppression is the only
        # thing standing between a stray "@everyone" on the mesh and
        # pinging the whole Discord server.
        payload = app._meshtastic_discord_relay_build_payload("N8G", "@everyone hi @here <@123>")
        assert payload["allowed_mentions"] == {"parse": []}
        assert "@everyone" in payload["content"]


class TestMeshtasticDiscordRelayConfigRoute:
    def test_get_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        assert client.get("/api/meshtastic-discord-relay/config").status_code == 401

    def test_get_rejects_admin(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.get("/api/meshtastic-discord-relay/config").status_code == 403

    def test_get_rejects_plain_user(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        assert client.get("/api/meshtastic-discord-relay/config").status_code == 403

    def test_get_allows_owner_and_returns_defaults(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/meshtastic-discord-relay/config").get_json()
        assert body["enabled"] == 0
        assert body["webhook_url"] == ""

    def test_save_rejects_non_owner(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.put("/api/meshtastic-discord-relay/config",
                           json={"enabled": True, "webhook_url": "https://discord.com/api/webhooks/1/abc"})
        assert resp.status_code == 403

    def test_owner_can_save_and_it_persists(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/meshtastic-discord-relay/config",
                           json={"enabled": True, "webhook_url": "https://discord.com/api/webhooks/1/abc"})
        assert resp.status_code == 200
        cfg = app._get_meshtastic_discord_relay_config()
        assert cfg["enabled"] == 1
        assert cfg["webhook_url"] == "https://discord.com/api/webhooks/1/abc"

    def test_save_rejects_enabled_without_webhook_url(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/meshtastic-discord-relay/config", json={"enabled": True, "webhook_url": ""})
        assert resp.status_code == 400

    def test_save_rejects_non_discord_webhook_url(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/meshtastic-discord-relay/config",
                           json={"enabled": True, "webhook_url": "https://evil.example/hook"})
        assert resp.status_code == 400

    def test_save_allows_disabling_with_blank_url(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/meshtastic-discord-relay/config", json={"enabled": False, "webhook_url": ""})
        assert resp.status_code == 200

    def test_config_independent_of_chat_discord_relay(self, client, create_user):
        # The whole point of a separate table: saving one relay's webhook
        # must never touch the other's.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/discord-relay/config",
                   json={"enabled": True, "webhook_url": "https://discord.com/api/webhooks/1/chat"})
        client.put("/api/meshtastic-discord-relay/config",
                   json={"enabled": True, "webhook_url": "https://discord.com/api/webhooks/2/mesh"})
        assert app._get_discord_relay_config()["webhook_url"] == "https://discord.com/api/webhooks/1/chat"
        assert app._get_meshtastic_discord_relay_config()["webhook_url"] == "https://discord.com/api/webhooks/2/mesh"


class TestMeshtasticOnMessageEnqueuesDiscordRelay:
    @pytest.fixture(autouse=True)
    def _clear_state(self, fresh_db):
        app._meshtastic_cache.clear()
        app._meshtastic_names_cache.clear()
        while not app._meshtastic_discord_relay_queue.empty():
            app._meshtastic_discord_relay_queue.get_nowait()
        yield
        app._meshtastic_cache.clear()
        app._meshtastic_names_cache.clear()
        while not app._meshtastic_discord_relay_queue.empty():
            app._meshtastic_discord_relay_queue.get_nowait()

    def test_decoded_text_message_is_enqueued_with_raw_node_id_when_name_unknown(self, fresh_db):
        class _FakeMsg:
            payload = _build_envelope("AQ==", portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello mesh",
                                       from_node=0xAABBCC11)

        app._meshtastic_on_message("AQ==", _FakeMsg())

        display_name, text = app._meshtastic_discord_relay_queue.get_nowait()
        assert display_name == "!aabbcc11"
        assert text == "hello mesh"

    def test_decoded_text_message_uses_known_friendly_name(self, fresh_db):
        app._meshtastic_names_cache["!aabbcc11"] = {
            "short_name": "N8G", "long_name": "N8GMZ Repeater", "ts": 111.0
        }

        class _FakeMsg:
            payload = _build_envelope("AQ==", portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello mesh",
                                       from_node=0xAABBCC11)

        app._meshtastic_on_message("AQ==", _FakeMsg())

        display_name, text = app._meshtastic_discord_relay_queue.get_nowait()
        assert display_name == "N8G"
        assert text == "hello mesh"

    def test_nodeinfo_message_is_not_enqueued(self, fresh_db):
        NODEINFO = portnums_pb2.PortNum.Value("NODEINFO_APP")

        def _build_user_payload(short_name="", long_name=""):
            user = mesh_min_pb2.User()
            user.short_name = short_name
            user.long_name = long_name
            return user.SerializeToString()

        class _FakeMsg:
            payload = _build_envelope("AQ==", NODEINFO,
                                       _build_user_payload(short_name="N8G", long_name="N8GMZ Repeater"),
                                       from_node=0xAABBCC11)

        app._meshtastic_on_message("AQ==", _FakeMsg())

        assert app._meshtastic_discord_relay_queue.empty()

    def test_full_queue_drops_message_instead_of_raising(self, fresh_db, monkeypatch):
        monkeypatch.setattr(app, "_meshtastic_discord_relay_queue", _queue_mod.Queue(maxsize=1))
        app._meshtastic_discord_relay_queue.put_nowait(("someone", "already queued"))

        class _FakeMsg:
            payload = _build_envelope("AQ==", portnums_pb2.PortNum.TEXT_MESSAGE_APP, b"hello mesh",
                                       from_node=0xAABBCC11)

        # Must not raise even though the queue is already full.
        app._meshtastic_on_message("AQ==", _FakeMsg())

        assert app._meshtastic_discord_relay_queue.qsize() == 1
        assert app._meshtastic_discord_relay_queue.get_nowait() == ("someone", "already queued")
