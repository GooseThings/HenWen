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


class TestBuildYoutubeOutputArgs:
    def test_builds_rtmp_url_with_stream_key(self):
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://a.rtmp.youtube.com/live2", stream_key="abcd-1234-efgh")
        assert args[-1] == "rtmp://a.rtmp.youtube.com/live2/abcd-1234-efgh"

    def test_trailing_slash_on_rtmp_url_does_not_double_up(self):
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://a.rtmp.youtube.com/live2/", stream_key="abcd-1234")
        assert args[-1] == "rtmp://a.rtmp.youtube.com/live2/abcd-1234"

    def test_includes_a_dummy_video_track(self):
        # YouTube Live's RTMP ingest is not reliably known to accept
        # audio-only input (untested against a real account) -- a static
        # color source is included by default rather than gambling.
        args = stream_relay.build_youtube_output_args(
            rtmp_url="rtmp://host/live", stream_key="key")
        assert any("color=" in a for a in args)
        assert "-c:v" in args
        # Output muxer is the last "-f flv" (there's an earlier "-f lavfi"
        # for the dummy video input) -- confirmed by its position right
        # before the destination URL.
        assert args[-3:-1] == ["-f", "flv"]
