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
    # frames actually read from MixMonitor; silence_frames are frames we
    # injected because nothing arrived within the 20ms window; resyncs
    # counts how many times we fell more than 2 frames behind and gave up
    # trying to catch up.
    real_frames    = 0
    silence_frames = 0
    short_reads    = 0
    resyncs        = 0
    stats_deadline = time.monotonic() + STATS_INTERVAL

    deadline = time.monotonic()
    while running:
        deadline += FRAME_INTERVAL
        now  = time.monotonic()
        wait = deadline - now
        # If we've fallen more than two frames behind, resync instead of
        # bursting writes to catch up (a burst would be audible as a lump).
        if wait < -(FRAME_INTERVAL * 2):
            deadline = now + FRAME_INTERVAL
            wait     = FRAME_INTERVAL
            resyncs += 1
            if DEBUG:
                _stat(f'resync: fell behind by {(now - deadline):.3f}s')

        # Always wait out the rest of this frame's slot before consuming
        # anything, even if data is already sitting in the FIFO. select()
        # returns the instant a fd is readable — if we read as soon as it
        # does, a backlog (MixMonitor writing in bursts rather than a
        # smooth 20ms trickle) gets drained as fast as the CPU allows
        # instead of at the paced rate, which plays the audio back sped
        # up. Sleeping first, then doing a non-blocking check for what
        # arrived during the sleep, keeps consumption locked to one frame
        # per FRAME_INTERVAL regardless of how bursty the source is; any
        # backlog just sits in the kernel FIFO buffer instead.
        if wait > 0:
            time.sleep(wait)

        try:
            data = os.read(in_fd, FRAME_BYTES)
            if not data:
                if DEBUG:
                    _stat('read() returned EOF, write end closed '
                          '(MixMonitor stopped) — exiting')
                break   # write end closed (MixMonitor stopped)
            if len(data) < FRAME_BYTES:
                short_reads += 1
                data += SILENCE_FRAME[len(data):]
            real_frames += 1
        except BlockingIOError:
            data = SILENCE_FRAME       # nothing arrived within the window
            silence_frames += 1
        except OSError as e:
            if DEBUG:
                _stat(f'read() failed, exiting: {e!r}')
            break

        try:
            os.write(out_fd, data)
        except OSError as e:
            if DEBUG:
                _stat(f'write() failed, exiting: {e!r}')
            break

        if DEBUG and now >= stats_deadline:
            total = real_frames + silence_frames
            pct_silence = (100.0 * silence_frames / total) if total else 0.0
            _stat(f'frames real={real_frames} silence={silence_frames} '
                  f'({pct_silence:.1f}% silence) short_reads={short_reads} '
                  f'resyncs={resyncs} drift={(now - deadline):+.3f}s')
            stats_deadline = now + STATS_INTERVAL

    if DEBUG:
        _stat(f'exiting: real_frames={real_frames} silence_frames={silence_frames} '
              f'short_reads={short_reads} resyncs={resyncs}')

    for fd in (in_fd, out_fd):
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == '__main__':
    main()
