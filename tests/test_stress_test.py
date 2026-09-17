"""Tests for the owner-only Manager > Stress Test feature (app.py's
/api/stress-test/* routes). Two independent load generators:

  - "audio" fans extra in-process listeners out against a node's existing
    _AudioBroadcast, exercising add_client()/remove_client() exactly like a
    real browser Listen session -- but never over real HTTP, so it can't
    touch gunicorn's thread pool or a real Asterisk channel. Tested here
    against a FakeAudioBroadcast (same trick test_audio_stop.py uses via
    monkeypatch.setitem(app._audio_active, ...)) so no live Asterisk/AMI is
    needed.
  - "ami" bursts app.ami_send_command() calls. Tested here with
    ami_send_command() itself monkeypatched to a fast stub, since the real
    one needs a live AMI connection -- this suite is only verifying the
    stress-test plumbing (start/stop/status/aggregation/role-gating), not
    re-proving ami_send_command()'s own locking (that's exercised live).
"""
import queue
import threading
import time

import app


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]      = True
        sess["username"]       = username
        sess["role"]           = row["role"]
        sess["user_id"]        = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"]   = app.SESSION_IDLE_TIMEOUT
        sess["sid"]            = "test-sid-" + username


class FakeAudioBroadcast:
    """Minimal stand-in for _AudioBroadcast: just enough surface
    (_lock/_client_meta/add_client/remove_client) for _stress_audio_client()
    and _run_audio_stress_test() to run against without real ffmpeg/Asterisk."""

    _dead = False

    def __init__(self):
        self._lock = threading.Lock()
        self._client_meta = {}

    def add_client(self, remote_addr="?"):
        q = queue.Queue(maxsize=25)
        q.put_nowait(b"fake-webm-chunk")
        with self._lock:
            self._client_meta[id(q)] = {"remote": remote_addr, "drops": 0}
        return q

    def remove_client(self, q, remote_addr="?"):
        with self._lock:
            self._client_meta.pop(id(q), None)


def _reset_stress_state():
    app._stress_state["audio"] = None
    app._stress_state["ami"] = None


def _wait_until_idle(client, kind, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = client.get("/api/stress-test/status").get_json()[kind]
        if not d or not d.get("running"):
            return d
        time.sleep(0.05)
    raise AssertionError(f"{kind} stress test did not finish within {timeout}s")


class TestRoleGating:
    def test_non_owner_rejected_on_every_route(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.post("/api/stress-test/audio/start", json={"node": "546054"}).status_code == 403
        assert client.post("/api/stress-test/audio/stop").status_code == 403
        assert client.post("/api/stress-test/ami/start", json={}).status_code == 403
        assert client.post("/api/stress-test/ami/stop").status_code == 403
        assert client.get("/api/stress-test/status").status_code == 403

    def test_owner_allowed(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        assert client.get("/api/stress-test/status").status_code == 200


class TestValidation:
    def test_audio_rejects_bad_node(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/stress-test/audio/start", json={"node": "not-a-node"})
        assert resp.status_code == 400

    def test_audio_rejects_out_of_range_concurrency(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/stress-test/audio/start",
                            json={"node": "546054", "concurrency": 0, "duration_sec": 5})
        assert resp.status_code == 400
        resp = client.post("/api/stress-test/audio/start",
                            json={"node": "546054", "concurrency": 101, "duration_sec": 5})
        assert resp.status_code == 400

    def test_audio_rejects_out_of_range_duration(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/stress-test/audio/start",
                            json={"node": "546054", "concurrency": 5, "duration_sec": 0})
        assert resp.status_code == 400
        resp = client.post("/api/stress-test/audio/start",
                            json={"node": "546054", "concurrency": 5, "duration_sec": 121})
        assert resp.status_code == 400

    def test_ami_rejects_out_of_range_concurrency(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/stress-test/ami/start", json={"concurrency": 31, "duration_sec": 5})
        assert resp.status_code == 400


class TestAudioStressRun:
    def test_full_run_against_fake_broadcast(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        _reset_stress_state()

        fake = FakeAudioBroadcast()
        monkeypatch.setitem(app._audio_active, "546054", fake)

        resp = client.post("/api/stress-test/audio/start",
                            json={"node": "546054", "concurrency": 3, "duration_sec": 1})
        assert resp.status_code == 200

        d = _wait_until_idle(client, "audio")
        assert d is not None
        assert d["node"] == "546054"
        assert d["clients_connected"] == 3
        assert d["clients_got_data"] == 3
        assert d["clients_no_data"] == 0
        assert d["total_bytes"] == 3 * len(b"fake-webm-chunk")
        # Every fake client was cleaned up -- no leaked entries in the fake
        # broadcast's own bookkeeping once the run finished.
        assert fake._client_meta == {}

    def test_rejects_starting_a_second_run_while_one_is_active(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        _reset_stress_state()

        fake = FakeAudioBroadcast()
        monkeypatch.setitem(app._audio_active, "546054", fake)

        resp1 = client.post("/api/stress-test/audio/start",
                             json={"node": "546054", "concurrency": 2, "duration_sec": 3})
        assert resp1.status_code == 200
        resp2 = client.post("/api/stress-test/audio/start",
                             json={"node": "546054", "concurrency": 2, "duration_sec": 3})
        assert resp2.status_code == 409

        # Stop it early so the test doesn't hang around for the full 3s.
        stop_resp = client.post("/api/stress-test/audio/stop")
        assert stop_resp.status_code == 200
        _wait_until_idle(client, "audio")

    def test_stop_ends_the_run_before_its_duration_elapses(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        _reset_stress_state()

        fake = FakeAudioBroadcast()
        monkeypatch.setitem(app._audio_active, "546054", fake)

        client.post("/api/stress-test/audio/start",
                     json={"node": "546054", "concurrency": 2, "duration_sec": 30})
        time.sleep(0.1)
        started = time.monotonic()
        stop_resp = client.post("/api/stress-test/audio/stop")
        assert stop_resp.status_code == 200
        d = _wait_until_idle(client, "audio", timeout=5.0)
        elapsed = time.monotonic() - started
        assert elapsed < 5.0, "stop should end the run almost immediately, not wait out the full duration"
        assert d["clients_connected"] == 2


class TestAmiStressRun:
    def test_full_run_with_stubbed_ami_send_command(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        _reset_stress_state()

        def fake_ami_send_command(fn):
            time.sleep(0.001)
            return {"ok": True}

        monkeypatch.setattr(app, "ami_send_command", fake_ami_send_command)

        resp = client.post("/api/stress-test/ami/start", json={"concurrency": 3, "duration_sec": 1})
        assert resp.status_code == 200

        d = _wait_until_idle(client, "ami")
        assert d is not None
        assert d["total_calls"] > 0
        assert d["total_errors"] == 0
        assert d["latency_ms_avg"] is not None
        assert d["calls_per_sec"] is not None

    def test_errors_from_ami_send_command_are_counted_not_raised(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        _reset_stress_state()

        def failing_ami_send_command(fn):
            raise RuntimeError("AMI unreachable")

        monkeypatch.setattr(app, "ami_send_command", failing_ami_send_command)

        resp = client.post("/api/stress-test/ami/start", json={"concurrency": 2, "duration_sec": 1})
        assert resp.status_code == 200

        d = _wait_until_idle(client, "ami")
        assert d["total_calls"] > 0
        assert d["total_errors"] == d["total_calls"]
