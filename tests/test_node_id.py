"""Tests for the Node ID feature's play_cmd (localplay/playback) setting.

Mirrors the same play_cmd convention already used by announcements and
nws_alert_config: 'localplay' reaches other connected AllStarLink nodes
but never this node's own local RF-attached repeater/controller;
'playback' reaches that local RF gear too, but also every currently-
linked node's own (possibly RF) equipment -- see the warning in the
Manager UI and README.md for the full explanation. These tests cover
the create/update routes' validation and _play_id_sound()'s command
construction; they don't touch real AMI.
"""
import pytest

import app


@pytest.fixture(autouse=True)
def _sounds_root(tmp_path, monkeypatch):
    """Point ASTERISK_SOUNDS_ROOT at a temp dir holding the one sound file
    these tests reference, since create/update now refuse a path Asterisk
    couldn't find."""
    (tmp_path / "henwen").mkdir()
    (tmp_path / "henwen" / "my-id.ulaw").write_bytes(b"\xff" * 160)
    monkeypatch.setattr(app, "ASTERISK_SOUNDS_ROOT", str(tmp_path))
    return tmp_path


def _login(client, username, password=None):
    """Stamp the session directly rather than going through the real
    /login route -- see tests/test_recording_config.py's identical
    helper for why (rate limiting)."""
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


class TestCreateIdConfigPlayCmd:
    def test_defaults_to_localplay_when_omitted(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/my-id",
        })
        assert resp.status_code == 201
        assert resp.get_json()["play_cmd"] == "localplay"

    def test_playback_is_accepted_and_persisted(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Repeater ID", "node": "546054", "sound_path": "henwen/my-id",
            "play_cmd": "playback",
        })
        assert resp.status_code == 201
        assert resp.get_json()["play_cmd"] == "playback"

    def test_invalid_value_falls_back_to_localplay(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/my-id",
            "play_cmd": "rm -rf /",
        })
        assert resp.status_code == 201
        assert resp.get_json()["play_cmd"] == "localplay"


class TestUpdateIdConfigPlayCmd:
    def _create(self, client, play_cmd="localplay"):
        resp = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/my-id",
            "play_cmd": play_cmd,
        })
        return resp.get_json()["id"]

    def test_can_switch_to_playback(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = self._create(client, "localplay")
        resp = client.patch(f"/api/id/{iid}", json={"play_cmd": "playback"})
        assert resp.status_code == 200
        assert resp.get_json()["play_cmd"] == "playback"

    def test_omitting_play_cmd_preserves_existing_value(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = self._create(client, "playback")
        resp = client.patch(f"/api/id/{iid}", json={"name": "Renamed ID"})
        assert resp.status_code == 200
        assert resp.get_json()["play_cmd"] == "playback"

    def test_invalid_value_falls_back_to_localplay(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = self._create(client, "playback")
        resp = client.patch(f"/api/id/{iid}", json={"play_cmd": "not-a-real-mode"})
        assert resp.status_code == 200
        assert resp.get_json()["play_cmd"] == "localplay"


class _FakeAmi:
    def __init__(self, sent):
        self._sent = sent

    def command(self, cmd):
        self._sent["cmd"] = cmd
        return "OK"


class TestPlayIdSoundCommand:
    def test_localplay_builds_the_expected_ami_command(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_FakeAmi(sent)))
        app._play_id_sound({"node": "546054", "sound_path": "henwen/my-id",
                             "name": "Main ID", "play_cmd": "localplay"})
        assert sent["cmd"] == "rpt localplay 546054 henwen/my-id"

    def test_playback_builds_the_expected_ami_command(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_FakeAmi(sent)))
        app._play_id_sound({"node": "546054", "sound_path": "henwen/my-id",
                             "name": "Repeater ID", "play_cmd": "playback"})
        assert sent["cmd"] == "rpt playback 546054 henwen/my-id"


class TestSoundPathMustExist:
    def test_create_rejects_missing_file(self, client, create_user):
        # en/morse/<call> was the form's own example, but ASL3 ships no
        # such directory -- this used to save fine and then never key up.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Morse ID", "node": "546054", "sound_path": "en/morse/WSCK466",
        })
        assert resp.status_code == 400
        assert "No sound file found" in resp.get_json()["error"]

    def test_update_rejects_changing_to_missing_file(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/my-id",
        }).get_json()["id"]
        resp = client.patch(f"/api/id/{iid}", json={"sound_path": "henwen/nope"})
        assert resp.status_code == 400

    def test_update_of_other_fields_allowed_after_file_removed(self, client, create_user, _sounds_root):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/my-id",
        }).get_json()["id"]
        (_sounds_root / "henwen" / "my-id.ulaw").unlink()
        resp = client.patch(f"/api/id/{iid}", json={"name": "Renamed"})
        assert resp.status_code == 200

    def test_exists_helper_any_format_and_absolute(self, _sounds_root):
        (_sounds_root / "en").mkdir()
        (_sounds_root / "en" / "WSCK466_ID.gsm").write_bytes(b"x")
        assert app._asterisk_sound_exists("en/WSCK466_ID")
        assert app._asterisk_sound_exists(str(_sounds_root / "en" / "WSCK466_ID"))
        assert not app._asterisk_sound_exists("en/WSCK466")
        # a directory with the right name is not a sound file
        (_sounds_root / "en" / "dir.ulaw").mkdir()
        assert not app._asterisk_sound_exists("en/dir")
