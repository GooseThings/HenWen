"""Tests for audio_ws_relay.py's WebSocket protocol primitives (handshake +
RFC 6455 frame parsing/building) -- the highest-risk new code in the
"Low-Latency Listen Path" feature per its planning notes, since a WebSocket
framing bug is subtle and this module is hand-rolled rather than backed by a
pip library. Every function under test is pure/sans-I/O (see the module
docstring), so these tests never open a real socket.
"""
import struct

import pytest

import app
import audio_ws_relay as wsrelay


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------

class TestComputeAcceptKey:
    def test_rfc6455_worked_example(self):
        # RFC 6455 §1.3's own example -- the canonical cross-implementation
        # test vector for a WebSocket handshake.
        key = "dGhlIHNhbXBsZSBub25jZQ=="
        assert wsrelay.compute_accept_key(key) == "s3pPLMBiTxaQ9kYGzzhZRbK+xOo="

    def test_strips_surrounding_whitespace(self):
        assert (wsrelay.compute_accept_key("  dGhlIHNhbXBsZSBub25jZQ==  ")
                == wsrelay.compute_accept_key("dGhlIHNhbXBsZSBub25jZQ=="))


class TestParseHandshakeRequest:
    VALID = (
        b"GET /ws-audio/628280 HTTP/1.1\r\n"
        b"Host: 127.0.0.1:8098\r\n"
        b"Upgrade: websocket\r\n"
        b"Connection: Upgrade\r\n"
        b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        b"Sec-WebSocket-Version: 13\r\n"
        b"Cookie: henwen_session=abc123\r\n"
        b"\r\n"
    )

    def test_parses_valid_request(self):
        result = wsrelay.parse_handshake_request(self.VALID)
        assert result["method"] == "GET"
        assert result["path"] == "/ws-audio/628280"
        assert result["headers"]["sec-websocket-key"] == "dGhlIHNhbXBsZSBub25jZQ=="
        assert result["headers"]["cookie"] == "henwen_session=abc123"

    def test_header_names_are_lowercased(self):
        result = wsrelay.parse_handshake_request(self.VALID)
        assert "upgrade" in result["headers"]
        assert result["headers"]["upgrade"] == "websocket"

    def test_connection_header_with_multiple_tokens(self):
        raw = self.VALID.replace(b"Connection: Upgrade", b"Connection: keep-alive, Upgrade")
        result = wsrelay.parse_handshake_request(raw)
        assert result["headers"]["connection"] == "keep-alive, Upgrade"

    def test_incomplete_request_raises(self):
        with pytest.raises(ValueError):
            wsrelay.parse_handshake_request(b"GET /ws-audio/628280 HTTP/1.1\r\nHost: x")

    def test_non_get_method_rejected(self):
        raw = self.VALID.replace(b"GET ", b"POST ")
        with pytest.raises(ValueError):
            wsrelay.parse_handshake_request(raw)

    def test_missing_upgrade_header_rejected(self):
        raw = self.VALID.replace(b"Upgrade: websocket\r\n", b"")
        with pytest.raises(ValueError):
            wsrelay.parse_handshake_request(raw)

    def test_wrong_upgrade_value_rejected(self):
        raw = self.VALID.replace(b"Upgrade: websocket", b"Upgrade: h2c")
        with pytest.raises(ValueError):
            wsrelay.parse_handshake_request(raw)

    def test_missing_connection_header_rejected(self):
        raw = self.VALID.replace(b"Connection: Upgrade\r\n", b"")
        with pytest.raises(ValueError):
            wsrelay.parse_handshake_request(raw)

    def test_wrong_version_rejected(self):
        raw = self.VALID.replace(b"Sec-WebSocket-Version: 13", b"Sec-WebSocket-Version: 8")
        with pytest.raises(ValueError):
            wsrelay.parse_handshake_request(raw)

    def test_missing_key_rejected(self):
        raw = self.VALID.replace(b"Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n", b"")
        with pytest.raises(ValueError):
            wsrelay.parse_handshake_request(raw)

    def test_malformed_header_line_rejected(self):
        raw = self.VALID.replace(b"Host: 127.0.0.1:8098\r\n", b"NotAHeaderLine\r\n")
        with pytest.raises(ValueError):
            wsrelay.parse_handshake_request(raw)


