"""Tests for recording.py: the in-browser recording pipeline module.

Pure-function tests (frame_rms, SilenceGate, the retention/cap purge
selectors) need no external tools and always run. The pipeline
integration test drives a real Recorder end-to-end against a synthetic
WebM/Opus clip built with ffmpeg -- unlike the AMI/audio_relay.py path,
this needs no live Asterisk, only ffmpeg itself (already a hard runtime
dependency of the app), so it's skipped rather than required when
ffmpeg isn't on PATH.
"""
import math
import shutil
import struct
import subprocess
import time

import pytest

import recording


def _make_frame(amplitude, n_samples=160):
    """A 160-sample (20ms @ 8kHz) s16le mono frame at a constant amplitude."""
    return struct.pack(f"<{n_samples}h", *([amplitude] * n_samples))


class TestFrameRms:
    def test_silence_is_zero(self):
        assert recording.frame_rms(_make_frame(0)) == 0.0

    def test_full_scale_matches_amplitude(self):
        assert recording.frame_rms(_make_frame(1000)) == pytest.approx(1000, rel=0.01)

    def test_empty_frame_is_zero_not_an_error(self):
        assert recording.frame_rms(b"") == 0.0

    def test_odd_length_trailing_byte_is_ignored(self):
        # 3 bytes = 1 full sample + 1 stray byte; must not raise.
        assert recording.frame_rms(_make_frame(500, 1) + b"\x00") == pytest.approx(500, rel=0.01)


class TestSilenceGate:
    def test_loud_frame_always_passes(self):
        gate = recording.SilenceGate(rms_thresh=300, min_gap_ms=100)
        assert gate.should_pass(_make_frame(1000)) is True
        assert gate.should_pass(_make_frame(1000)) is True

    def test_short_silence_within_gap_budget_passes(self):
        # min_gap_ms=100 at 20ms frames = 5 frames of budget.
        gate = recording.SilenceGate(rms_thresh=300, min_gap_ms=100)
        results = [gate.should_pass(_make_frame(0)) for _ in range(5)]
        assert all(results)

    def test_silence_beyond_gap_budget_is_dropped(self):
        gate = recording.SilenceGate(rms_thresh=300, min_gap_ms=100)
        results = [gate.should_pass(_make_frame(0)) for _ in range(10)]
        assert results[:5] == [True] * 5
        assert results[5:] == [False] * 5

    def test_silent_run_resets_on_speech(self):
        gate = recording.SilenceGate(rms_thresh=300, min_gap_ms=40)  # 2-frame budget
        assert gate.should_pass(_make_frame(0)) is True
        assert gate.should_pass(_make_frame(0)) is True
        assert gate.should_pass(_make_frame(0)) is False  # over budget
        assert gate.should_pass(_make_frame(1000)) is True  # speech resets
        assert gate.should_pass(_make_frame(0)) is True  # budget available again

    def test_min_gap_shorter_than_one_frame_still_allows_one_frame(self):
        gate = recording.SilenceGate(rms_thresh=300, min_gap_ms=1)
        assert gate.should_pass(_make_frame(0)) is True
        assert gate.should_pass(_make_frame(0)) is False


class TestNextTtsDue:
    def test_not_due_before_interval_elapses(self):
        assert recording.next_tts_due(elapsed_sec=10, interval_sec=60, last_tts_sec=0) is False

    def test_due_once_interval_elapses(self):
        assert recording.next_tts_due(elapsed_sec=60, interval_sec=60, last_tts_sec=0) is True

    def test_due_measured_from_last_splice_not_from_zero(self):
        # A splice already happened at 60s; the next one isn't due until 120s.
        assert recording.next_tts_due(elapsed_sec=90, interval_sec=60, last_tts_sec=60) is False
        assert recording.next_tts_due(elapsed_sec=120, interval_sec=60, last_tts_sec=60) is True

    def test_disabled_when_interval_is_none_or_non_positive(self):
        assert recording.next_tts_due(elapsed_sec=1000, interval_sec=None, last_tts_sec=0) is False
        assert recording.next_tts_due(elapsed_sec=1000, interval_sec=0, last_tts_sec=0) is False
        assert recording.next_tts_due(elapsed_sec=1000, interval_sec=-5, last_tts_sec=0) is False


