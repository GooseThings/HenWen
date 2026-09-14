"""Tests for app.py's _ensure_capture_only()/_release_capture_only() --
the low-latency RX audio path's per-node capture refcounting, driven by
audio_ws_relay.py's own first-attach/last-detach bookkeeping (see
_CaptureOnlyRelay's docstring). These call _start_capture_only() directly
rather than through a real AMI connection, so every test here monkeypatches
it out with a lightweight fake -- there's no live Asterisk in this suite
(see CLAUDE.md's "Testing" section).

Covers two races found by review and fixed together:
  - Two concurrent first-listeners for the same node used to both see no
    existing capture and both call the slow _start_capture_only(), the
    loser's relay silently clobbered in _capture_only_active and leaked.
  - A departing last-listener used to call capture.shutdown() *after*
    releasing _capture_only_lock, leaving a window where a concurrent
    _ensure_capture_only() could attach to a still-registered,
    still-not-dead relay that then got torn down out from under it.
"""
import threading
import time

import pytest

import app


class _FakeCapture:
    """Stands in for _CaptureOnlyRelay without touching AMI/subprocesses --
    only the attributes/methods _ensure_capture_only()/
    _release_capture_only() and this class's own _teardown() actually
    read/call."""

    def __init__(self, node):
        self.node = node
        self.listener_count = 0
        self._dead = False
        self.teardown_calls = 0

    def _teardown(self):
        self.teardown_calls += 1


def _reset(node):
    app._capture_only_active.pop(node, None)
    app._capture_only_starting.pop(node, None)


class TestConcurrentEnsure:
    def test_two_concurrent_first_listeners_start_only_once(self, monkeypatch):
        node = "628280"
        _reset(node)

        starts = []
        start_entered = threading.Event()
        proceed = threading.Event()

        def _fake_start(n):
            starts.append(n)
            start_entered.set()
            proceed.wait(timeout=5)  # simulates the slow AMI/subprocess work
            return _FakeCapture(n)

        monkeypatch.setattr(app, "_start_capture_only", _fake_start)

        t1 = threading.Thread(target=app._ensure_capture_only, args=(node,))
        t1.start()
        assert start_entered.wait(timeout=2), "first caller should have entered _start_capture_only()"

        # A second "first listener" arrives while the first is still
        # starting -- it must wait, not race its own _start_capture_only().
        t2 = threading.Thread(target=app._ensure_capture_only, args=(node,))
        t2.start()
        time.sleep(0.2)
        assert t2.is_alive(), "second caller should be waiting on the first, not racing it"

        proceed.set()
        t1.join(timeout=2)
        t2.join(timeout=2)

        assert len(starts) == 1, "only one _start_capture_only() call for two concurrent first-listeners"
        capture = app._capture_only_active[node]
        assert capture.listener_count == 2

        _reset(node)

    def test_a_failed_start_lets_the_next_caller_retry(self, monkeypatch):
        node = "628280"
        _reset(node)

        attempts = {"n": 0}

        def _flaky_start(n):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("simulated AMI failure")
            return _FakeCapture(n)

        monkeypatch.setattr(app, "_start_capture_only", _flaky_start)

        with pytest.raises(RuntimeError):
            app._ensure_capture_only(node)
        assert node not in app._capture_only_active
        assert node not in app._capture_only_starting

        app._ensure_capture_only(node)
        assert app._capture_only_active[node].listener_count == 1

        _reset(node)


class TestReleaseThenEnsureRace:
    def test_release_unregisters_before_the_slow_teardown_runs(self, monkeypatch):
        """The dict removal must happen atomically with listener_count
        reaching zero, strictly before the (potentially slow) physical
        teardown -- otherwise a concurrent _ensure_capture_only() could
        attach to a relay that's already committed to shutting down."""
        node = "628280"
        _reset(node)
        monkeypatch.setattr(app, "_start_capture_only", lambda n: _FakeCapture(n))

        app._ensure_capture_only(node)
        capture = app._capture_only_active[node]
        assert capture.listener_count == 1

        seen_active_during_teardown = {}
        orig_teardown = capture._teardown

        def _checking_teardown():
            seen_active_during_teardown["value"] = app._capture_only_active.get(node)
            orig_teardown()

        capture._teardown = _checking_teardown

        app._release_capture_only(node)

        assert seen_active_during_teardown["value"] is None, (
            "the entry must already be gone from _capture_only_active by "
            "the time _teardown() runs"
        )
        assert capture.teardown_calls == 1
        assert node not in app._capture_only_active

        _reset(node)

    def test_release_after_count_drops_to_zero_does_not_double_teardown(self, monkeypatch):
        node = "628280"
        _reset(node)
        monkeypatch.setattr(app, "_start_capture_only", lambda n: _FakeCapture(n))

        app._ensure_capture_only(node)
        capture = app._capture_only_active[node]

        app._release_capture_only(node)
        assert capture.teardown_calls == 1

        # A stray extra release (e.g. from the kind of bug fixed in
        # audio_ws_relay.py's _handle_ws_connection, where a connection
        # whose own ensure-capture never succeeded still triggered a
        # release) must not find anything left to tear down again.
        app._release_capture_only(node)
        assert capture.teardown_calls == 1

        _reset(node)
