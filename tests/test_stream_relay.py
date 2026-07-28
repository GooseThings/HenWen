"""Tests for stream_relay.py's URL/args construction (pure functions, no
network or subprocess). The Relay/RelayTarget classes that actually run
ffmpeg were verified interactively against a real local Icecast instance
and an ffmpeg-as-RTMP-server loopback during development (see the
plan's Phase 4 verification notes) rather than in the automated suite,
matching this repo's policy of not depending on external services or
long-lived local servers from pytest.
"""
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
        assert "-map" in args and "[v]" in args
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
