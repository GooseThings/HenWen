"""Tests for the Node ID feature's three sound slots and per-event choice.

A monitor holds up to three sounds -- a short voice ID, a long voice ID and
a Morse (CW) ID -- and each of the three ID events (initial key-up,
in-use interval, final) picks which slot it plays. The short slot is
`id_configs.sound_path`, the column the single-sound version always used,
so an existing row with every event left on 'short' behaves exactly as it
did before. These cover the resolution helper, the create/update routes'
validation, and that the monitor's three events really reach for their own
slot; no AMI is involved.
"""
import pytest

import app


@pytest.fixture(autouse=True)
def _sounds_root(tmp_path, monkeypatch):
    root = tmp_path / "sounds-root"
    (root / "henwen").mkdir(parents=True)
    for name in ("short", "long", "cw"):
        (root / "henwen" / f"{name}.ulaw").write_bytes(b"\xff" * 160)
    monkeypatch.setattr(app, "ASTERISK_SOUNDS_ROOT", str(root))
    return root


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]      = True
        sess["username"]       = username
        sess["role"]           = row["role"]
        sess["user_id"]        = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"]   = (row["session_idle_timeout"] if row["session_idle_timeout"] is not None
                                  else app.SESSION_IDLE_TIMEOUT)
        sess["sid"]            = "test-sid-" + username


ALL_THREE = {
    "sound_path":  "henwen/short",
    "sound_long":  "henwen/long",
    "sound_morse": "henwen/cw",
}


class TestSoundResolution:
    def test_each_event_resolves_to_its_own_slot(self):
        row = dict(ALL_THREE, initial_sound="long", interval_sound="morse",
                   final_sound="short")
        assert app._id_sound_for(row, "initial")  == "henwen/long"
        assert app._id_sound_for(row, "interval") == "henwen/cw"
        assert app._id_sound_for(row, "final")    == "henwen/short"

    def test_empty_slot_falls_back_to_short(self):
        # The routes refuse to save this, but a slot can still be emptied
        # straight in the DB -- an ID event must never resolve to nothing,
        # since `rpt localplay` with no file just fails silently on the air.
        row = {"sound_path": "henwen/short", "sound_long": "", "sound_morse": "",
               "initial_sound": "long", "interval_sound": "morse", "final_sound": "long"}
        for event in ("initial", "interval", "final"):
            assert app._id_sound_for(row, event) == "henwen/short"

    def test_unknown_slot_name_falls_back_to_short(self):
        row = dict(ALL_THREE, initial_sound="bogus", interval_sound="",
                   final_sound="short")
        assert app._id_sound_for(row, "initial")  == "henwen/short"
        assert app._id_sound_for(row, "interval") == "henwen/short"

    def test_row_predating_the_slot_columns_still_resolves(self):
        # A plain dict with only the old single column, as a pre-migration
        # row (or an older caller) would hand over.
        row = {"sound_path": "henwen/short"}
        for event in ("initial", "interval", "final"):
            assert app._id_sound_for(row, event) == "henwen/short"


class TestCreateValidation:
    def test_defaults_every_event_to_short(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/short"})
        assert resp.status_code == 201
        d = resp.get_json()
        assert (d["initial_sound"], d["interval_sound"], d["final_sound"]) == \
               ("short", "short", "short")
        assert d["sound_long"] == "" and d["sound_morse"] == ""

    def test_all_three_slots_persist(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json=dict(
            ALL_THREE, name="Main ID", node="546054",
            initial_sound="long", interval_sound="morse", final_sound="long"))
        assert resp.status_code == 201
        d = resp.get_json()
        assert d["sound_long"] == "henwen/long"
        assert d["sound_morse"] == "henwen/cw"
        assert d["interval_sound"] == "morse"

    def test_rejects_an_event_pointed_at_an_empty_slot(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/short",
            "final_sound": "morse"})
        assert resp.status_code == 400
        assert "is empty" in resp.get_json()["error"]

    def test_rejects_unknown_slot_choice(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/short",
            "interval_sound": "loud"})
        assert resp.status_code == 400

    def test_rejects_missing_file_in_a_secondary_slot(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_path": "henwen/short",
            "sound_long": "henwen/nope", "final_sound": "long"})
        assert resp.status_code == 400
        assert "No sound file found" in resp.get_json()["error"]

    def test_short_slot_is_still_required(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/id", json={
            "name": "Main ID", "node": "546054", "sound_long": "henwen/long"})
        assert resp.status_code == 400
        assert "required" in resp.get_json()["error"]


