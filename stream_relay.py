"""
stream_relay.py -- persistent live audio relay to Broadcastify and/or
YouTube Live (Phase 4 of the audio recording feature plan).

Deliberately independent of recording.py: this module has no import-time
dependency on app.py or on recording.py, and owns nothing but the
decode-and-fan-out pipeline for a single relay session. app.py's
background poller (_stream_relay_loop) drives it -- attaching to a
node's _AudioBroadcast, feeding WebM/Opus chunks in via Relay.feed() --
the same dependency-injection shape recording.py uses (log callback,
raw ffmpeg output args, nothing reached-back-into from app.py's
globals). If this file were deleted, recording.py and the rest of the
app would keep working unmodified.

Unlike recording.py's Recorder (one object per browser-triggered
session, torn down when the tab closes), a Relay is meant to run for as
long as the owner's config has it enabled -- app.py's poller is the one
that decides when to build and tear down a Relay, re-reading
stream_relay_config on every reconnect cycle the same way
start_aprs_poller() does.

Pipeline:
    WebM/Opus chunks (Relay.feed()) -> decode ffmpeg -> raw PCM
        -> fanned out, unmodified (no silence-trim/TTS -- a live relay
           should track real time), to each configured RelayTarget's own
           encode+push ffmpeg process (Broadcastify Icecast / YouTube
           RTMP), entirely independent of one another: one target's
           process dying or reconnecting never touches the other or the
           decode stage.
"""
import subprocess
import threading


def build_broadcastify_output_args(host, port, mount, user, password):
    """Icecast source-client output args for ffmpeg, mp3-encoded (typical
    Icecast/Broadcastify ingest expectation). Pure function -- no
    subprocess, no network -- so the URL construction is testable without
    a real Icecast server.

    -legacy_icecast 1: Broadcastify's ingest server expects the older
    Shoutcast/Icecast-<2.4-style "SOURCE" HTTP method, not the modern
    Icecast2 PUT method ffmpeg's icecast:// protocol uses by default --
    confirmed live: a bare curl PUT against a real Broadcastify mount got
    no response at all (matching ffmpeg's "End of file" failure exactly),
    while the legacy SOURCE method returned 200 OK. A local Icecast 2
    instance (used during earlier development) accepts either, so this
    wasn't caught until testing against the real target."""
    url = f"icecast://{user}:{password}@{host}:{port}{mount if mount.startswith('/') else '/' + mount}"
    return [
        "-ac", "1", "-ar", "8000",
        "-c:a", "libmp3lame", "-b:a", "64k",
        "-content_type", "audio/mpeg",
        "-legacy_icecast", "1",
        "-f", "mp3", url,
    ]


YOUTUBE_VIDEO_FPS = 25
# 2s GOP, comfortably under YouTube's documented 4s max -- libx264 defaults
# to a 250-frame GOP when -g is left unset, which at YOUTUBE_VIDEO_FPS
# works out to a 10s keyframe interval (confirmed live: YouTube reported
# exactly "10.0 seconds" before this was added).
YOUTUBE_GOP_FRAMES = YOUTUBE_VIDEO_FPS * 2


def build_youtube_output_args(rtmp_url, stream_key):
    """YouTube Live RTMP push args. Plain RTMP, no OAuth/Data API --
    the same mechanism OBS uses for "Go Live". HenWen has no camera, so
    the video track is a live waveform generated directly from the
    relayed audio itself (ffmpeg's showwaves filter) rather than a
    blank placeholder frame -- gives viewers something that actually
    reflects on-air activity, and also sidesteps the open question of
    whether YouTube's RTMP ingest accepts a truly audio-only stream
    (untested against a real account in this environment) by always
    having a genuine, non-trivial video track.

    The waveform is rendered at half width (640, not the final 1280) and
    explicitly scaled to that same size -- letting showwaves size its
    output to match the declared canvas exactly avoids a mismatch between
    how many audio samples it maps per column and the frame width, which
    otherwise left the right half of a directly-1280-wide render
    permanently blank (confirmed live). That half-width waveform is then
    split, mirrored (hflip), and stacked side by side (hstack) into the
    final 1280x720 frame -- a deliberate symmetric look, and it also means
    each half only ever has to fill its own correctly-sized 640px region
    rather than one attempt spanning the full width.

    Verified interactively via an ffmpeg-as-RTMP-server loopback: valid
    FLV with synced H.264 video (visibly non-blank frames, confirmed via
    frame extraction) + AAC audio."""
    url = rtmp_url.rstrip("/") + "/" + stream_key
    filter_complex = (
        "[0:a]aformat=channel_layouts=mono,"
        f"showwaves=s=640x720:mode=cline:rate={YOUTUBE_VIDEO_FPS}:colors=0x00cc66,"
        "scale=640:720,split=2[wa][wb];"
        "[wa]hflip[wf];"
        "[wf][wb]hstack=inputs=2[v]"
    )
    return [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "0:a",
        "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
        "-g", str(YOUTUBE_GOP_FRAMES), "-keyint_min", str(YOUTUBE_GOP_FRAMES),
        "-sc_threshold", "0",
        "-c:a", "aac", "-ar", "48000", "-b:a", "128k",
        "-f", "flv", url,
    ]


