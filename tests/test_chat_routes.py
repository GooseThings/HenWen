"""Route-level tests for the kiosk chat panel: the live-panel read/post
routes (any logged-in role, per check_auth()'s _USER_OR_ABOVE) and the
owner-only log/delete routes and janitor sweep. Mirrors the gating style of
test_recording_config.py/test_recording_routes.py -- these only exercise
validation/permission/retention logic, no live AMI/audio involved.
"""
from datetime import datetime, timedelta, timezone

import pytest

import app


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"] = app.SESSION_IDLE_TIMEOUT
        sess["sid"]          = "test-sid-" + username


def _insert_message(db, user_id=1, username="alice", role="user", message="hi",
                     created_at=None, deleted_at=None, deleted_by_user_id=None):
    cur = db.execute(
        "INSERT INTO chat_messages (user_id, username, role, message, created_at, "
        "deleted_at, deleted_by_user_id) VALUES (?,?,?,?, COALESCE(?, datetime('now')), ?, ?)",
        (user_id, username, role, message, created_at, deleted_at, deleted_by_user_id),
    )
    db.commit()
    return cur.lastrowid


class TestChatMessagesGating:
    def test_get_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/api/chat/messages")
        assert resp.status_code == 401

    def test_post_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post("/api/chat/messages", json={"message": "hi"})
        assert resp.status_code == 401

    def test_logged_in_user_can_read_and_post(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        resp = client.post("/api/chat/messages", json={"message": "hello net"})
        assert resp.status_code == 201
        body = resp.get_json()["message"]
        assert body["username"] == "alice"
        assert body["message"] == "hello net"

        resp = client.get("/api/chat/messages")
        assert resp.status_code == 200
        msgs = resp.get_json()["messages"]
        assert len(msgs) == 1
        assert msgs[0]["message"] == "hello net"

    def test_post_succeeds_while_a_node_is_locked(self, client, create_user):
        """Chat is exempt from the Owner's node-lockout (check_auth()'s
        _LOCKOUT_EXEMPT) -- lockout is about node/RF control, not
        conversation. Every other non-owner POST route is blocked with 423
        while locked; chat must not be."""
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        db = app.get_db()
        db.execute("INSERT INTO node_lockouts (node, locked_by) VALUES (?, ?)", ("64393", "owner1"))
        db.commit()
        _login(client, "alice")
        resp = client.post("/api/chat/messages", json={"message": "still here"})
        assert resp.status_code == 201

    def test_get_response_is_not_cached(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.get("/api/chat/messages")
        assert resp.headers.get("Cache-Control") == "no-store"

    def test_since_id_only_returns_newer_rows(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        db = app.get_db()
        first_id = _insert_message(db, message="first")
        _insert_message(db, message="second")
        resp = client.get(f"/api/chat/messages?since_id={first_id}")
        msgs = resp.get_json()["messages"]
        assert [m["message"] for m in msgs] == ["second"]

    def test_deleted_messages_excluded_from_live_panel(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        db = app.get_db()
        _insert_message(db, message="visible")
        _insert_message(db, message="hidden", deleted_at="2020-01-01 00:00:00")
        resp = client.get("/api/chat/messages")
        msgs = resp.get_json()["messages"]
        assert [m["message"] for m in msgs] == ["visible"]


class TestChatMessagesValidation:
    def test_rejects_empty_message(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        resp = client.post("/api/chat/messages", json={"message": "   "})
        assert resp.status_code == 400

    def test_rejects_message_over_max_length(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        resp = client.post("/api/chat/messages", json={"message": "x" * (app.CHAT_MESSAGE_MAX_LEN + 1)})
        assert resp.status_code == 400


class TestChatTimestampConversion:
    def test_created_at_converts_to_correct_utc_epoch_regardless_of_server_tz(self, client, create_user, monkeypatch):
        """Regression test for the "every message shows just now" bug:
        created_at is UTC (SQLite's datetime('now')), but a bare
        datetime.strptime(...).timestamp() interprets the naive value as the
        SERVER'S LOCAL time instead. Force local time to a non-UTC zone here
        so this test would catch the bug even on a UTC-configured CI box."""
        import os
        import time as time_module

        create_user("owner1", role="owner")
        _login(client, "owner1")
        original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time_module.tzset()
        try:
            db = app.get_db()
            _insert_message(db, message="hi", created_at="2026-08-13 12:00:00")
            resp = client.get("/api/chat/messages")
            msg = resp.get_json()["messages"][0]
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time_module.tzset()

        expected_ts = datetime(2026, 8, 13, 12, 0, 0, tzinfo=timezone.utc).timestamp()
        assert msg["ts"] == expected_ts


class TestChatLogGating:
    def test_get_requires_login(self, client, create_user):
        # check_auth()'s default-deny returns 401 for anonymous before this
        # route's inline owner check ever runs (it only fires once logged_in
        # is true) -- a logged-in non-owner gets 403, see tests below.
        create_user("owner1", role="owner")
        resp = client.get("/api/chat/log")
        assert resp.status_code == 401

    def test_admin_cannot_view_log(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.get("/api/chat/log")
        assert resp.status_code == 403

    def test_superuser_cannot_view_log(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("su1", password="password12345", role="superuser")
        _login(client, "su1")
        resp = client.get("/api/chat/log")
        assert resp.status_code == 403

    def test_owner_can_view_log_including_deleted(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        db = app.get_db()
        _insert_message(db, message="visible")
        _insert_message(db, message="hidden", deleted_at="2020-01-01 00:00:00", deleted_by_user_id=1)
        resp = client.get("/api/chat/log")
        assert resp.status_code == 200
        msgs = resp.get_json()["messages"]
        assert len(msgs) == 2
        hidden = next(m for m in msgs if m["message"] == "hidden")
        assert hidden["deleted_at"] is not None


class TestChatDeleteGating:
    def test_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.delete("/api/chat/messages/1")
        assert resp.status_code == 401

    def test_admin_cannot_delete(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        mid = _insert_message(app.get_db(), message="hi")
        resp = client.delete(f"/api/chat/messages/{mid}")
        assert resp.status_code == 403

    def test_non_owner_gets_uniform_403_for_nonexistent_message(self, client, create_user):
        # Role checked before the DB lookup -- a non-owner probing ids gets
        # the same 403 whether or not the id exists.
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.delete("/api/chat/messages/99999")
        assert resp.status_code == 403

    def test_owner_soft_deletes_message(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        mid = _insert_message(app.get_db(), message="hi")
        resp = client.delete(f"/api/chat/messages/{mid}")
        assert resp.status_code == 200

        row = app.get_db().execute("SELECT * FROM chat_messages WHERE id=?", (mid,)).fetchone()
        assert row is not None, "soft delete must not remove the row"
        assert row["deleted_at"] is not None

    def test_owner_delete_of_missing_message_is_404(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.delete("/api/chat/messages/99999")
        assert resp.status_code == 404


class TestChatJanitorSweep:
    def test_ages_out_old_messages_regardless_of_server_tz(self, client, create_user):
        """Regression test for the same class of bug as
        TestChatTimestampConversion: _chat_janitor_sweep()'s cutoff must be
        computed against a UTC `now` to compare correctly against UTC-stored
        created_at strings. Forces a non-UTC local zone so this would catch
        a regression to a naive datetime.now() at the call site even on a
        UTC-configured CI box."""
        import os
        import time as time_module

        create_user("owner1", role="owner")
        original_tz = os.environ.get("TZ")
        os.environ["TZ"] = "America/New_York"
        time_module.tzset()
        try:
            db = app.get_db()
            now_utc = datetime.now(timezone.utc)
            old_ts = (now_utc - timedelta(hours=app.CHAT_RETENTION_HOURS + 1)).strftime('%Y-%m-%d %H:%M:%S')
            recent_id = _insert_message(db, message="recent", created_at=now_utc.strftime('%Y-%m-%d %H:%M:%S'))
            _insert_message(db, message="old", created_at=old_ts)

            aged, over_cap = app._chat_janitor_sweep(db, now_utc)
        finally:
            if original_tz is None:
                os.environ.pop("TZ", None)
            else:
                os.environ["TZ"] = original_tz
            time_module.tzset()

        assert aged == 1
        assert over_cap == 0
        remaining = db.execute("SELECT id FROM chat_messages").fetchall()
        assert [r["id"] for r in remaining] == [recent_id]

    def test_enforces_row_cap_oldest_first(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        monkeypatch.setattr(app, "CHAT_LOG_MAX_ROWS", 3)
        db = app.get_db()
        ids = [_insert_message(db, message=f"m{i}") for i in range(5)]

        aged, over_cap = app._chat_janitor_sweep(db, datetime.now(timezone.utc))
        assert aged == 0
        assert over_cap == 2
        remaining = {r["id"] for r in db.execute("SELECT id FROM chat_messages").fetchall()}
        assert remaining == set(ids[2:])

    def test_no_op_when_nothing_to_prune(self, client, create_user):
        create_user("owner1", role="owner")
        db = app.get_db()
        _insert_message(db, message="fresh")
        aged, over_cap = app._chat_janitor_sweep(db, datetime.now(timezone.utc))
        assert (aged, over_cap) == (0, 0)

    def test_loop_passes_a_utc_aware_now_to_the_sweep(self, client, create_user, monkeypatch):
        """Guards the actual bug location: _chat_janitor_loop() originally
        called _chat_janitor_sweep(get_db(), datetime.now()) -- naive,
        server-local time -- instead of datetime.now(timezone.utc). Mocks
        the sweep to capture what `now` it's called with and aborts the
        loop after one iteration, rather than calling the real infinite
        loop."""
        import time as time_module

        create_user("owner1", role="owner")
        captured = {}

        def fake_sweep(db, now):
            captured['now'] = now
            raise SystemExit   # not caught by the loop's `except Exception`

        monkeypatch.setattr(app, "_chat_janitor_sweep", fake_sweep)
        monkeypatch.setattr(app.time, "sleep", lambda *a: None)

        with pytest.raises(SystemExit):
            app._chat_janitor_loop()

        assert captured['now'].tzinfo is not None
        assert captured['now'].utcoffset() == timedelta(0)
