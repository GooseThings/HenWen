"""Tests for the Owner-triggered kiosk banner (issue #138):
_get_active_kiosk_banner()'s expiry logic, and the owner-gating on
POST/DELETE /api/kiosk/banner, mirroring tests/test_discord_relay.py's
pattern for an owner-only route. The banner also rides along on the public
/api/status/board payload -- covered here too, since that's how the kiosk
itself (and the Manager card) actually reads it.
"""
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


class TestGetActiveKioskBanner:
    def test_none_when_never_set(self, fresh_db):
        assert app._get_active_kiosk_banner() is None

    def test_active_banner_returned(self, fresh_db):
        fresh_db.execute(
            "INSERT INTO kiosk_banner (id, message, expires_at, created_by, created_at) "
            "VALUES (1, 'Node down for maintenance', ?, 'owner1', ?)",
            (time.time() + 300, time.time()),
        )
        fresh_db.commit()
        banner = app._get_active_kiosk_banner()
        assert banner is not None
        assert banner["message"] == "Node down for maintenance"
        assert banner["created_by"] == "owner1"

    def test_expired_banner_not_returned(self, fresh_db):
        fresh_db.execute(
            "INSERT INTO kiosk_banner (id, message, expires_at, created_by, created_at) "
            "VALUES (1, 'Old notice', ?, 'owner1', ?)",
            (time.time() - 1, time.time() - 300),
        )
        fresh_db.commit()
        assert app._get_active_kiosk_banner() is None

    def test_cleared_banner_not_returned(self, fresh_db):
        fresh_db.execute(
            "INSERT INTO kiosk_banner (id, message, expires_at, created_by, created_at) "
            "VALUES (1, '', NULL, 'owner1', ?)",
            (time.time(),),
        )
        fresh_db.commit()
        assert app._get_active_kiosk_banner() is None


class TestKioskBannerSetRoute:
    def test_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.post("/api/kiosk/banner", json={"message": "hi", "duration_min": 30})
        assert resp.status_code == 401

    def test_rejects_admin(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.post("/api/kiosk/banner", json={"message": "hi", "duration_min": 30})
        assert resp.status_code == 403

    def test_rejects_superuser(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("su1", password="password12345", role="superuser")
        _login(client, "su1")
        resp = client.post("/api/kiosk/banner", json={"message": "hi", "duration_min": 30})
        assert resp.status_code == 403

    def test_owner_can_set_and_it_persists(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/kiosk/banner",
                            json={"message": "Node down 1-2AM", "duration_min": 60})
        assert resp.status_code == 200
        banner = app._get_active_kiosk_banner()
        assert banner["message"] == "Node down 1-2AM"
        assert banner["created_by"] == "owner1"
        # expires_at should be ~60 minutes out
        assert 3595 <= (banner["expires_at"] - time.time()) <= 3600

    def test_rejects_empty_message(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/kiosk/banner", json={"message": "   ", "duration_min": 30})
        assert resp.status_code == 400

    def test_rejects_overlong_message(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/kiosk/banner",
                            json={"message": "x" * 301, "duration_min": 30})
        assert resp.status_code == 400

    def test_rejects_zero_duration(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/kiosk/banner", json={"message": "hi", "duration_min": 0})
        assert resp.status_code == 400

    def test_rejects_duration_over_max(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/kiosk/banner", json={"message": "hi", "duration_min": 10081})
        assert resp.status_code == 400

    def test_second_set_replaces_first(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.post("/api/kiosk/banner", json={"message": "First", "duration_min": 30})
        client.post("/api/kiosk/banner", json={"message": "Second", "duration_min": 15})
        banner = app._get_active_kiosk_banner()
        assert banner["message"] == "Second"


class TestKioskBannerClearRoute:
    def test_rejects_non_owner(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "owner1")
        client.post("/api/kiosk/banner", json={"message": "hi", "duration_min": 30})
        _login(client, "admin1")
        resp = client.delete("/api/kiosk/banner")
        assert resp.status_code == 403
        assert app._get_active_kiosk_banner() is not None

    def test_owner_can_clear(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.post("/api/kiosk/banner", json={"message": "hi", "duration_min": 30})
        assert app._get_active_kiosk_banner() is not None
        resp = client.delete("/api/kiosk/banner")
        assert resp.status_code == 200
        assert app._get_active_kiosk_banner() is None


class TestKioskBannerOnStatusBoard:
    def test_board_reports_null_when_no_banner(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/api/status/board")
        assert resp.status_code == 200
        assert resp.get_json()["banner"] is None

    def test_board_reports_active_banner_without_login(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.post("/api/kiosk/banner", json={"message": "Net at 7PM tonight", "duration_min": 30})
        client.post("/logout")  # board is public; confirm it's readable logged-out too
        resp = client.get("/api/status/board")
        assert resp.status_code == 200
        banner = resp.get_json()["banner"]
        assert banner["message"] == "Net at 7PM tonight"
