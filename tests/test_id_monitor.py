"""Regression tests for the Node ID monitor's state machine.

The ID's own playback reads as node activity (the receiver hears the
transmitter, or a link keys), which used to make every final ID trigger an
"initial" ID 2s later, and that one a final ID 120s after it -- forever.
These drive _run_id_monitors() against a fake clock and a scripted keyed
state; no AMI is involved.
"""
import pytest

import app


@pytest.fixture
def monitor(fresh_db, tmp_path, monkeypatch):
    root = tmp_path / "sounds"
    (root / "henwen").mkdir(parents=True)
    # 3s of u-law
    (root / "henwen" / "id.ulaw").write_bytes(b"\xff" * 24000)
    monkeypatch.setattr(app, "ASTERISK_SOUNDS_ROOT", str(root))

    db = app.get_db()
    db.execute(
        "INSERT INTO id_configs (name, node, sound_path, interval_sec, idle_delay_sec, initial_id, play_cmd) "
        "VALUES ('ID', '1999', 'henwen/id', 1800, 120, 1, 'playback')")
    db.commit()
    app._id_runtime.clear()

    clock = {"t": 1_000_000.0}
    keyed = {"v": False}
    plays = []
    events = []
    monkeypatch.setattr(app.time, "time", lambda: clock["t"])
    monkeypatch.setattr(app, "_node_active", lambda node: keyed["v"])

    def _fake_play(row, event="final"):
        plays.append(clock["t"])
        events.append(event)
        return app._id_sound_for(row, event)

    monkeypatch.setattr(app, "_play_id_sound", _fake_play)

    def step(seconds, is_keyed):
        """Advance in 2s monitor ticks with the node keyed or not."""
        keyed["v"] = is_keyed
        end = clock["t"] + seconds
        while clock["t"] < end:
            clock["t"] += 2
            app._run_id_monitors()

    step.events = events
    yield step, plays
    app._id_runtime.clear()


def test_own_playback_does_not_retrigger(monitor):
    step, plays = monitor
    step(10, True)              # real QSO -> initial ID
    assert len(plays) == 1
    step(10, True)              # still talking
    step(122, False)            # idle_delay elapses on the last tick -> final ID
    assert len(plays) == 2
    step(6, True)               # the final ID itself keys the node
    step(600, False)            # quiet afterwards
    assert len(plays) == 2


def test_activity_soon_after_an_id_skips_initial_but_gets_final(monitor):
    step, plays = monitor
    step(10, True)
    step(122, False)
    assert len(plays) == 2      # initial + final
    step(6, True)               # own ID on air
    step(30, False)
    step(4, True)               # a real user keys up shortly after
    assert len(plays) == 2      # ID'd under 10 min ago: no initial ID
    step(122, False)
    assert len(plays) == 3      # but still a final ID


def test_activity_long_after_an_id_gets_initial(monitor):
    step, plays = monitor
    step(10, True)
    step(122, False)
    step(6, True)
    step(app.ID_REQUIRED_SEC, False)
    step(4, True)
    assert len(plays) == 3


def test_guard_ceiling_releases_continuous_activity(monitor):
    step, plays = monitor
    step(10, True)
    step(122, False)
    assert len(plays) == 2
    # Someone keys straight off the ID's tail with no unkeyed gap: ignored
    # only until sound length + grace, not forever -- and since the node
    # just ID'd, that resumed activity gets no initial ID of its own.
    step(4 + 3 + app.ID_GUARD_GRACE_SEC + 2, True)
    assert len(plays) == 2
    step(122, False)
    assert len(plays) == 3


def test_interval_capped_at_required(monitor):
    """The row's 1800s interval is longer than allowed; it's capped."""
    step, plays = monitor
    step(4, True)
    assert len(plays) == 1
    step(app.ID_REQUIRED_SEC - 10, True)
    assert len(plays) == 1
    step(12, True)
    assert len(plays) == 2


def test_interval_off_still_forced(monitor):
    step, plays = monitor
    db = app.get_db()
    db.execute("UPDATE id_configs SET interval_enabled=0, interval_sec=60, initial_id=0")
    db.commit()
    step(300, True)             # interval off: no ID at 60s, no initial ID
    assert plays == []
    step(app.ID_REQUIRED_SEC - 300 + 2, True)
    assert len(plays) == 1      # forced at 10 min of continuous use


def test_restart_remembers_recent_id(monitor):
    step, plays = monitor
    db = app.get_db()
    db.execute("UPDATE id_configs SET last_id_time=?",
               (app.datetime.fromtimestamp(app.time.time() - 60).strftime("%Y-%m-%d %H:%M:%S"),))
    db.commit()
    step(4, True)
    assert plays == []


def test_each_event_plays_its_own_slot(monitor, tmp_path):
    """The three ID events each resolve their own sound slot, so an
    operator can run a long voice ID on key-up and after the exchange with
    a short CW ID on the in-use interval."""
    step, plays = monitor
    root = app.ASTERISK_SOUNDS_ROOT
    for name in ("long", "cw"):
        with open(app.os.path.join(root, "henwen", name + ".ulaw"), "wb") as fh:
            fh.write(b"\xff" * 8000)
    db = app.get_db()
    db.execute("UPDATE id_configs SET sound_long='henwen/long', sound_morse='henwen/cw', "
               "initial_sound='long', interval_sound='morse', final_sound='long', "
               "interval_enabled=1, interval_sec=60")
    db.commit()
    app._id_runtime.clear()

    step(4, True)                       # key up -> initial ID
    step(70, True)                      # stay in use -> interval ID(s)
    step(200, False)                    # go idle past the 120s delay -> final ID
    assert step.events[0]     == "initial"
    assert step.events[-1]    == "final"
    assert set(step.events[1:-1]) == {"interval"}


def test_sound_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(app, "ASTERISK_SOUNDS_ROOT", str(tmp_path))
    (tmp_path / "a.ulaw").write_bytes(b"\xff" * 16000)
    assert app._sound_duration_sec("a") == 2.0
    assert app._sound_duration_sec("missing") is None
