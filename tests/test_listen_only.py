"""Tests for Listen-Only accounts (issue #157): a per-user flag, meaningful
only for role='user', for an unlicensed listener. Such an account may
connect/disconnect a node exactly like any other 'user' account (one
connection at a time, and only ever tearing down a link it made itself --
never someone else's pre-existing connection); the one thing it's actually
blocked from is pulling Browser TX credentials, since that personally keys
the repeater's real transmitter under the club callsign in a way linking two
already-authorized nodes does not (see api_tx_config()'s docstring). Mirrors
the existing restrict_disconnect tests in test_invites.py for the
invite-carries-the-flag case.
"""
import re

import pytest

import app


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """accept_invite() carries a real rate limit; app.limiter is built once
    at import time before conftest's RATELIMIT_ENABLED=False update runs, so
    its in-memory storage persists across the whole test session. Reset it
    before every test here so this file's request volume can't trip it (see
    the identical fixture in test_invites.py)."""
    app.limiter.reset()


@pytest.fixture(autouse=True)
def _clean_kiosk_temp_conns():
    """_kiosk_temp_conns is a module-level in-process dict (not reset by the
    fresh_db fixture, which only resets the DB), and the disconnect-ownership
    gate below reads/writes it directly -- clear it around every test in this
    file so state can't leak between tests sharing this one process."""
    app._kiosk_temp_conns.clear()
    yield
    app._kiosk_temp_conns.clear()


def _login(client, username, password="password12345"):
    return client.post("/login", data={"username": username, "password": password})


def _make_listen_only_user(fresh_db, username="listener1", password="listeners-password1"):
    from werkzeug.security import generate_password_hash

    fresh_db.execute(
        "INSERT INTO users (username, password_hash, role, listen_only) VALUES (?,?,?,1)",
        (username, generate_password_hash(password), "user"),
    )
    fresh_db.commit()
    return username, password


class _FakeAmi:
    """Fails the test if a connect/disconnect route ever reaches the AMI --
    used for the cases that must be rejected before any AMI command is sent."""

    def rpt_cmd(self, node, cmd):
        raise AssertionError(f"AMI command should never be sent here: {cmd!r}")


class _RecordingAmi:
    def __init__(self, sent):
        self._sent = sent

    def rpt_cmd(self, node, cmd):
        self._sent["cmd"] = cmd
        return "OK"


class TestConnectBehavesLikeAPlainUser:
    def test_connect_succeeds_for_listen_only_account(self, client, create_user, fresh_db, monkeypatch):
        create_user("owner1", role="owner")
        username, password = _make_listen_only_user(fresh_db)
        sent = {}
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_RecordingAmi(sent)))
        _login(client, username, password)
        resp = client.post("/api/status/connect", json={"local_node": "546054", "remote_node": "546055"})
        assert resp.status_code == 200
        assert "cmd" in sent
        # And it's attributed to the caller, same as any other account --
        # this is what the disconnect-ownership gate keys off of.
        entry = app._kiosk_temp_conns.get(("546054", "546055"))
        assert entry and entry["initiated_by"] == username

    def test_plain_user_account_is_unaffected(self, client, create_user, monkeypatch):
        """A regular (non-listen-only) user account must still reach the AMI
        call -- confirms the new gate doesn't leak onto every 'user' role."""
        create_user("owner1", role="owner")
        create_user("kiosk1", password="kiosk1s-password1", role="user")
        sent = {}
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_RecordingAmi(sent)))
        _login(client, "kiosk1", "kiosk1s-password1")
        resp = client.post("/api/status/connect", json={"local_node": "546054", "remote_node": "546055"})
        assert resp.status_code == 200
        assert "cmd" in sent


class TestDisconnectOwnershipGate:
    def test_disconnecting_own_connection_succeeds(self, client, create_user, fresh_db, monkeypatch):
        create_user("owner1", role="owner")
        username, password = _make_listen_only_user(fresh_db)
        app._kiosk_temp_conns[("546054", "546055")] = {
            "permanent": False, "monitor": False, "no_timeout": False,
            "last_active": 0, "initiated_by": username,
        }
        sent = {}
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_RecordingAmi(sent)))
        _login(client, username, password)
        resp = client.post("/api/status/disconnect", json={"local_node": "546054", "remote_node": "546055"})
        assert resp.status_code == 200
        assert "cmd" in sent

    def test_disconnecting_someone_elses_connection_is_rejected(self, client, create_user, fresh_db, monkeypatch):
        create_user("owner1", role="owner")
        username, password = _make_listen_only_user(fresh_db)
        app._kiosk_temp_conns[("546054", "546055")] = {
            "permanent": False, "monitor": False, "no_timeout": False,
            "last_active": 0, "initiated_by": "someone-else",
        }
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_FakeAmi()))
        _login(client, username, password)
        resp = client.post("/api/status/disconnect", json={"local_node": "546054", "remote_node": "546055"})
        assert resp.status_code == 403
        assert "listen-only" in resp.get_json()["error"]

    def test_disconnecting_an_untracked_connection_is_rejected(self, client, create_user, fresh_db, monkeypatch):
        """No _kiosk_temp_conns entry at all -- a pre-existing connection made
        outside the kiosk (or by a different session before a restart wiped
        the in-process dict) must be treated as "not theirs" too."""
        create_user("owner1", role="owner")
        username, password = _make_listen_only_user(fresh_db)
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_FakeAmi()))
        _login(client, username, password)
        resp = client.post("/api/status/disconnect", json={"local_node": "546054", "remote_node": "546055"})
        assert resp.status_code == 403
        assert "listen-only" in resp.get_json()["error"]

    def test_plain_user_may_disconnect_a_connection_it_did_not_make(self, client, create_user, monkeypatch):
        """The ownership restriction is specific to listen_only -- a regular
        'user' account keeps today's behavior (can disconnect anything not
        Smart-Connector-managed, regardless of who connected it)."""
        create_user("owner1", role="owner")
        create_user("kiosk1", password="kiosk1s-password1", role="user")
        app._kiosk_temp_conns[("546054", "546055")] = {
            "permanent": False, "monitor": False, "no_timeout": False,
            "last_active": 0, "initiated_by": "someone-else",
        }
        sent = {}
        monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(_RecordingAmi(sent)))
        _login(client, "kiosk1", "kiosk1s-password1")
        resp = client.post("/api/status/disconnect", json={"local_node": "546054", "remote_node": "546055"})
        assert resp.status_code == 200
        assert "cmd" in sent