def _drain_stderr(proc, tag, log_fn):
    try:
        for raw_line in proc.stderr:
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            if line:
                log_fn(f"{tag}: {line}")
    except Exception:
        pass


class RelayTarget:
    """One push destination, entirely independent of any other target:
    owns its own ffmpeg subprocess, its own PCM-input stage, and reports
    its own connected/dead state. `output_args` is a raw ffmpeg argument
    list (see build_broadcastify_output_args/build_youtube_output_args)
    appended after the shared raw-PCM input args."""

    def __init__(self, name, output_args, log_fn=None):
        self.name = name
        self._log = log_fn or (lambda msg: None)
        self._dead = False
        self._proc = subprocess.Popen(
            ["ffmpeg", "-hide_banner", "-loglevel", "warning",
             "-f", "s16le", "-ar", "8000", "-ac", "1", "-i", "pipe:0",
             *output_args],
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
        )
        self._stderr_thread = threading.Thread(
            target=_drain_stderr, args=(self._proc, f"[STREAM-RELAY] {name}", self._log),
            daemon=True)
        self._stderr_thread.start()

    @property
    def connected(self) -> bool:
        return not self._dead and self._proc.poll() is None

    def feed(self, pcm: bytes):
        if self._dead:
            return
        try:
            self._proc.stdin.write(pcm)
        except (BrokenPipeError, ValueError, OSError):
            self._dead = True

    def stop(self):
        self._dead = True
        try:
            self._proc.stdin.close()
        except Exception:
            pass
        try:
            self._proc.wait(timeout=5)
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass


class Relay:
    """One relay session: decodes WebM/Opus (fed via .feed()) to raw PCM
    once, and fans that PCM out unmodified to every target in `targets`
    (name -> ffmpeg output args, see build_*_output_args above). No
    silence-gating or TTS splicing here -- deliberately a straight
    passthrough, since a live relay should track real time rather than
    trim dead air the way an archival recording does."""

    def __init__(self, targets: dict, log_fn=None):
        self._log = log_fn or (lambda msg: None)
        self._decode_proc = subprocess.Popen(
            # See the matching comment in recording.py's Recorder.__init__ --
            # same fast-start rationale, same fix.
            ["ffmpeg", "-hide_banner", "-loglevel", "warning",
             "-probesize", "32768", "-analyzeduration", "0",
             "-f", "webm", "-i", "pipe:0",
             "-f", "s16le", "-ar", "8000", "-ac", "1", "pipe:1"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        )
        self._targets = {
            name: RelayTarget(name, output_args, log_fn=self._log)
            for name, output_args in targets.items()
        }
        self._decode_stderr_thread = threading.Thread(
            target=_drain_stderr, args=(self._decode_proc, "[STREAM-RELAY] decode ffmpeg", self._log),
            daemon=True)
        self._decode_stderr_thread.start()
        self._pump_thread = threading.Thread(target=self._pump_loop, daemon=True, name="stream-relay-pump")
        self._pump_thread.start()

    def feed(self, chunk: bytes):
        """Called by the owning poller with each WebM chunk received from
        the relayed node's broadcast client queue."""
        try:
            self._decode_proc.stdin.write(chunk)
        except (BrokenPipeError, ValueError, OSError):
            pass

    def _pump_loop(self):
        try:
            while True:
                chunk = self._decode_proc.stdout.read(4096)
                if not chunk:
                    break
                for target in self._targets.values():
                    target.feed(chunk)
        except Exception as e:
            self._log(f"[STREAM-RELAY] pump loop error: {e}")

    def status(self) -> dict:
        return {name: {"connected": t.connected} for name, t in self._targets.items()}

    def stop(self):
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
        self._pump_thread.join(timeout=5)
        for target in self._targets.values():
            target.stop()
