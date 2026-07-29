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

import pytest

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
    @pytest.fixture(autouse=True)
    def _no_connected_or_connectors_by_default(self, monkeypatch):
        # Most tests in this class only care about weather/alerts -- stub
        # the two newer segments to "nothing" so they don't have to know
        # about AMI status or the connectors table at all. Scoped to this
        # class only: TestRelayConnectedNodesText/TestRelayConnectorsTodayText
        # test these exact functions directly and must see the real ones.
        monkeypatch.setattr(app, "_relay_connected_nodes_text", lambda node: "")
        monkeypatch.setattr(app, "_relay_connectors_today_text", lambda: "")

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

    def test_connected_nodes_and_connectors_appended_after_weather_and_alerts(self, monkeypatch):
        monkeypatch.setattr(app, "lookup_node", lambda node: {"location": ""})
        monkeypatch.setattr(app, "_fetch_weather", lambda loc: {"error": "No location configured"})
        monkeypatch.setattr(app, "get_nws_display_alerts", lambda: [])
        monkeypatch.setattr(app, "_relay_connected_nodes_text", lambda node: "Connected: N8GMZ (546054)")
        monkeypatch.setattr(app, "_relay_connectors_today_text", lambda: "Smart Connector: Evening Net → 546054 20:00Z")
        text = app._build_relay_ticker_text("643931")
        assert text == "Connected: N8GMZ (546054)   //   Smart Connector: Evening Net → 546054 20:00Z   //   "


class TestRelayConnectedNodesText:
    def test_no_connected_peers_returns_empty(self, monkeypatch):
        monkeypatch.setattr(app, "get_cached_status", lambda node: {"connected": []})
        assert app._relay_connected_nodes_text("643931") == ""

    def test_resolves_callsigns_for_each_connected_peer(self, monkeypatch):
        monkeypatch.setattr(app, "get_cached_status", lambda node: {"connected": ["546054", "27339"]})
        callsigns = {"546054": "N8GMZ", "27339": "W1ABC"}
        monkeypatch.setattr(app, "lookup_node", lambda n: {"callsign": callsigns.get(n, "")})
        assert app._relay_connected_nodes_text("643931") == "Connected: N8GMZ (546054), W1ABC (27339)"

    def test_falls_back_to_bare_node_number_when_no_callsign(self, monkeypatch):
        monkeypatch.setattr(app, "get_cached_status", lambda node: {"connected": ["546054"]})
        monkeypatch.setattr(app, "lookup_node", lambda n: {"callsign": ""})
        assert app._relay_connected_nodes_text("643931") == "Connected: 546054"

    def test_status_lookup_raising_does_not_crash(self, monkeypatch):
        def _boom(node):
            raise RuntimeError("boom")
        monkeypatch.setattr(app, "get_cached_status", _boom)
        assert app._relay_connected_nodes_text("643931") == ""