class TestBuildHandshakeResponse:
    def test_contains_correct_accept_key(self):
        resp = wsrelay.build_handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
        assert b"HTTP/1.1 101 Switching Protocols\r\n" in resp
        assert b"Sec-WebSocket-Accept: s3pPLMBiTxaQ9kYGzzhZRbK+xOo=\r\n" in resp
        assert resp.endswith(b"\r\n\r\n")

    def test_includes_upgrade_and_connection_headers(self):
        resp = wsrelay.build_handshake_response("dGhlIHNhbXBsZSBub25jZQ==")
        assert b"Upgrade: websocket\r\n" in resp
        assert b"Connection: Upgrade\r\n" in resp


# ---------------------------------------------------------------------------
# Frame building
# ---------------------------------------------------------------------------

class TestBuildFrame:
    def test_small_payload_uses_7_bit_length(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"x" * 100)
        assert frame[1] == 100  # no mask bit, length fits in 7 bits
        assert len(frame) == 2 + 100

    def test_fin_bit_set_by_default(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"hi")
        assert frame[0] & 0x80

    def test_fin_bit_clear_when_requested(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"hi", fin=False)
        assert not (frame[0] & 0x80)

    def test_opcode_encoded_in_low_nibble(self):
        frame = wsrelay.build_frame(wsrelay.OP_PING, b"")
        assert (frame[0] & 0x0F) == wsrelay.OP_PING

    def test_never_sets_mask_bit(self):
        # RFC 6455 5.1: server->client frames must never be masked.
        for payload in (b"", b"x" * 10, b"x" * 200, b"x" * wsrelay.MAX_FRAME_PAYLOAD):
            frame = wsrelay.build_frame(wsrelay.OP_BINARY, payload)
            assert not (frame[1] & 0x80)

    def test_125_byte_boundary_uses_7_bit_length(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"x" * 125)
        assert frame[1] == 125
        assert len(frame) == 2 + 125

    def test_126_byte_payload_uses_16_bit_extended_length(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"x" * 126)
        assert frame[1] == 126
        assert struct.unpack_from(">H", frame, 2)[0] == 126
        assert len(frame) == 2 + 2 + 126

    def test_65535_byte_payload_uses_16_bit_extended_length(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"x" * 65535)
        assert frame[1] == 126
        assert struct.unpack_from(">H", frame, 2)[0] == 65535

    def test_65536_byte_payload_uses_64_bit_extended_length(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"x" * 65536)
        assert frame[1] == 127
        assert struct.unpack_from(">Q", frame, 2)[0] == 65536

    def test_payload_over_cap_rejected(self):
        with pytest.raises(ValueError):
            wsrelay.build_frame(wsrelay.OP_BINARY, b"x" * (wsrelay.MAX_FRAME_PAYLOAD + 1))

    def test_payload_at_cap_accepted(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"x" * wsrelay.MAX_FRAME_PAYLOAD)
        assert len(frame) > wsrelay.MAX_FRAME_PAYLOAD


# ---------------------------------------------------------------------------
# Frame parsing
# ---------------------------------------------------------------------------

def _mask_payload(payload, key):
    return bytes(b ^ key[i % 4] for i, b in enumerate(payload))


def _build_masked_client_frame(opcode, payload, fin=True, mask_key=b"\x01\x02\x03\x04"):
    """Build a frame the way a real browser WebSocket client would -- masked,
    per RFC 6455 5.1 -- independently of audio_ws_relay.build_frame() (which
    deliberately never masks), so round-tripping through parse_frame() here
    is a genuine cross-check rather than testing build/parse against only
    each other's assumptions."""
    byte0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    if length <= 125:
        header = bytes([byte0, 0x80 | length])
    elif length <= 0xFFFF:
        header = bytes([byte0, 0x80 | 126]) + struct.pack(">H", length)
    else:
        header = bytes([byte0, 0x80 | 127]) + struct.pack(">Q", length)
    return header + mask_key + _mask_payload(payload, mask_key)


