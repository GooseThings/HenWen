"""Tests for stream_relay.py's URL/args construction (pure functions, no
network or subprocess). The Relay/RelayTarget classes that actually run
ffmpeg were verified interactively against a real local Icecast instance
and an ffmpeg-as-RTMP-server loopback during development (see the
plan's Phase 4 verification notes) rather than in the automated suite,
matching this repo's policy of not depending on external services or
long-lived local servers from pytest.

TestRelayDeadDetection is the one exception: it mocks subprocess.Popen
entirely (no real ffmpeg spawned) to cover Relay's dead/alive bookkeeping,
which is pure Python control flow around a process's lifetime rather than
anything that needs real ffmpeg behavior to verify.
"""
import io
import threading
import time
from unittest.mock import MagicMock, patch

import stream_relay


class TestBuildBroadcastifyOutputArgs:
    def test_builds_icecast_url_with_credentials(self):
        args = stream_relay.build_broadcastify_output_args(
            host="audio.broadcastify.com", port=8000, mount="/mymount",
            user="source", password="s3cret")
        assert args[-1] == "icecast://source:s3cret@audio.broadcastify.com:8000/mymount"

    def test_mount_without_leading_slash_gets_one_added(self):
        args = stream_relay.build_broadcastify_output_args(
            host="host", port=8000, mount="mymount", user="u", password="p")
        assert args[-1] == "icecast://u:p@host:8000/mymount"

    def test_uses_mp3_encoding_for_icecast_compatibility(self):
        args = stream_relay.build_broadcastify_output_args(
            host="h", port=1, mount="/m", user="u", password="p")
        assert "-c:a" in args
        assert args[args.index("-c:a") + 1] == "libmp3lame"
        assert "-f" in args
        assert args[args.index("-f") + 1] == "mp3"

    def test_uses_legacy_icecast_source_method(self):
        # Broadcastify's ingest server only accepts the older Shoutcast/
        # Icecast<2.4-style SOURCE method, not ffmpeg's default modern PUT
        # -- confirmed against a real Broadcastify mount (see module
        # docstring in build_broadcastify_output_args).
        args = stream_relay.build_broadcastify_output_args(
            host="h", port=1, mount="/m", user="u", password="p")
        assert args[args.index("-legacy_icecast") + 1] == "1"

    def test_sets_an_rw_timeout_so_a_dead_connection_is_noticed(self):
        # Without this, a connection that goes silent mid-stream (internet
        # drops, the far end stops reading) can block on a write for
        # minutes before the OS's own TCP timeouts eventually notice --
        # during which app.py's per-target dead-connection check has
        # nothing to detect, since the ffmpeg process is still technically
        # running, just stuck. Confirmed live against a black-holed
        # address that this option actually bounds connection/write time
        # instead of being silently ignored.
        args = stream_relay.build_broadcastify_output_args(
            host="h", port=1, mount="/m", user="u", password="p")
        assert args[args.index("-rw_timeout") + 1] == str(stream_relay.RELAY_RW_TIMEOUT_USEC)