class TestTxConfigBlocked:
    def _write_secret(self, tmp_path, monkeypatch):
        secret_path = tmp_path / "henwen-tx.secret"
        secret_path.write_text("s3kret\n")
        monkeypatch.setattr(app, "TX_SECRET_PATH", str(secret_path))

    def test_probe_reports_unavailable_for_listen_only_account(self, client, create_user, fresh_db, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        username, password = _make_listen_only_user(fresh_db)
        self._write_secret(tmp_path, monkeypatch)
        _login(client, username, password)
        resp = client.get("/api/tx/config?probe=1")
        assert resp.status_code == 404
        assert resp.get_json()["enabled"] is False

    def test_full_credential_request_is_blocked_for_listen_only_account(self, client, create_user, fresh_db, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        username, password = _make_listen_only_user(fresh_db)
        self._write_secret(tmp_path, monkeypatch)
        _login(client, username, password)
        resp = client.get("/api/tx/config")
        assert resp.status_code == 404
        body = resp.get_json()
        assert body["enabled"] is False
        assert "username" not in body  # never mints a real credential

    def test_plain_user_account_still_gets_tx_credentials(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        create_user("kiosk1", password="kiosk1s-password1", role="user")
        self._write_secret(tmp_path, monkeypatch)
        # api_tx_config() also needs a local node configured to succeed --
        # stub get_node_numbers so this test isn't coupled to rpt.conf parsing.
        monkeypatch.setattr(app, "read_conf_file", lambda path: "fake")
        monkeypatch.setattr(app, "get_node_numbers", lambda content: [546054])
        _login(client, "kiosk1", "kiosk1s-password1")
        resp = client.get("/api/tx/config?probe=1")
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is True


class TestSessionAndLoginReportListenOnly:
    def test_api_session_reports_listen_only_true(self, client, create_user, fresh_db):
        create_user("owner1", role="owner")
        username, password = _make_listen_only_user(fresh_db)
        _login(client, username, password)
        resp = client.get("/api/session")
        assert resp.get_json()["listen_only"] is True

    def test_api_login_reports_listen_only_true(self, client, create_user, fresh_db):
        create_user("owner1", role="owner")
        username, password = _make_listen_only_user(fresh_db)
        resp = client.post("/api/login", json={"username": username, "password": password})
        assert resp.get_json()["listen_only"] is True

    def test_api_session_reports_listen_only_false_for_normal_account(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("kiosk1", password="kiosk1s-password1", role="user")
        _login(client, "kiosk1", "kiosk1s-password1")
        resp = client.get("/api/session")
        assert resp.get_json()["listen_only"] is False


class TestUserManagementApiPersistsListenOnly:
    def test_create_user_persists_listen_only(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/users", json={
            "username": "listener2", "password": "listeners-password2",
            "role": "user", "listen_only": True,
        })
        assert resp.status_code == 200
        row = app.get_db().execute(
            "SELECT listen_only FROM users WHERE username='listener2'"
        ).fetchone()
        assert row["listen_only"] == 1

    def test_listen_only_is_ignored_for_non_user_roles(self, client, create_user):
        """Mirrors restrict_disconnect's own rule -- the flag only means
        something for role='user'; other roles bypass the gates entirely."""
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/users", json={
            "username": "admin2", "password": "admins-password2",
            "role": "admin", "listen_only": True,
        })
        assert resp.status_code == 200
        row = app.get_db().execute(
            "SELECT listen_only FROM users WHERE username='admin2'"
        ).fetchone()
        assert row["listen_only"] == 0

    def test_update_user_toggles_listen_only(self, client, create_user):
        create_user("owner1", role="owner")
        kiosk = create_user("kiosk1", password="kiosk1s-password1", role="user")
        _login(client, "owner1")
        resp = client.put(f"/api/users/{kiosk['id']}", json={"listen_only": True})
        assert resp.status_code == 200
        row = app.get_db().execute("SELECT listen_only FROM users WHERE id=?", (kiosk["id"],)).fetchone()
        assert row["listen_only"] == 1

        resp2 = client.put(f"/api/users/{kiosk['id']}", json={"listen_only": False})
        assert resp2.status_code == 200
        row2 = app.get_db().execute("SELECT listen_only FROM users WHERE id=?", (kiosk["id"],)).fetchone()
        assert row2["listen_only"] == 0


class TestInviteCarriesListenOnly:
    def test_invite_carries_listen_only_onto_new_account(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/invites", json={"role": "user", "listen_only": True})
        assert resp.status_code == 200
        token = re.search(r"token=([^&]+)", resp.get_json()["invite_url"]).group(1)
        client.get("/logout")

        client.post("/accept-invite", data={
            "token": token, "username": "listener3",
            "new_password": "listeners-password3", "confirm_password": "listeners-password3",
        })
        row = app.get_db().execute(
            "SELECT listen_only FROM users WHERE username='listener3'"
        ).fetchone()
        assert row["listen_only"] == 1
