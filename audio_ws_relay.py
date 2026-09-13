#!/usr/bin/env python3
"""
HenWen low-latency RX audio relay — WebSocket protocol primitives.

Part of the "Low-Latency Listen Path" feature (see the project's planning
notes): an opt-in alternative to the default WebM/MSE Listen pipeline that
streams raw Opus packets to browsers over a plain WebSocket instead of
muxing into a container and serving over chunked HTTP, trading the WebM
path's AGC/robustness for substantially lower latency. Selected per-install
via the `rx_audio_config` setting (see app.py's `_validate_rx_audio_path()`)
— but this *process* runs unconditionally from the moment HenWen starts
(app.py's `start_audio_ws_relay()`/`_audio_ws_relay_supervisor_loop()`),
regardless of that setting; only the browser WebSocket handshake
(`_authorize_ws()` below, gated on `rx_audio_config.path == 'lowlatency'`)
and the per-node ffmpeg spawn actually depend on it. Kept always-on rather
than started/stopped per setting change because idle cost is genuinely
near-zero — both listener loops below block in `accept()` with no polling,
and PCM frames dual-written by every legacy MixMonitor broadcast
(`audio_relay.py`) are a dict-lookup no-op here (`_feed_node_pcm()`) until a
real low-latency WS client attaches — and because RX Diagnostics and the
settings-change reload signal (`_signal_audio_ws_relay_reload()`) both
already assume this process is always there to check/signal. See CLAUDE.md's
"Background threads" section for the Pi Zero 2 W hardware-cost reasoning.

This file holds both the WebSocket wire protocol (the opening HTTP-Upgrade
handshake, RFC 6455 §4, and binary frame encode/decode, RFC 6455 §5.2) and
the real-time server built on top of it. Hand-rolled over stdlib `socket`/
`hashlib`/`base64`/`struct` rather than a pip dependency, mirroring this
project's existing precedent for simple protocols it already owns end-to-end
(`irc_relay.py`'s IRC client, `AMIClient`'s raw AMI socket in app.py) — a
WebSocket server for a single first-party audio stream is well within
hand-rolling range, and it keeps this optional feature from ever depending
on something that has to "fail clean" the way Piper/aprslib/paho-mqtt do for
their own optional features.

The protocol functions (`compute_accept_key`, `parse_handshake_request`,
`build_handshake_response`, `build_frame`, `parse_frame`) are a **pure,
sans-I/O transformation** layer — bytes/strings in, bytes/values out, no
socket access — deliberately, so the protocol logic (the highest-risk new
code in this feature; WebSocket frame parsing bugs are subtle) is fully
unit-testable without a live server or a real client. This mirrors how
audio_relay.py keeps `_fade_frame()` (the DSP-ish part) separable from its
real-time read/write loop. Everything below that layer — accepting
connections, running the per-node Opus ffmpeg, fanning frames out to
connected clients, the `/internal/audio/*` control-plane calls into app.py —
is real-time/live-socket code in the same spirit as audio_relay.py's own
main loop, and (matching that file's own precedent, and this project's
`tests/conftest.py`-documented boundary that the audio pipeline itself isn't
unit tested) is verified manually/in the field rather than under pytest.

Two independent listeners:
  - PCM ingestion (`AUDIO_WS_RELAY_PORT`, default 8099, loopback-only): a
    trivial internal protocol between this process and every audio_relay.py
    instance on the box (see that module's "WS-relay dual-write" docstring
    section) -- a one-line `NODE <n>\n` handshake, then a raw stream of
    320-byte paced PCM frames, forever, best-effort on both ends.
  - Browser WebSocket (`AUDIO_WS_PORT`, default 8098, loopback-only, proxied
    by Apache at `/ws-audio` -- see `ws-audio/apply.sh`): real RFC 6455
    WebSocket connections from browsers, path `/<node>` (Apache's ProxyPass
    strips the `/ws-audio` prefix it matched on before forwarding). Each
    connection is authorized via `/internal/audio/authorize-ws` (forwarding
    the browser's own Cookie header) before anything else happens.

Per node, a lightweight ffmpeg instance (raw PCM in via stdin, RTP-Opus out
via a local UDP socket -- see `_start_node_ffmpeg()`'s docstring for why RTP
rather than piping ffmpeg's own container-muxed stdout) turns the dual-written
PCM into individual Opus packets, each immediately wrapped in a WS binary
frame and fanned out to every browser client currently attached to that node.
Started on that node's first WS client, stopped on its last.

Wire protocol summary (RFC 6455):
  Handshake: client sends `GET <path> HTTP/1.1` with `Upgrade: websocket`,
  `Connection: Upgrade`, `Sec-WebSocket-Version: 13`, and a random
  `Sec-WebSocket-Key`; server replies `101 Switching Protocols` with
  `Sec-WebSocket-Accept` = base64(sha1(key + a fixed GUID)).

  Frames: a 2-byte base header (FIN+RSV+opcode, then MASK+7-bit length),
  optionally extended to a 16- or 64-bit length, optionally followed by a
  4-byte masking key, then the (masked, if a key was present) payload.
  Client->server frames MUST be masked; server->client frames MUST NOT be
  (RFC 6455 §5.1) — this module enforces both directions of that rule:
  build_frame() never masks, parse_frame() rejects an unmasked frame by
  default (the server-receiving-from-client case, which is this module's
  only real use for parse_frame — the browser side of this feature only
  ever sends small control frames; the audio itself flows server->client).
"""
import base64
import hashlib
import json
import os
import re
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import namedtuple