class TestParseFrameIncremental:
    def test_empty_buffer_needs_more(self):
        assert wsrelay.parse_frame(b"") == (None, b"")

    def test_single_byte_needs_more(self):
        assert wsrelay.parse_frame(b"\x82") == (None, b"\x82")

    def test_incomplete_16_bit_extended_length_needs_more(self):
        buf = bytes([0x82, 0x80 | 126]) + b"\x00"  # only 1 of 2 length bytes
        assert wsrelay.parse_frame(buf) == (None, buf)

    def test_incomplete_64_bit_extended_length_needs_more(self):
        buf = bytes([0x82, 0x80 | 127]) + b"\x00" * 5  # only 5 of 8 length bytes
        assert wsrelay.parse_frame(buf) == (None, buf)

    def test_incomplete_mask_key_needs_more(self):
        buf = bytes([0x82, 0x80 | 5]) + b"\x01\x02"  # only 2 of 4 mask bytes
        assert wsrelay.parse_frame(buf) == (None, buf)

    def test_incomplete_payload_needs_more(self):
        full = _build_masked_client_frame(wsrelay.OP_BINARY, b"hello")
        truncated = full[:-2]
        assert wsrelay.parse_frame(truncated) == (None, truncated)


class TestParseFrameRoundTrip:
    def test_small_masked_binary_frame(self):
        frame = _build_masked_client_frame(wsrelay.OP_BINARY, b"opus-packet-bytes")
        parsed, remainder = wsrelay.parse_frame(frame)
        assert parsed.fin is True
        assert parsed.opcode == wsrelay.OP_BINARY
        assert parsed.payload == b"opus-packet-bytes"
        assert remainder == b""

    def test_empty_payload_frame(self):
        frame = _build_masked_client_frame(wsrelay.OP_PING, b"")
        parsed, remainder = wsrelay.parse_frame(frame)
        assert parsed.payload == b""
        assert remainder == b""

    def test_16_bit_extended_length_round_trips(self):
        payload = b"y" * 500
        frame = _build_masked_client_frame(wsrelay.OP_BINARY, payload)
        parsed, remainder = wsrelay.parse_frame(frame)
        assert parsed.payload == payload
        assert remainder == b""

    def test_64_bit_extended_length_round_trips(self):
        payload = b"z" * wsrelay.MAX_FRAME_PAYLOAD
        frame = _build_masked_client_frame(wsrelay.OP_BINARY, payload)
        parsed, remainder = wsrelay.parse_frame(frame)
        assert parsed.payload == payload
        assert remainder == b""

    def test_fin_false_is_reported(self):
        frame = _build_masked_client_frame(wsrelay.OP_BINARY, b"partial", fin=False)
        parsed, _ = wsrelay.parse_frame(frame)
        assert parsed.fin is False

    def test_multiple_frames_in_one_buffer_parsed_one_at_a_time(self):
        buf = (_build_masked_client_frame(wsrelay.OP_PING, b"first")
               + _build_masked_client_frame(wsrelay.OP_PING, b"second"))
        parsed1, remainder = wsrelay.parse_frame(buf)
        assert parsed1.payload == b"first"
        parsed2, remainder = wsrelay.parse_frame(remainder)
        assert parsed2.payload == b"second"
        assert remainder == b""

    def test_different_mask_keys_still_unmask_correctly(self):
        for key in (b"\x00\x00\x00\x00", b"\xff\xff\xff\xff", b"\x12\x34\x56\x78"):
            frame = _build_masked_client_frame(wsrelay.OP_BINARY, b"payload-data", mask_key=key)
            parsed, _ = wsrelay.parse_frame(frame)
            assert parsed.payload == b"payload-data"

    def test_build_frame_output_parses_with_require_masked_false(self):
        # build_frame() never masks (correct for server->client) -- confirm
        # its own output is still structurally valid and parses back
        # correctly when the caller doesn't require masking (e.g. a test
        # harness acting as the client, reading the server's frames).
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"server-to-client-audio")
        parsed, remainder = wsrelay.parse_frame(frame, require_masked=False)
        assert parsed.payload == b"server-to-client-audio"
        assert remainder == b""