class TestUpdateValidation:
    def _create(self, client, **extra):
        return client.post("/api/id", json=dict(
            {"name": "Main ID", "node": "546054", "sound_path": "henwen/short"},
            **extra)).get_json()["id"]

    def test_can_add_slots_and_repoint_events(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = self._create(client)
        resp = client.patch(f"/api/id/{iid}", json={
            "sound_morse": "henwen/cw", "interval_sound": "morse"})
        assert resp.status_code == 200
        d = resp.get_json()
        assert d["interval_sound"] == "morse" and d["sound_morse"] == "henwen/cw"
        # Untouched fields keep their values.
        assert d["final_sound"] == "short" and d["sound_path"] == "henwen/short"

    def test_omitting_everything_preserves_the_slots(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = self._create(client, sound_long="henwen/long", final_sound="long")
        resp = client.patch(f"/api/id/{iid}", json={"name": "Renamed"})
        assert resp.status_code == 200
        d = resp.get_json()
        assert d["sound_long"] == "henwen/long" and d["final_sound"] == "long"

    def test_cannot_empty_a_slot_an_event_still_points_at(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = self._create(client, sound_long="henwen/long", final_sound="long")
        resp = client.patch(f"/api/id/{iid}", json={"sound_long": ""})
        assert resp.status_code == 400

    def test_other_fields_editable_after_a_slot_file_is_removed(self, client, create_user, _sounds_root):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        iid = self._create(client, sound_long="henwen/long", final_sound="long")
        (_sounds_root / "henwen" / "long.ulaw").unlink()
        resp = client.patch(f"/api/id/{iid}", json={"name": "Renamed"})
        assert resp.status_code == 200


class _FakeAmi:
    def __init__(self, sent):
        self._sent = sent

    def command(self, cmd):
        self._sent["cmd"] = cmd
        return "OK"


class TestPlayCommandUsesTheEventsSlot:
    @pytest.mark.parametrize("event,expected", [
        ("initial",  "henwen/long"),
        ("interval", "henwen/cw"),
        ("final",    "henwen/short"),
    ])
    def test_command_names_the_events_own_sound(self, monkeypatch, event, expected):
        sent = {}
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_FakeAmi(sent)))
        row = dict(ALL_THREE, node="546054", name="Main ID", play_cmd="playback",
                   initial_sound="long", interval_sound="morse", final_sound="short")
        assert app._play_id_sound(row, event) == expected
        assert sent["cmd"] == f"rpt playback 546054 {expected}"

    def test_defaults_to_the_final_events_slot(self, monkeypatch):
        sent = {}
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_FakeAmi(sent)))
        row = dict(ALL_THREE, node="546054", name="Main ID", play_cmd="localplay",
                   initial_sound="short", interval_sound="short", final_sound="long")
        app._play_id_sound(row)
        assert sent["cmd"] == "rpt localplay 546054 henwen/long"