class TestSelectPurgeByAge:
    def test_old_recordings_are_selected(self):
        now = 1_000_000.0
        rows = [
            {"id": 1, "started_at": now - 40 * 86400},  # 40 days old
            {"id": 2, "started_at": now - 5 * 86400},   # 5 days old
        ]
        assert recording.select_purge_by_age(rows, now, retention_days=30) == [1]

    def test_nothing_selected_when_all_recent(self):
        now = 1_000_000.0
        rows = [{"id": 1, "started_at": now - 1 * 86400}]
        assert recording.select_purge_by_age(rows, now, retention_days=30) == []

    def test_empty_input(self):
        assert recording.select_purge_by_age([], 0, 30) == []


class TestSelectPurgeByCap:
    def test_deletes_oldest_first_until_under_cap(self):
        rows = [
            {"id": 1, "started_at": 1, "size_bytes": 100},
            {"id": 2, "started_at": 2, "size_bytes": 100},
            {"id": 3, "started_at": 3, "size_bytes": 100},
        ]
        # cap 150 -> total 300 must drop to <=150: delete id 1 (200 left, still
        # over), then id 2 (100 left, under cap) -> stop.
        assert recording.select_purge_by_cap(rows, cap_bytes=150) == [1, 2]

    def test_nothing_deleted_when_already_under_cap(self):
        rows = [{"id": 1, "started_at": 1, "size_bytes": 50}]
        assert recording.select_purge_by_cap(rows, cap_bytes=100) == []

    def test_deletes_everything_if_cap_is_zero(self):
        rows = [
            {"id": 1, "started_at": 1, "size_bytes": 50},
            {"id": 2, "started_at": 2, "size_bytes": 50},
        ]
        assert recording.select_purge_by_cap(rows, cap_bytes=0) == [1, 2]

    def test_empty_input(self):
        assert recording.select_purge_by_cap([], cap_bytes=100) == []


