"""Tests for the self-service "Forgot password?" flow: a logged-out user
requests a reset at /forgot-password, an admin+ approves it from Manager >
User Management (api_password_reset_request_approve), and the resulting
one-time link completes the change at /reset-password. See the
"Self-service password reset" section in app.py (near forgot_password()) and
password_reset_requests' CREATE TABLE comment in get_db() for why there's no
automatic email/SMS delivery -- the approving admin relays the link
out-of-band.
"""
import re

import pytest

import app


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """forgot_password()/reset_password_page() carry real per-minute rate
    limits (5/min and 20/min). app.limiter is constructed once at import
    time -- before conftest.py's RATELIMIT_ENABLED=False config update runs
    -- so that flag never actually reaches it, and its in-memory storage
    persists across the whole test session rather than per-test like
    fresh_db does. Reset it before every test here so this file's own
    request volume can't spill across test boundaries or trip a limit these
    tests aren't about."""
    app.limiter.reset()


def _login(client, username, password="password12345"):
    return client.post("/login", data={"username": username, "password": password})


def _request_reset(client, username):
    return client.post("/forgot-password", data={"username": username})


class TestRequestReset:
    def test_existing_user_creates_a_pending_request(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        resp = _request_reset(client, "bob")
        assert resp.status_code == 200
        row = app.get_db().execute(
            "SELECT status FROM password_reset_requests WHERE username='bob'"
        ).fetchone()
        assert row["status"] == "pending"

    def test_unknown_username_gets_identical_response_and_no_row(self, client, create_user):
        create_user("owner1", role="owner")
        real_resp    = _request_reset(client, "owner1")
        unknown_resp = _request_reset(client, "nobody-here")
        assert real_resp.status_code == unknown_resp.status_code == 200
        assert real_resp.data == unknown_resp.data  # no username-enumeration signal
        rows = app.get_db().execute(
            "SELECT username FROM password_reset_requests"
        ).fetchall()
        assert [r["username"] for r in rows] == ["owner1"]

    def test_duplicate_request_does_not_create_a_second_pending_row(self, client, create_user):
        create_user("owner1", role="owner")
        _request_reset(client, "owner1")
        _request_reset(client, "owner1")
        rows = app.get_db().execute(
            "SELECT id FROM password_reset_requests WHERE username='owner1' AND status='pending'"
        ).fetchall()
        assert len(rows) == 1

    def test_login_page_links_to_forgot_password(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/login")
        assert b'/forgot-password' in resp.data


class TestRequestNotifiesOwner:
    def test_fires_alert_when_enabled(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute(
            "INSERT OR REPLACE INTO alert_config (id, enabled, on_password_reset_request) "
            "VALUES (1, 1, 1)"
        )
        db.commit()
        calls = []
        monkeypatch.setattr(
            app, "_send_alert",
            lambda title, msg, priority="default": calls.append((title, msg))
        )
        _request_reset(client, "owner1")
        assert len(calls) == 1
        assert "Password Reset Requested" in calls[0][0]
        assert "owner1" in calls[0][1]

    def test_no_alert_when_toggle_off(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute(
            "INSERT OR REPLACE INTO alert_config (id, enabled, on_password_reset_request) "
            "VALUES (1, 1, 0)"
        )
        db.commit()
        calls = []
        monkeypatch.setattr(
            app, "_send_alert",
            lambda title, msg, priority="default": calls.append((title, msg))
        )
        _request_reset(client, "owner1")
        assert calls == []

    def test_no_alert_when_unknown_username(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute(
            "INSERT OR REPLACE INTO alert_config (id, enabled, on_password_reset_request) "
            "VALUES (1, 1, 1)"
        )
        db.commit()
        calls = []
        monkeypatch.setattr(
            app, "_send_alert",
            lambda title, msg, priority="default": calls.append((title, msg))
        )
        _request_reset(client, "nobody-here")
        assert calls == []


class TestPendingRequestsListAndRankGating:
    def test_requires_admin_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/api/users/password-reset-requests")
        assert resp.status_code == 401

    def test_admin_sees_user_role_request_but_not_owner_role_request(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="admins-password1", role="admin")
        create_user("bob", password="bobs-password1", role="user")
        _request_reset(client, "owner1")
        _request_reset(client, "bob")
        client.get("/logout")  # drop the anonymous session used by _request_reset
        _login(client, "admin1", "admins-password1")
        resp = client.get("/api/users/password-reset-requests")
        assert resp.status_code == 200
        usernames = {r["username"] for r in resp.get_json()["requests"]}
        assert usernames == {"bob"}

    def test_owner_sees_every_pending_request(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        _request_reset(client, "owner1")
        _request_reset(client, "bob")
        client.get("/logout")
        _login(client, "owner1")
        resp = client.get("/api/users/password-reset-requests")
        usernames = {r["username"] for r in resp.get_json()["requests"]}
        assert usernames == {"owner1", "bob"}


class TestApproveAndDeny:
    def test_admin_cannot_approve_a_reset_for_an_owner_account(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="admins-password1", role="admin")
        _request_reset(client, "owner1")
        client.get("/logout")
        _login(client, "admin1", "admins-password1")
        req_id = app.get_db().execute(
            "SELECT id FROM password_reset_requests WHERE username='owner1'"
        ).fetchone()["id"]
        resp = client.post(f"/api/users/password-reset-requests/{req_id}/approve")
        assert resp.status_code == 403
        row = app.get_db().execute(
            "SELECT status FROM password_reset_requests WHERE id=?", (req_id,)
        ).fetchone()
        assert row["status"] == "pending"

    def test_owner_approve_returns_a_working_one_time_link(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        _request_reset(client, "bob")
        client.get("/logout")
        _login(client, "owner1")
        req_id = app.get_db().execute(
            "SELECT id FROM password_reset_requests WHERE username='bob'"
        ).fetchone()["id"]
        resp = client.post(f"/api/users/password-reset-requests/{req_id}/approve")
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert "token=" in body["reset_url"]
        row = app.get_db().execute(
            "SELECT status, token_hash FROM password_reset_requests WHERE id=?", (req_id,)
        ).fetchone()
        assert row["status"] == "approved"
        assert row["token_hash"]  # only the hash is stored, never the plaintext token

        token = re.search(r"token=([^&]+)", body["reset_url"]).group(1)
        client.get("/logout")
        get_resp = client.get(f"/reset-password?token={token}")
        assert b"Set New Password" in get_resp.data

        post_resp = client.post("/reset-password", data={
            "token": token, "new_password": "brand-new-pass1", "confirm_password": "brand-new-pass1",
        })
        assert b"Password Updated" in post_resp.data

        # New password actually works, and the token is now single-use-spent.
        login_resp = _login(client, "bob", "brand-new-pass1")
        assert login_resp.status_code in (302, 303)
        client.get("/logout")
        reuse_resp = client.get(f"/reset-password?token={token}")
        assert b"Invalid or Expired" in reuse_resp.data

    def test_deny_leaves_password_unchanged_and_clears_pending_list(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        _request_reset(client, "bob")
        client.get("/logout")
        _login(client, "owner1")
        req_id = app.get_db().execute(
            "SELECT id FROM password_reset_requests WHERE username='bob'"
        ).fetchone()["id"]
        resp = client.post(f"/api/users/password-reset-requests/{req_id}/deny")
        assert resp.status_code == 200
        row = app.get_db().execute(
            "SELECT status FROM password_reset_requests WHERE id=?", (req_id,)
        ).fetchone()
        assert row["status"] == "denied"
        pending = client.get("/api/users/password-reset-requests").get_json()["requests"]
        assert pending == []
        client.get("/logout")
        login_resp = _login(client, "bob", "bobs-password1")
        assert login_resp.status_code in (302, 303)  # original password still works


class TestResetPageRejectsBadTokens:
    def test_missing_token_shows_invalid_stage(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/reset-password")
        assert b"Invalid or Expired" in resp.data

    def test_garbage_token_shows_invalid_stage(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/reset-password?token=not-a-real-token")
        assert b"Invalid or Expired" in resp.data

    def test_expired_token_is_rejected(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        _request_reset(client, "bob")
        client.get("/logout")
        _login(client, "owner1")
        req_id = app.get_db().execute(
            "SELECT id FROM password_reset_requests WHERE username='bob'"
        ).fetchone()["id"]
        resp = client.post(f"/api/users/password-reset-requests/{req_id}/approve")
        token = re.search(r"token=([^&]+)", resp.get_json()["reset_url"]).group(1)
        # Force the stored expiry into the past instead of sleeping 30 minutes.
        app.get_db().execute(
            "UPDATE password_reset_requests SET token_expires_at=0 WHERE id=?", (req_id,)
        )
        app.get_db().commit()
        client.get("/logout")
        get_resp = client.get(f"/reset-password?token={token}")
        assert b"Invalid or Expired" in get_resp.data

    def test_short_new_password_is_rejected_without_consuming_token(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        _request_reset(client, "bob")
        client.get("/logout")
        _login(client, "owner1")
        req_id = app.get_db().execute(
            "SELECT id FROM password_reset_requests WHERE username='bob'"
        ).fetchone()["id"]
        resp  = client.post(f"/api/users/password-reset-requests/{req_id}/approve")
        token = re.search(r"token=([^&]+)", resp.get_json()["reset_url"]).group(1)
        client.get("/logout")

        post_resp = client.post("/reset-password", data={
            "token": token, "new_password": "short", "confirm_password": "short",
        })
        assert b"at least 8 characters" in post_resp.data
        row = app.get_db().execute(
            "SELECT status FROM password_reset_requests WHERE id=?", (req_id,)
        ).fetchone()
        assert row["status"] == "approved"  # not consumed by the failed attempt
