"""Node lockout (Owner-only): the central 423 gate in check_auth() plus the
one GET route that has to honor it itself (/api/tx/config)."""
import pytest

import app


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"] = True
        sess["username"] = username
        sess["role"] = row["role"]
        sess["user_id"] = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"] = app.SESSION_IDLE_TIMEOUT


def _lock():
    db = app.get_db()
    db.execute("INSERT INTO node_lockouts (node, locked_by) VALUES (?, ?)", ("64393", "owner1"))
    db.commit()


@pytest.fixture()
def tx_secret(tmp_path, monkeypatch):
    p = tmp_path / "tx.secret"
    p.write_text("sekret\n")
    monkeypatch.setattr(app, "TX_SECRET_PATH", str(p))


class TestLockoutGate:
    @pytest.mark.parametrize("role", ["user", "admin", "superuser"])
    def test_non_owner_mutations_blocked(self, client, create_user, role):
        create_user("owner1", role="owner")
        create_user("bob", role=role)
        _lock()
        _login(client, "bob")
        resp = client.post("/api/status/connect", json={"node": "1", "target": "2"})
        assert resp.status_code == 423
        assert resp.get_json()["locked"] is True

    def test_dvswitch_tune_blocked(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", role="user")
        _lock()
        _login(client, "bob")
        assert client.post("/api/dvswitch/tune", json={"tg": "91"}).status_code == 423

    def test_owner_not_blocked(self, client, create_user):
        create_user("owner1", role="owner")
        _lock()
        _login(client, "owner1")
        assert client.post("/api/status/connect", json={}).status_code != 423

    def test_unlocked_does_not_block(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", role="user")
        _login(client, "bob")
        assert client.post("/api/status/connect", json={}).status_code != 423

    def test_reads_still_allowed(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", role="user")
        _lock()
        _login(client, "bob")
        assert client.get("/api/status/board").status_code == 200

    def test_non_owner_cannot_unlock(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", role="superuser")
        _lock()
        _login(client, "bob")
        assert client.post("/api/nodes/64393/lockout", json={"locked": False}).status_code == 423
        assert app.is_any_node_locked()


class TestTxConfigHonorsLockout:
    @pytest.mark.parametrize("role", ["user", "admin", "superuser"])
    def test_credentials_withheld_while_locked(self, client, create_user, tx_secret, role):
        create_user("owner1", role="owner")
        create_user("bob", role=role)
        _lock()
        _login(client, "bob")
        for url in ("/api/tx/config", "/api/tx/config?probe=1"):
            resp = client.get(url)
            assert resp.status_code == 423
            assert "password" not in resp.get_json()

    def test_owner_still_gets_probe_while_locked(self, client, create_user, tx_secret):
        create_user("owner1", role="owner")
        _lock()
        _login(client, "owner1")
        resp = client.get("/api/tx/config?probe=1")
        assert resp.status_code == 200 and resp.get_json()["enabled"] is True

    def test_non_owner_gets_probe_when_unlocked(self, client, create_user, tx_secret):
        create_user("owner1", role="owner")
        create_user("bob", role="user")
        _login(client, "bob")
        assert client.get("/api/tx/config?probe=1").status_code == 200
