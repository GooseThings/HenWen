"""
recording.py -- in-browser audio recording (Phase 2-3 of the audio
recording feature plan).

Deliberately independent of stream_relay.py: this module owns nothing
but the per-recording capture pipeline and has no import-time dependency
on app.py or on stream_relay.py. app.py's routes drive it entirely
through the small surface below (Recorder, the retention/cap pure
functions), passing in whatever context (dest path, config values, a
logging callback) it needs rather than this module reaching back into
app.py's globals. If this file were deleted, stream_relay.py and the
rest of the app would keep working unmodified.

Pipeline per active recording, fed by Recorder.feed() as WebM/Opus
chunks arrive from the node's existing _AudioBroadcast (app.py) --
attached the same way a browser listener is, via add_client(), so
audio_relay.py and the raw-PCM FIFO plumbing are never touched:

    WebM/Opus chunks -> decode ffmpeg -> raw PCM -> SilenceGate
        (-> Phase 3 TTS splice, see recording_config.tts_* fields)
        -> encode ffmpeg -> file on disk

Runs as plain daemon threads, not a separate OS process the way
audio_relay.py's pacing loop does -- unlike that 20ms real-time
deadline, nothing here has a hard timing requirement to protect from
GIL jitter.
"""
import struct
import subprocess
import threading
import time

FRAME_MS = 20
FRAME_BYTES = 320  # 8000 Hz * 0.02s * 2 bytes/sample, s16le mono


def frame_rms(frame: bytes) -> float:
    """RMS of a 16-bit signed little-endian mono PCM frame. 0.0 for an
    empty or odd-length trailing scrap (e.g. the final partial frame at
    EOF) rather than raising, since callers just want a silence signal."""
    n = len(frame) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", frame[:n * 2])
    return (sum(s * s for s in samples) / n) ** 0.5