class TestBuildYoutubeOutputArgs:
    def test_builds_rtmp_url_with_stream_key(self):
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://a.rtmp.youtube.com/live2", stream_key="abcd-1234-efgh")
        assert args[-1] == "rtmp://a.rtmp.youtube.com/live2/abcd-1234-efgh"

    def test_trailing_slash_on_rtmp_url_does_not_double_up(self):
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://a.rtmp.youtube.com/live2/", stream_key="abcd-1234")
        assert args[-1] == "rtmp://a.rtmp.youtube.com/live2/abcd-1234"

    def test_includes_a_live_waveform_video_track(self):
        # YouTube Live's RTMP ingest is not reliably known to accept
        # audio-only input (untested against a real account), and HenWen
        # has no camera -- the video track is a waveform generated from
        # the relayed audio itself via showwaves, rather than a blank
        # placeholder or gambling on audio-only being accepted.
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        assert any("showwaves" in a for a in args)
        assert "[0:a]" in args[args.index("-filter_complex") + 1]
        video_label = args[args.index("-map") + 1]
        assert video_label in ("[v0]", "[vout]")

    def test_text_overlay_included_when_a_font_is_available(self, monkeypatch):
        monkeypatch.setattr(stream_relay, "_overlay_font", lambda: "/fake/font.ttf")
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        fc = args[args.index("-filter_complex") + 1]
        assert "drawtext" in fc
        assert stream_relay.STATION_OVERLAY_FILE in fc
        assert stream_relay.CLOCK_OVERLAY_FILE in fc
        assert stream_relay.WEBSITE_OVERLAY_FILE in fc
        assert stream_relay.TICKER_OVERLAY_FILE in fc
        assert args[args.index("-map") + 1] == "[vout]"

    def test_falls_back_to_plain_waveform_when_no_font_available(self, monkeypatch):
        # A system missing every candidate font must not fail the whole
        # relay -- just skip the text overlay.
        monkeypatch.setattr(stream_relay, "_overlay_font", lambda: None)
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        fc = args[args.index("-filter_complex") + 1]
        assert "drawtext" not in fc
        assert args[args.index("-map") + 1] == "[v0]"
        assert "-c:v" in args
        # Output muxer is the last "-f flv", right before the destination URL.
        assert args[-3:-1] == ["-f", "flv"]

    def test_waveform_is_rendered_at_half_width_and_mirrored(self):
        # Rendering directly at the final 1280px width left the right half
        # permanently blank (confirmed live) -- half-width render + scale
        # to lock in a correct fill, then split/hflip/hstack to mirror it
        # into the full-width frame.
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        fc = args[args.index("-filter_complex") + 1]
        assert "showwaves=s=640x720" in fc
        assert "scale=640:720" in fc
        assert "hflip" in fc
        assert "hstack" in fc

    def test_ambient_background_is_composited_behind_the_waveform(self):
        # Rendered small (YOUTUBE_BG_SMALL_SIZE) and upscaled rather than
        # generated at full 1280x720 directly -- a full-res "gradients"
        # render was measured locally to run *below* real-time on this
        # pipeline, nowhere near "not too CPU intensive". Composited via
        # a cheap `screen` blend (colorkey+overlay was measured ~2x
        # slower) so it never has to be perfectly keyed, and dimmed
        # afterwards so it can't compete with the waveform.
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        fc = args[args.index("-filter_complex") + 1]
        assert f"gradients=s={stream_relay.YOUTUBE_BG_SMALL_SIZE}" in fc
        assert "scale=1280:720" in fc
        assert "blend=all_mode=screen" in fc
        assert "colorchannelmixer" in fc

    def test_background_is_a_frozen_single_frame_not_regenerated_per_frame(self):
        # Two cellular-automaton attempts came before this: Conway's
        # classic rule went near-static within about a minute regardless
        # of tuning, and the fix for that (the "Seeds" rule) looked like
        # noise since it evolves a full new generation every frame.
        # Beyond either specific problem, the pattern itself wasn't
        # wanted -- this generates one gradient frame and freezes it
        # (trim to a single frame, then `loop` replays it) rather than
        # computing a new pattern every tick, which is also markedly
        # cheaper: libx264 sees an almost-unchanged frame most ticks.
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        fc = args[args.index("-filter_complex") + 1]
        assert "trim=start_frame=0:end_frame=1" in fc
        assert "loop=loop=-1:size=1" in fc

    def test_background_hue_oscillates_within_a_small_range(self):
        # hue= rotates the *existing* pixel hue by the given number of
        # degrees, it's not an absolute target -- an earlier version used
        # the intended target hue directly here and it sent the base teal
        # into dark maroon instead (confirmed visually). The swing must
        # stay a small offset around 0 so the color only ever drifts
        # between blue and green, never anything that would clash with
        # the waveform's own bright green.
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        fc = args[args.index("-filter_complex") + 1]
        swing = stream_relay.YOUTUBE_BG_HUE_SWING_DEG
        cycle = stream_relay.YOUTUBE_BG_HUE_CYCLE_SEC
        assert f"hue=h={swing}*sin(2*PI*t/{cycle})" in fc
        assert 0 < swing <= 30, "a swing this large starts drifting toward clashing colors"

    def test_keyframe_interval_is_set_explicitly(self):
        # Without -g, libx264 defaults to a 250-frame GOP -- at this
        # module's declared waveform framerate that's a 10s keyframe
        # interval, over YouTube's documented 4s max (confirmed live).
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        assert "-g" in args
        gop = int(args[args.index("-g") + 1])
        assert gop == stream_relay.YOUTUBE_GOP_FRAMES
        seconds = gop / stream_relay.YOUTUBE_VIDEO_FPS
        assert seconds <= 4
        assert "-keyint_min" in args
        assert "-sc_threshold" in args

    def test_audio_bitrate_and_sample_rate(self):
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        assert args[args.index("-b:a") + 1] == "128k"
        assert args[args.index("-ar") + 1] == "48000"

    def test_sets_an_rw_timeout_so_a_dead_connection_is_noticed(self):
        # Same rationale as the Broadcastify target -- see that test's
        # docstring. rw_timeout is a generic AVIOContext-level option, not
        # protocol-specific, so it applies here too even though it's not
        # listed among rtmp's own AVOptions.
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        assert args[args.index("-rw_timeout") + 1] == str(stream_relay.RELAY_RW_TIMEOUT_USEC)

    def test_video_bitrate_is_true_cbr_not_just_a_target(self):
        # An unconstrained encode (or -b:v alone, without matching
        # -minrate/-bufsize and nal-hrd=cbr padding) swings with scene
        # content -- confirmed live and reproduced locally: 7-8+ Mbps
        # during active audio, ~14 Kbps of video during a quiet/idle
        # period, each tripping one of YouTube's two opposite bitrate
        # warnings. minrate == maxrate == b:v == bufsize plus
        # nal-hrd=cbr:force-cfr=1 forces libx264 to pad output and hold
        # a steady rate regardless of content.
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        target = stream_relay.YOUTUBE_VIDEO_BITRATE
        assert args[args.index("-b:v") + 1] == target
        assert args[args.index("-minrate") + 1] == target
        assert args[args.index("-maxrate") + 1] == target
        assert args[args.index("-bufsize") + 1] == target
        assert "nal-hrd=cbr" in args[args.index("-x264-params") + 1]
        assert "force-cfr=1" in args[args.index("-x264-params") + 1]


