#!/usr/bin/env python3
"""
HenWen real-time audio relay — standalone process.

Reads raw 8kHz/mono/s16le PCM from either a MixMonitor FIFO or an AudioSocket
TCP connection and writes a strictly paced 20ms-frame stream (silence-filled
whenever the node is quiet) to a second FIFO that ffmpeg reads directly.

This runs as its own OS process, spawned by app.py, rather than as a thread
inside the gunicorn worker. The previous in-process design shared a GIL with
Flask's request handlers, the AMI poller, and every other background thread;
any of those holding the GIL for a few milliseconds during a 20ms frame
window delayed the next write, and a delayed/dropped frame is audible as a
click or stutter. Running the pacing loop in its own process lets the kernel
schedule it independently of everything else the app is doing.

Input modes (first argv):
  <path>    legacy: a MixMonitor FIFO, opened O_RDWR|O_NONBLOCK.
  tcp:<port>  AudioSocket: listens on 127.0.0.1:<port> for the one inbound
              connection Asterisk's AudioSocket() app makes (see
              app.py's _start_broadcast() and audiosocket-tap/ for how that
              connection gets the node's audio onto it). Added to eliminate
              MixMonitor's own ~2s buffered-stdio-write latency (issue #30)
              -- everything downstream of "raw PCM bytes appended to buf"
              (jitter buffer, pacing, declick fades) is identical between
              the two modes; only how bytes get into `buf` differs.

Usage: audio_relay.py <in_spec> <out_fifo_path>
"""
import os
import sys
import time
import signal
import struct
import array
import socket

FRAME_BYTES    = 320    # 20 ms at 8 kHz mono s16le (160 samples x 2 bytes)
FRAME_INTERVAL = 0.020
SILENCE_FRAME  = b'\x00' * FRAME_BYTES
STATS_INTERVAL = 5.0    # seconds between STATS lines on stderr

# Length of the declick ramp applied at a discontinuity (real<->silence
# transition, or an overflow-drop splice) — see _fade_frame() below. 40
# samples = 5ms at 8kHz: long enough to turn a hard sample-value jump into an
# inaudible ramp, short enough that the ramp itself is never perceptible as
# its own artifact. Applied only at the rare frames where a discontinuity is
# known to exist, not on every frame, so the added cost is a handful of
# float multiplies a few times a second at most — negligible even on a Pi
# Zero 2 W.
FADE_SAMPLES = 40
_NATIVE_LE   = sys.byteorder == 'little'


def _fade_frame(frame_bytes, start_sample, n=FADE_SAMPLES):
    """Blend the first `n` samples of `frame_bytes` from `start_sample`
    (the last sample actually written to out_fd) into the frame's own
    content, linearly. Used to smooth over a point where the output is not
    a sample-continuous extension of what was just emitted:

      - a genuine underrun (real audio -> silence): the silence frame is
        all zero, so this ramps start_sample down to ~0 instead of an
        instant drop.
      - recovery from an underrun (silence -> real audio): start_sample is
        ~0, so this ramps up into the real frame's content instead of an
        instant jump.
      - an overflow drop (oldest audio discarded to bound latency): both
        sides are real audio, but no longer contiguous samples, so this
        smooths the splice the same way.

    Without this, any of the above is a hard sample-value discontinuity —
    audible as a click or pop. frame_bytes is always exactly FRAME_BYTES
    long (a full real slice or a copy of SILENCE_FRAME), so the return
    value is too."""
    samples = array.array('h')
    samples.frombytes(frame_bytes)
    if not _NATIVE_LE:
        samples.byteswap()
    n = min(n, len(samples))
    for i in range(n):
        t = (i + 1) / (n + 1)
        samples[i] = int(start_sample * (1 - t) + samples[i] * t)
    if not _NATIVE_LE:
        samples.byteswap()
    return samples.tobytes()

