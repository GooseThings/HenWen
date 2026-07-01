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
import select
import signal

FRAME_BYTES    = 320    # 20 ms at 8 kHz mono s16le (160 samples x 2 bytes)
FRAME_INTERVAL = 0.020
SILENCE_FRAME  = b'\x00' * FRAME_BYTES


def main():
    in_path, out_path = sys.argv[1], sys.argv[2]

    # O_RDWR on both ends: lets us open immediately without waiting for the
    # other side (Asterisk / ffmpeg) to open its end first, and prevents
    # either FIFO from ever seeing EOF.
    in_fd  = os.open(in_path,  os.O_RDWR | os.O_NONBLOCK)
    out_fd = os.open(out_path, os.O_RDWR)

    running = True

    def _stop(signum, frame):
        nonlocal running
        running = False

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

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

        try:
            r, _, _ = select.select([in_fd], [], [], max(0.0, wait))
        except (OSError, ValueError):
            break

        if r:
            try:
                data = os.read(in_fd, FRAME_BYTES)
                if not data:
                    break   # write end closed (MixMonitor stopped)
                if len(data) < FRAME_BYTES:
                    data += SILENCE_FRAME[len(data):]
            except BlockingIOError:
                data = SILENCE_FRAME   # spurious select() wakeup
            except OSError:
                break
        else:
            data = SILENCE_FRAME       # nothing arrived within the window

        try:
            os.write(out_fd, data)
        except OSError:
            break

    for fd in (in_fd, out_fd):
        try:
            os.close(fd)
        except OSError:
            pass


if __name__ == '__main__':
    main()