class _FakeProc:
    """Stands in for subprocess.Popen's return value -- stdout is whatever
    bytes-like object the test supplies, so tests can control exactly when
    (or whether) the fake decode process "exits" from Relay's point of view."""

    def __init__(self, stdout):
        self.stdin = MagicMock()
        self.stdout = stdout
        self.stderr = io.BytesIO(b"")
        self.returncode = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


class _BlockingStdout:
    """A stdout whose .read() blocks until the test releases it -- lets a
    test observe Relay while its (fake) decode process is still "running",
    deterministically, instead of racing a real process's exit timing."""

    def __init__(self):
        self._event = threading.Event()

    def read(self, n):
        self._event.wait(timeout=5)
        return b""

    def release(self):
        self._event.set()


class TestRelayDeadDetection:
    # Confirmed live: a Relay whose internal decode ffmpeg exits right after
    # construction (a transient "EBML header parsing failed" on the very
    # first chunk, in the incident that prompted this) used to leave the
    # relay silently feeding a dead process forever -- feed() swallows the
    # resulting BrokenPipeError, so nothing in app.py's poller loop ever
    # noticed and reconnected. Both Broadcastify and YouTube stayed dark
    # until a full service restart. These tests cover the fix: Relay now
    # tracks whether its decode process has exited, via .alive.

    def _wait_until(self, predicate, timeout=2.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.01)
        return False

    def test_relay_marks_itself_dead_when_decode_process_exits_immediately(self):
        with patch.object(stream_relay.subprocess, "Popen",
                           side_effect=lambda *a, **k: _FakeProc(io.BytesIO(b""))):
            relay = stream_relay.Relay(targets={}, log_fn=lambda msg: None)
            try:
                assert self._wait_until(lambda: not relay.alive), (
                    "Relay never noticed its decode process had exited"
                )
            finally:
                relay.stop()

    def test_relay_stays_alive_while_decode_process_is_still_running(self):
        blocking_stdout = _BlockingStdout()
        with patch.object(stream_relay.subprocess, "Popen",
                           side_effect=lambda *a, **k: _FakeProc(blocking_stdout)):
            relay = stream_relay.Relay(targets={}, log_fn=lambda msg: None)
            try:
                # Give the pump thread a moment to start and block on read();
                # it must NOT have flipped to dead just because it hasn't
                # produced output yet.
                time.sleep(0.1)
                assert relay.alive
            finally:
                blocking_stdout.release()
                relay.stop()

    def test_feed_marks_relay_dead_on_a_broken_pipe(self):
        proc = _FakeProc(_BlockingStdout())

        def _raise_broken_pipe(data):
            raise BrokenPipeError()
        proc.stdin.write.side_effect = _raise_broken_pipe

        with patch.object(stream_relay.subprocess, "Popen",
                           side_effect=lambda *a, **k: proc):
            relay = stream_relay.Relay(targets={}, log_fn=lambda msg: None)
            try:
                relay.feed(b"some webm bytes")
                assert not relay.alive
            finally:
                proc.stdout.release()
                relay.stop()
