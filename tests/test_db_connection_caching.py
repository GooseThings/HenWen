"""Tests for get_db()'s thread-local connection cache. Before this, get_db()
opened a brand-new sqlite3 connection on every single call -- ~90 call sites
across app.py, some (the AMI poll loop) hit every second. See get_db()'s own
comment for why thread-local (not a single shared connection, not
flask.g-per-request) is the right cache scope for this app's threading model.
"""
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
