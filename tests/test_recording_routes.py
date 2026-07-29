"""Route-level tests for the in-browser recording routes added in Phase 2
(/api/recording/start, /api/recordings, /api/recording/<id>/download,
/api/recordings/<id>). These only exercise the validation/permission/
storage-cap/ownership logic that runs *before* a route would ever touch
_start_broadcast() or a live recording.Recorder -- actually starting a
broadcast needs a live Asterisk channel, and the streaming
generate()/GeneratorExit lifecycle in api_recording_start is exercised
manually against real audio instead (see the plan's Phase 2 verification
section), matching this repo's existing "no coverage of the AMI client
or audio pipeline itself" testing philosophy (see CLAUDE.md).
"""
import os

import app


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["idle_timeout"] = app.SESSION_IDLE_TIMEOUT
        sess["sid"]          = "test-sid-" + username


def _insert_recording(db, node="546054", user_id=1, username="alice", filename="rec1.ogg",
                       status="finished", size_bytes=1000):
    cur = db.execute(
        "INSERT INTO recordings (node, user_id, username, filename, status, size_bytes) "
        "VALUES (?,?,?,?,?,?)",
        (node, user_id, username, filename, status, size_bytes),
    )
    db.commit()
    return cur.lastrowid


class TestRecordingStartValidation:
    def test_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post("/api/recording/start", json={"node": "546054"})
        assert resp.status_code == 401

    def test_rejects_invalid_node_format(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        resp = client.post("/api/recording/start", json={"node": "not-a-node"})
        assert resp.status_code == 400

    def test_rejects_user_without_can_record(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        resp = client.post("/api/recording/start", json={"node": "546054"})
        assert resp.status_code == 403
        assert "not approved" in resp.get_json()["error"].lower()

    def test_rejects_when_global_cap_reached(self, client, create_user):
        owner = create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        db = app.get_db()
        db.execute("UPDATE users SET can_record=1 WHERE id=?", (alice["id"],))
        db.execute(
            "INSERT OR REPLACE INTO recording_config (id, global_cap_gb) VALUES (1, 0.000001)"
        )
        db.commit()
        _insert_recording(db, user_id=alice["id"], username="alice", size_bytes=10_000)
        _login(client, "alice")
        resp = client.post("/api/recording/start", json={"node": "546054"})
        assert resp.status_code == 400
        assert "global" in resp.get_json()["error"].lower()

    def test_rejects_when_per_user_cap_reached_even_under_global_cap(self, client, create_user):
        create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        db = app.get_db()
        db.execute("UPDATE users SET can_record=1 WHERE id=?", (alice["id"],))
        db.execute(
            "INSERT OR REPLACE INTO recording_config (id, global_cap_gb, per_user_cap_gb) "
            "VALUES (1, 999, 0.000001)"
        )
        db.commit()
        _insert_recording(db, user_id=alice["id"], username="alice", size_bytes=10_000)
        _login(client, "alice")
        resp = client.post("/api/recording/start", json={"node": "546054"})
        assert resp.status_code == 400
        assert "your" in resp.get_json()["error"].lower()

    def test_other_users_recordings_do_not_count_against_my_per_user_cap(self, client, create_user):
        create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        bob = create_user("bob", password="password12345", role="user")
        db = app.get_db()
        db.execute("UPDATE users SET can_record=1 WHERE id IN (?,?)", (alice["id"], bob["id"]))
        db.execute(
            "INSERT OR REPLACE INTO recording_config (id, global_cap_gb, per_user_cap_gb) "
            "VALUES (1, 999, 0.00001)"
        )
        db.commit()
        # bob has a large recording, but alice's own usage is zero -- this
        # must not block alice, otherwise the per-user cap would leak
        # across accounts.
        _insert_recording(db, user_id=bob["id"], username="bob", size_bytes=10_000)
        _login(client, "alice")
        # alice will still fail past this point (no live Asterisk channel
        # in this test environment), but that failure must be the 500 from
        # _start_broadcast(), not the 400 cap rejection -- proving the cap
        # check itself passed for her.
        resp = client.post("/api/recording/start", json={"node": "546054"})
        assert resp.status_code != 400


class TestRecordingsList:
    def test_plain_user_only_sees_own_recordings(self, client, create_user):
        create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        bob = create_user("bob", password="password12345", role="user")
        db = app.get_db()
        _insert_recording(db, user_id=alice["id"], username="alice", filename="a.ogg")
        _insert_recording(db, user_id=bob["id"], username="bob", filename="b.ogg")
        _login(client, "alice")
        body = client.get("/api/recordings").get_json()
        usernames = {r["username"] for r in body["recordings"]}
        assert usernames == {"alice"}

    def test_admin_sees_every_users_recordings(self, client, create_user):
        create_user("owner1", role="owner")
        admin = create_user("admin1", password="password12345", role="admin")
        alice = create_user("alice", password="password12345", role="user")
        db = app.get_db()
        _insert_recording(db, user_id=alice["id"], username="alice", filename="a.ogg")
        _login(client, "admin1")
        body = client.get("/api/recordings").get_json()
        usernames = {r["username"] for r in body["recordings"]}
        assert usernames == {"alice"}

    def test_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        assert client.get("/api/recordings").status_code == 401


class TestRecordingDownload:
    def test_404_for_unknown_id(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        assert client.get("/api/recording/9999/download").status_code == 404

    def test_403_for_someone_elses_recording(self, client, create_user, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "RECORDINGS_DIR", str(tmp_path))
        create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        create_user("bob", password="password12345", role="user")
        db = app.get_db()
        rid = _insert_recording(db, user_id=alice["id"], username="alice", filename="a.ogg")
        (tmp_path / "a.ogg").write_bytes(b"fake audio")
        _login(client, "bob")
        resp = client.get(f"/api/recording/{rid}/download")
        assert resp.status_code == 403

    def test_owner_role_can_download_anyones_recording(self, client, create_user, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "RECORDINGS_DIR", str(tmp_path))
        create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        db = app.get_db()
        rid = _insert_recording(db, user_id=alice["id"], username="alice", filename="a.ogg")
        (tmp_path / "a.ogg").write_bytes(b"fake audio")
        _login(client, "owner1")
        resp = client.get(f"/api/recording/{rid}/download")
        assert resp.status_code == 200
        assert resp.data == b"fake audio"

    def test_400_when_recording_still_in_progress(self, client, create_user, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "RECORDINGS_DIR", str(tmp_path))
        create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        db = app.get_db()
        rid = _insert_recording(db, user_id=alice["id"], username="alice",
                                 filename="a.ogg", status="recording")
        _login(client, "alice")
        resp = client.get(f"/api/recording/{rid}/download")
        assert resp.status_code == 400

    def test_404_when_finished_but_file_missing_on_disk(self, client, create_user, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "RECORDINGS_DIR", str(tmp_path))
        create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        db = app.get_db()
        rid = _insert_recording(db, user_id=alice["id"], username="alice", filename="missing.ogg")
        _login(client, "alice")
        resp = client.get(f"/api/recording/{rid}/download")
        assert resp.status_code == 404


class TestRecordingsDelete:
    def test_plain_user_cannot_delete(self, client, create_user):
        create_user("owner1", role="owner")
        alice = create_user("alice", password="password12345", role="user")
        db = app.get_db()
        rid = _insert_recording(db, user_id=alice["id"], username="alice")
        _login(client, "alice")
        resp = client.delete(f"/api/recordings/{rid}")
        assert resp.status_code == 403

    def test_admin_can_delete_a_finished_recording_and_its_file(self, client, create_user, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "RECORDINGS_DIR", str(tmp_path))
        create_user("owner1", role="owner")
        admin = create_user("admin1", password="password12345", role="admin")
        db = app.get_db()
        rid = _insert_recording(db, user_id=admin["id"], username="admin1", filename="a.ogg")
        (tmp_path / "a.ogg").write_bytes(b"fake audio")
        _login(client, "admin1")
        resp = client.delete(f"/api/recordings/{rid}")
        assert resp.status_code == 200
        assert not (tmp_path / "a.ogg").exists()
        assert app.get_db().execute("SELECT 1 FROM recordings WHERE id=?", (rid,)).fetchone() is None

    def test_cannot_delete_a_recording_in_progress(self, client, create_user):
        create_user("owner1", role="owner")
        admin = create_user("admin1", password="password12345", role="admin")
        db = app.get_db()
        rid = _insert_recording(db, user_id=admin["id"], username="admin1", status="recording")
        _login(client, "admin1")
        resp = client.delete(f"/api/recordings/{rid}")
        assert resp.status_code == 400

    def test_404_for_unknown_id(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        assert client.delete("/api/recordings/9999").status_code == 404
