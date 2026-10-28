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
import shutil

import pytest

import app


@pytest.fixture(autouse=True)
def _sounds_root(tmp_path, monkeypatch):
    """Point ASTERISK_SOUNDS_ROOT at a temp dir holding the one sound file
    these tests reference, since create/update now refuse a path Asterisk
    couldn't find."""
    root = tmp_path / "sounds-root"
    (root / "henwen").mkdir(parents=True)
    (root / "henwen" / "my-id.ulaw").write_bytes(b"\xff" * 160)
    monkeypatch.setattr(app, "ASTERISK_SOUNDS_ROOT", str(root))
    return root


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


class TestSoundPicker:
    def test_custom_sounds_listed_first_and_deduped(self, client, create_user, _sounds_root, tmp_path, monkeypatch):
        (_sounds_root / "en" / "digits").mkdir(parents=True)
        stock = _sounds_root / "en" / "digits" / "1.ulaw"
        stock.write_bytes(b"x")
        (_sounds_root / "en" / "WSCK466_ID.ulaw").write_bytes(b"x")
        (_sounds_root / "en" / "WSCK466_ID.gsm").write_bytes(b"x")   # same sound, 2nd format
        (_sounds_root / "en" / "bad name.ulaw").write_bytes(b"x")    # unsaveable path, hidden
        dpkg = tmp_path / "dpkg"
        dpkg.mkdir()
        (dpkg / "asl3-asterisk.list").write_text(f"{_sounds_root}\n{stock}\n")
        monkeypatch.setattr(app, "DPKG_INFO_DIR", str(dpkg))
        app._packaged_sound_files.cache_clear()
        try:
            create_user("owner1", role="owner")
            _login(client, "owner1")
            sounds = client.get("/api/id/sounds").get_json()["sounds"]
        finally:
            app._packaged_sound_files.cache_clear()
        assert sounds == [
            {"path": "en/WSCK466_ID", "custom": True},
            {"path": "henwen/my-id", "custom": True},
            {"path": "en/digits/1", "custom": False},
        ]

    def test_morse_defaults_come_from_rpt_conf(self, tmp_path, monkeypatch):
        conf = tmp_path / "rpt.conf"
        conf.write_text("[morse-main](!)\nspeed = 15 ; WPM\nidfrequency = 750\nidamplitude = 512\n\n"
                        "[morse](morse-main)\nspeed = 18\n")
        monkeypatch.setattr(app, "RPT_CONF_PATH", str(conf))
        assert app._morse_defaults() == {"wpm": 18, "freq": 750, "amplitude": 512}

    def test_morse_defaults_fall_back_without_rpt_conf(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "RPT_CONF_PATH", str(tmp_path / "missing.conf"))
        assert app._morse_defaults() == {"wpm": 20, "freq": 1065, "amplitude": 4096}


class TestMorseGeneration:
    def test_paris_timing(self):
        # "E" is one dit: 100ms lead + 1 unit + 100ms tail. At 20 WPM one
        # unit is 60ms -> 480 samples at 8kHz.
        pcm = app._morse_pcm("E", 20, 800, 4096)
        assert len(pcm) // 2 == 800 + 480 + 800
        # "EE" adds a 3-unit letter gap and another dit; "E E" a 7-unit word gap.
        assert len(app._morse_pcm("EE", 20, 800, 4096)) // 2 == 800 + 480 * 5 + 800
        assert len(app._morse_pcm("E E", 20, 800, 4096)) // 2 == 800 + 480 * 9 + 800

    def test_amplitude_and_click_free_edges(self):
        pcm = app._morse_pcm("T", 20, 800, 4096)
        samples = [int.from_bytes(pcm[i:i + 2], "little", signed=True) for i in range(0, len(pcm), 2)]
        assert max(abs(s) for s in samples) <= 4096
        # 800Hz at 8kHz never samples the exact crest; sin(2pi/5) of it.
        assert max(abs(s) for s in samples) > 3800
        first_tone = 800   # right after the 100ms lead-in
        assert abs(samples[first_tone + 1]) < 50   # ramped in, not a hard step

    def test_route_generates_file_and_reuses_name(self, client, create_user, tmp_path, monkeypatch):
        sounds = tmp_path / "henwen-sounds"
        monkeypatch.setattr(app, "SOUNDS_DIR", str(sounds))
        monkeypatch.setattr(app, "_convert_to_ulaw", lambda src, dest: shutil.copyfile(src, dest))
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = {"text": " wsck466 ", "wpm": 20, "freq": 1065}
        d1 = client.post("/api/id/morse", json=body).get_json()
        d2 = client.post("/api/id/morse", json=body).get_json()
        assert d1["path"] == d2["path"] == "henwen/morse-wsck466-20wpm-1065hz"
        assert d1["seconds"] > 1
        assert [p.name for p in sounds.iterdir()] == ["morse-wsck466-20wpm-1065hz.ulaw"]

    def test_route_rejects_bad_input(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        for body in ({"text": ""}, {"text": "W8ABC!"}, {"text": "X" * 41},
                     {"text": "W8ABC", "wpm": 100}, {"text": "W8ABC", "freq": 50},
                     {"text": "W8ABC", "wpm": "fast"}):
            assert client.post("/api/id/morse", json=body).status_code == 400, body

    def test_routes_need_superuser(self, client, create_user):
        create_user("owner1", role="owner")   # leaves first-run setup mode
        create_user("admin1", role="admin")
        _login(client, "admin1")
        assert client.get("/api/id/sounds").status_code == 403
        assert client.post("/api/id/morse", json={"text": "W8ABC"}).status_code == 403