# DEBUG env var (set by app.py when it spawns us) enables the STATS heartbeat
# below plus a couple of one-off diagnostic lines. Left off by default since
# this loop runs once per 20ms frame and per-frame logging would itself be
# enough IO to reintroduce the timing problem this process exists to avoid.
DEBUG = os.environ.get('AUDIO_RELAY_DEBUG', '') == '1'


def _stat(msg):
    print(f'STATS {msg}', file=sys.stderr, flush=True)


class _AudioSocketReader:
    """Pulls raw PCM out of an AudioSocket TCP connection and hands it to
    the caller with the same calling convention as os.read(fd, n): raises
    BlockingIOError when nothing new is parseable yet, returns b'' on a
    clean hangup, lets a genuine socket error propagate as OSError. This
    lets main()'s read loop stay identical between FIFO and AudioSocket
    input modes -- only what backs `read_input` differs.

    Wire format (https://docs.asterisk.org/Configuration/Channel-Driver/
    AudioSocket/, confirmed empirically against a live Asterisk 22/ASL3
    instance since the spec alone wasn't enough to trust blindly on a live
    repeater's audio path): a 3-byte header (1-byte kind, 2-byte big-endian
    length) followed by that many payload bytes. kind 0x01 is a 16-byte
    UUID sent once right after connect; kind 0x10 is audio (payload was
    measured at exactly 320 bytes -- one 20ms slin8 frame, matching
    FRAME_BYTES exactly, so no reframing is needed); kind 0x00 is a
    terminate/hangup with no payload.
    """
    KIND_TERMINATE = 0x00
    KIND_UUID      = 0x01
    KIND_AUDIO     = 0x10

    def __init__(self, port):
        # Loopback only -- AudioSocket has no auth, so this must never be
        # reachable from anywhere but this same box's Asterisk instance.
        # port=0 lets the OS pick a free port (the caller, app.py, doesn't
        # know a port in advance and needs to read the actual bound one
        # back -- see accept()'s "PORT " stdout line in main() below --
        # before it can Originate the call that connects here).
        self._srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._srv.bind(('127.0.0.1', port))
        self._srv.listen(1)
        self.port = self._srv.getsockname()[1]
        self.conn = None
        self._buf      = bytearray()
        self._hungup   = False
        self._got_uuid = False

    def accept(self, timeout=10.0):
        """Block until Asterisk's AudioSocket() connects, or raise
        socket.timeout if nothing does within `timeout` seconds (the
        caller, app.py, treats that as "feature unavailable, fall back to
        MixMonitor")."""
        self._srv.settimeout(timeout)
        try:
            self.conn, _addr = self._srv.accept()
        finally:
            self._srv.close()
        self.conn.setblocking(False)

    def read(self, _n=65536):
        if self._hungup:
            return b''
        try:
            chunk = self.conn.recv(65536)
        except BlockingIOError:
            chunk = None
        if chunk == b'':
            self._hungup = True
            return b''
        if chunk:
            self._buf += chunk

        out = bytearray()
        while len(self._buf) >= 3:
            kind   = self._buf[0]
            length = (self._buf[1] << 8) | self._buf[2]
            if len(self._buf) < 3 + length:
                break   # incomplete packet -- wait for more to arrive
            payload = bytes(self._buf[3:3 + length])
            del self._buf[:3 + length]
            if kind == self.KIND_AUDIO:
                out += payload
            elif kind == self.KIND_TERMINATE:
                self._hungup = True
                break
            elif kind == self.KIND_UUID and DEBUG and not self._got_uuid:
                self._got_uuid = True
                _stat(f'AudioSocket UUID packet: {payload.hex()}')
        if out:
            return bytes(out)
        if self._hungup:
            return b''
        raise BlockingIOError()

    def close(self):
        try:
            self.conn.close()
        except OSError:
            pass


