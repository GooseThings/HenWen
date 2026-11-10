"""Serving HenWen under a URL path prefix (https://host/henwen/).

The app historically assumed it owned the root of its hostname: every
template hardcodes root-absolute URLs and nothing uses url_for(). The two
halves of the fix are _ScriptNamePrefixFix (SCRIPT_NAME, so Flask's own
routing/url_for/redirects are right) and templates/_basepath.html (the
client-side adapter plus prefixed href/src/action attributes).

The attribute check below is the one that matters most: login.html's form is
action="/login", and an unprefixed form action under a prefix posts
credentials outside the mount -- which, on a box running two HenWen installs,
means posting them to the *other* install. That was found for real while
standing HenWen2 up at /2.
"""
import re

import pytest

import app


@pytest.fixture(autouse=True)
def _trust_forwarded_prefix(monkeypatch):
    """The X-Forwarded-Prefix header is opt-in (TRUST_FORWARDED_PREFIX) because
    Apache forwards a client-supplied one verbatim -- see the long comment on
    _ScriptNamePrefixFix. These tests drive the prefix through the header
    because URL_PREFIX is read at import, so they have to opt in explicitly."""
    monkeypatch.setattr(app, "TRUST_FORWARDED_PREFIX", True)


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """/login carries a real per-minute limit and app.limiter's in-memory
    storage persists for the whole session, so this file's several GETs of
    /login trip it once other modules' login tests have run first (seen as
    429s only in the full suite, never in isolation). Same reset
    tests/test_auth_hardening.py uses, for the same reason."""
    app.limiter.reset()


ATTR_RE = re.compile(r'(?:action|href|src)="(/[^"]*)"')
PREFIX  = "/henwen"


def _root_absolute_attrs(html):
    """Every href/src/action that starts with a single '/' (so not an
    external URL, not protocol-relative, not a fragment)."""
    return [u for u in ATTR_RE.findall(html) if not u.startswith('//')]


class TestNormalizeUrlPrefix:
    def test_accepts_a_simple_prefix(self):
        assert app._normalize_url_prefix('/henwen') == '/henwen'

    def test_adds_the_leading_slash_and_drops_a_trailing_one(self):
        assert app._normalize_url_prefix('henwen/') == '/henwen'

    def test_accepts_nested_segments(self):
        assert app._normalize_url_prefix('/apps/henwen') == '/apps/henwen'

    def test_empty_stays_empty(self):
        assert app._normalize_url_prefix('') == ''
        assert app._normalize_url_prefix(None) == ''
        assert app._normalize_url_prefix('/') == ''

    def test_rejects_anything_that_is_not_plain_path_segments(self):
        # A prefix lands in generated URLs and in the session cookie path, so
        # these are ignored rather than sanitized.
        for bad in ('http://evil.example/x', '//evil.example', '/a/../b',
                    '/a?b=c', '/a#b', '/a b', '/a%2fb', '/-leading-dash'):
            assert app._normalize_url_prefix(bad) == '', bad


class TestPrefixedRequests:
    def test_page_renders_under_the_prefix(self, client, create_user):
        create_user("owner1", role="owner")
        r = client.get(PREFIX + "/login", headers={"X-Forwarded-Prefix": PREFIX})
        assert r.status_code == 200

    def test_app_works_when_the_proxy_already_stripped_the_prefix(self, client, create_user):
        """ProxyPass /henwen -> backend/ is as common as passing it through."""
        create_user("owner1", role="owner")
        r = client.get("/login", headers={"X-Forwarded-Prefix": PREFIX})
        assert r.status_code == 200
        assert all(u.startswith(PREFIX + '/') for u in _root_absolute_attrs(r.get_data(as_text=True)))

    def test_every_root_absolute_attribute_is_prefixed(self, client, create_user):
        """The regression guard: one unprefixed action="/login" sends
        credentials outside the mount."""
        create_user("owner1", role="owner")
        for page in ("/login", "/", "/accessible"):
            r = client.get(PREFIX + page, headers={"X-Forwarded-Prefix": PREFIX},
                           follow_redirects=True)
            assert r.status_code == 200, page
            urls = _root_absolute_attrs(r.get_data(as_text=True))
            unprefixed = [u for u in urls if not u.startswith(PREFIX + '/')]
            assert not unprefixed, f"{page} has unprefixed URLs: {unprefixed}"

    def test_hw_base_is_handed_to_the_client_adapter(self, client, create_user):
        create_user("owner1", role="owner")
        r = client.get(PREFIX + "/login", headers={"X-Forwarded-Prefix": PREFIX})
        assert f'window.HW_BASE = "{PREFIX}"' in r.get_data(as_text=True)

    def test_redirects_are_prefixed(self, client, create_user):
        create_user("owner1", role="owner")
        r = client.get(PREFIX + "/henwen-manager", headers={"X-Forwarded-Prefix": PREFIX})
        assert r.status_code in (301, 302)
        assert r.headers["Location"].startswith(PREFIX + "/"), r.headers["Location"]


