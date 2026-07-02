#!/usr/bin/env python3
"""
HenWen real-time audio relay — standalone process.

Reads raw 8kHz/mono/s16le PCM from the MixMonitor FIFO and writes a strictly
paced 20ms-frame stream (silence-filled whenever the node is quiet) to a
second FIFO that ffmpeg reads directly.

This runs as its own OS process, spawned by app.py, rather than as a thread
inside the gunicorn worker. The previous in-process design shared a GIL with
Flask's request handlers, the AMI poller, and every other background thread;
any of those holding the GIL for a few milliseconds during a 20ms frame
window delayed the next write, and a delayed/dropped frame is audible as a
click or stutter. Running the pacing loop in its own process lets the kernel
schedule it independently of everything else the app is doing.

Usage: audio_relay.py <in_fifo_path> <out_fifo_path>
"""
import os
import sys
import time
import signal

FRAME_BYTES    = 320    # 20 ms at 8 kHz mono s16le (160 samples x 2 bytes)
FRAME_INTERVAL = 0.020
SILENCE_FRAME  = b'\x00' * FRAME_BYTES
STATS_INTERVAL = 5.0    # seconds between STATS lines on stderr

# DEBUG env var (set by app.py when it spawns us) enables the STATS heartbeat
# below plus a couple of one-off diagnostic lines. Left off by default since
# this loop runs once per 20ms frame and per-frame logging would itself be
# enough IO to reintroduce the timing problem this process exists to avoid.
DEBUG = os.environ.get('AUDIO_RELAY_DEBUG', '') == '1'


def _stat(msg):
    print(f'STATS {msg}', file=sys.stderr, flush=True)


def main():
    in_path, out_path = sys.argv[1], sys.argv[2]

    if DEBUG:
        _stat(f'starting pid={os.getpid()} in={in_path} out={out_path}')

    # O_RDWR on both ends: lets us open immediately without waiting for the
    # other side (Asterisk / ffmpeg) to open its end first, and prevents
    # either FIFO from ever seeing EOF.
    in_fd  = os.open(in_path,  os.O_RDWR | os.O_NONBLOCK)
    out_fd = os.open(out_path, os.O_RDWR)

    if DEBUG:
        _stat(f'both FIFOs opened in_fd={in_fd} out_fd={out_fd}')

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
    # counts scheduler-slip recoveries.
    real_frames    = 0
    silence_frames = 0
    overflows      = 0
    resyncs        = 0
    stats_deadline = time.monotonic() + STATS_INTERVAL

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
    buf           = bytearray()
    MAX_BUF_BYTES = FRAME_BYTES * 25   # ~500ms of audio before dropping oldest

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
                chunk = os.read(in_fd, 65536)
            except BlockingIOError:
                break
            except OSError as e:
                if DEBUG:
                    _stat(f'read() failed, exiting: {e!r}')
                eof = True
                break
            if not chunk:
                # Write end closed (MixMonitor stopped). With O_RDWR this
                # normally can't happen — shutdown is via SIGTERM — but
                # handle it defensively.
                if DEBUG:
                    _stat('read() returned EOF (MixMonitor stopped) — exiting')
                eof = True
                break
            buf += chunk

        # Bound latency: if the downstream reader stalled and the buffer
        # ran away, drop the oldest audio down to the cap.
        if len(buf) > MAX_BUF_BYTES:
            drop = len(buf) - MAX_BUF_BYTES
            del buf[:drop]
            overflows += 1
            if DEBUG:
                _stat(f'buffer overflow: dropped {drop} byte(s) of oldest audio')

        if len(buf) >= FRAME_BYTES:
            frame = bytes(buf[:FRAME_BYTES])
            del buf[:FRAME_BYTES]
            real_frames += 1
        else:
            frame = SILENCE_FRAME       # genuine underrun — buffer is empty
            silence_frames += 1

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
                  f'resyncs={resyncs} buf={len(buf)}B drift={(now - deadline):+.3f}s')
            stats_deadline = now + STATS_INTERVAL

    if DEBUG:
        _stat(f'exiting: real_frames={real_frames} silence_frames={silence_frames} '
              f'overflows={overflows} resyncs={resyncs}')

    for fd in (in_fd, out_fd):
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == '__main__':
    main()
