"""Unit tests for _connector_should_fire(), the Smart Connector scheduler's
pure decision function: given one connector DB row and a `now` datetime,
should it fire this minute? All schedule_type variants are covered.

Row is accessed via row["key"] in app.py, so plain dicts stand in fine for
sqlite3.Row here.

Also covers _run_connectors() itself -- the state-machine driver that reads
real connectors rows from the DB -- for the two bugs that made scheduled
connections silently stop firing: a connector left in 'error' by one failed
attempt could never re-arm on its own future schedule (needed a manual
"Clear Error" click), and a one-time connector was flagged enabled=0 the
instant it entered 'waiting', which -- since every scheduler tick's SELECT
is WHERE enabled=1 -- dropped it out of consideration before it ever
actually got to connect whenever the local node wasn't idle at that exact
moment. These need a real sqlite3.Row (not a plain dict), so they go through
the fresh_db fixture and real INSERTs.
"""
from datetime import datetime

import app


def _row(**overrides):
    base = {
        "connect_time": "14:30",
        "schedule_type": "daily",
        "schedule_days": "",
    }
    base.update(overrides)
    return base


AT_TIME = datetime(2024, 1, 1, 14, 30)      # matches connect_time "14:30", a Monday, week 1
OFF_TIME = datetime(2024, 1, 1, 14, 31)


class TestBasicGating:
    def test_no_connect_time_never_fires(self):
        assert app._connector_should_fire(_row(connect_time=""), AT_TIME) is False
        assert app._connector_should_fire(_row(connect_time=None), AT_TIME) is False

    def test_time_must_match_exactly_to_the_minute(self):
        assert app._connector_should_fire(_row(), OFF_TIME) is False
        assert app._connector_should_fire(_row(), AT_TIME) is True

    def test_manual_schedule_never_fires_automatically(self):
        assert app._connector_should_fire(_row(schedule_type="manual"), AT_TIME) is False


class TestDaily:
    def test_fires_every_day_at_the_right_time(self):
        assert app._connector_should_fire(_row(schedule_type="daily"), AT_TIME) is True


class TestWeekly:
    def test_fires_on_an_allowed_weekday(self):
        row = _row(schedule_type="weekly", schedule_days="0,2,4")  # Mon/Wed/Fri
        assert app._connector_should_fire(row, AT_TIME) is True  # 2024-01-01 is a Monday

    def test_does_not_fire_on_a_non_allowed_weekday(self):
        row = _row(schedule_type="weekly", schedule_days="1,3")  # Tue/Thu
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_blank_days_means_never(self):
        row = _row(schedule_type="weekly", schedule_days="")
        assert app._connector_should_fire(row, AT_TIME) is False


class TestBiweekly:
    def test_fires_on_matching_weekday_and_parity(self):
        # 2024-01-01: Monday, ISO week 1 (odd -> parity 1)
        row = _row(schedule_type="biweekly", schedule_days="0:1")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_skips_the_off_week(self):
        row = _row(schedule_type="biweekly", schedule_days="0:0")
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_fires_two_weeks_later_on_the_matching_parity(self):
        two_weeks_later = datetime(2024, 1, 15, 14, 30)  # Monday, ISO week 3 (odd)
        row = _row(schedule_type="biweekly", schedule_days="0:1")
        assert app._connector_should_fire(row, two_weeks_later) is True

    def test_malformed_days_field_does_not_raise(self):
        row = _row(schedule_type="biweekly", schedule_days="not-a-number")
        assert app._connector_should_fire(row, AT_TIME) is False


class TestMonthly:
    def test_fires_on_the_configured_day_of_month(self):
        row = _row(schedule_type="monthly", schedule_days="1")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_does_not_fire_on_other_days(self):
        row = _row(schedule_type="monthly", schedule_days="15")
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_malformed_day_does_not_raise(self):
        row = _row(schedule_type="monthly", schedule_days="abc")
        assert app._connector_should_fire(row, AT_TIME) is False