class TestParseFrameProtocolViolations:
    def test_rsv_bits_set_rejected(self):
        buf = bytearray(_build_masked_client_frame(wsrelay.OP_BINARY, b"x"))
        buf[0] |= 0x40  # set RSV1
        with pytest.raises(ValueError):
            wsrelay.parse_frame(bytes(buf))

    def test_unmasked_client_frame_rejected_by_default(self):
        # build_frame() produces unmasked frames -- correct for the server,
        # but if a "client" ever sent one, RFC 6455 5.1 requires the server
        # reject it.
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"should-be-masked")
        with pytest.raises(ValueError):
            wsrelay.parse_frame(frame)  # require_masked defaults to True

    def test_unmasked_frame_accepted_when_not_required(self):
        frame = wsrelay.build_frame(wsrelay.OP_BINARY, b"x")
        parsed, _ = wsrelay.parse_frame(frame, require_masked=False)
        assert parsed.payload == b"x"

    def test_declared_length_over_cap_rejected(self):
        # A frame claiming a payload larger than MAX_FRAME_PAYLOAD must be
        # rejected from the header alone, before any attempt to read that
        # much data.
        byte0 = 0x80 | wsrelay.OP_BINARY
        header = bytes([byte0, 0x80 | 127]) + struct.pack(">Q", wsrelay.MAX_FRAME_PAYLOAD + 1)
        buf = header + b"\x01\x02\x03\x04"  # mask key, no payload bytes needed to trigger this
        with pytest.raises(ValueError):
            wsrelay.parse_frame(buf)


# ---------------------------------------------------------------------------
# parse_rtp_packet — the per-node Opus encoder's ffmpeg emits RTP-Opus over
# a local UDP socket specifically so this stripping is simple/correct
# (versus demuxing WebM or Ogg from a piped stdout byte stream); the
# second-highest-risk piece of wire-format code in this feature after the
# WebSocket frame functions above.
# ---------------------------------------------------------------------------

def _build_rtp_packet(payload, padding=False, extension=False, csrc_count=0,
                       version=2, seq=1, timestamp=0, ssrc=0x11223344):
    byte0 = (version << 6) | ((1 if padding else 0) << 5) | ((1 if extension else 0) << 4) | csrc_count
    byte1 = 111  # payload type -- irrelevant to parsing, arbitrary Opus-ish value
    header = bytes([byte0, byte1]) + struct.pack(">HII", seq, timestamp, ssrc)
    header += b"\xAA\xBB\xCC\xDD" * csrc_count
    if extension:
        ext_len_words = 1
        header += struct.pack(">HH", 0xBEDE, ext_len_words) + b"\x00\x00\x00\x00" * ext_len_words
    body = payload
    if padding:
        pad_len = 4
        body = payload + b"\x00" * (pad_len - 1) + bytes([pad_len])
    return header + body


class TestParseRtpPacket:
    def test_basic_12_byte_header(self):
        packet = _build_rtp_packet(b"opus-packet-payload")
        assert wsrelay.parse_rtp_packet(packet) == b"opus-packet-payload"

    def test_with_csrc_entries(self):
        packet = _build_rtp_packet(b"payload-with-csrc", csrc_count=2)
        assert wsrelay.parse_rtp_packet(packet) == b"payload-with-csrc"

    def test_with_extension_header(self):
        packet = _build_rtp_packet(b"payload-with-ext", extension=True)
        assert wsrelay.parse_rtp_packet(packet) == b"payload-with-ext"

    def test_with_padding(self):
        packet = _build_rtp_packet(b"payload-with-padding", padding=True)
        assert wsrelay.parse_rtp_packet(packet) == b"payload-with-padding"

    def test_with_csrc_and_extension_and_padding_combined(self):
        packet = _build_rtp_packet(b"combined", csrc_count=1, extension=True, padding=True)
        assert wsrelay.parse_rtp_packet(packet) == b"combined"

    def test_empty_payload(self):
        packet = _build_rtp_packet(b"")
        assert wsrelay.parse_rtp_packet(packet) == b""

    def test_too_short_rejected(self):
        with pytest.raises(ValueError):
            wsrelay.parse_rtp_packet(b"\x80\x6f\x00\x01")  # only 4 of 12 header bytes

    def test_wrong_version_rejected(self):
        packet = _build_rtp_packet(b"x", version=1)
        with pytest.raises(ValueError):
            wsrelay.parse_rtp_packet(packet)

    def test_truncated_csrc_list_rejected(self):
        packet = _build_rtp_packet(b"x", csrc_count=2)
        truncated = packet[:12 + 4]  # header says 2 CSRC entries (8 bytes) but only provides 4
        with pytest.raises(ValueError):
            wsrelay.parse_rtp_packet(truncated)

    def test_truncated_extension_rejected(self):
        packet = _build_rtp_packet(b"x", extension=True)
        truncated = packet[:12 + 2]  # extension header itself (4 bytes) is cut short
        with pytest.raises(ValueError):
            wsrelay.parse_rtp_packet(truncated)

    def test_invalid_padding_length_rejected(self):
        packet = bytearray(_build_rtp_packet(b"1234", padding=True))
        packet[-1] = 0  # a padding length of 0 is invalid (RFC 3550: "at least one")
        with pytest.raises(ValueError):
            wsrelay.parse_rtp_packet(bytes(packet))

    def test_padding_length_larger_than_payload_rejected(self):
        packet = bytearray(_build_rtp_packet(b"1234", padding=True))
        packet[-1] = 255
        with pytest.raises(ValueError):
            wsrelay.parse_rtp_packet(bytes(packet))


