"""Tests for the geocode cache's success/failure TTL split in app.py.

Confirmed live: a single transient Nominatim error (429, timeout, ...)
used to be cached identically to a real result -- forever, with no retry.
When that happened to be the node's own rpt.conf location, it silently and
permanently broke APRS (_aprs_home_coords) and the kiosk board's own map
pin (which feeds the ISS layer's observer coordinates) until the next
HenWen restart. These tests cover the fix: only a successful lookup is
cached forever, a failed one expires after _GEOCODE_FAIL_TTL and is retried.
"""
import io
import json

import pytest

import app


@pytest.fixture(autouse=True)
def _clean_geocode_state(monkeypatch):
    # Module-level caches/queues are shared mutable state -- isolate each
    # test from whatever another test (or the loop below) left behind.
    monkeypatch.setattr(app, "_geocode_cache", {})
    monkeypatch.setattr(app, "_geocode_queue", app.deque())
    monkeypatch.setattr(app, "_geocode_queue_seen", set())
    monkeypatch.setattr(app, "_geocode_last", [0.0])


class _CtxBytes:
    def __init__(self, body):
        self._body = body

    def __enter__(self):
        return io.BytesIO(self._body)

    def __exit__(self, *a):
        return False


class TestGeocodeSuccessCachedForever:
    def test_second_call_does_not_hit_the_network_again(self, monkeypatch):
        calls = []

        def fake_urlopen(req, timeout=10):
            calls.append(1)
            return _CtxBytes(json.dumps([{"lat": "43.1", "lon": "-86.2"}]).encode())

        monkeypatch.setattr(app.urlreq, "urlopen", fake_urlopen)
        monkeypatch.setattr(app.time, "sleep", lambda s: None)

        first = app._geocode("Grand Haven, MI")
        second = app._geocode("Grand Haven, MI")

        assert first == {"lat": 43.1, "lon": -86.2}
        assert second == first
        assert len(calls) == 1


class TestGeocodeFailureExpires:
    def test_failure_is_not_returned_forever(self, monkeypatch):
        def failing_urlopen(req, timeout=10):
            raise TimeoutError("simulated Nominatim 429")

        monkeypatch.setattr(app.urlreq, "urlopen", failing_urlopen)
        monkeypatch.setattr(app.time, "sleep", lambda s: None)

        t = [1000.0]
        monkeypatch.setattr(app.time, "time", lambda: t[0])

        result = app._geocode("Grand Haven, MI")
        assert result is None

        # Still within the TTL -- cache hit, no retry, still None.
        calls = []
        monkeypatch.setattr(app.urlreq, "urlopen",
                             lambda req, timeout=10: calls.append(1))
        t[0] += 60
        assert app._geocode("Grand Haven, MI") is None
        assert calls == []

        # Past the TTL -- must actually retry, and this time succeeds.
        def now_succeeds(req, timeout=10):
            return _CtxBytes(json.dumps([{"lat": "43.1", "lon": "-86.2"}]).encode())

        monkeypatch.setattr(app.urlreq, "urlopen", now_succeeds)
        t[0] += app._GEOCODE_FAIL_TTL + 1
        result = app._geocode("Grand Haven, MI")
        assert result == {"lat": 43.1, "lon": -86.2}

    def test_nonblocking_requeues_an_expired_failure(self, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(app.time, "time", lambda: t[0])
        app._geocode_cache["Grand Haven, MI"] = (None, t[0] - 1)  # already-expired failure

        result = app._geocode_nonblocking("Grand Haven, MI")

        assert result is None
        assert "Grand Haven, MI" in app._geocode_queue_seen

    def test_nonblocking_does_not_requeue_a_live_failure_entry(self, monkeypatch):
        t = [1000.0]
        monkeypatch.setattr(app.time, "time", lambda: t[0])
        app._geocode_cache["Grand Haven, MI"] = (None, t[0] + app._GEOCODE_FAIL_TTL)

        result = app._geocode_nonblocking("Grand Haven, MI")

        assert result is None
        assert "Grand Haven, MI" not in app._geocode_queue_seen

    def test_nonblocking_returns_a_cached_success_without_requeueing(self, monkeypatch):
        app._geocode_cache["Grand Haven, MI"] = ({"lat": 43.1, "lon": -86.2}, None)

        result = app._geocode_nonblocking("Grand Haven, MI")

        assert result == {"lat": 43.1, "lon": -86.2}
        assert "Grand Haven, MI" not in app._geocode_queue_seen
