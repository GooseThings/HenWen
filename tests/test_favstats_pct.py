"""Tests for the Favorites "Rx%"/"LCnt" duty-cycle columns (issue #113,
modeled on AllScan's own Rx%/LCnt columns for its connected-node list).

get_cached_favstats() derives keyed_pct/keyups from _favstats_history, a
rolling (ts, keyed) sample deque appended to by the favstats poller on every
clean (non-error) cycle -- see _favstats_poll_loop's own comments in app.py.
These tests drive that derivation directly by seeding _favstats_history,
without needing a live poller thread or network access.
"""
import time

import pytest

import app


@pytest.fixture(autouse=True)
def _clean_favstats_state(monkeypatch):
    monkeypatch.setattr(app, "_favstats_cache", {})
    monkeypatch.setattr(app, "_favstats_cache_ts", {})
    monkeypatch.setattr(app, "_favstats_history", {})


def _seed(node, keyed_sequence, start_ts=1000.0, step=180.0):
    hist = [(start_ts + i * step, k) for i, k in enumerate(keyed_sequence)]
    app._favstats_history[node] = app.deque(hist)
    app._favstats_cache[node]    = {"keyed": keyed_sequence[-1], "connected_count": 0, "error": None}
    app._favstats_cache_ts[node] = time.time()


class TestKeyedPct:
    def test_no_samples_yet_is_none_not_zero(self):
        # A favorite with no clean poll cycle yet must read as "no data",
        # not as a false 0% duty cycle.
        data = app.get_cached_favstats("54576")
        assert data["keyed_pct"] is None
        assert data["keyups"] is None

    def test_all_keyed_samples_is_100_pct(self):
        _seed("27339", [True, True, True, True])
        data = app.get_cached_favstats("27339")
        assert data["keyed_pct"] == 100

    def test_all_unkeyed_samples_is_0_pct(self):
        _seed("47243", [False, False, False])
        data = app.get_cached_favstats("47243")
        assert data["keyed_pct"] == 0

    def test_mixed_samples_rounds_to_nearest_percent(self):
        # 3 of 8 keyed = 37.5% -> rounds to 38, matching AllScan's own
        # rounding in the referenced screenshot (issue #113).
        _seed("64549", [True, False, False, True, False, False, False, True])
        data = app.get_cached_favstats("64549")
        assert data["keyed_pct"] == 38

    def test_error_cycles_are_never_recorded_as_unkeyed_samples(self, monkeypatch):
        # An API outage (429 / DNS failure / etc.) must not silently drag
        # every favorite's duty cycle toward 0% -- _favstats_poll_loop skips
        # appending a history sample entirely on an error result, so a run
        # of nothing but errors leaves keyed_pct at None, not 0.
        app._favstats_cache["666380"]    = {"keyed": False, "connected_count": 0, "error": "HTTP 429"}
        app._favstats_cache_ts["666380"] = time.time()
        data = app.get_cached_favstats("666380")
        assert data["keyed_pct"] is None
        assert data["keyups"] is None


class TestKeyups:
    def test_counts_false_to_true_transitions_only(self):
        # Two keyups: index 0 (True with no prior sample doesn't count as a
        # transition), index 3 (False->True), index 6 (False->True). A
        # True->True run (indices 0-1) or True immediately followed by
        # another True is not a second keyup.
        _seed("472440", [True, True, False, True, True, False, True])
        data = app.get_cached_favstats("472440")
        assert data["keyups"] == 2

    def test_never_keyed_has_zero_keyups(self):
        _seed("27404", [False, False, False, False])
        data = app.get_cached_favstats("27404")
        assert data["keyups"] == 0

    def test_single_sample_has_zero_keyups(self):
        # No prior sample to transition from, regardless of its own state.
        _seed("27664", [True])
        data = app.get_cached_favstats("27664")
        assert data["keyups"] == 0


class TestHistoryPruning:
    def test_poll_loop_drops_samples_older_than_the_window(self, monkeypatch):
        # Mirrors the pruning _favstats_poll_loop does inline on every
        # append (see its own comment) -- a sample older than
        # FAVSTATS_PCT_WINDOW_SEC must not keep dragging keyed_pct once it
        # ages out, the same way _link_stats/_keyed_history are bounded.
        monkeypatch.setattr(app, "FAVSTATS_PCT_WINDOW_SEC", 600.0)
        now = time.time()
        hist = app.deque([
            (now - 10000, True),   # long expired -- would flip this to 100% if kept
            (now - 100, False),
            (now - 50, False),
        ])
        app._favstats_history["55553"] = hist
        cutoff = now - app.FAVSTATS_PCT_WINDOW_SEC
        while hist and hist[0][0] < cutoff:
            hist.popleft()
        data = app.get_cached_favstats("55553")
        assert data["keyed_pct"] == 0