# ---------------------------------------------------------------------------
# _opus_ffmpeg_cmd -- the low-latency path's own AGC toggle (unified with
# the legacy WebM path's app._webm_af_filter(), see the "Unified AGC
# Toggle" plan). Pure function, no subprocess spawned.
# ---------------------------------------------------------------------------

class TestOpusFfmpegCmdAgc:
    def _af_arg(self, cmd):
        return cmd[cmd.index('-af') + 1]

    def test_agc_disabled_by_default(self):
        af = self._af_arg(wsrelay._opus_ffmpeg_cmd(12345))
        assert af == "alimiter=limit=0.85:attack=5:release=50:level=false"
        assert "dynaudnorm" not in af

    def test_agc_explicitly_disabled(self):
        af = self._af_arg(wsrelay._opus_ffmpeg_cmd(12345, agc_enabled=False))
        assert "dynaudnorm" not in af

    def test_agc_enabled_prepends_dynaudnorm(self):
        af = self._af_arg(wsrelay._opus_ffmpeg_cmd(12345, agc_enabled=True))
        assert af == ("dynaudnorm=f=50:g=5:p=0.95:m=4:r=0.2,"
                       "alimiter=limit=0.85:attack=5:release=50:level=false")

    def test_agc_enabled_uses_same_tuning_as_legacy_webm_path(self):
        # Reuses app._webm_af_filter()'s exact dynaudnorm parameters --
        # one proven tuning, not two independently maintained ones.
        af_lowlatency = self._af_arg(wsrelay._opus_ffmpeg_cmd(12345, agc_enabled=True))
        af_legacy = app._webm_af_filter(True)
        assert af_lowlatency == af_legacy

    def test_udp_port_still_correct_regardless_of_agc(self):
        cmd = wsrelay._opus_ffmpeg_cmd(54321, agc_enabled=True)
        assert cmd[-1] == 'rtp://127.0.0.1:54321'

    def test_rtp_and_encoder_args_unaffected_by_agc(self):
        without = wsrelay._opus_ffmpeg_cmd(12345, agc_enabled=False)
        with_agc = wsrelay._opus_ffmpeg_cmd(12345, agc_enabled=True)
        # Only the -af value should differ; everything else is identical.
        without_af_idx = without.index('-af')
        with_af_idx = with_agc.index('-af')
        assert without[:without_af_idx] == with_agc[:with_af_idx]
        assert without[without_af_idx + 2:] == with_agc[with_af_idx + 2:]


# ---------------------------------------------------------------------------
# _force_disconnect_all -- triggered by app.py's SIGHUP when RX audio
# settings change (see main()'s _reload handler), so an already-connected
# low-latency listener is dropped immediately rather than keeping the old
# encoder settings until it disconnects on its own. Uses a real connected
# socket pair (not a mock) specifically to verify the one subtly-important
# claim in its own docstring: shutdown(SHUT_RDWR) from one thread reliably
# unblocks another thread's in-progress recv() on that same socket (unlike
# a bare close(), which has a well-known fd-reuse race across threads).
# ---------------------------------------------------------------------------