class TestBimonthly:
    def test_fires_in_a_matching_month(self):
        # start_month=1: matches months 1, 3, 5, 7, 9, 11
        row = _row(schedule_type="bimonthly", schedule_days="1:1")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_skips_a_non_matching_month(self):
        row = _row(schedule_type="bimonthly", schedule_days="1:1")
        feb = datetime(2024, 2, 1, 14, 30)
        assert app._connector_should_fire(row, feb) is False


class TestQuarterly:
    def test_fires_every_third_month_from_start(self):
        row = _row(schedule_type="quarterly", schedule_days="1:1")
        assert app._connector_should_fire(row, AT_TIME) is True
        april = datetime(2024, 4, 1, 14, 30)
        assert app._connector_should_fire(row, april) is True

    def test_skips_non_quarter_months(self):
        row = _row(schedule_type="quarterly", schedule_days="1:1")
        feb = datetime(2024, 2, 1, 14, 30)
        assert app._connector_should_fire(row, feb) is False


class TestYearly:
    def test_fires_on_the_configured_month_and_day(self):
        row = _row(schedule_type="yearly", schedule_days="01-01")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_does_not_fire_on_a_different_day(self):
        row = _row(schedule_type="yearly", schedule_days="07-04")
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_malformed_value_does_not_raise(self):
        row = _row(schedule_type="yearly", schedule_days="not-a-date")
        assert app._connector_should_fire(row, AT_TIME) is False


class TestOnetime:
    def test_fires_on_the_exact_date(self):
        row = _row(schedule_type="onetime", schedule_days="2024-01-01")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_does_not_fire_on_other_dates(self):
        row = _row(schedule_type="onetime", schedule_days="2024-01-02")
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_malformed_date_does_not_raise(self):
        row = _row(schedule_type="onetime", schedule_days="not-a-date")
        assert app._connector_should_fire(row, AT_TIME) is False


def test_unknown_schedule_type_never_fires():
    row = _row(schedule_type="bogus")
    assert app._connector_should_fire(row, AT_TIME) is False


