"""Tests for the hardware-performance fixes made during a Pi Zero 2 W
oriented audit: read_conf_file()'s mtime-based cache (rpt.conf was being
read from disk and re-parsed on every call -- ~20 call sites, including
two hot paths: the 1s AMI poll loop and /api/status/board, hit every
1.5-2s by every open kiosk tab) and the connection_history indexes (its
lookup queries, hit on every connect/disconnect from that same 1s poll
loop, had no index to use and only get slower as the table grows over an
install's lifetime).
"""
import time

import app


class TestReadConfFileCache:
    def test_second_read_without_a_change_is_served_from_cache(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "_conf_file_cache", {})
        path = tmp_path / "rpt.conf"
        path.write_text("[1000]\nnode=1000\n")

        calls = []
        real_open = open

        def counting_open(p, *a, **k):
            if str(p) == str(path):
                calls.append(1)
            return real_open(p, *a, **k)

        monkeypatch.setattr("builtins.open", counting_open)

        first = app.read_conf_file(str(path))
        second = app.read_conf_file(str(path))

        assert first == second == "[1000]\nnode=1000\n"
        assert len(calls) == 1, "second read should have come from cache, not disk"

    def test_a_real_content_change_is_picked_up(self, tmp_path):
        # Exercises the real filesystem, not a mock -- the whole point of
        # this cache is mtime-based invalidation, so this confirms it
        # actually works against a real file, not just the theory of it.
        path = tmp_path / "rpt.conf"
        path.write_text("[1000]\nnode=1000\n")
        first = app.read_conf_file(str(path))

        path.write_text("[2000]\nnode=2000\n")
        second = app.read_conf_file(str(path))

        assert first == "[1000]\nnode=1000\n"
        assert second == "[2000]\nnode=2000\n"

    def test_missing_file_is_not_cached_as_a_permanent_failure(self, tmp_path):
        path = tmp_path / "does_not_exist.conf"
        assert app.read_conf_file(str(path)) is None
        path.write_text("[1000]\nnode=1000\n")
        assert app.read_conf_file(str(path)) == "[1000]\nnode=1000\n"

    def test_different_paths_are_cached_independently(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "_conf_file_cache", {})
        path_a = tmp_path / "a.conf"
        path_b = tmp_path / "b.conf"
        path_a.write_text("A")
        path_b.write_text("B")
        assert app.read_conf_file(str(path_a)) == "A"
        assert app.read_conf_file(str(path_b)) == "B"


class TestConnectionHistoryIndexes:
    def test_lookup_and_ordering_indexes_exist(self, fresh_db):
        names = {
            r["name"] for r in fresh_db.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='connection_history'"
            ).fetchall()
        }
        assert "idx_connhist_lookup" in names
        assert "idx_connhist_connected_at" in names

    def test_index_creation_is_idempotent_against_an_existing_table(self, fresh_db):
        # get_db() only runs schema/migration statements once per process
        # (see conftest.py's fresh_db fixture docstring) -- but the
        # CREATE INDEX IF NOT EXISTS statements themselves must also be
        # safe to run again against a table that already has data and
        # already has the index, the same way every CREATE TABLE IF NOT
        # EXISTS in this schema already is.
        fresh_db.execute(
            "INSERT INTO connection_history (local_node, peer_node, connected_at) "
            "VALUES ('1000', '2000', ?)", (time.time(),)
        )
        fresh_db.commit()
        fresh_db.execute("""CREATE INDEX IF NOT EXISTS idx_connhist_lookup
            ON connection_history(local_node, peer_node, disconnected_at)""")
        fresh_db.execute("""CREATE INDEX IF NOT EXISTS idx_connhist_connected_at
            ON connection_history(connected_at DESC)""")
        fresh_db.commit()
        row = fresh_db.execute(
            "SELECT peer_node FROM connection_history WHERE local_node='1000'"
        ).fetchone()
        assert row["peer_node"] == "2000"