class TestUnprefixedIsUnchanged:
    def test_no_prefix_means_no_base_and_no_rewriting(self, client, create_user):
        """Every existing install is this case -- it must be untouched."""
        create_user("owner1", role="owner")
        r = client.get("/login")
        body = r.get_data(as_text=True)
        assert r.status_code == 200
        assert 'window.HW_BASE = ""' in body
        assert 'action="/login"' in body

    def test_the_header_is_ignored_unless_the_deployment_opts_in(self, client, create_user, monkeypatch):
        """The regression guard for a remotely exploitable bug: the header used
        to be trusted whenever the TCP peer was loopback, which is *every*
        proxied request, because Apache forwards a client's own
        X-Forwarded-Prefix verbatim. Confirmed live against the public
        hostname before the fix:

            curl -H 'X-Forwarded-Prefix: /pwned' https://<host>/login
            -> action="/pwned/login"

        An attacker-controlled prefix rewrites every URL on the page,
        including the login form's action, and is the standard ingredient for
        web cache poisoning. Default must be: header ignored."""
        monkeypatch.setattr(app, "TRUST_FORWARDED_PREFIX", False)
        create_user("owner1", role="owner")
        r = client.get("/login", headers={"X-Forwarded-Prefix": "/pwned"})
        body = r.get_data(as_text=True)
        assert r.status_code == 200
        assert 'window.HW_BASE = ""' in body
        assert 'action="/login"' in body
        assert '/pwned' not in body


class TestSessionCookieScope:
    """Two installs on one hostname is the reason prefix support exists, so
    their cookies must not reach each other."""

    def test_cookie_is_scoped_to_the_mount(self, client, create_user):
        create_user("owner1", role="owner")
        r = client.post(PREFIX + "/login",
                        data={"username": "owner1", "password": "password12345"},
                        headers={"X-Forwarded-Prefix": PREFIX})
        setcookie = r.headers.get("Set-Cookie", "")
        assert setcookie, "login should set a session cookie"
        assert f"Path={PREFIX}" in setcookie, setcookie

    def test_cookie_name_follows_the_mount_too(self, client, create_user):
        """Path alone isn't enough: a Path=/ 'session' cookie from a root
        install is also sent on /henwen requests, so both arrive together and
        the browser's ordering picks the winner. The name is derived per
        request, so it stays consistent in header-prefix mode as well."""
        create_user("owner1", role="owner")
        r = client.post(PREFIX + "/login",
                        data={"username": "owner1", "password": "password12345"},
                        headers={"X-Forwarded-Prefix": PREFIX})
        assert "session_henwen=" in r.headers.get("Set-Cookie", ""), r.headers.get("Set-Cookie")

    def test_logout_also_expires_a_legacy_root_path_cookie(self, client, create_user):
        """Logout can only work by clearing cookies, and session.clear() only
        clears this mount's one. An install previously served at the root can
        have left a validly-signed cookie at Path=/ that would revive if the
        mount moved back."""
        create_user("owner1", role="owner")
        client.post(PREFIX + "/login",
                    data={"username": "owner1", "password": "password12345"},
                    headers={"X-Forwarded-Prefix": PREFIX})
        r = client.post(PREFIX + "/logout", headers={"X-Forwarded-Prefix": PREFIX})
        cookies = r.headers.getlist("Set-Cookie")
        expired_at_root = [c for c in cookies
                           if c.startswith("session=")
                           and ("Path=/;" in c or c.rstrip().endswith("Path=/"))
                           and ("Expires=Thu, 01 Jan 1970" in c or "Max-Age=0" in c)]
        assert expired_at_root, cookies

    def test_cookie_is_path_root_when_mounted_at_root(self, client, create_user):
        create_user("owner1", role="owner")
        r = client.post("/login", data={"username": "owner1", "password": "password12345"})
        assert "Path=/;" in r.headers.get("Set-Cookie", "") or \
               r.headers.get("Set-Cookie", "").rstrip().endswith("Path=/")