class TestSelectPurgeByUserCap:
    def test_each_user_capped_independently(self):
        rows = [
            {"id": 1, "user_id": 1, "started_at": 1, "size_bytes": 100},
            {"id": 2, "user_id": 1, "started_at": 2, "size_bytes": 100},
            {"id": 3, "user_id": 2, "started_at": 1, "size_bytes": 10},
        ]
        # user 1 is over a 150-byte cap (total 200) -> oldest (id 1) purged.
        # user 2 is well under cap -> untouched.
        ids = recording.select_purge_by_user_cap(rows, cap_bytes=150)
        assert ids == [1]

    def test_empty_input(self):
        assert recording.select_purge_by_user_cap([], cap_bytes=100) == []


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not available")
class TestRecorderPipeline:
    """End-to-end test of the actual decode -> gate -> encode pipeline
    against a synthetic WebM/Opus clip, matching _start_broadcast()'s own
    ffmpeg encode settings (8kHz mono source, 48kHz libopus output) so the
    fixture is a faithful stand-in for what the real broadcast produces.
    No Asterisk/AMI involved -- this only exercises code that never talks
    to Asterisk in the first place."""

    @staticmethod
    @pytest.fixture(scope="class")
    def synthetic_webm(tmp_path_factory):
        """2s tone + 3s silence + 2s tone, encoded exactly like the live
        broadcast pipeline (see _start_broadcast() in app.py)."""
        out = tmp_path_factory.mktemp("recording_fixture") / "synthetic.webm"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=2",
            "-f", "lavfi", "-i", "anullsrc=r=8000:cl=mono:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:duration=2",
            "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1[out]", "-map", "[out]",
            "-ar", "48000", "-c:a", "libopus", "-b:a", "24k", "-vbr", "off",
            "-cutoff", "4000", "-frame_duration", "20", "-application", "audio",
            "-f", "webm", "-cluster_time_limit", "200", str(out),
        ], check=True, capture_output=True, timeout=30)
        return out

    def _run(self, synthetic_webm, dest_path, silence_rms_thresh, silence_min_gap_ms):
        result = {}

        def on_stop(reason, elapsed):
            result["reason"] = reason
            result["elapsed"] = elapsed

        rec = recording.Recorder(
            node="546054", dest_path=str(dest_path), output_format="opus",
            silence_rms_thresh=silence_rms_thresh, silence_min_gap_ms=silence_min_gap_ms,
            max_duration_sec=300, on_stop=on_stop,
        )
        with open(synthetic_webm, "rb") as f:
            while True:
                chunk = f.read(2048)
                if not chunk:
                    break
                rec.feed(chunk)
        time.sleep(1.0)  # let the decode->gate->encode pipeline drain
        rec.stop("manual")
        assert result.get("reason") == "manual"
        return dest_path

    def _duration(self, path):
        out = subprocess.run(
            ["ffprobe", "-hide_banner", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
            check=True, capture_output=True, text=True, timeout=10,
        )
        return float(out.stdout.strip())

    def test_no_trim_preserves_full_duration(self, synthetic_webm, tmp_path):
        dest = tmp_path / "out_full.ogg"
        self._run(synthetic_webm, dest, silence_rms_thresh=50, silence_min_gap_ms=999_999)
        assert dest.exists() and dest.stat().st_size > 0
        assert self._duration(dest) == pytest.approx(7.0, abs=0.5)

    def test_aggressive_trim_shortens_the_silent_stretch(self, synthetic_webm, tmp_path):
        dest = tmp_path / "out_trimmed.ogg"
        self._run(synthetic_webm, dest, silence_rms_thresh=200, silence_min_gap_ms=200)
        assert dest.exists() and dest.stat().st_size > 0
        duration = self._duration(dest)
        # 7s of source minus most of the 3s silent stretch, capped near the
        # 200ms gap budget on each edge -- verified empirically at ~4.2s.
        assert duration < 5.0
        assert duration > 3.5  # the two 2s tones must still be fully present

    def test_tts_splice_inserts_extra_audio(self, synthetic_webm, tmp_path):
        """Uses a fake tts_fn (no real Piper/network dependency) to verify
        the splice mechanism itself: periodic extra PCM actually reaches
        the encoded file, on top of whatever the source contributed. Real
        Piper synthesis via _synthesize_timestamp_pcm() is exercised
        manually against app.py's live TTS_VOICES_DIR/PIPER_BIN instead of
        here, matching this repo's general policy of not depending on
        Piper/network access from the automated suite."""
        dest = tmp_path / "out_tts.ogg"
        result = {}

        def on_stop(reason, elapsed):
            result["reason"] = reason

        splice_calls = []

        def fake_tts_fn():
            splice_calls.append(1)
            return b"\x00\x01" * 4000  # ~0.5s of fake PCM @ 8kHz mono s16le

        rec = recording.Recorder(
            node="546054", dest_path=str(dest), output_format="opus",
            silence_rms_thresh=50, silence_min_gap_ms=999_999,
            max_duration_sec=300, on_stop=on_stop,
            tts_interval_sec=0.3, tts_fn=fake_tts_fn,
        )
        with open(synthetic_webm, "rb") as f:
            data = f.read()
        chunk_size = 1024
        for i in range(0, len(data), chunk_size):
            rec.feed(data[i:i + chunk_size])
            time.sleep(0.05)  # let wall-clock elapse past the splice interval
        time.sleep(0.5)
        rec.stop("manual")

        assert result.get("reason") == "manual"
        assert len(splice_calls) >= 1, "expected at least one TTS splice to fire"
        duration = self._duration(dest)
        assert duration > 7.0  # source alone is ~7s; splices must add more on top

    def test_max_duration_auto_stops(self, synthetic_webm, tmp_path):
        dest = tmp_path / "out_capped.ogg"
        result = {}

        def on_stop(reason, elapsed):
            result["reason"] = reason
            result["elapsed"] = elapsed

        rec = recording.Recorder(
            node="546054", dest_path=str(dest), output_format="opus",
            silence_rms_thresh=50, silence_min_gap_ms=999_999,
            max_duration_sec=1, on_stop=on_stop,
        )
        with open(synthetic_webm, "rb") as f:
            rec.feed(f.read())
        # The watchdog polls once a second; give it a couple of cycles.
        deadline = time.monotonic() + 5
        while "reason" not in result and time.monotonic() < deadline:
            time.sleep(0.2)
        assert result.get("reason") == "max_duration"
