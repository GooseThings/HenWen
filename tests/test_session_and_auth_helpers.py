"""Unit tests for the in-process active-session tracker (used for the Status
Board's "logged in" footer count) and small DB-backed auth helpers.
"""
import pytest

import app


@pytest.fixture()
def clean_sessions():
    app._active_sessions.clear()
    yield app._active_sessions
    app._active_sessions.clear()


class TestActiveSessions:
    def test_touch_then_snapshot_reports_one_session(self, clean_sessions):
        app.touch_active_session("sid-1", "alice", "owner")
        assert app.get_active_user_count() == 1
        snap = app._active_sessions_snapshot()
        assert snap[0]["username"] == "alice"
        assert snap[0]["role"] == "owner"

    def test_remove_active_session(self, clean_sessions):
        app.touch_active_session("sid-1", "alice")
        app.remove_active_session("sid-1")
        assert app.get_active_user_count() == 0

    def test_remove_active_session_with_falsy_sid_is_a_no_op(self, clean_sessions):
        app.touch_active_session("sid-1", "alice")
        app.remove_active_session(None)
        app.remove_active_session("")
        assert app.get_active_user_count() == 1

    def test_multiple_sessions_same_user_count_separately(self, clean_sessions):
        app.touch_active_session("sid-1", "alice")
        app.touch_active_session("sid-2", "alice")
        assert app.get_active_user_count() == 2

    def test_snapshot_prunes_sessions_idle_past_the_window(self, clean_sessions):
        app.touch_active_session("sid-stale", "alice")
        # Directly age the entry past ACTIVE_SESSION_WINDOW (90s) rather than
        # mocking time.time(), which would affect the whole process.
        app._active_sessions["sid-stale"]["last_active"] -= (app.ACTIVE_SESSION_WINDOW + 1)
        app.touch_active_session("sid-fresh", "bob")

        result = app._active_sessions_snapshot()

        assert len(result) == 1
        assert result[0]["username"] == "bob"
        assert "sid-stale" not in app._active_sessions

    def test_snapshot_keeps_sessions_just_inside_the_window(self, clean_sessions):
        app.touch_active_session("sid-1", "alice")
        app._active_sessions["sid-1"]["last_active"] -= (app.ACTIVE_SESSION_WINDOW - 1)
        assert app.get_active_user_count() == 1

    def test_get_active_sessions_detail_collapses_same_user(self, clean_sessions):
        app.touch_active_session("sid-1", "alice", "owner")
        app.touch_active_session("sid-2", "alice", "owner")
        app.touch_active_session("sid-3", "bob", "user")

        detail = app.get_active_sessions_detail()

        by_user = {d["username"]: d for d in detail}
        assert by_user["alice"]["sessions"] == 2
        assert by_user["bob"]["sessions"] == 1
        # Sorted case-insensitively by username.
        assert [d["username"] for d in detail] == ["alice", "bob"]


class TestAuthConfiguredAndLockouts:
    def test_is_auth_configured_false_with_no_users(self, fresh_db):
        assert app.is_auth_configured() is False

    def test_is_auth_configured_true_once_an_owner_exists(self, fresh_db, create_user):
        create_user("alice", role="owner")
        assert app.is_auth_configured() is True

    def test_is_auth_configured_false_for_plain_user_role_only(self, fresh_db, create_user):
        create_user("kiosk", role="user")
        assert app.is_auth_configured() is False

    def test_no_locked_nodes_by_default(self, fresh_db):
        assert app.get_locked_nodes() == {}
        assert app.is_any_node_locked() is False

    def test_lock_and_read_back_a_node(self, fresh_db):
        fresh_db.execute(
            "INSERT INTO node_lockouts (node, locked_by) VALUES (?, ?)", ("64393", "owner1")
        )
        fresh_db.commit()

        assert app.is_any_node_locked() is True
        locked = app.get_locked_nodes()
        assert locked["64393"]["locked_by"] == "owner1"
