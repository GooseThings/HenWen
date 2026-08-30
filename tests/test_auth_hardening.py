"""Tests for the auth-hardening pass: soft per-account login lockout,
password-epoch session invalidation, the optional PASSWORD_PEPPER upgrade
path, case-insensitive usernames, password length bounds, the (mocked)
breached-password check, and TOTP-based two-factor authentication
(enrollment, login challenge, recovery codes, admin-forced reset). See the
"Auth hardening helpers" section in app.py (near _do_login_attempt()) and
the users table's ALTER TABLE migration block in get_db() for the new
columns these exercise.
"""
import pyotp
import pytest
from werkzeug.security import generate_password_hash

import app


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """/login, /login/2fa, and the /api/mfa/* routes all carry real
    per-minute limits; app.limiter is built once at import time before
    conftest's RATELIMIT_ENABLED=False update runs, so its in-memory
    storage persists across the whole test session. Reset it before every
    test here so this file's request volume (several logins per lockout
    test) can't trip a limit or spill across test boundaries."""
    app.limiter.reset()


def _login(client, username, password="password12345"):
    return client.post("/login", data={"username": username, "password": password})


def _api_login(client, username, password="password12345"):
    return client.post("/api/login", json={"username": username, "password": password})


class TestLoginLockout:
    def test_locks_after_threshold_and_generic_message_throughout(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        for _ in range(app.LOGIN_LOCKOUT_THRESHOLD):
            resp = _login(client, "bob", "wrong-password")
            assert b"Invalid username or password" in resp.data
        row = app.get_db().execute(
            "SELECT failed_login_count, locked_until FROM users WHERE username='bob'"
        ).fetchone()
        assert row["failed_login_count"] == app.LOGIN_LOCKOUT_THRESHOLD
        assert row["locked_until"] and row["locked_until"] > app.time.time()

        # Even the *correct* password is rejected while locked, with the
        # exact same generic message -- no distinct "account locked" text.
        resp = _login(client, "bob", "bobs-password1")
        assert resp.status_code == 200
        assert b"Invalid username or password" in resp.data

    def test_successful_login_clears_a_partial_failure_count(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        _login(client, "bob", "wrong-password")
        _login(client, "bob", "wrong-password")
        resp = _login(client, "bob", "bobs-password1")
        assert resp.status_code in (302, 303)
        row = app.get_db().execute(
            "SELECT failed_login_count FROM users WHERE username='bob'"
        ).fetchone()
        assert row["failed_login_count"] == 0

    def test_lock_expires_after_the_backoff_window(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        for _ in range(app.LOGIN_LOCKOUT_THRESHOLD):
            _login(client, "bob", "wrong-password")
        # Force the lock into the past instead of sleeping out the backoff.
        app.get_db().execute("UPDATE users SET locked_until=1 WHERE username='bob'")
        app.get_db().commit()
        resp = _login(client, "bob", "bobs-password1")
        assert resp.status_code in (302, 303)

    def test_unknown_username_never_locks_or_errors(self, client, create_user):
        create_user("owner1", role="owner")
        for _ in range(app.LOGIN_LOCKOUT_THRESHOLD + 2):
            resp = _login(client, "totally-nobody", "whatever")
            assert resp.status_code == 200
            assert b"Invalid username or password" in resp.data


class TestPasswordEpochInvalidatesOtherSessions:
    def test_self_service_change_logs_out_other_sessions(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")

        client_a = app.app.test_client()
        client_b = app.app.test_client()
        _login(client_a, "bob", "bobs-password1")
        _login(client_b, "bob", "bobs-password1")
        assert client_a.get("/api/session").get_json()["logged_in"] is True
        assert client_b.get("/api/session").get_json()["logged_in"] is True

        r = client_a.post("/api/auth/change-password", json={
            "current_password": "bobs-password1", "new_password": "bobs-new-password1",
        })
        assert r.status_code == 200

        # client_a changed it in place -- its own session must still work.
        assert client_a.get("/api/session").get_json()["logged_in"] is True
        # client_b's session predates the change and must be force-logged-out
        # on its next request (check_auth()'s password_epoch check).
        assert client_b.get("/api/session").get_json()["logged_in"] is False

    def test_admin_forced_password_update_logs_out_the_users_session(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        bob_client = app.app.test_client()
        _login(bob_client, "bob", "bobs-password1")
        assert bob_client.get("/api/session").get_json()["logged_in"] is True

        _login(client, "owner1")
        bob_id = app.get_db().execute("SELECT id FROM users WHERE username='bob'").fetchone()["id"]
        r = client.put(f"/api/users/{bob_id}", json={"password": "admin-set-password1"})
        assert r.status_code == 200

        assert bob_client.get("/api/session").get_json()["logged_in"] is False


class TestPasswordPepperUpgrade:
    def test_legacy_unpeppered_hash_still_verifies_and_gets_upgraded(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        # Simulate an account whose hash predates PASSWORD_PEPPER being set:
        # stored without any pepper mixed in.
        app.get_db().execute(
            "INSERT INTO users (username, password_hash, role) VALUES (?,?,?)",
            ("legacy", generate_password_hash("legacys-password1"), "user"),
        )
        app.get_db().commit()
        before = app.get_db().execute("SELECT password_hash FROM users WHERE username='legacy'").fetchone()

        monkeypatch.setattr(app, "PASSWORD_PEPPER", "a-fresh-pepper-secret")
        resp = _login(client, "legacy", "legacys-password1")
        assert resp.status_code in (302, 303)

        after = app.get_db().execute("SELECT password_hash FROM users WHERE username='legacy'").fetchone()
        assert after["password_hash"] != before["password_hash"]

        # Logging in again now goes through the peppered branch directly.
        client.get("/logout")
        resp2 = _login(client, "legacy", "legacys-password1")
        assert resp2.status_code in (302, 303)


class TestCaseInsensitiveUsernames:
    def test_login_is_case_insensitive(self, client, create_user):
        create_user("Owner1", role="owner")
        resp = _login(client, "OWNER1", "password12345")
        assert resp.status_code in (302, 303)

    def test_create_user_rejects_case_variant_duplicate(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("Bob", password="bobs-password1", role="user")
        _login(client, "owner1")
        r = client.post("/api/users", json={
            "username": "bob", "password": "another-password1", "role": "user",
        })
        assert r.status_code == 409


class TestPasswordLengthBounds:
    def test_rejects_password_over_max_length(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        r = client.post("/api/users", json={
            "username": "toolong", "password": "x" * (app.PASSWORD_MAX_LEN + 1), "role": "user",
        })
        assert r.status_code == 400
        assert "256" in r.get_json()["error"] or str(app.PASSWORD_MAX_LEN) in r.get_json()["error"]


class TestBreachedPasswordCheck:
    def test_rejected_when_breached(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        monkeypatch.setattr(app, "_is_password_breached", lambda pw: True)
        resp = client.post("/api/users", json={
            "username": "newuser", "password": "password12345", "role": "user",
        })
        assert resp.status_code == 400
        assert "data breach" in resp.get_json()["error"]

    def test_fails_open_on_check_error(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")

        def _boom(url, headers=None, timeout=6):
            raise TimeoutError("simulated HIBP outage")

        monkeypatch.setattr(app, "_http_get_with_dns_backstop", _boom)
        error = app._validate_new_password("some-fine-password1")
        assert error is None  # soft/fail-open -- an outage must never block setting a password


class TestMfaEnrollmentAndLogin:
    def _enroll(self, client):
        setup = client.post("/api/mfa/setup")
        assert setup.status_code == 200
        secret = setup.get_json()["secret"]
        code = pyotp.TOTP(secret).now()
        confirm = client.post("/api/mfa/confirm", json={"code": code})
        assert confirm.status_code == 200
        return secret, confirm.get_json()["recovery_codes"]

    def test_full_enrollment_then_login_requires_code(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        bob_client = app.app.test_client()
        _login(bob_client, "bob", "bobs-password1")
        secret, recovery_codes = self._enroll(bob_client)
        assert len(recovery_codes) == app.MFA_RECOVERY_CODE_COUNT
        row = app.get_db().execute("SELECT totp_enabled FROM users WHERE username='bob'").fetchone()
        assert row["totp_enabled"] == 1

        # A fresh login now stops short of a real session and asks for a code.
        fresh = app.app.test_client()
        resp = _login(fresh, "bob", "bobs-password1")
        assert resp.status_code in (302, 303)
        assert resp.headers["Location"].endswith("/login/2fa")
        assert fresh.get("/api/session").get_json()["logged_in"] is False

        code = pyotp.TOTP(secret).now()
        finish = fresh.post("/login/2fa", data={"code": code})
        assert finish.status_code in (302, 303)
        assert fresh.get("/api/session").get_json()["logged_in"] is True

    def test_api_login_flags_mfa_required_then_completes(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        bob_client = app.app.test_client()
        _login(bob_client, "bob", "bobs-password1")
        secret, _ = self._enroll(bob_client)

        fresh = app.app.test_client()
        first = _api_login(fresh, "bob", "bobs-password1")
        assert first.status_code == 200
        assert first.get_json()["mfa_required"] is True
        assert first.get_json()["ok"] is False

        code = pyotp.TOTP(secret).now()
        second = fresh.post("/api/login/2fa", json={"code": code})
        assert second.status_code == 200
        assert second.get_json()["ok"] is True
        assert second.get_json()["username"] == "bob"

    def test_wrong_code_counts_toward_lockout(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        bob_client = app.app.test_client()
        _login(bob_client, "bob", "bobs-password1")
        self._enroll(bob_client)

        fresh = app.app.test_client()
        _login(fresh, "bob", "bobs-password1")
        for _ in range(app.LOGIN_LOCKOUT_THRESHOLD):
            r = fresh.post("/login/2fa", data={"code": "000000"})
            assert b"Invalid code" in r.data
        row = app.get_db().execute(
            "SELECT locked_until FROM users WHERE username='bob'"
        ).fetchone()
        assert row["locked_until"] and row["locked_until"] > app.time.time()

    def test_recovery_code_is_single_use(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        bob_client = app.app.test_client()
        _login(bob_client, "bob", "bobs-password1")
        _, recovery_codes = self._enroll(bob_client)
        rc = recovery_codes[0]

        fresh = app.app.test_client()
        _login(fresh, "bob", "bobs-password1")
        r1 = fresh.post("/login/2fa", data={"code": rc})
        assert r1.status_code in (302, 303)
        client.get("/logout")

        fresh2 = app.app.test_client()
        _login(fresh2, "bob", "bobs-password1")
        r2 = fresh2.post("/login/2fa", data={"code": rc})
        assert b"Invalid code" in r2.data

    def test_disable_requires_current_password(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        bob_client = app.app.test_client()
        _login(bob_client, "bob", "bobs-password1")
        self._enroll(bob_client)

        bad = bob_client.post("/api/mfa/disable", json={"password": "wrong-password"})
        assert bad.status_code == 400
        row = app.get_db().execute("SELECT totp_enabled FROM users WHERE username='bob'").fetchone()
        assert row["totp_enabled"] == 1

        good = bob_client.post("/api/mfa/disable", json={"password": "bobs-password1"})
        assert good.status_code == 200
        row = app.get_db().execute("SELECT totp_enabled FROM users WHERE username='bob'").fetchone()
        assert row["totp_enabled"] == 0

    def test_plain_user_role_can_self_service_mfa(self, client, create_user):
        """api_mfa_* routes must be reachable by role='user', not just
        admin+ -- MFA is meant to be an option for every account."""
        create_user("owner1", role="owner")
        create_user("kiosk1", password="kiosk1s-password1", role="user")
        kiosk_client = app.app.test_client()
        _login(kiosk_client, "kiosk1", "kiosk1s-password1")
        status = kiosk_client.get("/api/mfa/status")
        assert status.status_code == 200
        self._enroll(kiosk_client)

    def test_admin_can_reset_a_users_mfa(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("bob", password="bobs-password1", role="user")
        bob_client = app.app.test_client()
        _login(bob_client, "bob", "bobs-password1")
        self._enroll(bob_client)

        _login(client, "owner1")
        bob_id = app.get_db().execute("SELECT id FROM users WHERE username='bob'").fetchone()["id"]
        r = client.post(f"/api/users/{bob_id}/mfa/reset")
        assert r.status_code == 200
        row = app.get_db().execute("SELECT totp_enabled FROM users WHERE username='bob'").fetchone()
        assert row["totp_enabled"] == 0

        # bob can now log straight in without a code.
        fresh = app.app.test_client()
        resp = _login(fresh, "bob", "bobs-password1")
        assert resp.status_code in (302, 303)
        assert fresh.get("/api/session").get_json()["logged_in"] is True

    def test_admin_cannot_reset_mfa_on_an_account_ranked_above_their_own(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="admins-password1", role="admin")
        owner_client = app.app.test_client()
        _login(owner_client, "owner1")
        self._enroll(owner_client)

        _login(client, "admin1", "admins-password1")
        owner_id = app.get_db().execute("SELECT id FROM users WHERE username='owner1'").fetchone()["id"]
        r = client.post(f"/api/users/{owner_id}/mfa/reset")
        assert r.status_code == 403