def main():
    in_spec, out_path = sys.argv[1], sys.argv[2]

    if DEBUG:
        _stat(f'starting pid={os.getpid()} in={in_spec} out={out_path}')

    in_fd  = None
    reader = None
    if in_spec.startswith('tcp:'):
        # Handshake with app.py over stdout (never used again after this,
        # since ordinary operation only ever logs to stderr): "PORT n" as
        # soon as the listen socket is bound, so app.py can pass it to the
        # AMI Originate call that makes Asterisk connect here, then
        # "CONNECTED" once that connection actually completes. app.py
        # reads both with a bounded timeout and falls back to MixMonitor
        # if either is missing or late -- see app.py's _try_audiosocket_tap().
        reader = _AudioSocketReader(int(in_spec[len('tcp:'):]))
        print(f'PORT {reader.port}', flush=True)
        reader.accept()
        print('CONNECTED', flush=True)
        read_input = reader.read
        if DEBUG:
            _stat(f'AudioSocket connection accepted on 127.0.0.1:{reader.port}')
    else:
        # O_RDWR: lets us open immediately without waiting for the other
        # side (MixMonitor) to open its end first, and prevents the FIFO
        # from ever seeing EOF.
        in_fd = os.open(in_spec, os.O_RDWR | os.O_NONBLOCK)
        read_input = lambda: os.read(in_fd, 65536)

    # O_RDWR: lets us open immediately without waiting for the other side
    # (ffmpeg) to open its end first, and prevents the FIFO from ever
    # seeing EOF.
    out_fd = os.open(out_path, os.O_RDWR)

    if DEBUG:
        _stat(f'input ready, out FIFO opened out_fd={out_fd}')

    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False
        if DEBUG:
            _stat(f'received signal {signum}, stopping')

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    # Running counters for the periodic heartbeat below. real_frames are
    # frames emitted from buffered MixMonitor audio; silence_frames were
    # injected on a genuine buffer underrun (nothing left to send);
    # overflows counts how many times the jitter buffer exceeded its cap
    # and we dropped the oldest audio to keep latency bounded; resyncs
    # counts scheduler-slip recoveries; fades counts declick ramps applied
    # (see _fade_frame) — a real<->silence transition or an overflow splice.
    real_frames    = 0
    silence_frames = 0
    overflows      = 0
    resyncs        = 0
    fades          = 0
    stats_deadline = time.monotonic() + STATS_INTERVAL

    # Declick state: the last sample actually written to out_fd, and whether
    # that frame was real audio or injected silence — used by _fade_frame to
    # smooth the next frame if it turns out to be a discontinuity. fade_pending
    # is set by the overflow-drop path below, since that's a real->real splice
    # that the real/silence transition check alone wouldn't catch.
    last_sample       = 0
    prev_frame_is_real = False
    fade_pending       = False

    # Jitter buffer. MixMonitor delivers PCM in bursts, not a smooth 20ms
    # trickle, so a per-slot "is there exactly one frame readable right
    # now?" decision splices silence into the middle of continuous audio
    # (and padding a partial read misaligns the 16-bit sample framing) —
    # both audible as clicks/pops. Instead we accumulate everything that
    # arrives into this buffer and emit exactly one *sample-aligned* 20ms
    # frame per slot, so frame boundaries always fall on real sample
    # boundaries and silence is only ever sent on a true underrun, never
    # spliced into ongoing speech. The buffer is capped so a stalled
    # downstream reader can't build unbounded latency.
    # Asterisk writes MixMonitor audio to the FIFO through a buffered stdio
    # stream that flushes in ~32KB lumps — about 2 seconds of 8kHz s16le at a
    # time, not a 20ms trickle. The buffer must hold at least one full lump or
    # the excess gets dropped here, which audibly chops ~1.5s out of every 2s
    # of speech (silence-padded by the underrun branch below). 4s of headroom
    # absorbs a full lump plus arrival jitter; the cap now only guards against
    # a genuinely runaway backlog (e.g. downstream reader stalled for many
    # seconds), at the cost of up to ~2s of pipeline latency, which the
    # browser player's live-edge controller already accounts for.
    buf           = bytearray()
    MAX_BUF_BYTES = FRAME_BYTES * 200  # ~4s of audio before dropping oldest

    deadline = time.monotonic()
    while running:
        deadline += FRAME_INTERVAL
        now  = time.monotonic()
        wait = deadline - now
        # If the scheduler slipped and we're more than two frames behind,
        # reset the frame clock instead of bursting writes to catch up (a
        # burst would be audible as a lump).
        if wait < -(FRAME_INTERVAL * 2):
            deadline = now + FRAME_INTERVAL
            wait     = FRAME_INTERVAL
            resyncs += 1
            if DEBUG:
                _stat('resync: scheduler slip, resetting frame clock')

        # Wait out the rest of this frame's slot, then drain whatever
        # arrived during the sleep. Emission stays locked to one frame per
        # FRAME_INTERVAL regardless of how bursty the source is; the buffer
        # (not the playback rate) absorbs the jitter.
        if wait > 0:
            time.sleep(wait)

        # Drain everything currently readable into the buffer — a slot may
        # bring zero, one, or several frames' worth after a bursty write.
        eof = False
        while True:
            try:
                chunk = read_input()
            except BlockingIOError:
                break
            except OSError as e:
                if DEBUG:
                    _stat(f'read() failed, exiting: {e!r}')
                eof = True
                break
            if not chunk:
                # Write end closed (MixMonitor stopped) or the AudioSocket
                # peer hung up. With O_RDWR the FIFO case normally can't
                # happen — shutdown is via SIGTERM — but handle it
                # defensively; for AudioSocket this is the normal way a
                # broadcast's teardown (shutdown() hanging up the
                # originated channel) ends this process's input side.
                if DEBUG:
                    _stat('read() returned EOF (input closed) — exiting')
                eof = True
                break
            buf += chunk

        # Bound latency: if the downstream reader stalled and the buffer
        # ran away, drop the oldest audio down to the cap.
        if len(buf) > MAX_BUF_BYTES:
            drop = len(buf) - MAX_BUF_BYTES
            del buf[:drop]
            overflows += 1
            fade_pending = True    # real audio, but no longer contiguous — declick the splice
            if DEBUG:
                _stat(f'buffer overflow: dropped {drop} byte(s) of oldest audio')

        frame_is_real = len(buf) >= FRAME_BYTES
        if frame_is_real:
            frame = bytearray(buf[:FRAME_BYTES])
            del buf[:FRAME_BYTES]
            real_frames += 1
        else:
            frame = bytearray(SILENCE_FRAME)   # genuine underrun — buffer is empty
            silence_frames += 1

        # A real<->silence transition or an overflow splice means this frame
        # isn't a sample-continuous extension of the last one written — ramp
        # from last_sample into this frame's own content instead of emitting
        # the hard jump raw (audible as a click/pop). See _fade_frame().
        if fade_pending or frame_is_real != prev_frame_is_real:
            frame = _fade_frame(frame, last_sample)
            fade_pending = False
            fades += 1
        prev_frame_is_real = frame_is_real
        last_sample = struct.unpack_from('<h', frame, FRAME_BYTES - 2)[0]

        try:
            os.write(out_fd, frame)
        except OSError as e:
            if DEBUG:
                _stat(f'write() failed, exiting: {e!r}')
            break

        if eof:
            break

        if DEBUG and now >= stats_deadline:
            total = real_frames + silence_frames
            pct_silence = (100.0 * silence_frames / total) if total else 0.0
            _stat(f'frames real={real_frames} silence={silence_frames} '
                  f'({pct_silence:.1f}% silence) overflows={overflows} '
                  f'resyncs={resyncs} fades={fades} buf={len(buf)}B '
                  f'drift={(now - deadline):+.3f}s')
            stats_deadline = now + STATS_INTERVAL

    if DEBUG:
        _stat(f'exiting: real_frames={real_frames} silence_frames={silence_frames} '
              f'overflows={overflows} resyncs={resyncs} fades={fades}')

    if reader is not None:
        reader.close()
    for fd in (in_fd, out_fd):
        if fd is None:
            continue
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == '__main__':
    main()