import socket as _socket_mod
import threading as _threading_mod
import time as _time_mod


class TestForceDisconnectAll:
    def setup_method(self):
        wsrelay._nodes.clear()

    def teardown_method(self):
        wsrelay._nodes.clear()

    def test_unblocks_a_thread_blocked_in_recv(self):
        server_sock, client_sock = _socket_mod.socketpair()
        state = wsrelay._get_or_create_node("628280")
        state.add_client(server_sock, ("127.0.0.1", 12345))

        result = {}

        def _blocked_reader():
            try:
                data = client_sock.recv(4096)
                result["data"] = data
            except OSError as e:
                result["error"] = e

        reader = _threading_mod.Thread(target=_blocked_reader, daemon=True)
        reader.start()
        _time_mod.sleep(0.2)  # give the reader time to actually block in recv()
        assert reader.is_alive(), "reader should still be blocked before force-disconnect"

        n = wsrelay._force_disconnect_all()

        reader.join(timeout=2.0)
        assert not reader.is_alive(), "recv() should have unblocked within 2s"
        assert n == 1
        # An orderly shutdown unblocks recv() with b'' (EOF), not an
        # exception -- confirms this is a clean disconnect signal to the
        # peer, not a connection reset.
        assert result.get("data") == b""

        client_sock.close()

    def test_removes_clients_from_all_nodes(self):
        s1a, s1b = _socket_mod.socketpair()
        s2a, s2b = _socket_mod.socketpair()
        state1 = wsrelay._get_or_create_node("628280")
        state2 = wsrelay._get_or_create_node("546054")
        state1.add_client(s1a, ("127.0.0.1", 1))
        state2.add_client(s2a, ("127.0.0.1", 2))

        n = wsrelay._force_disconnect_all()
        assert n == 2

        # Each server-side socket was shutdown+closed -- confirm via the
        # peer observing EOF, same signal checked above, for both nodes.
        s1b.settimeout(2.0)
        s2b.settimeout(2.0)
        assert s1b.recv(4096) == b""
        assert s2b.recv(4096) == b""
        s1b.close()
        s2b.close()

    def test_no_op_with_no_connected_clients(self):
        wsrelay._get_or_create_node("628280")  # node exists, but no clients
        assert wsrelay._force_disconnect_all() == 0


class TestPcmOwnership:
    """_claim_pcm_owner()/_release_pcm_owner() -- only one audio_relay.py
    connection may feed a given node's low-latency ffmpeg at a time. Plain
    object() sentinels stand in for connection sockets: only identity
    matters to this logic, never actual socket I/O."""

    def setup_method(self):
        wsrelay._pcm_owners.clear()

    def teardown_method(self):
        wsrelay._pcm_owners.clear()

    def test_first_connection_claims_the_node(self):
        conn = object()
        assert wsrelay._claim_pcm_owner("628280", conn) is True

    def test_same_connection_reclaiming_still_succeeds(self):
        conn = object()
        wsrelay._claim_pcm_owner("628280", conn)
        assert wsrelay._claim_pcm_owner("628280", conn) is True

    def test_second_connection_for_same_node_is_rejected(self):
        first, second = object(), object()
        assert wsrelay._claim_pcm_owner("628280", first) is True
        assert wsrelay._claim_pcm_owner("628280", second) is False

    def test_different_nodes_do_not_conflict(self):
        a, b = object(), object()
        assert wsrelay._claim_pcm_owner("628280", a) is True
        assert wsrelay._claim_pcm_owner("546054", b) is True

    def test_release_lets_a_new_connection_claim_the_node(self):
        first, second = object(), object()
        wsrelay._claim_pcm_owner("628280", first)
        wsrelay._release_pcm_owner("628280", first)
        assert wsrelay._claim_pcm_owner("628280", second) is True

    def test_release_by_non_owner_is_a_no_op(self):
        owner, impostor = object(), object()
        wsrelay._claim_pcm_owner("628280", owner)
        wsrelay._release_pcm_owner("628280", impostor)
        # The real owner's claim must survive an unrelated release() call --
        # otherwise a stale/rejected connection's own cleanup could evict the
        # legitimate owner out from under it.
        assert wsrelay._pcm_owners.get("628280") is owner
