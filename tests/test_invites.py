"""Tests for the invite-link flow: an admin+ creates an invite for a chosen
role at /api/invites (Manager > User Management "+ Invite"), and the
one-time link that mints lets the invitee pick their own username/password
at /accept-invite -- so the account's password is never known to whoever
created it. See the "User invite links" section in app.py (near
accept_invite()) and invites' CREATE TABLE comment in get_db().
"""
import re

import pytest

import app


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """accept_invite() carries a real rate limit; app.limiter is built once
    at import time before conftest's RATELIMIT_ENABLED=False update runs, so
    its in-memory storage persists across the whole test session. Reset it
    before every test here so this file's request volume can't trip it."""
    app.limiter.reset()


def _login(client, username, password="password12345"):
    return client.post("/login", data={"username": username, "password": password})


def _create_invite(client, role="user", restrict_disconnect=False, can_record=None):
    # Mirrors the real UI (henwen-manager.html createInvite()): "can_record"
    # is only sent at all when the caller is the owner -- api_invites_create()
    # gates on the key's mere *presence* in the JSON body (same rule
    # api_users_create() already uses), so a non-owner caller must omit it
    # entirely rather than send can_record=False.
    payload = {"role": role, "restrict_disconnect": restrict_disconnect}
    if can_record is not None:
        payload["can_record"] = can_record
    return client.post("/api/invites", json=payload)


def _token_from(resp):
    return re.search(r"token=([^&]+)", resp.get_json()["invite_url"]).group(1)


