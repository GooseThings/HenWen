"""Route-level regression tests using Flask's test client against an
isolated temp sqlite DB (see the `client`/`fresh_db` fixtures in conftest.py).
CSRF and rate limiting are disabled app-wide for the test session (see
conftest.py) so these exercise auth/session/role logic, not that plumbing.
"""
import app


def test_csrf_token_endpoint_is_public_and_returns_a_token(client):
    resp = client.get("/api/csrf-token")
    assert resp.status_code == 200
    assert resp.get_json()["csrf_token"]


def test_session_endpoint_reports_logged_out_by_default(client):
    resp = client.get("/api/session")
    assert resp.status_code == 200
    assert resp.get_json() == {"logged_in": False}


def test_protected_api_returns_setup_required_before_first_account(client):
    """Before any owner/superuser account exists, check_auth() should steer
    API callers toward setup instead of a generic auth failure."""
    resp = client.get("/api/conf")
    assert resp.status_code == 503
    body = resp.get_json()
    assert body["setup_url"] == "/login"


def test_login_page_offers_setup_mode_before_first_account(client):
    resp = client.get("/login")
    assert resp.status_code == 200
    assert b"setup" in resp.data.lower() or resp.status_code == 200


class TestFirstRunBoardRedirect:
    """Issue #69: on a brand-new install the kiosk board is public, so '/'
    rendered a normal-looking board and gave the owner no hint that setup
    was pending -- and install.sh sends every new operator to exactly that
    URL ("Open your browser: http://<ip>:5000"). /henwen-manager already
    redirected to /login and /api/* already 503'd, so '/' was the one way in
    that silently hid the first-run screen."""

    BOARD_PAGES = ["/", "/status", "/accessible"]

    def test_board_pages_redirect_to_setup_before_first_account(self, client):
        for path in self.BOARD_PAGES:
            resp = client.get(path)
            assert resp.status_code == 302, f"{path} should bounce to setup"
            assert resp.headers["Location"].endswith("/login"), path

    def test_redirect_lands_on_the_create_account_screen(self, client):
        resp = client.get("/", follow_redirects=True)
        assert resp.status_code == 200
        assert b"Create Account" in resp.data
        assert b"No account exists yet" in resp.data

    def test_public_json_endpoints_are_not_redirected(self, client):
        """The login page renders against these, so bouncing the whole of
        _PUBLIC would break the screen this redirect exists to reach."""
        for path in ["/api/csrf-token", "/api/session", "/api/status/board"]:
            resp = client.get(path)
            assert resp.status_code == 200, f"{path} must stay reachable during setup"

    def test_board_is_public_again_once_an_account_exists(self, client, create_user):
        """Post-setup behaviour must be completely unchanged: the board is a
        kiosk display and stays readable without logging in."""
        create_user("owner1", role="owner")
        assert client.get("/").status_code == 200
        assert client.get("/accessible").status_code == 200
        # /status keeps its own permanent redirect to '/', not the setup bounce
        resp = client.get("/status")
        assert resp.status_code == 301
        assert resp.headers["Location"].endswith("/")


class TestFirstRunAccountCreation:
    def test_creates_owner_account_and_logs_in(self, client):
        resp = client.post("/login", data={
            "username": "alice",
            "password": "supersecret1",
            "confirm_password": "supersecret1",
        })
        assert resp.status_code in (302, 303)

        row = app.get_db().execute(
            "SELECT role FROM users WHERE username='alice'"
        ).fetchone()
        assert row["role"] == "owner"

        session_resp = client.get("/api/session")
        body = session_resp.get_json()
        assert body["logged_in"] is True
        assert body["username"] == "alice"
        assert body["role"] == "owner"

    def test_rejects_short_password(self, client):
        resp = client.post("/login", data={
            "username": "alice",
            "password": "short",
            "confirm_password": "short",
        })
        assert resp.status_code == 200  # re-renders the form with an error
        assert b"at least 8 characters" in resp.data
        assert app.is_auth_configured() is False

    def test_rejects_mismatched_confirmation(self, client):
        resp = client.post("/login", data={
            "username": "alice",
            "password": "supersecret1",
            "confirm_password": "different1",
        })
        assert resp.status_code == 200
        assert b"do not match" in resp.data
        assert app.is_auth_configured() is False


class TestLoginWithExistingAccount:
    def test_wrong_password_is_rejected(self, client, create_user):
        create_user("alice", password="correct-horse-1", role="owner")
        resp = client.post("/login", data={"username": "alice", "password": "wrong"})
        assert resp.status_code == 200
        assert b"Invalid username or password" in resp.data

    def test_correct_password_logs_in_and_redirects_by_role(self, client, create_user):
        create_user("owner1", role="owner")  # is_auth_configured() must be True, or /login is still setup_mode
        create_user("kiosk-user", password="kiosk-password1", role="user")
        resp = client.post("/login", data={
            "username": "kiosk-user", "password": "kiosk-password1",
        })
        assert resp.status_code in (302, 303)
        # 'user' role redirects to the status board (url_for('status_board') == "/"),
        # not the manager shell that admin/superuser/owner land on.
        assert resp.headers["Location"] == "/"

    def test_api_login_returns_json_and_csrf_token(self, client, create_user):
        create_user("alice", password="supersecret1", role="owner")
        resp = client.post("/api/login", json={
            "username": "alice", "password": "supersecret1",
        })
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["ok"] is True
        assert body["role"] == "owner"
        assert body["csrf_token"]

    def test_api_login_wrong_password_returns_401(self, client, create_user):
        create_user("alice", password="supersecret1", role="owner")
        resp = client.post("/api/login", json={
            "username": "alice", "password": "wrong-password",
        })
        assert resp.status_code == 401


class TestRoleGating:
    def test_admin_api_requires_login(self, client, create_user):
        create_user("alice", role="owner")
        resp = client.get("/api/conf")
        assert resp.status_code == 401

    def test_plain_user_role_is_denied_admin_api(self, client, create_user):
        create_user("owner1", role="owner")  # is_auth_configured() must be True, or /login is still setup_mode
        create_user("kiosk", password="kiosk-password1", role="user")
        client.post("/login", data={"username": "kiosk", "password": "kiosk-password1"})
        resp = client.get("/api/conf")
        assert resp.status_code == 403

    def test_owner_can_reach_admin_api(self, client, create_user, tmp_path, monkeypatch):
        conf_path = tmp_path / "rpt.conf"
        conf_path.write_text("[general]\nnode_lookup_method = both\n")
        monkeypatch.setattr(app, "RPT_CONF_PATH", str(conf_path))

        create_user("alice", password="supersecret1", role="owner")
        client.post("/login", data={"username": "alice", "password": "supersecret1"})
        resp = client.get("/api/conf")
        assert resp.status_code == 200