class TestGuardSizedOffThePlayedSound:
    def test_long_sound_extends_the_activity_guard(self, _sounds_root):
        """The guard that keeps an ID's own transmission from reading as
        activity has to be sized off the sound that actually went out --
        off the short slot while a long ID is still transmitting, the ID's
        own tail reads as activity and retriggers the clock."""
        (_sounds_root / "henwen" / "long.ulaw").write_bytes(b"\xff" * (8000 * 12))
        row = dict(ALL_THREE, final_sound="long")
        short_rt, long_rt = {}, {}
        app._id_mark_played(short_rt, row, 1000.0, "henwen/short")
        app._id_mark_played(long_rt,  row, 1000.0, "henwen/long")
        assert long_rt["guard_max_until"] > short_rt["guard_max_until"]

    def test_omitted_path_falls_back_to_the_short_slot(self):
        rt = {}
        app._id_mark_played(rt, dict(ALL_THREE), 1000.0)
        assert rt["guard_max_until"] > 1000.0


class TestUploadSlugsDoNotCollide:
    """Every "create a sound" route shares one SOUNDS_DIR, but only
    announcements get a row to collide against. Uniqueness therefore has to
    consider the directory too: two different recordings that happen to
    slug the same (an 'ID.wav' into the short slot and another into the
    long slot) otherwise resolved to one path, ffmpeg -y overwrote the
    first, and both slots played the second recording."""

    def test_second_upload_of_the_same_name_gets_its_own_slug(self, fresh_db, tmp_path, monkeypatch):
        sounds = tmp_path / "henwen"
        sounds.mkdir()
        monkeypatch.setattr(app, "SOUNDS_DIR", str(sounds))
        first = app._ann_unique_slug(app._ann_slug("ID"))
        (sounds / f"{first}.ulaw").write_bytes(b"FIRST")
        second = app._ann_unique_slug(app._ann_slug("ID"))
        assert second != first
        assert not (sounds / f"{second}.ulaw").exists()
        # The first recording is still intact behind its own slug.
        assert (sounds / f"{first}.ulaw").read_bytes() == b"FIRST"

    def test_any_extension_counts_as_taken(self, fresh_db, tmp_path, monkeypatch):
        sounds = tmp_path / "henwen"
        sounds.mkdir()
        monkeypatch.setattr(app, "SOUNDS_DIR", str(sounds))
        (sounds / "id.gsm").write_bytes(b"x")
        assert app._ann_unique_slug(app._ann_slug("ID")) != "id"

    def test_an_upload_cannot_steal_an_announcements_slug(self, fresh_db, tmp_path, monkeypatch):
        sounds = tmp_path / "henwen"
        sounds.mkdir()
        monkeypatch.setattr(app, "SOUNDS_DIR", str(sounds))
        db = app.get_db()
        db.execute("INSERT INTO announcements (name, slug, node) "
                   "VALUES ('Net reminder', 'id', '546054')")
        db.commit()
        assert app._ann_unique_slug(app._ann_slug("ID")) != "id"

    def test_excluded_row_keeps_its_own_slug(self, fresh_db, tmp_path, monkeypatch):
        """What lets a TTS edit re-synthesize over its existing path
        atomically instead of drifting to a new filename."""
        sounds = tmp_path / "henwen"
        sounds.mkdir()
        monkeypatch.setattr(app, "SOUNDS_DIR", str(sounds))
        db = app.get_db()
        db.execute("INSERT INTO announcements (name, slug, node) "
                   "VALUES ('Net reminder', 'net-reminder', '546054')")
        db.commit()
        aid = db.execute("SELECT id FROM announcements WHERE slug='net-reminder'").fetchone()["id"]
        (sounds / "net-reminder.ulaw").write_bytes(b"x")
        assert app._ann_unique_slug("net-reminder", exclude_id=aid) == "net-reminder"

    def test_a_free_slug_is_returned_unchanged(self, fresh_db, tmp_path, monkeypatch):
        sounds = tmp_path / "henwen"
        sounds.mkdir()
        monkeypatch.setattr(app, "SOUNDS_DIR", str(sounds))
        assert app._ann_unique_slug(app._ann_slug("My Repeater ID")) == "my_repeater_id"
