"""Regression test for /api/audio/stop: it must acknowledge a listener's
stop request without tearing down the shared broadcast itself. That used
to happen unconditionally (broadcast.shutdown()), which was harmless back
when a browser Listen session was the only kind of client a broadcast
could ever have, but became a real bug once recording.py and
stream_relay.py started holding long-lived clients on the same broadcast:
any single listener toggling Listen off would also kill an unrelated
in-progress recording or the persistent stream relay for everyone.
Confirmed live: a YouTube relay stream cut off mid-sentence the instant
Listen was toggled off. Actual per-client cleanup already happens
correctly via the browser's own connection closing (GeneratorExit in
api_audio_stream()'s generate() -> broadcast.remove_client()).
"""
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


class FakeBroadcast:
    _dead = False

    def __init__(self):
        self.shutdown_called = False
        self.clients = {"1.2.3.4"}

    def has_client(self, remote):
        return remote in self.clients

    def shutdown(self):
        self.shutdown_called = True


class TestAudioStopDoesNotShutdownBroadcast:
    def test_stop_acknowledges_without_shutting_down_the_shared_broadcast(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")

        fake = FakeBroadcast()
        monkeypatch.setitem(app._audio_active, "546054", fake)

        resp = client.post(
            "/api/audio/stop", json={"node": "546054"},
            environ_overrides={"REMOTE_ADDR": "1.2.3.4"},
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
        assert fake.shutdown_called is False, (
            "api_audio_stop() must not call broadcast.shutdown() -- that kills "
            "every other client sharing the broadcast (relay, recorder, other listeners)"
        )

    def test_still_rejects_a_caller_who_is_not_a_current_listener(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")

        fake = FakeBroadcast()
        monkeypatch.setitem(app._audio_active, "546054", fake)

        resp = client.post(
            "/api/audio/stop", json={"node": "546054"},
            environ_overrides={"REMOTE_ADDR": "9.9.9.9"},
        )
        assert resp.status_code == 403
        assert fake.shutdown_called is False

    def test_rejects_invalid_node_format(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/audio/stop", json={"node": "not-a-node"})
        assert resp.status_code == 400