# RFC 6455 §1.3's fixed handshake GUID -- not a secret, just a magic
# constant every WebSocket implementation concatenates onto the client's
# Sec-WebSocket-Key before hashing, so a server can prove it actually speaks
# the WebSocket protocol (rather than e.g. an HTTP cache blindly echoing the
# client's own header back).
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# Opcodes (RFC 6455 §5.2).
OP_CONTINUATION = 0x0
OP_TEXT         = 0x1
OP_BINARY       = 0x2
OP_CLOSE        = 0x8
OP_PING         = 0x9
OP_PONG         = 0xA

# This stream only ever carries tiny payloads -- ~20-100 byte Opus packets
# (a 24kbps/20ms frame is on the order of 60 bytes) and even tinier control
# frames. 64KiB is generous headroom for that while still bounding how much
# a malformed or hostile peer can make this server try to buffer/allocate
# for one claimed frame length.
MAX_FRAME_PAYLOAD = 65536


def compute_accept_key(sec_websocket_key):
    """RFC 6455 §4.2.2 step 5: base64(sha1(key + GUID)). `sec_websocket_key`
    is the client's raw Sec-WebSocket-Key header value (already base64 --
    this does not decode it, just concatenates and re-hashes per spec)."""
    digest = hashlib.sha1((sec_websocket_key.strip() + _WS_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


def parse_handshake_request(raw):
    """Parse a raw HTTP/1.1 Upgrade request's header block (everything up
    to and including the blank line that terminates it -- any bytes after
    that, e.g. the start of a already-arrived WS frame on a pipelined
    connection, are simply not consumed or returned here; the caller is
    reading a handshake, not framed data, at this point).

    Returns {'method': str, 'path': str, 'headers': dict} where `headers`
    keys are lowercased header names. Raises ValueError if `raw` doesn't
    yet contain a complete header block (caller should read more and
    retry), isn't a GET, or is missing/has an invalid value for any header
    RFC 6455 requires for a valid WebSocket upgrade (Upgrade, Connection,
    Sec-WebSocket-Version: 13, Sec-WebSocket-Key)."""
    if b"\r\n\r\n" not in raw:
        raise ValueError("incomplete request (no header terminator yet)")
    head, _, _ = raw.partition(b"\r\n\r\n")
    lines = head.split(b"\r\n")
    if not lines or not lines[0]:
        raise ValueError("empty request line")

    request_line = lines[0].decode("iso-8859-1", errors="replace")
    parts = request_line.split(" ")
    if len(parts) != 3 or parts[0] != "GET":
        raise ValueError(f"not a GET request: {request_line!r}")
    path = parts[1]

    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        name, sep, value = line.partition(b":")
        if not sep:
            raise ValueError(f"malformed header line: {line!r}")
        headers[name.decode("iso-8859-1").strip().lower()] = value.decode("iso-8859-1").strip()

    def _has_token(header_value, token):
        return token.lower() in [t.strip().lower() for t in header_value.split(",") if t.strip()]

    if headers.get("upgrade", "").lower() != "websocket":
        raise ValueError(f"missing/invalid Upgrade header: {headers.get('upgrade')!r}")
    if not _has_token(headers.get("connection", ""), "upgrade"):
        raise ValueError(f"missing/invalid Connection header: {headers.get('connection')!r}")
    if headers.get("sec-websocket-version") != "13":
        raise ValueError(f"unsupported Sec-WebSocket-Version: {headers.get('sec-websocket-version')!r}")
    if "sec-websocket-key" not in headers:
        raise ValueError("missing Sec-WebSocket-Key header")

    return {"method": parts[0], "path": path, "headers": headers}


def build_handshake_response(sec_websocket_key):
    """The `101 Switching Protocols` response confirming the upgrade,
    per RFC 6455 §4.2.2. `sec_websocket_key` is the client's raw header
    value (from parse_handshake_request()'s output)."""
    accept = compute_accept_key(sec_websocket_key)
    return (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {accept}\r\n"
        "\r\n"
    ).encode("ascii")


def build_frame(opcode, payload=b"", fin=True):
    """Build one **unmasked** frame — correct for every frame this server
    sends, since RFC 6455 §5.1 requires server->client frames to never be
    masked (only client->server frames are). Raises ValueError if `payload`
    exceeds MAX_FRAME_PAYLOAD (every real payload here — an Opus packet or a
    control frame — is tiny; a caller asking to send more than that is a
    bug, not a normal condition to silently truncate)."""
    if len(payload) > MAX_FRAME_PAYLOAD:
        raise ValueError(f"payload too large ({len(payload)} > {MAX_FRAME_PAYLOAD})")
    byte0 = (0x80 if fin else 0x00) | (opcode & 0x0F)
    length = len(payload)
    if length <= 125:
        header = bytes([byte0, length])
    elif length <= 0xFFFF:
        header = bytes([byte0, 126]) + struct.pack(">H", length)
    else:
        header = bytes([byte0, 127]) + struct.pack(">Q", length)
    return header + payload


ParsedFrame = namedtuple("ParsedFrame", ["fin", "opcode", "payload"])


def _unmask(data, key):
    return bytes(b ^ key[i % 4] for i, b in enumerate(data))


def parse_frame(buf, require_masked=True):
    """Try to parse exactly one frame from the start of `buf` (a bytes-like
    object — typically everything read so far from the client socket that
    hasn't been consumed by an earlier successful parse).

    Returns `(None, buf)` if `buf` doesn't yet contain a complete frame —
    the caller should read more bytes from the socket, append, and retry.
    This incremental shape (return "not enough yet" rather than blocking or
    raising) mirrors how audio_relay.py's own `_AudioSocketReader.read()`
    already handles a framed protocol arriving in arbitrary-sized chunks
    over a real socket.

    Returns `(ParsedFrame(fin, opcode, payload), remainder)` on success,
    where `remainder` is whatever bytes in `buf` followed the parsed frame
    (0 or more further frames' worth, or a partial start of the next one).

    Raises ValueError for a frame that violates a protocol invariant this
    server enforces rather than tolerates:
      - RSV1-3 set (no WebSocket extensions are negotiated or supported)
      - `require_masked=True` (the default — this is the server parsing
        frames sent *by* a client) and the frame is unmasked: RFC 6455
        §5.1 requires the server to fail the connection in this case,
        since an unmasked client frame is either a broken client or
        something attempting to smuggle raw bytes past a proxy that
        expects client traffic to always be masked.
      - a declared payload length over MAX_FRAME_PAYLOAD, before any
        attempt is made to actually read that much payload -- protects
        against a malformed or hostile peer's declared length alone
        forcing a large allocation/read.
    """
    if len(buf) < 2:
        return None, buf

    byte0, byte1 = buf[0], buf[1]
    fin    = bool(byte0 & 0x80)
    rsv    = byte0 & 0x70
    opcode = byte0 & 0x0F
    if rsv:
        raise ValueError(f"reserved bits set (0x{rsv:02x}); no extensions are supported")

    masked  = bool(byte1 & 0x80)
    length7 = byte1 & 0x7F

    pos = 2
    if length7 <= 125:
        length = length7
    elif length7 == 126:
        if len(buf) < pos + 2:
            return None, buf
        length = struct.unpack_from(">H", buf, pos)[0]
        pos += 2
    else:  # 127
        if len(buf) < pos + 8:
            return None, buf
        length = struct.unpack_from(">Q", buf, pos)[0]
        pos += 8

    if length > MAX_FRAME_PAYLOAD:
        raise ValueError(f"declared payload length {length} exceeds cap {MAX_FRAME_PAYLOAD}")

    mask_key = b""
    if masked:
        if len(buf) < pos + 4:
            return None, buf
        mask_key = bytes(buf[pos:pos + 4])
        pos += 4
    elif require_masked:
        raise ValueError("received unmasked frame from client (RFC 6455 5.1 violation)")

    if len(buf) < pos + length:
        return None, buf

    payload = bytes(buf[pos:pos + length])
    if masked:
        payload = _unmask(payload, mask_key)

    return ParsedFrame(fin, opcode, payload), bytes(buf[pos + length:])


def parse_rtp_packet(datagram):
    """Strip an RTP header (RFC 3550 §5.1) off one UDP datagram from the
    per-node Opus ffmpeg's `-f rtp` output, returning just the Opus packet
    payload. Used instead of piping ffmpeg's stdout directly because every
    container ffmpeg can mux Opus into (WebM, Ogg) requires real demuxing to
    recover individual packet boundaries from a continuous byte stream —
    RTP's fixed-size header (12 bytes, plus 4 bytes per CSRC entry if any
    are present, which ffmpeg's own simple rtp muxer never adds) already
    delimits exactly one packet per UDP datagram at this frame duration, so
    "strip N header bytes" is the entire job.

    Kept pure (bytes in, bytes out) for the same testability reason as the
    WebSocket frame functions above — this is the second-highest-risk piece
    of wire-format code in this feature after those.

    Raises ValueError if `datagram` is too short to be a valid RTP packet,
    or declares an RTP version other than 2 (the only version RFC 3550
    defines; a mismatch here means this isn't actually RTP, not a supported
    protocol variant to tolerate).
    """
    if len(datagram) < 12:
        raise ValueError(f"datagram too short to be RTP ({len(datagram)} bytes)")
    byte0 = datagram[0]
    version = byte0 >> 6
    if version != 2:
        raise ValueError(f"unsupported RTP version {version} (only version 2 is defined)")
    padding   = bool(byte0 & 0x20)
    extension = bool(byte0 & 0x10)
    csrc_count = byte0 & 0x0F

    pos = 12 + 4 * csrc_count
    if len(datagram) < pos:
        raise ValueError("datagram shorter than its declared CSRC list")

    if extension:
        if len(datagram) < pos + 4:
            raise ValueError("datagram shorter than its RTP extension header")
        ext_len_words = struct.unpack_from(">H", datagram, pos + 2)[0]
        pos += 4 + 4 * ext_len_words
        if len(datagram) < pos:
            raise ValueError("datagram shorter than its declared RTP extension")

    payload = datagram[pos:]
    if padding:
        if not payload:
            raise ValueError("padding bit set but payload is empty")
        pad_len = payload[-1]
        if pad_len == 0 or pad_len > len(payload):
            raise ValueError(f"invalid RTP padding length {pad_len}")
        payload = payload[:-pad_len]

    return bytes(payload)


# ---------------------------------------------------------------------------
# Real-time server: PCM ingestion from audio_relay.py, per-node Opus ffmpeg,
# and the browser-facing WebSocket listener that fans encoded packets out.
# ---------------------------------------------------------------------------

FRAME_BYTES = 320   # matches audio_relay.py's own 20ms-at-8kHz frame size
MAX_HANDSHAKE_BYTES = 8192   # guards against a peer that never sends \r\n\r\n

# Where this process's own control-plane calls into app.py go. app.py hands
# these to us via our own spawn environment (see app.py's eventual
# "spawn/supervise audio_ws_relay.py" startup code) -- read here with
# permissive defaults purely so this module can be exercised standalone
# (e.g. `python3 audio_ws_relay.py` against a manually-started internal API)
# without every value having to be threaded through explicitly.
APP_BASE_URL     = os.environ.get('AUDIO_WS_APP_BASE_URL', 'http://127.0.0.1:5000')
INTERNAL_SECRET  = os.environ.get('AUDIO_WS_INTERNAL_SECRET', '')
PCM_LISTEN_HOST  = os.environ.get('AUDIO_WS_RELAY_HOST', '127.0.0.1')
PCM_LISTEN_PORT  = int(os.environ.get('AUDIO_WS_RELAY_PORT', '8099'))
WS_LISTEN_HOST   = os.environ.get('AUDIO_WS_HOST', '127.0.0.1')
WS_LISTEN_PORT   = int(os.environ.get('AUDIO_WS_PORT', '8098'))

_NODE_RE = re.compile(r'^\d{4,7}$')

# ffmpeg encodes this path's own Opus, deliberately lighter than the WebM
# path's by default: no resample (input is already the rate we encode at),
# and dynaudnorm is opt-in rather than always-on -- its lookahead is
# exactly the latency this whole path exists to avoid, so AGC costs ~0.4s
# here when a user explicitly turns it on (rx_audio_config.agc_enabled,
# unified with the legacy path's own AGC toggle -- see app.py's
# _webm_af_filter() and the "Unified AGC Toggle" plan). alimiter alone is
# always present for peak/clip protection regardless. Reuses the legacy
# path's exact dynaudnorm tuning verbatim when enabled (f=50:g=5:p=0.95:
# m=4:r=0.2) rather than re-deriving new parameters -- see app.py's
# _start_broadcast() comment block for that tuning's own (much longer)
# history.
def _opus_ffmpeg_cmd(udp_port, agc_enabled=False):
    af = 'alimiter=limit=0.85:attack=5:release=50:level=false'
    if agc_enabled:
        af = 'dynaudnorm=f=50:g=5:p=0.95:m=4:r=0.2,' + af
    return [
        'ffmpeg', '-loglevel', 'warning',
        '-fflags', '+nobuffer',
        '-f', 's16le', '-ar', '8000', '-ac', '1', '-channel_layout', 'mono',
        '-i', 'pipe:0',
        '-af', af,
        '-c:a', 'libopus', '-b:a', '24k', '-vbr', 'off', '-cutoff', '4000',
        '-frame_duration', '20', '-application', 'audio',
        '-f', 'rtp', '-payload_type', '111', f'rtp://127.0.0.1:{udp_port}',
    ]


def _stat(msg):
    """This process's whole logging convention: every intentional line is
    printed with a level prefix (INFO/WARN/DEBUG/STATS) app.py's own
    stderr-forwarding thread reads and maps to the matching real log level
    (STATS -> DEBUG, since those are the noisy periodic heartbeats; the
    others map directly) -- mirrors audio_relay.py's own 'STATS '-prefix
    convention but with real levels, since (unlike that file) this process's
    own lifecycle events (client connect/disconnect, encoder start/stop) are
    worth seeing without needing full DEBUG verbosity turned on."""
    print(msg, file=sys.stderr, flush=True)


def _log(level, msg):
    _stat(f'{level} {msg}')


_nodes_lock = threading.Lock()
_nodes = {}   # node (str) -> _NodeState

# Only one audio_relay.py instance may feed a given node's low-latency
# encoder at a time -- see _claim_pcm_owner()'s docstring below for why.
_pcm_owners_lock = threading.Lock()
_pcm_owners = {}   # node (str) -> the winning PCM connection socket


class _NodeState:
    """Everything this process tracks for one node: its currently-connected
    browser WS client sockets, and (if at least one is connected) the Opus
    ffmpeg encoding for them."""

    def __init__(self, node):
        self.node = node
        self.clients = []           # list of (socket, addr)
        self.clients_lock = threading.Lock()
        self.ffmpeg_proc = None
        self.udp_sock = None
        self.reader_thread = None
        self.stderr_thread = None

    def add_client(self, sock, addr):
        with self.clients_lock:
            self.clients.append((sock, addr))
            count = len(self.clients)
        _log('INFO', f'[node {self.node}] client {addr} connected ({count} total)')
        return count

    def remove_client(self, sock, addr):
        with self.clients_lock:
            self.clients = [(s, a) for (s, a) in self.clients if s is not sock]
            count = len(self.clients)
        _log('INFO', f'[node {self.node}] client {addr} disconnected ({count} remaining)')
        return count

    def fanout(self, opus_payload):
        frame = build_frame(OP_BINARY, opus_payload)
        with self.clients_lock:
            targets = list(self.clients)
        dead = []
        for sock, addr in targets:
            try:
                sock.sendall(frame)
            except OSError:
                dead.append((sock, addr))
        if dead:
            with self.clients_lock:
                self.clients = [(s, a) for (s, a) in self.clients if (s, a) not in dead]
            for sock, addr in dead:
                _log('WARN', f'[node {self.node}] client {addr} send failed, dropped')
                try:
                    sock.close()
                except Exception:
                    pass


def _get_or_create_node(node):
    with _nodes_lock:
        state = _nodes.get(node)
        if state is None:
            state = _NodeState(node)
            _nodes[node] = state
        return state


def _force_disconnect_all():
    """Force-closes every connected browser client across every node --
    triggered by app.py's SIGHUP (see main()) when the RX audio settings
    (path or AGC) change, so an already-connected low-latency listener
    doesn't keep hearing the old encoder settings until they disconnect on
    their own.

    Deliberately does none of _handle_ws_connection's own cleanup here
    (removing the client, stopping the encoder on the last one, releasing
    capture) -- shutdown(SHUT_RDWR) on a socket reliably unblocks another
    thread's in-progress recv() on that same socket (unlike close(), which
    has a well-known fd-reuse race across threads), so the owning
    connection's own thread sees the closed connection on its very next
    recv() and runs its existing normal teardown path itself. Nothing to
    keep in sync here beyond causing that to happen.
    """
    with _nodes_lock:
        states = list(_nodes.values())
    total = 0
    for state in states:
        with state.clients_lock:
            socks = [s for s, _addr in state.clients]
        for sock in socks:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
            total += 1
    _log('INFO', f'force-disconnected {total} client(s) across {len(states)} node(s) '
                f'(RX audio settings changed)')
    return total


# ---------------------------------------------------------------------------
# Internal control-plane calls into app.py (see app.py's "Low-latency RX
# audio" section — /internal/audio/ensure-capture, release-capture,
# authorize-ws).
# ---------------------------------------------------------------------------

def _internal_post(path, body):
    req = urllib.request.Request(
        f'{APP_BASE_URL}{path}',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json', 'X-Internal-Secret': INTERNAL_SECRET},
        method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode('utf-8'))
        except Exception:
            return e.code, {}
    except Exception as e:
        _log('WARN', f'internal API call to {path} failed: {e}')
        return 0, {}


def _ensure_capture(node):
    """Returns (ok, agc_enabled). agc_enabled is only meaningful when
    ok is True -- it rides along on this same round-trip since it's the
    exact moment the caller is about to spawn this node's Opus encoder and
    needs to know the current AGC setting for it (see
    api_internal_audio_ensure_capture()'s docstring in app.py)."""
    status, body = _internal_post('/internal/audio/ensure-capture', {'node': node})
    if status != 200:
        _log('WARN', f'[node {node}] ensure-capture failed: {status} {body}')
        return False, False
    return True, bool(body.get('agc_enabled', False))


def _release_capture(node):
    status, body = _internal_post('/internal/audio/release-capture', {'node': node})
    if status != 200:
        _log('WARN', f'[node {node}] release-capture failed: {status} {body}')


def _authorize_ws(node, cookie_header):
    """Forwards the browser's raw Cookie header from its WS handshake
    request onto a real request to app.py, so Flask's own session parsing
    and check_auth() (this app.py endpoint is in _USER_OR_ABOVE) decide
    whether it represents a currently-valid logged-in session — see
    api_internal_audio_authorize_ws()'s docstring in app.py."""
    req = urllib.request.Request(
        f'{APP_BASE_URL}/internal/audio/authorize-ws?node={node}',
        headers={'X-Internal-Secret': INTERNAL_SECRET, 'Cookie': cookie_header or ''},
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.loads(resp.read().decode('utf-8')).get('ok') is True
    except Exception as e:
        _log('WARN', f'[node {node}] authorize-ws failed: {e}')
        return False


# ---------------------------------------------------------------------------
# Per-node Opus encoder lifecycle
# ---------------------------------------------------------------------------

def _start_node_ffmpeg(state, agc_enabled=False):
    udp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    udp_sock.bind(('127.0.0.1', 0))
    udp_port = udp_sock.getsockname()[1]

    proc = subprocess.Popen(
        _opus_ffmpeg_cmd(udp_port, agc_enabled),
        stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        # bufsize=0: unbuffered. _feed_node_pcm() writes one 320-byte frame
        # every 20ms -- Python's default buffered stdin would sit on those
        # writes until its internal buffer filled (multiple seconds' worth
        # at this rate) before ffmpeg ever saw them, reintroducing exactly
        # the kind of latency this whole path exists to avoid. Confirmed
        # live: without this, zero encoded frames reached a connected
        # client despite PCM arriving correctly, since ffmpeg's stdin read
        # never unblocked.
        bufsize=0,
    )
    state.ffmpeg_proc = proc
    state.udp_sock = udp_sock

    def _udp_reader():
        udp_sock.settimeout(1.0)
        while state.ffmpeg_proc is proc:
            try:
                datagram, _addr = udp_sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                payload = parse_rtp_packet(datagram)
            except ValueError as e:
                _log('WARN', f'[node {state.node}] bad RTP packet from encoder: {e}')
                continue
            state.fanout(payload)

    def _stderr_reader():
        try:
            for raw_line in proc.stderr:
                line = raw_line.decode('utf-8', errors='replace').rstrip()
                if line:
                    _log('WARN', f'[node {state.node}] ffmpeg: {line}')
        except Exception:
            pass

    state.reader_thread = threading.Thread(target=_udp_reader, daemon=True,
                                           name=f'ws-udp-reader-{state.node}')
    state.stderr_thread = threading.Thread(target=_stderr_reader, daemon=True,
                                           name=f'ws-ffmpeg-stderr-{state.node}')
    state.reader_thread.start()
    state.stderr_thread.start()
    _log('INFO', f'[node {state.node}] Opus encoder started (PID {proc.pid}, RTP port {udp_port}, '
                f'agc_enabled={agc_enabled})')


def _stop_node_ffmpeg(state):
    proc = state.ffmpeg_proc
    if proc is None:
        return
    state.ffmpeg_proc = None
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.terminate()
        proc.wait(timeout=2)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=2)
        except Exception:
            pass
    if state.udp_sock is not None:
        try:
            state.udp_sock.close()
        except Exception:
            pass
        state.udp_sock = None
    _log('INFO', f'[node {state.node}] Opus encoder stopped')


def _claim_pcm_owner(node, conn):
    """True if `conn` is (or becomes) the sole authoritative PCM sender for
    `node` right now. Every audio_relay.py instance on the box dual-writes
    unconditionally -- a legacy WebM broadcast, a low-latency capture-only
    relay, and (in principle) more than one of either could all be alive for
    the same node at once. Without this, whichever ones are simultaneously
    connected all feed this node's shared low-latency ffmpeg at the same
    time, doubling (or worse) the audio it encodes -- confirmed live as the
    root cause of persistent choppiness on the low-latency path whenever a
    concurrent legacy Listen session (or recording, or the stream relay) was
    also active for the same node. First connection for a node wins;
    released on disconnect (see _handle_pcm_connection's finally block) so a
    later connection -- including a legitimate reconnect of the same
    instance after a drop -- can then claim it."""
    with _pcm_owners_lock:
        owner = _pcm_owners.get(node)
        if owner is None or owner is conn:
            _pcm_owners[node] = conn
            return True
        return False


def _release_pcm_owner(node, conn):
    with _pcm_owners_lock:
        if _pcm_owners.get(node) is conn:
            del _pcm_owners[node]


def _feed_node_pcm(node, frame):
    """Called by a PCM-ingestion connection thread for every 320-byte frame
    it reads from audio_relay.py's dual-write. A silent no-op if this node
    has no active encoder right now (no WS clients attached, or clients
    attached to a *different* node than this particular audio_relay.py
    instance happens to be dual-writing for) -- every audio_relay.py
    instance on the box dual-writes unconditionally (see its own docstring),
    so most received frames for most nodes most of the time are expected to
    have nowhere to go. Only ever called for the connection _claim_pcm_owner()
    accepted as this node's owner -- see _handle_pcm_connection()."""
    with _nodes_lock:
        state = _nodes.get(node)
    if state is None or state.ffmpeg_proc is None:
        return
    try:
        state.ffmpeg_proc.stdin.write(frame)
    except (BrokenPipeError, OSError):
        pass  # encoder died/is being torn down; the next frame just no-ops too


# ---------------------------------------------------------------------------
# PCM ingestion listener (audio_relay.py -> this process)
# ---------------------------------------------------------------------------

def _handle_pcm_connection(conn, addr):
    conn.settimeout(5.0)
    buf = b''
    node = None
    try:
        while b'\n' not in buf:
            chunk = conn.recv(256)
            if not chunk:
                return
            buf += chunk
            if len(buf) > 256:
                return
        line, _, rest = buf.partition(b'\n')
        if not line.startswith(b'NODE '):
            return
        node = line[len(b'NODE '):].decode('ascii', errors='replace').strip()
        if not _NODE_RE.match(node):
            return
        if not _claim_pcm_owner(node, conn):
            _log('INFO', f'[node {node}] duplicate PCM sender from {addr} rejected -- a '
                         f'low-latency feed for this node is already active from another '
                         f'audio_relay.py instance (e.g. a concurrent legacy Listen session, '
                         f'recording, or the stream relay for the same node)')
            return
        conn.settimeout(None)
        frame_buf = bytearray(rest)
        while True:
            while len(frame_buf) < FRAME_BYTES:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                frame_buf += chunk
            frame = bytes(frame_buf[:FRAME_BYTES])
            del frame_buf[:FRAME_BYTES]
            _feed_node_pcm(node, frame)
    except OSError:
        pass
    finally:
        if node is not None:
            _release_pcm_owner(node, conn)
        try:
            conn.close()
        except Exception:
            pass


def _pcm_listener_loop(host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(16)
    _log('INFO', f'PCM ingestion listening on {host}:{port}')
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=_handle_pcm_connection, args=(conn, addr),
                          daemon=True, name=f'ws-pcm-{addr[1]}').start()


# ---------------------------------------------------------------------------
# Browser WebSocket listener
# ---------------------------------------------------------------------------

def _read_handshake(conn):
    buf = b''
    while b'\r\n\r\n' not in buf:
        chunk = conn.recv(4096)
        if not chunk:
            raise ValueError('connection closed during handshake')
        buf += chunk
        if len(buf) > MAX_HANDSHAKE_BYTES:
            raise ValueError('handshake too large')
    return parse_handshake_request(buf)


def _handle_ws_connection(conn, addr):
    node = None
    state = None
    registered = False
    try:
        conn.settimeout(10.0)
        req = _read_handshake(conn)
        node = req['path'].lstrip('/').split('/')[0]
        if not _NODE_RE.match(node):
            _log('WARN', f'WS handshake for invalid path {req["path"]!r} from {addr}')
            return

        if not _authorize_ws(node, req['headers'].get('cookie', '')):
            _log('WARN', f'[node {node}] unauthorized WS connect from {addr}')
            return

        response = build_handshake_response(req['headers']['sec-websocket-key'])
        conn.sendall(response)
        conn.settimeout(None)

        state = _get_or_create_node(node)
        count = state.add_client(conn, addr)
        registered = True
        if count == 1:
            ok, agc_enabled = _ensure_capture(node)
            if not ok:
                _log('WARN', f'[node {node}] ensure-capture failed; closing {addr}')
                return
            _start_node_ffmpeg(state, agc_enabled)

        # Read loop: this stream is receive-mostly (audio only flows
        # server->client) -- the only thing worth doing with whatever a
        # client sends is answering PING with PONG and noticing a CLOSE, so
        # unlike the PCM side there's no need to reassemble anything beyond
        # single control frames.
        buf = b''
        while True:
            chunk = conn.recv(4096)
            if not chunk:
                break
            buf += chunk
            while True:
                try:
                    parsed, buf = parse_frame(buf, require_masked=True)
                except ValueError as e:
                    _log('WARN', f'[node {node}] bad WS frame from {addr}: {e}')
                    return
                if parsed is None:
                    break
                if parsed.opcode == OP_CLOSE:
                    return
                if parsed.opcode == OP_PING:
                    conn.sendall(build_frame(OP_PONG, parsed.payload))
    except (OSError, ValueError):
        pass
    finally:
        if registered and state is not None:
            remaining = state.remove_client(conn, addr)
            if remaining == 0:
                _stop_node_ffmpeg(state)
                _release_capture(node)
        try:
            conn.close()
        except Exception:
            pass


def _ws_listener_loop(host, port):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((host, port))
    srv.listen(32)
    _log('INFO', f'Browser WebSocket listening on {host}:{port}')
    while True:
        conn, addr = srv.accept()
        conn.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        threading.Thread(target=_handle_ws_connection, args=(conn, addr),
                          daemon=True, name=f'ws-client-{addr[1]}').start()


def main():
    if not INTERNAL_SECRET:
        _log('WARN', 'AUDIO_WS_INTERNAL_SECRET not set — every internal API '
                     'call will be rejected by app.py until this process is '
                     'restarted with it set (app.py sets this in our spawn '
                     'environment; running this file standalone needs it set '
                     'by hand for the internal API calls to succeed)')

    def _stop(signum, frame):
        _log('INFO', f'received signal {signum}, exiting')
        os._exit(0)   # daemon threads; nothing to flush/join that matters

    def _reload(signum, frame):
        _log('INFO', 'received SIGHUP, force-disconnecting all clients (RX audio settings changed)')
        _force_disconnect_all()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGHUP, _reload)

    pcm_thread = threading.Thread(target=_pcm_listener_loop,
                                  args=(PCM_LISTEN_HOST, PCM_LISTEN_PORT),
                                  daemon=True, name='ws-pcm-listener')
    ws_thread = threading.Thread(target=_ws_listener_loop,
                                 args=(WS_LISTEN_HOST, WS_LISTEN_PORT),
                                 daemon=True, name='ws-listener')
    pcm_thread.start()
    ws_thread.start()
    pcm_thread.join()
    ws_thread.join()


if __name__ == '__main__':
    main()
