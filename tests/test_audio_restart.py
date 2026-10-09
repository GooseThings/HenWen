"""Asterisk-restart detection for live audio sessions (app._check_asterisk_restart)."""
import app


def test_restarted_only_on_pid_change():
    assert app._asterisk_restarted(100, 200) is True
    assert app._asterisk_restarted(100, 100) is False


def test_unknown_pids_are_not_a_restart():
    assert app._asterisk_restarted(None, 200) is False   # first sample
    assert app._asterisk_restarted(100, None) is False   # Asterisk down


def _reset(monkeypatch, pids):
    monkeypatch.setitem(app._asterisk_restart_state, "pid", None)
    seq = iter(pids)
    monkeypatch.setattr(app, "_asterisk_pid", lambda: next(seq))


def test_check_tears_down_on_restart_including_down_then_up(monkeypatch):
    torn = []
    monkeypatch.setattr(app, "_force_teardown_all_broadcasts", lambda: torn.append(1) or 1)
    _reset(monkeypatch, [100, 100, None, 200])
    for _ in range(4):
        monkeypatch.setitem(app._asterisk_restart_state, "checked", 0.0)
        app._check_asterisk_restart()
    assert torn == [1]                                   # only the 100 -> 200 step
    assert app._asterisk_restart_state["pid"] == 200


def test_check_does_nothing_when_pid_stable(monkeypatch):
    torn = []
    monkeypatch.setattr(app, "_force_teardown_all_broadcasts", lambda: torn.append(1) or 0)
    _reset(monkeypatch, [100, 100, 100])
    for _ in range(3):
        monkeypatch.setitem(app._asterisk_restart_state, "checked", 0.0)
        app._check_asterisk_restart()
    assert torn == []


def test_check_is_throttled(monkeypatch):
    calls = []
    monkeypatch.setattr(app, "_asterisk_pid", lambda: calls.append(1) or 100)
    monkeypatch.setitem(app._asterisk_restart_state, "checked", 0.0)
    app._check_asterisk_restart()
    app._check_asterisk_restart()
    assert len(calls) == 1