class SilenceGate:
    """Stateful per-frame pass/drop decision: speech passes through
    untouched, and any continuous run of near-silence is capped at
    min_gap_ms of retained dead air rather than either playing out in
    full or being cut with a hard, instant edge -- a short natural pause
    survives; a multi-minute quiet stretch gets trimmed down to that
    ceiling on each end."""

    def __init__(self, rms_thresh: int, min_gap_ms: int, frame_ms: int = FRAME_MS):
        self.rms_thresh = rms_thresh
        self.max_silent_frames = max(1, min_gap_ms // frame_ms)
        self._silent_run = 0

    def should_pass(self, frame: bytes) -> bool:
        if frame_rms(frame) >= self.rms_thresh:
            self._silent_run = 0
            return True
        self._silent_run += 1
        return self._silent_run <= self.max_silent_frames


def select_purge_by_age(rows, now_ts: float, retention_days: float):
    """rows: iterable of {'id', 'started_at'} (started_at = epoch seconds).
    Returns ids whose age exceeds retention_days. Pure function -- no DB
    or filesystem access -- so the janitor's age-based sweep is testable
    without touching either."""
    cutoff = now_ts - retention_days * 86400
    return [r["id"] for r in rows if r["started_at"] < cutoff]


def select_purge_by_cap(rows, cap_bytes: float):
    """rows: iterable of {'id', 'size_bytes', 'started_at'}. Deletes
    oldest-first until total size is at or under cap_bytes. Returns the
    list of ids to delete, oldest first."""
    ordered = sorted(rows, key=lambda r: r["started_at"])
    total = sum(r["size_bytes"] for r in ordered)
    to_delete = []
    for r in ordered:
        if total <= cap_bytes:
            break
        to_delete.append(r["id"])
        total -= r["size_bytes"]
    return to_delete


def select_purge_by_user_cap(rows, cap_bytes: float):
    """rows: iterable of {'id', 'user_id', 'size_bytes', 'started_at'}.
    Applies select_purge_by_cap independently per user_id, so one user
    being over their cap never causes another user's recordings to be
    swept."""
    by_user = {}
    for r in rows:
        by_user.setdefault(r["user_id"], []).append(r)
    ids = []
    for user_rows in by_user.values():
        ids.extend(select_purge_by_cap(user_rows, cap_bytes))
    return ids


def _drain_stderr(proc, tag, log_fn):
    try:
        for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                log_fn(f"{tag}: {line}")
    except Exception:
        pass


class Recorder:
    """
    Captures one node's live audio to a file for as long as this object
    is alive. The caller is responsible for attaching it to the node's
    _AudioBroadcast (app.py) via add_client() and calling .feed() with
    each chunk received from that client queue -- this class has no
    knowledge of _AudioBroadcast itself, only of the WebM/Opus bytes it
    's handed.

    on_stop(reason: str, elapsed_sec: float) is called at most once, from
    whichever thread triggers the stop (the watchdog on max-duration, or
    the caller's own thread on manual/connection-closed stop) -- never
    from inside a lock held by the caller, so it's safe for on_stop to
    touch the database.
    """

    def __init__(self, node, dest_path, output_format, silence_rms_thresh,
                 silence_min_gap_ms, max_duration_sec, on_stop=None, log_fn=None):
        self.node = node
        self.dest_path = dest_path
        self.max_duration_sec = max_duration_sec
        self._log = log_fn or (lambda msg: None)
        self._gate = SilenceGate(silence_rms_thresh, silence_min_gap_ms)
        self._on_stop = on_stop
        self._stop_reason = None
        self._started_at = time.monotonic()
        self._lock = threading.Lock()
        self._stopped = False

        self._decode_proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "warning",
             "-f", "webm", "-i", "pipe:0",
             "-f", "s16le", "-ar", "8000", "-ac", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        if output_format == "mp3":
            codec_args = ["-c:a", "libmp3lame", "-b:a", "64k"]
        else:
            codec_args = ["-c:a", "libopus", "-b:a", "32k"]
        self._encode_proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
             "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
             *codec_args, dest_path],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )

        self._pump_thread = threading.Thread(
            target=self._pump_loop, daemon=True, name=f"recorder-pump-{node}")
        self._decode_stderr_thread = threading.Thread(
            target=_drain_stderr, args=(self._decode_proc, f"[RECORDING] decode ffmpeg [{node}]", self._log),
            daemon=True)
        self._encode_stderr_thread = threading.Thread(
            target=_drain_stderr, args=(self._encode_proc, f"[RECORDING] encode ffmpeg [{node}]", self._log),
            daemon=True)
        self._watchdog_thread = threading.Thread(
            target=self._watchdog_loop, daemon=True, name=f"recorder-watchdog-{node}")

        self._pump_thread.start()
        self._decode_stderr_thread.start()
        self._encode_stderr_thread.start()
        self._watchdog_thread.start()

    def feed(self, chunk: bytes):
        """Called by the owning route's request thread with each WebM
        chunk received from the node's broadcast client queue."""
        try:
            self._decode_proc.stdin.write(chunk)
        except (BrokenPipeError, ValueError, OSError):
            pass

    def _pump_loop(self):
        buf = b""
        try:
            while True:
                chunk = self._decode_proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                while len(buf) >= FRAME_BYTES:
                    frame, buf = buf[:FRAME_BYTES], buf[FRAME_BYTES:]
                    if self._gate.should_pass(frame):
                        try:
                            self._encode_proc.stdin.write(frame)
                        except (BrokenPipeError, ValueError, OSError):
                            return
        except Exception as e:
            self._log(f"[RECORDING] pump loop error for node {self.node}: {e}")

    def _watchdog_loop(self):
        while True:
            time.sleep(1.0)
            with self._lock:
                if self._stopped:
                    return
            if time.monotonic() - self._started_at >= self.max_duration_sec:
                self.stop("max_duration")
                return

    @property
    def elapsed_sec(self) -> float:
        return time.monotonic() - self._started_at

    @property
    def stop_reason(self):
        return self._stop_reason

    def stop(self, reason="manual"):
        with self._lock:
            if self._stopped:
                return
            self._stopped = True
            self._stop_reason = reason
        elapsed = self.elapsed_sec

        try:
            self._decode_proc.stdin.close()
        except Exception:
            pass
        try:
            self._decode_proc.wait(timeout=5)
        except Exception:
            try:
                self._decode_proc.kill()
            except Exception:
                pass
        # _pump_loop exits on its own once decode stdout hits EOF (the
        # decode process closing after the .stdin.close() above); give it
        # a moment to flush the last frames through before closing the
        # encoder's stdin behind it.
        self._pump_thread.join(timeout=5)
        try:
            self._encode_proc.stdin.close()
        except Exception:
            pass
        try:
            self._encode_proc.wait(timeout=10)
        except Exception:
            try:
                self._encode_proc.kill()
            except Exception:
                pass

        self._log(f"[RECORDING] stopped node={self.node} reason={reason} "
                   f"elapsed={elapsed:.1f}s dest={self.dest_path}")
        if self._on_stop:
            try:
                self._on_stop(reason, elapsed)
            except Exception as e:
                self._log(f"[RECORDING] on_stop callback error: {e}")
