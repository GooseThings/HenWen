"""Tests for the weather bar's source-staleness detection (issue #29:
the bar kept showing conditions -- e.g. heavy rain -- for hours after
they'd actually cleared, because wttr.in returning 200 doesn't mean the
station behind it has reported anything new recently).

_weather_observation_age_minutes() is a pure function covering the
UTC-comparison age estimate; the TestFetchWeatherFlagsStaleSource class
covers _fetch_weather() actually setting stale=True from it on an
otherwise-successful fetch.

Note: an earlier version of this function assumed wttr.in's
"observation_time" was location-local and estimated a per-location offset
from longitude. That was verified live to be wrong -- the field is
actually always UTC regardless of the queried location -- so these tests
compare directly against a fixed UTC "now" with no offset involved.
"""
import datetime as _dt
import io
import json

import pytest

import app


class _FixedUtcNow(_dt.datetime):
    """Subclasses the real datetime so strptime()/replace() etc. keep
    working unchanged -- only utcnow() is pinned, via a mutable class
    attribute so each test can set its own "now" without redefining the
    class."""
    _now = _dt.datetime(2026, 1, 1, 12, 0, 0)

    @classmethod
    def utcnow(cls):
        return cls._now


@pytest.fixture(autouse=True)
def _clean_weather_state(monkeypatch):
    monkeypatch.setattr(app, "_weather_cache", {})
    monkeypatch.setattr(app, "_weather_last_good", {})


class _CtxBytes:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return io.BytesIO(self._body)

    def __exit__(self, *a):
        return False


class TestWeatherObservationAgeMinutes:
    def test_fresh_observation_reads_near_zero(self, monkeypatch):
        _FixedUtcNow._now = _dt.datetime(2026, 1, 1, 16, 48, 0)
        monkeypatch.setattr(app, "datetime", _FixedUtcNow)
        age = app._weather_observation_age_minutes("04:48 PM")
        assert age == pytest.approx(0.0, abs=1.0)

    def test_three_hours_old(self, monkeypatch):
        _FixedUtcNow._now = _dt.datetime(2026, 1, 1, 16, 0, 0)
        monkeypatch.setattr(app, "datetime", _FixedUtcNow)
        age = app._weather_observation_age_minutes("01:00 PM")
        assert age == pytest.approx(180.0, abs=1.0)

    def test_midnight_rollover_does_not_read_as_almost_a_day_old(self, monkeypatch):
        # "Now" is 00:05 UTC, observation was stamped 23:55 -- that's 10
        # minutes old, not ~23h50m, even though the field carries no date.
        _FixedUtcNow._now = _dt.datetime(2026, 1, 1, 0, 5, 0)
        monkeypatch.setattr(app, "datetime", _FixedUtcNow)
        age = app._weather_observation_age_minutes("11:55 PM")
        assert age == pytest.approx(10.0, abs=1.0)

    def test_reading_is_utc_not_location_local(self, monkeypatch):
        # Regression guard for the wrong assumption an earlier version of
        # this function made: a reading stamped with the current UTC clock
        # time must read as fresh regardless of what a longitude-based
        # local-time guess would have said (verified live against Tokyo:
        # observation_time matched UTC, not JST, which is 9 hours ahead).
        _FixedUtcNow._now = _dt.datetime(2026, 1, 1, 16, 48, 0)
        monkeypatch.setattr(app, "datetime", _FixedUtcNow)
        age = app._weather_observation_age_minutes("04:48 PM")
        assert age == pytest.approx(0.0, abs=1.0)

    @pytest.mark.parametrize("observation_time", ["", None, "not a time"])
    def test_unparseable_input_returns_none_rather_than_guessing(self, observation_time):
        assert app._weather_observation_age_minutes(observation_time) is None


def _wttr_payload(observation_time, **cc_overrides):
    cc = {
        "temp_F": "71", "temp_C": "22", "humidity": "85",
        "windspeedMiles": "11", "winddir16Point": "S",
        "observation_time": observation_time,
        "weatherDesc": [{"value": "Moderate rain"}],
    }
    cc.update(cc_overrides)
    return {
        "current_condition": [cc],
        "nearest_area": [{"longitude": "-86.2", "latitude": "43.1"}],
        "weather": [{"astronomy": [{
            "sunrise": "07:00 AM", "sunset": "07:00 PM",
            "moon_phase": "Waxing Gibbous", "moon_illumination": "80",
        }]}],
    }


class TestFetchWeatherFlagsStaleSource:
    def test_fresh_reading_is_not_flagged_stale(self, monkeypatch):
        _FixedUtcNow._now = _dt.datetime(2026, 1, 1, 16, 48, 0)
        monkeypatch.setattr(app, "datetime", _FixedUtcNow)
        monkeypatch.setattr(app.urlreq, "urlopen",
            lambda *a, **k: _CtxBytes(json.dumps(_wttr_payload("04:48 PM")).encode()))

        data = app._fetch_weather("Spring Lake, MI")

        assert data["error"] is None
        assert data["stale"] is False
        assert data["desc"] == "Moderate rain"

    def test_hours_old_reading_from_a_reachable_server_is_flagged_stale(self, monkeypatch):
        # This is the exact issue #29 scenario: wttr.in answers 200 with a
        # perfectly well-formed payload, but the station's own reading is
        # hours old (e.g. rain that has since cleared) -- must not be
        # presented to the kiosk board as current.
        _FixedUtcNow._now = _dt.datetime(2026, 1, 1, 16, 0, 0)
        monkeypatch.setattr(app, "datetime", _FixedUtcNow)
        monkeypatch.setattr(app.urlreq, "urlopen",
            lambda *a, **k: _CtxBytes(json.dumps(_wttr_payload("10:00 AM")).encode()))

        data = app._fetch_weather("Spring Lake, MI")

        assert data["error"] is None
        assert data["stale"] is True
        # Stale doesn't mean withheld -- the reading is still the best data
        # available and is still returned, same as the existing
        # fetch-failure-falls-back-to-last-good path already does.
        assert data["desc"] == "Moderate rain"

    def test_a_western_michigan_location_near_utc_now_is_not_a_false_positive(self, monkeypatch):
        # Direct regression test for the bug this fix's first draft had:
        # treating observation_time as location-local and estimating an
        # offset from longitude flagged this exact live scenario (a
        # Michigan station, current UTC clock as its reading) as stale even
        # though it was actually fresh.
        _FixedUtcNow._now = _dt.datetime(2026, 1, 1, 16, 48, 0)
        monkeypatch.setattr(app, "datetime", _FixedUtcNow)
        monkeypatch.setattr(app.urlreq, "urlopen",
            lambda *a, **k: _CtxBytes(json.dumps(_wttr_payload("04:48 PM")).encode()))

        data = app._fetch_weather("Spring Lake, MI")

        assert data["stale"] is False

    def test_unparseable_observation_time_does_not_block_a_fresh_result(self, monkeypatch):
        # No usable timestamp to check age against -- degrade to "trust it"
        # rather than flagging every reading from a station wttr.in doesn't
        # give a clean time-of-day for.
        monkeypatch.setattr(app.urlreq, "urlopen",
            lambda *a, **k: _CtxBytes(json.dumps(_wttr_payload("")).encode()))

        data = app._fetch_weather("Spring Lake, MI")

        assert data["error"] is None
        assert data["stale"] is False
