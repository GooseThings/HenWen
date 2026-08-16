"""Tests for the Meshtastic MQTT panel: the decode module (meshtastic_mqtt.py,
pure/no DB/Flask dependency, per its own docstring) and the owner-gating on
/api/meshtastic/config, mirroring tests/test_recording_routes.py's pattern
for an owner-only config route. No live broker/network is touched here --
meshtastic_mqtt.py's crypto/protobuf pipeline was separately verified
against real mqtt.meshtastic.org traffic during development (see the
Meshtastic MQTT panel plan); these tests use synthetic packets built the
same way, so they stay deterministic and offline.
"""
import base64

import pytest
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

import app
import meshtastic_mqtt
from meshtastic_proto import mesh_min_pb2, mqtt_min_pb2, portnums_pb2


def _build_envelope(psk_b64, portnum, payload_bytes, packet_id=123456, from_node=0xAABBCC11,
                     channel_id="Michigan"):
    """Hand-encrypt a synthetic packet the same way a real Meshtastic node
    would, so decode_service_envelope() can be tested end-to-end without a
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


class TestDeriveKey:
    def test_preset_index_1_reproduces_default_key_unmodified(self):
        assert meshtastic_mqtt.derive_key("AQ==") == meshtastic_mqtt.DEFAULT_PSK

    def test_preset_index_48_matches_known_value(self):
        # Verified by hand against meshtastic/firmware's Channels::getKey():
        # last byte = defaultpsk[-1] (0x01) + (48 - 1) = 0x30, no wraparound.
        key = meshtastic_mqtt.derive_key("MA==")
        assert key.hex() == "d4f1bb3a20290759f0bcffabcf4e6930"

    def test_preset_derivation_wraps_mod_256(self):
        # index 255 -> last byte = 0x01 + 254 = 255 = 0xff, still no wrap;
        # push it past 256 to actually exercise the wraparound.
        idx = 200
        b64 = base64.b64encode(bytes([idx])).decode()
        key = meshtastic_mqtt.derive_key(b64)
        expected_last = (meshtastic_mqtt.DEFAULT_PSK[-1] + idx - 1) & 0xFF
        assert key[-1] == expected_last
        assert key[:-1] == meshtastic_mqtt.DEFAULT_PSK[:-1]

    def test_16_byte_psk_used_directly(self):
        raw = bytes(range(16))
        assert meshtastic_mqtt.derive_key(base64.b64encode(raw).decode()) == raw

    def test_32_byte_psk_used_directly(self):
        raw = bytes(range(32))
        assert meshtastic_mqtt.derive_key(base64.b64encode(raw).decode()) == raw

    def test_preset_index_0_means_encryption_off_raises(self):
        with pytest.raises(meshtastic_mqtt.InvalidPSK):
            meshtastic_mqtt.derive_key(base64.b64encode(bytes([0])).decode())

    def test_wrong_length_psk_raises(self):
        with pytest.raises(meshtastic_mqtt.InvalidPSK):
            meshtastic_mqtt.derive_key(base64.b64encode(bytes(5)).decode())

    def test_invalid_base64_raises(self):
        with pytest.raises(meshtastic_mqtt.InvalidPSK):
            meshtastic_mqtt.derive_key("not valid base64!!!")


class TestDecodeServiceEnvelope:
    TEXT = portnums_pb2.PortNum.Value("TEXT_MESSAGE_APP")
    POSITION = portnums_pb2.PortNum.Value("POSITION_APP")

    def test_decodes_real_text_message_round_trip(self):
        raw = _build_envelope("MA==", self.TEXT, "Hello Michigan mesh!".encode("utf-8"),
                               packet_id=42, from_node=0xAABBCC11)
        result = meshtastic_mqtt.decode_service_envelope(raw, "MA==")
        assert result == {"from_node": "!aabbcc11", "text": "Hello Michigan mesh!", "ts": None}

    def test_wrong_psk_does_not_return_a_fake_message(self):
        raw = _build_envelope("MA==", self.TEXT, b"Hello Michigan mesh!")
        # A different preset key -- garbage plaintext should fail the
        # portnum/UTF-8 sanity gate (or the protobuf parse itself) rather
        # than silently surfacing wrong content as if it decrypted fine.
        assert meshtastic_mqtt.decode_service_envelope(raw, "AQ==") is None

    def test_non_text_portnum_is_discarded(self):
        raw = _build_envelope("MA==", self.POSITION, b"\x0d\x00\x00\x00\x00")
        assert meshtastic_mqtt.decode_service_envelope(raw, "MA==") is None

    def test_invalid_utf8_payload_is_discarded(self):
        raw = _build_envelope("MA==", self.TEXT, b"\xff\xfe\x00\x01")
        assert meshtastic_mqtt.decode_service_envelope(raw, "MA==") is None

    def test_empty_text_after_strip_is_discarded(self):
        raw = _build_envelope("MA==", self.TEXT, b"   ")
        assert meshtastic_mqtt.decode_service_envelope(raw, "MA==") is None

    def test_garbage_bytes_return_none_not_an_exception(self):
        assert meshtastic_mqtt.decode_service_envelope(b"\x00\x01\x02not-a-protobuf", "MA==") is None

    def test_packet_with_no_encrypted_field_returns_none(self):
        env = mqtt_min_pb2.ServiceEnvelope()
        env.channel_id = "Michigan"
        setattr(env.packet, "id", 1)
        setattr(env.packet, "from", 2)
        assert meshtastic_mqtt.decode_service_envelope(env.SerializeToString(), "MA==") is None

    def test_invalid_psk_in_config_returns_none_not_an_exception(self):
        raw = _build_envelope("MA==", self.TEXT, b"hello")
        assert meshtastic_mqtt.decode_service_envelope(raw, "not-base64!!!") is None


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["idle_timeout"] = app.SESSION_IDLE_TIMEOUT
        sess["sid"]          = "test-sid-" + username


class TestMeshtasticConfigRoute:
    def test_get_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        assert client.get("/api/meshtastic/config").status_code == 401

    def test_get_rejects_admin(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.get("/api/meshtastic/config").status_code == 403

    def test_get_rejects_plain_user(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        assert client.get("/api/meshtastic/config").status_code == 403

    def test_get_allows_owner_and_returns_defaults(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/meshtastic/config").get_json()
        assert body["root_topic"] == "/msh/US/MI"
        assert body["channel_name"] == "Michigan"
        assert body["psk"] == "MA=="

    def test_save_rejects_non_owner(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.put("/api/meshtastic/config",
                           json={"root_topic": "msh/US/OH", "channel_name": "Ohio", "psk": "AQ=="})
        assert resp.status_code == 403

    def test_owner_can_save_and_it_persists(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/meshtastic/config",
                           json={"root_topic": "/msh/US/OH", "channel_name": "Ohio", "psk": "AQ=="})
        assert resp.status_code == 200
        cfg = app._get_meshtastic_config()
        # Leading "/" stripped, matching how the MQTT poller builds its
        # subscribe topic.
        assert cfg["root_topic"] == "msh/US/OH"
        assert cfg["channel_name"] == "Ohio"
        assert cfg["psk"] == "AQ=="

    def test_save_rejects_invalid_psk(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/meshtastic/config",
                           json={"root_topic": "/msh/US/MI", "channel_name": "Michigan", "psk": "not-base64!!!"})
        assert resp.status_code == 400

    def test_save_rejects_empty_channel_name(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/meshtastic/config",
                           json={"root_topic": "/msh/US/MI", "channel_name": "", "psk": "AQ=="})
        assert resp.status_code == 400


class TestMeshtasticMessagesRoute:
    def test_public_no_login_required(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/api/meshtastic/messages")
        assert resp.status_code == 200
        assert resp.get_json() == {"messages": [], "channel_name": "Michigan", "root_topic": "/msh/US/MI"}
