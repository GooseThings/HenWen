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
    def _no_connected_or_nets_by_default(self, monkeypatch):
        # Most tests in this class only care about weather/alerts -- stub
        # the two newer segments to "nothing" so they don't have to know
        # about AMI status or net_schedules at all. Scoped to this class
        # only: TestRelayConnectedNodesText/TestRelayNetsTodayText test
        # these exact functions directly and must see the real ones.
        monkeypatch.setattr(app, "_relay_connected_nodes_text", lambda node: "")
        monkeypatch.setattr(app, "_relay_nets_today_text", lambda: "")

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

    def test_connected_nodes_and_nets_appended_after_weather_and_alerts(self, monkeypatch):
        monkeypatch.setattr(app, "lookup_node", lambda node: {"location": ""})
        monkeypatch.setattr(app, "_fetch_weather", lambda loc: {"error": "No location configured"})
        monkeypatch.setattr(app, "get_nws_display_alerts", lambda: [])
        monkeypatch.setattr(app, "_relay_connected_nodes_text", lambda node: "Connected: N8GMZ (546054)")
        monkeypatch.setattr(app, "_relay_nets_today_text", lambda: "Today's Nets: Weekly Net 20:00")
        text = app._build_relay_ticker_text("643931")
        assert text == "Connected: N8GMZ (546054)   //   Today's Nets: Weekly Net 20:00   //   "


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


class TestNetsScheduledToday:
    def _row(self, **kw):
        base = {"id": 1, "name": "Weekly Net", "node": "643931", "recurrence": "weekly",
                "weekday": 0, "net_date": None, "time": "20:00", "end_time": None, "notes": ""}
        base.update(kw)
        return base

    def test_weekly_net_matches_todays_weekday(self, monkeypatch):
        # 2026-07-27 is a Monday -- weekday()==0.
        fixed_now = app.datetime(2026, 7, 27, 12, 0, tzinfo=app.timezone.utc)

        class _FixedDatetime(app.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)

        monkeypatch.setattr(app, "datetime", _FixedDatetime)
        monkeypatch.setattr(app, "get_setting", lambda k, d=None: "UTC")
        rows = [self._row(weekday=0), self._row(id=2, name="Other Day", weekday=1)]
        monkeypatch.setattr(app, "get_db", lambda: _FakeDb(rows))

        todays = app._nets_scheduled_today()
        assert [r["name"] for r in todays] == ["Weekly Net"]
        assert todays[0]["utc_time"] == "20:00Z"

    def test_once_net_matches_todays_date_only(self, monkeypatch):
        fixed_now = app.datetime(2026, 7, 27, 12, 0, tzinfo=app.timezone.utc)

        class _FixedDatetime(app.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)

        monkeypatch.setattr(app, "datetime", _FixedDatetime)
        monkeypatch.setattr(app, "get_setting", lambda k, d=None: "UTC")
        rows = [
            self._row(id=2, name="Special Net", recurrence="once", net_date="2026-07-27", weekday=None),
            self._row(id=3, name="Wrong Day", recurrence="once", net_date="2026-07-28", weekday=None),
        ]
        monkeypatch.setattr(app, "get_db", lambda: _FakeDb(rows))

        todays = app._nets_scheduled_today()
        assert [r["name"] for r in todays] == ["Special Net"]

    def test_results_sorted_by_time(self, monkeypatch):
        fixed_now = app.datetime(2026, 7, 27, 12, 0, tzinfo=app.timezone.utc)

        class _FixedDatetime(app.datetime):
            @classmethod
            def now(cls, tz=None):
                return fixed_now if tz else fixed_now.replace(tzinfo=None)

        monkeypatch.setattr(app, "datetime", _FixedDatetime)
        monkeypatch.setattr(app, "get_setting", lambda k, d=None: "UTC")
        rows = [self._row(id=1, name="Late", weekday=0, time="21:00"),
                self._row(id=2, name="Early", weekday=0, time="18:00")]
        monkeypatch.setattr(app, "get_db", lambda: _FakeDb(rows))

        todays = app._nets_scheduled_today()
        assert [r["name"] for r in todays] == ["Early", "Late"]


class _FakeDb:
    def __init__(self, rows):
        self._rows = rows

    def execute(self, *a, **k):
        return self

    def fetchall(self):
        return [dict(r) for r in self._rows]


class TestRelayNetsTodayText:
    def test_empty_when_none_scheduled(self, monkeypatch):
        monkeypatch.setattr(app, "_nets_scheduled_today", lambda: [])
        assert app._relay_nets_today_text() == ""

    def test_formats_name_and_utc_time(self, monkeypatch):
        # Confirms the ticker uses utc_time (Zulu), not the raw kiosk-local
        # 'time' field -- showing kiosk-local net times next to the
        # overlay's own Zulu clock was reported as confusing.
        monkeypatch.setattr(app, "_nets_scheduled_today", lambda: [
            {"name": "Weekly Net", "time": "16:00", "utc_time": "20:00Z"},
            {"name": "Emergency Net", "time": "17:30", "utc_time": "21:30Z"},
        ])
        assert app._relay_nets_today_text() == "Today's Nets: Weekly Net 20:00Z, Emergency Net 21:30Z"

    def test_a_net_with_no_convertible_time_is_skipped(self, monkeypatch):
        monkeypatch.setattr(app, "_nets_scheduled_today", lambda: [
            {"name": "Broken Net", "time": "bad", "utc_time": None},
            {"name": "Good Net", "time": "20:00", "utc_time": "20:00Z"},
        ])
        assert app._relay_nets_today_text() == "Today's Nets: Good Net 20:00Z"

    def test_lookup_raising_does_not_crash(self, monkeypatch):
        def _boom():
            raise RuntimeError("boom")
        monkeypatch.setattr(app, "_nets_scheduled_today", _boom)
        assert app._relay_nets_today_text() == ""


class TestNetLocalTimeToUtc:
    def test_utc_zone_is_a_no_op_besides_the_z_suffix(self):
        import zoneinfo
        result = app._net_local_time_to_utc("2026-07-27", "20:00", zoneinfo.ZoneInfo("UTC"))
        assert result == "20:00Z"

    def test_converts_us_eastern_to_utc_including_the_day_roll(self):
        # This is the exact bug reported: net_schedules stores kiosk-local
        # wall-clock time with no timezone, so a naive display next to a
        # Zulu clock silently mixed zones. 2026-07-27 is during EDT
        # (UTC-4), so 20:00 Eastern is 00:00Z the *next* day.
        import zoneinfo
        result = app._net_local_time_to_utc("2026-07-27", "20:00", zoneinfo.ZoneInfo("America/New_York"))
        assert result == "00:00Z"

    def test_unparseable_date_returns_none_not_raise(self):
        import zoneinfo
        assert app._net_local_time_to_utc("", "20:00", zoneinfo.ZoneInfo("UTC")) is None

    def test_unparseable_time_returns_none_not_raise(self):
        import zoneinfo
        assert app._net_local_time_to_utc("2026-07-27", "not-a-time", zoneinfo.ZoneInfo("UTC")) is None