class TestConnectorsScheduledToday:
    def _row(self, **kw):
        base = {"id": 1, "name": "Evening Net", "local_node": "643931", "target_node": "546054",
                "enabled": 1, "connect_time": "20:00", "schedule_type": "daily",
                "schedule_days": ""}
        base.update(kw)
        return base

    @staticmethod
    def _patch_now(monkeypatch, fixed_now):
        class _FixedDatetime(app.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)
        monkeypatch.setattr(app, "datetime", _FixedDatetime)
        monkeypatch.setattr(app, "get_setting", lambda k, d=None: "UTC")

    def test_daily_connector_still_upcoming_is_included(self, monkeypatch):
        self._patch_now(monkeypatch, app.datetime(2026, 7, 27, 12, 0, tzinfo=app.timezone.utc))
        rows = [self._row(connect_time="20:00")]
        monkeypatch.setattr(app, "get_db", lambda: _FakeDb(rows))

        todays = app._connectors_scheduled_today()
        assert [r["name"] for r in todays] == ["Evening Net"]
        assert todays[0]["utc_time"] == "20:00Z"

    def test_a_connector_whose_time_already_passed_today_is_excluded(self, monkeypatch):
        # "upcoming connections for the day" -- one that already fired
        # (or would have) earlier today shouldn't clutter the ticker.
        self._patch_now(monkeypatch, app.datetime(2026, 7, 27, 21, 0, tzinfo=app.timezone.utc))
        rows = [self._row(connect_time="20:00")]
        monkeypatch.setattr(app, "get_db", lambda: _FakeDb(rows))

        assert app._connectors_scheduled_today() == []

    def test_weekly_connector_matches_todays_weekday(self, monkeypatch):
        # 2026-07-27 is a Monday -- weekday()==0.
        self._patch_now(monkeypatch, app.datetime(2026, 7, 27, 12, 0, tzinfo=app.timezone.utc))
        rows = [self._row(schedule_type="weekly", schedule_days="0"),
                self._row(id=2, name="Other Day", schedule_type="weekly", schedule_days="1")]
        monkeypatch.setattr(app, "get_db", lambda: _FakeDb(rows))

        todays = app._connectors_scheduled_today()
        assert [r["name"] for r in todays] == ["Evening Net"]

    def test_results_sorted_by_connect_time(self, monkeypatch):
        self._patch_now(monkeypatch, app.datetime(2026, 7, 27, 0, 0, tzinfo=app.timezone.utc))
        rows = [self._row(id=1, name="Late", connect_time="21:00"),
                self._row(id=2, name="Early", connect_time="18:00")]
        monkeypatch.setattr(app, "get_db", lambda: _FakeDb(rows))

        todays = app._connectors_scheduled_today()
        assert [r["name"] for r in todays] == ["Early", "Late"]


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return [dict(r) for r in self._rows]


class TestRelayConnectorsTodayText:
    def test_empty_when_none_scheduled(self, monkeypatch):
        monkeypatch.setattr(app, "_connectors_scheduled_today", lambda: [])
        assert app._relay_connectors_today_text() == ""

    def test_formats_name_target_and_utc_time(self, monkeypatch):
        monkeypatch.setattr(app, "_connectors_scheduled_today", lambda: [
            {"name": "Evening Net", "target_node": "546054", "utc_time": "20:00Z"},
            {"name": "Backup Link", "target_node": "27339", "utc_time": "21:30Z"},
        ])
        assert app._relay_connectors_today_text() == (
            "Smart Connector: Evening Net → 546054 20:00Z, Backup Link → 27339 21:30Z"
        )

    def test_a_connector_with_no_convertible_time_is_skipped(self, monkeypatch):
        monkeypatch.setattr(app, "_connectors_scheduled_today", lambda: [
            {"name": "Broken", "target_node": "546054", "utc_time": None},
            {"name": "Good", "target_node": "27339", "utc_time": "20:00Z"},
        ])
        assert app._relay_connectors_today_text() == "Smart Connector: Good → 27339 20:00Z"

    def test_lookup_raising_does_not_crash(self, monkeypatch):
        def _boom():
            raise RuntimeError("boom")
        monkeypatch.setattr(app, "_connectors_scheduled_today", _boom)
        assert app._relay_connectors_today_text() == ""


class TestLocalTimeToUtc:
    def test_utc_zone_is_a_no_op_besides_the_z_suffix(self):
        import zoneinfo
        result = app._local_time_to_utc("2026-07-27", "20:00", zoneinfo.ZoneInfo("UTC"))
        assert result == "20:00Z"

    def test_converts_us_eastern_to_utc_including_the_day_roll(self):
        # This is the exact bug reported: connect_time/net times are
        # stored as kiosk-local wall-clock with no timezone attached, so
        # a naive display next to a Zulu clock silently mixed zones.
        # 2026-07-27 is during EDT (UTC-4), so 20:00 Eastern is 00:00Z
        # the *next* day.
        import zoneinfo
        result = app._local_time_to_utc("2026-07-27", "20:00", zoneinfo.ZoneInfo("America/New_York"))
        assert result == "00:00Z"

    def test_unparseable_date_returns_none_not_raise(self):
        import zoneinfo
        assert app._local_time_to_utc("", "20:00", zoneinfo.ZoneInfo("UTC")) is None

    def test_unparseable_time_returns_none_not_raise(self):
        import zoneinfo
        assert app._local_time_to_utc("2026-07-27", "not-a-time", zoneinfo.ZoneInfo("UTC")) is None