def _patch_now(monkeypatch, fixed_now):
    """Freeze app.datetime.now()/strptime still work normally since
    _FixedDatetime only overrides now() -- same pattern as
    TestConnectorsScheduledToday._patch_now in test_relay_overlay.py."""
    class _FixedDatetime(app.datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed_now
    monkeypatch.setattr(app, "datetime", _FixedDatetime)


def _insert_connector(db, **overrides):
    row = {
        "name": "Test Net", "local_node": "643931", "target_node": "546054",
        "enabled": 1, "connect_time": "14:30", "idle_limit_sec": 180,
        "settle_sec": 300, "state": "idle", "state_msg": "", "state_updated": None,
        "connected_at": None, "last_activity": None,
        "schedule_type": "daily", "schedule_days": "",
        "disconnect_all_first": 0, "disconnect_skip_permanent": 0,
    }
    row.update(overrides)
    cols = ", ".join(row)
    qs = ", ".join("?" for _ in row)
    db.execute(f"INSERT INTO connectors ({cols}) VALUES ({qs})", tuple(row.values()))
    db.commit()
    return db.execute("SELECT * FROM connectors WHERE rowid=last_insert_rowid()").fetchone()["id"]


AT_1430 = datetime(2024, 1, 1, 14, 30)  # matches connect_time "14:30" used by _insert_connector's default


class TestRunConnectorsErrorRecovery:
    """A connector stuck in 'error' from some earlier failed attempt must
    still fire on its next legitimately scheduled occurrence, not require a
    manual 'Clear Error' click just to get back on schedule."""

    def test_error_state_refires_and_connects_when_schedule_matches(self, fresh_db, monkeypatch):
        cid = _insert_connector(fresh_db, state="error", state_msg="ilink 3 rejected by Asterisk: boom")
        _patch_now(monkeypatch, AT_1430)
        monkeypatch.setattr(app, "_node_active", lambda node: False)  # local node idle
        monkeypatch.setattr(app, "_connector_do_connect", lambda *a, **k: {"success": True})

        app._run_connectors()

        row = fresh_db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
        assert row["state"] == "connected"

    def test_error_state_does_not_refire_off_schedule(self, fresh_db, monkeypatch):
        cid = _insert_connector(fresh_db, state="error")
        _patch_now(monkeypatch, datetime(2024, 1, 1, 9, 0))  # doesn't match connect_time "14:30"
        monkeypatch.setattr(app, "_node_active", lambda node: False)
        monkeypatch.setattr(app, "_connector_do_connect", lambda *a, **k: {"success": True})

        app._run_connectors()

        row = fresh_db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
        assert row["state"] == "error"


class TestRunConnectorsOnetimeEnabledRace:
    """A one-time connector must stay enabled=1 all the way through 'waiting'
    -- disabling it the instant it entered 'waiting' (the old behavior) drops
    it out of every future SELECT ... WHERE enabled=1 scheduler tick before
    it ever actually gets to connect, whenever the local node isn't
    immediately idle."""

    def test_stays_enabled_while_waiting_for_node_to_go_idle(self, fresh_db, monkeypatch):
        cid = _insert_connector(fresh_db, schedule_type="onetime", schedule_days="2024-01-01")
        _patch_now(monkeypatch, AT_1430)
        monkeypatch.setattr(app, "_node_active", lambda node: True)  # local node busy -- can't connect yet

        app._run_connectors()

        row = fresh_db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
        assert row["state"] == "waiting"
        assert row["enabled"] == 1  # must still be selectable on the next tick

    def test_disables_only_once_actually_connected(self, fresh_db, monkeypatch):
        cid = _insert_connector(fresh_db, schedule_type="onetime", schedule_days="2024-01-01")
        _patch_now(monkeypatch, AT_1430)
        monkeypatch.setattr(app, "_node_active", lambda node: True)
        app._run_connectors()  # -> waiting, still enabled (previous test's assertion)

        monkeypatch.setattr(app, "_node_active", lambda node: False)  # node goes idle
        monkeypatch.setattr(app, "_connector_do_connect", lambda *a, **k: {"success": True})
        app._run_connectors()

        row = fresh_db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
        assert row["state"] == "connected"
        assert row["enabled"] == 0

    def test_disables_after_a_failed_attempt_too(self, fresh_db, monkeypatch):
        cid = _insert_connector(fresh_db, schedule_type="onetime", schedule_days="2024-01-01")
        _patch_now(monkeypatch, AT_1430)
        monkeypatch.setattr(app, "_node_active", lambda node: False)
        monkeypatch.setattr(app, "_connector_do_connect", lambda *a, **k: {"success": False, "raw": "boom"})

        app._run_connectors()

        row = fresh_db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
        assert row["state"] == "error"
        assert row["enabled"] == 0

    def test_recurring_connector_is_never_auto_disabled(self, fresh_db, monkeypatch):
        cid = _insert_connector(fresh_db, schedule_type="daily")
        _patch_now(monkeypatch, AT_1430)
        monkeypatch.setattr(app, "_node_active", lambda node: False)
        monkeypatch.setattr(app, "_connector_do_connect", lambda *a, **k: {"success": True})

        app._run_connectors()

        row = fresh_db.execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
        assert row["state"] == "connected"
        assert row["enabled"] == 1


def _login(client, username):
    """Stamp the session directly rather than POSTing to /login -- see the
    identical helper's docstring in test_recording_config.py for why."""
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"] = (row["session_idle_timeout"] if row["session_idle_timeout"] is not None
                                 else app.SESSION_IDLE_TIMEOUT)
        sess["sid"]          = "test-sid-" + username


class TestManualConnectReEnables:
    """The 'Connect Now' button is shown for any connector in state
    idle/error regardless of its enabled flag (see connCard() in
    henwen-manager.html), but the scheduler only ever looks at enabled=1
    rows -- so without re-enabling here, clicking it on a disabled connector
    (manually disabled, or a one-time connector past its auto-disable) left
    it stuck in 'waiting' forever, never picked up by _run_connectors()."""

    def test_connect_now_re_enables_a_disabled_connector(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        cid = _insert_connector(app.get_db(), enabled=0, state="idle")

        resp = client.post(f"/api/connectors/{cid}/connect")

        assert resp.status_code == 200
        row = app.get_db().execute("SELECT * FROM connectors WHERE id=?", (cid,)).fetchone()
        assert row["state"] == "waiting"
        assert row["enabled"] == 1
