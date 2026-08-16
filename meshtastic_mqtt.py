"""Decode Meshtastic public-channel MQTT traffic.

Standalone like recording.py/stream_relay.py — no Flask/DB knowledge, just
bytes in, a dict (or None) out, so it's testable without the app running.

Background: a Meshtastic node with MQTT uplink enabled publishes an
AES-encrypted `ServiceEnvelope` protobuf to
`<root_topic>/2/e/<channel_name>/<node_id>` on the shared broker. The
encryption key comes from the channel's PSK, and public channels almost
always use one of the "preset" single-byte PSK shorthands rather than a
full 16/32-byte key. Every constant and byte layout below was verified
against meshtastic/firmware source (not implemented from memory — see
CryptoEngine.cpp's initNonce()/encryptAESCtr() and Channels.cpp's
getKey()), since a wrong nonce or key byte silently produces garbage
plaintext instead of a decode error.
"""

import base64
import struct

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from google.protobuf.message import DecodeError

from meshtastic_proto import mesh_min_pb2, mqtt_min_pb2, portnums_pb2

# The fixed 16-byte AES-128 key all "preset" single-byte PSKs derive from.
# Verified against meshtastic/firmware's src/mesh/Channels.h.
DEFAULT_PSK = bytes.fromhex("d4f1bb3a20290759f0bcffabcf4e6901")

_TEXT_MESSAGE_APP = portnums_pb2.PortNum.Value("TEXT_MESSAGE_APP")


class InvalidPSK(ValueError):
    """Raised by derive_key() when the configured PSK can't be used as-is."""


def derive_key(psk_b64: str) -> bytes:
    """Turn a base64-encoded PSK (as stored in meshtastic_config) into the
    raw AES key bytes used to decrypt channel traffic.

    Mirrors Channels::getKey() in meshtastic/firmware's Channels.cpp:
    16/32-byte PSKs are used directly (AES-128/256); a single-byte PSK is a
    "preset" index that expands to DEFAULT_PSK with `index - 1` added
    (unsigned 8-bit, wrapping) onto its last byte — NOT XORed, despite that
    being the more commonly repeated (wrong) description of this scheme.
    """
    try:
        raw = base64.b64decode(psk_b64, validate=True)
    except Exception as e:
        raise InvalidPSK(f"PSK is not valid base64: {e}") from e

    if len(raw) in (16, 32):
        return raw

    if len(raw) == 1:
        index = raw[0]
        if index == 0:
            raise InvalidPSK("PSK byte 0 means encryption is disabled on this channel")
        key = bytearray(DEFAULT_PSK)
        key[-1] = (key[-1] + index - 1) & 0xFF
        return bytes(key)

    raise InvalidPSK(f"PSK must decode to 1, 16, or 32 bytes, got {len(raw)}")


def _build_nonce(packet_id: int, from_node: int) -> bytes:
    """16-byte AES-CTR initial counter block. Verified against
    CryptoEngine::initNonce() in meshtastic/firmware: 8 bytes of packet id
    (a uint32 field widened to uint64, so really 4 id bytes + 4 zero bytes),
    then 4 bytes of the sender's node number, then 4 zero bytes (the
    trailing `extraNonce` word is only used by the separate PKI direct-
    message path, never by standard channel encryption). All little-endian.
    """
    return (
        struct.pack("<I", packet_id & 0xFFFFFFFF)
        + b"\x00\x00\x00\x00"
        + struct.pack("<I", from_node & 0xFFFFFFFF)
        + b"\x00\x00\x00\x00"
    )


def decode_service_envelope(raw_bytes: bytes, psk_b64: str) -> dict | None:
    """Decode one MQTT message payload into
    `{"from_node": "!xxxxxxxx", "text": str, "ts": None}`, or None if it
    isn't a decodable public-channel text message (wrong/malformed PSK,
    not a text message, not actually encrypted, or corrupt data).

    `ts` is left for the caller to fill in (wall-clock receipt time isn't
    this function's concern — it's a pure decode step).

    Channel-PSK encryption here is unauthenticated (no MAC/auth tag,
    unlike the PKI direct-message path) — a wrong key doesn't reliably
    raise an error, it can silently "succeed" into garbage. The portnum +
    strict-UTF-8 checks below are the defense against surfacing that
    garbage as a fake chat message, not just a style preference.
    """
    try:
        key = derive_key(psk_b64)
    except InvalidPSK:
        return None

    envelope = mqtt_min_pb2.ServiceEnvelope()
    try:
        envelope.ParseFromString(raw_bytes)
    except DecodeError:
        return None

    packet = envelope.packet
    encrypted = getattr(packet, "encrypted", b"")
    if not encrypted:
        return None  # no ciphertext (e.g. an already-plaintext admin message) — not our concern

    nonce = _build_nonce(getattr(packet, "id"), getattr(packet, "from"))
    decryptor = Cipher(algorithms.AES(key), modes.CTR(nonce)).decryptor()
    try:
        plaintext = decryptor.update(encrypted) + decryptor.finalize()
    except Exception:
        return None

    data = mesh_min_pb2.Data()
    try:
        data.ParseFromString(plaintext)
    except DecodeError:
        return None

    if data.portnum != _TEXT_MESSAGE_APP:
        return None

    try:
        text = data.payload.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    if not text.strip():
        return None

    from_node = getattr(packet, "from")
    return {
        "from_node": f"!{from_node:08x}",
        "text": text,
        "ts": None,
    }
