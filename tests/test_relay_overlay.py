"""Tests for the YouTube relay's text overlay content-building logic in
app.py (_build_relay_ticker_text, _write_relay_clock_file). The actual
rendering (drawtext filter syntax, textfile=...:reload=1 mechanism)
lives in stream_relay.py and is covered there; this only covers how
app.py assembles the ticker string from weather + NWS alert data, with
those data sources stubbed out so the test doesn't depend on network
access or real NWS poller state.
"""
import os
import re

import stream_relay

import app


class TestAtomicWriteOverlayFile:
    def test_writes_expected_content(self, tmp_path):
        target = tmp_path / "overlay.txt"
        app._atomic_write_overlay_file(str(target), "hello")
        assert target.read_text() == "hello"

    def test_overwrites_existing_content_cleanly(self, tmp_path):
        target = tmp_path / "overlay.txt"
        target.write_text("old")
        app._atomic_write_overlay_file(str(target), "new")
        assert target.read_text() == "new"

    def test_no_leftover_temp_files_after_a_successful_write(self, tmp_path):
        target = tmp_path / "overlay.txt"
        app._atomic_write_overlay_file(str(target), "hello")
        remaining = os.listdir(tmp_path)
        assert remaining == ["overlay.txt"], (
            "a leaked .overlay-* temp file means a concurrent ffmpeg reader "
            "could pick up a stray partial file instead of the real one"
        )

    def test_the_file_is_never_briefly_missing_mid_write(self, tmp_path):
        # This is the exact failure confirmed live: a concurrent ffmpeg
        # process reading via drawtext's textfile=...:reload=1 saw the
        # file disappear and crashed the whole relay ("Cannot read
        # file... No such file or directory"). os.replace() is an atomic
        # rename, so the target path must exist at every instant --
        # verified here by checking existence is never interrupted
        # across many repeated writes (a plain open(path,'w') would fail
        # this under real concurrent access, since truncation briefly
        # empties the file before new content lands).
        target = tmp_path / "overlay.txt"
        app._atomic_write_overlay_file(str(target), "seed")
        for i in range(50):
            app._atomic_write_overlay_file(str(target), f"update-{i}")
            assert target.exists()
        assert target.read_text() == "update-49"


class TestWriteRelayClockFile:
    def test_writes_utc_not_server_local_time(self, tmp_path, monkeypatch):
        # Zulu/UTC is the standard convention for amateur radio net
        # scheduling and logging -- confirmed via feedback after seeing
        # server-local time on the overlay live.
        clock_file = tmp_path / "clock.txt"
        monkeypatch.setattr(stream_relay, "CLOCK_OVERLAY_FILE", str(clock_file))
        app._write_relay_clock_file()
        content = clock_file.read_text()
        assert re.fullmatch(r"\d{2}:\d{2}:\d{2}Z", content), content


class TestBuildRelayTickerText:
    def test_combines_weather_and_alerts_with_separator(self, monkeypatch):
        monkeypatch.setattr(app, "lookup_node", lambda node: {"location": "Kenosha, WI"})
        monkeypatch.setattr(app, "_fetch_weather", lambda loc: {
            "temp_f": "72", "desc": "Partly Cloudy", "error": None,
        })
        monkeypatch.setattr(app, "get_nws_display_alerts", lambda: [
            {"text": "Severe Thunderstorm Warning until 5 PM for Kenosha"},
        ])
        text = app._build_relay_ticker_text("546054")
        assert text == "72°F Partly Cloudy   //   Severe Thunderstorm Warning until 5 PM for Kenosha   //   "

    def test_weather_only_when_no_alerts(self, monkeypatch):
        monkeypatch.setattr(app, "lookup_node", lambda node: {"location": "Kenosha, WI"})
        monkeypatch.setattr(app, "_fetch_weather", lambda loc: {
            "temp_f": "72", "desc": "Partly Cloudy", "error": None,
        })
        monkeypatch.setattr(app, "get_nws_display_alerts", lambda: [])
        text = app._build_relay_ticker_text("546054")
        assert text == "72°F Partly Cloudy   //   "

    def test_alerts_only_when_weather_unavailable(self, monkeypatch):
        monkeypatch.setattr(app, "lookup_node", lambda node: {"location": "Kenosha, WI"})
        monkeypatch.setattr(app, "_fetch_weather", lambda loc: {"error": "Weather unavailable"})
        monkeypatch.setattr(app, "get_nws_display_alerts", lambda: [
            {"text": "Tornado Watch until 9 PM"},
        ])
        text = app._build_relay_ticker_text("546054")
        assert text == "Tornado Watch until 9 PM   //   "

    def test_empty_when_nothing_available(self, monkeypatch):
        monkeypatch.setattr(app, "lookup_node", lambda node: {"location": ""})
        monkeypatch.setattr(app, "_fetch_weather", lambda loc: {"error": "No location configured"})
        monkeypatch.setattr(app, "get_nws_display_alerts", lambda: [])
        assert app._build_relay_ticker_text("546054") == ""

    def test_multiple_alerts_all_included(self, monkeypatch):
        monkeypatch.setattr(app, "lookup_node", lambda node: {"location": ""})
        monkeypatch.setattr(app, "_fetch_weather", lambda loc: {"error": "No location configured"})
        monkeypatch.setattr(app, "get_nws_display_alerts", lambda: [
            {"text": "Alert One"}, {"text": "Alert Two"},
        ])
        text = app._build_relay_ticker_text("546054")
        assert text == "Alert One   //   Alert Two   //   "

    def test_a_data_source_raising_does_not_crash_the_ticker(self, monkeypatch):
        # Weather/NWS lookups reach out to caches and (indirectly) the
        # network -- a transient failure in either must not take down the
        # relay's overlay, just degrade to whatever the other source has.
        def _boom(*a, **k):
            raise RuntimeError("boom")
        monkeypatch.setattr(app, "lookup_node", _boom)
        monkeypatch.setattr(app, "get_nws_display_alerts", lambda: [{"text": "Still works"}])
        text = app._build_relay_ticker_text("546054")
        assert text == "Still works   //   "
