"""Tests for get_db()'s thread-local connection cache. Before this, get_db()
opened a brand-new sqlite3 connection on every single call -- ~90 call sites
across app.py, some (the AMI poll loop) hit every second. See get_db()'s own
comment for why thread-local (not a single shared connection, not
flask.g-per-request) is the right cache scope for this app's threading model.
"""
import sqlite3
import threading

import app


class TestGetDbThreadLocalCaching:
    def test_two_calls_from_the_same_thread_return_the_same_connection(self, fresh_db):
        first = app.get_db()
        second = app.get_db()
        assert first is second

    def test_a_different_thread_gets_its_own_connection(self, fresh_db):
        # fresh_db already established a connection for the main (test)
        # thread. A different thread must not see that cached connection --
        # sqlite3 connections aren't safe to share across threads (using one
        # from a thread other than the one that created it raises
        # sqlite3.ProgrammingError), which is exactly why this is a
        # *thread*-local cache and not a single shared connection.
        main_thread_conn = app.get_db()
        result = {}

        def worker():
            conn = app.get_db()
            result["conn"] = conn
            result["is_different_object"] = conn is not main_thread_conn
            # Proves the worker's connection is actually usable *in that
            # thread* -- using main_thread_conn here instead would raise
            # sqlite3.ProgrammingError, since it belongs to the main thread.
            conn.execute("SELECT 1").fetchone()
            result["query_ok"] = True

        t = threading.Thread(target=worker)
        t.start()
        t.join()

        assert result.get("is_different_object") is True
        assert result.get("query_ok") is True

    def test_fixture_reset_opens_a_genuinely_fresh_connection_not_a_stale_cache(
        self, tmp_path, monkeypatch
    ):
        # Simulates what tests/conftest.py's fresh_db fixture does between
        # two tests: point at a new DB_PATH, reset _db_ready, and reset the
        # thread-local cache. Without that last reset, this would silently
        # hand back the *first* database's connection instead of opening a
        # real one against the second path -- the exact regression the
        # thread-local cache could have introduced into every other test in
        # this suite if the fixture hadn't been updated alongside it.
        path_a = tmp_path / "a.db"
        monkeypatch.setattr(app, "DB_PATH", str(path_a))
        monkeypatch.setattr(app, "_db_ready", False)
        monkeypatch.setattr(app._db_local, "conn", None, raising=False)
        conn_a = app.get_db()
        conn_a.execute("INSERT INTO settings (key, value) VALUES ('marker', 'from-db-a')")
        conn_a.commit()

        path_b = tmp_path / "b.db"
        monkeypatch.setattr(app, "DB_PATH", str(path_b))
        monkeypatch.setattr(app, "_db_ready", False)
        monkeypatch.setattr(app._db_local, "conn", None, raising=False)
        conn_b = app.get_db()

        assert conn_b is not conn_a
        row = conn_b.execute("SELECT value FROM settings WHERE key='marker'").fetchone()
        assert row is None, "got db-a's data through db-b's connection -- cache wasn't reset"


def _other_writer_can_write():
    """Whether a separate connection on another thread can write right now --
    what every other writer in the app needs to be true."""
    result = {}

    def worker():
        conn = sqlite3.connect(app.DB_PATH, timeout=0.2)
        try:
            conn.execute("INSERT INTO settings (key, value) VALUES ('other', 'x')")
            conn.commit()
            result["ok"] = True
        except sqlite3.OperationalError as e:
            result["ok"] = str(e)
        finally:
            conn.close()

    t = threading.Thread(target=worker)
    t.start()
    t.join()
    return result["ok"]


class TestRequestTeardownReleasesTransaction:
    """get_db()'s connections live as long as their thread, so a request that
    raised between a write and its commit() used to keep the write lock
    forever, and every other writer in the app failed with "database is
    locked" until a restart. Seen live from a duplicate-slug INSERT."""

    def test_a_failed_request_rolls_back_and_releases_the_write_lock(self, fresh_db):
        with app.app.test_request_context("/"):
            conn = app.get_db()
            conn.execute("INSERT INTO settings (key, value) VALUES ('half', 'done')")
            assert conn.in_transaction
            assert _other_writer_can_write() == "database is locked"
            app.app.do_teardown_request(RuntimeError("route blew up"))
        assert not conn.in_transaction
        assert _other_writer_can_write() is True
        assert conn.execute("SELECT 1 FROM settings WHERE key='half'").fetchone() is None

    def test_a_successful_request_that_forgot_commit_is_committed(self, fresh_db):
        with app.app.test_request_context("/"):
            conn = app.get_db()
            conn.execute("INSERT INTO settings (key, value) VALUES ('kept', 'yes')")
            app.app.do_teardown_request(None)
        assert not conn.in_transaction
        assert conn.execute("SELECT value FROM settings WHERE key='kept'").fetchone()[0] == "yes"


class TestBackgroundThreadsReleaseLeakedTransactions:
    """Pollers' cached connections never close either, and most of their
    write paths catch the error and carry on, so a failed write used to
    leave the transaction open: that thread's next write failed too, or it
    held the write lock and every other writer in the app failed with it."""

    def test_helper_rolls_back_and_releases_the_lock(self, fresh_db):
        conn = app.get_db()
        conn.execute("INSERT INTO settings (key, value) VALUES ('half', 'done')")
        assert _other_writer_can_write() == "database is locked"
        app._rollback_leaked_db_transaction("test")
        assert not conn.in_transaction
        assert _other_writer_can_write() is True
        assert conn.execute("SELECT 1 FROM settings WHERE key='half'").fetchone() is None

    def test_helper_is_a_no_op_without_an_open_transaction(self, fresh_db, monkeypatch):
        logged = []
        monkeypatch.setattr(app, "log", lambda *a, **k: logged.append(a))
        app._rollback_leaked_db_transaction("test")
        assert logged == []

    def test_a_failed_irc_inbound_save_does_not_hold_the_lock(self, fresh_db):
        # The IRC callback runs on the relay's own thread and swallows its
        # errors; message=None trips chat_messages' NOT NULL mid-transaction.
        conn = app.get_db()
        conn.execute("INSERT INTO settings (key, value) VALUES ('earlier', 'write')")
        app._irc_relay_on_message("nick", None)
        assert not conn.in_transaction
        assert _other_writer_can_write() is True

    def test_a_loop_iteration_that_failed_mid_write_is_rolled_back(self, fresh_db, monkeypatch):
        class Stop(Exception):
            pass

        sleeps = []

        def fake_sleep(_s):
            sleeps.append(_s)
            if len(sleeps) > 1:
                raise Stop

        def failing_run():
            app.get_db().execute("INSERT INTO settings (key, value) VALUES ('half', 'done')")
            raise RuntimeError("write path blew up before commit")

        monkeypatch.setattr(app.time, "sleep", fake_sleep)
        monkeypatch.setattr(app, "_run_due_announcements", failing_run)
        try:
            app._announce_loop()
        except Stop:
            pass
        assert not app.get_db().in_transaction
        assert _other_writer_can_write() is True