class TestCreateInviteRankGating:
    def test_requires_admin_login(self, client, create_user):
        create_user("owner1", role="owner")
        resp = _create_invite(client)
        assert resp.status_code == 401

    def test_admin_cannot_create_an_admin_invite(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="admins-password1", role="admin")
        _login(client, "admin1", "admins-password1")
        resp = _create_invite(client, role="admin")
        assert resp.status_code == 403
        assert app.get_db().execute("SELECT COUNT(*) c FROM invites").fetchone()["c"] == 0

    def test_admin_can_create_a_user_invite(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="admins-password1", role="admin")
        _login(client, "admin1", "admins-password1")
        resp = _create_invite(client, role="user")
        assert resp.status_code == 200
        assert "token=" in resp.get_json()["invite_url"]

    def test_only_owner_can_grant_can_record(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("super1", password="supers-password1", role="superuser")
        _login(client, "super1", "supers-password1")
        resp = _create_invite(client, role="user", can_record=True)
        assert resp.status_code == 403

    def test_owner_invite_stores_only_token_hash(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        _create_invite(client, role="user")
        row = app.get_db().execute("SELECT token_hash FROM invites").fetchone()
        assert row["token_hash"]  # never the plaintext token


class TestPendingInvitesListAndRankGating:
    def test_admin_sees_user_invite_but_not_admin_invite(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="admins-password1", role="admin")
        _login(client, "owner1")
        _create_invite(client, role="user")
        _create_invite(client, role="admin")
        client.get("/logout")
        _login(client, "admin1", "admins-password1")
        resp = client.get("/api/invites")
        roles = {i["role"] for i in resp.get_json()["invites"]}
        assert roles == {"user"}

    def test_owner_sees_every_pending_invite(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        _create_invite(client, role="user")
        _create_invite(client, role="admin")
        resp = client.get("/api/invites")
        roles = {i["role"] for i in resp.get_json()["invites"]}
        assert roles == {"user", "admin"}


class TestRevoke:
    def test_revoked_invite_link_no_longer_works(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = _create_invite(client, role="user")
        token = _token_from(resp)
        inv_id = app.get_db().execute("SELECT id FROM invites").fetchone()["id"]
        revoke_resp = client.post(f"/api/invites/{inv_id}/revoke")
        assert revoke_resp.status_code == 200
        client.get("/logout")
        get_resp = client.get(f"/accept-invite?token={token}")
        assert b"Invalid or Expired" in get_resp.data

    def test_admin_cannot_revoke_an_admin_invite(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="admins-password1", role="admin")
        _login(client, "owner1")
        _create_invite(client, role="admin")
        inv_id = app.get_db().execute("SELECT id FROM invites").fetchone()["id"]
        client.get("/logout")
        _login(client, "admin1", "admins-password1")
        resp = client.post(f"/api/invites/{inv_id}/revoke")
        assert resp.status_code == 403
        row = app.get_db().execute("SELECT status FROM invites WHERE id=?", (inv_id,)).fetchone()
        assert row["status"] == "pending"


class TestAcceptInvite:
    def test_full_flow_creates_account_with_invited_role_and_logs_in(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = _create_invite(client, role="admin")
        token = _token_from(resp)
        client.get("/logout")

        get_resp = client.get(f"/accept-invite?token={token}")
        assert b"You're Invited" in get_resp.data

        post_resp = client.post("/accept-invite", data={
            "token": token, "username": "newbie",
            "new_password": "newbies-password1", "confirm_password": "newbies-password1",
        })
        assert post_resp.status_code in (302, 303)

        row = app.get_db().execute("SELECT role FROM users WHERE username='newbie'").fetchone()
        assert row["role"] == "admin"

        invite_row = app.get_db().execute("SELECT status, used_by FROM invites").fetchone()
        assert invite_row["status"] == "used"
        assert invite_row["used_by"] == "newbie"

        # The POST itself already established a session (auto-login) --
        # confirm the chosen password also works for a fresh login.
        client.get("/logout")
        login_resp = _login(client, "newbie", "newbies-password1")
        assert login_resp.status_code in (302, 303)

    def test_invite_carries_restrict_disconnect_onto_new_account(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = _create_invite(client, role="user", restrict_disconnect=True)
        token = _token_from(resp)
        client.get("/logout")
        client.post("/accept-invite", data={
            "token": token, "username": "kiosk1",
            "new_password": "kiosk1s-password1", "confirm_password": "kiosk1s-password1",
        })
        row = app.get_db().execute(
            "SELECT restrict_disconnect FROM users WHERE username='kiosk1'"
        ).fetchone()
        assert row["restrict_disconnect"] == 1

    def test_token_is_single_use(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = _create_invite(client, role="user")
        token = _token_from(resp)
        client.get("/logout")
        client.post("/accept-invite", data={
            "token": token, "username": "first",
            "new_password": "firsts-password1", "confirm_password": "firsts-password1",
        })
        client.get("/logout")
        reuse_resp = client.get(f"/accept-invite?token={token}")
        assert b"Invalid or Expired" in reuse_resp.data

    def test_duplicate_username_is_rejected_without_consuming_token(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        _login(client, "owner1")
        resp = _create_invite(client, role="user")
        token = _token_from(resp)
        client.get("/logout")
        post_resp = client.post("/accept-invite", data={
            "token": token, "username": "bob",
            "new_password": "bobs-new-password1", "confirm_password": "bobs-new-password1",
        })
        assert b"already exists" in post_resp.data
        row = app.get_db().execute("SELECT status FROM invites").fetchone()
        assert row["status"] == "pending"  # not consumed by the failed attempt

    def test_short_password_is_rejected(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = _create_invite(client, role="user")
        token = _token_from(resp)
        client.get("/logout")
        post_resp = client.post("/accept-invite", data={
            "token": token, "username": "newbie",
            "new_password": "short", "confirm_password": "short",
        })
        assert b"at least 8 characters" in post_resp.data

    def test_mismatched_confirm_is_rejected(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = _create_invite(client, role="user")
        token = _token_from(resp)
        client.get("/logout")
        post_resp = client.post("/accept-invite", data={
            "token": token, "username": "newbie",
            "new_password": "newbies-password1", "confirm_password": "does-not-match1",
        })
        assert b"do not match" in post_resp.data


class TestAcceptInvitePageRejectsBadTokens:
    def test_missing_token_shows_invalid_stage(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/accept-invite")
        assert b"Invalid or Expired" in resp.data

    def test_garbage_token_shows_invalid_stage(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/accept-invite?token=not-a-real-token")
        assert b"Invalid or Expired" in resp.data

    def test_expired_token_is_rejected(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = _create_invite(client, role="user")
        token = _token_from(resp)
        inv_id = app.get_db().execute("SELECT id FROM invites").fetchone()["id"]
        # Force the stored expiry into the past instead of sleeping 24 hours.
        app.get_db().execute("UPDATE invites SET expires_at=0 WHERE id=?", (inv_id,))
        app.get_db().commit()
        client.get("/logout")
        get_resp = client.get(f"/accept-invite?token={token}")
        assert b"Invalid or Expired" in get_resp.data
