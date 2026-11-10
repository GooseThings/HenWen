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

    def test_the_header_is_ignored_from_a_non_local_peer(self, client, create_user):
        """Otherwise any client could rewrite every URL the app generates."""
        create_user("owner1", role="owner")
        r = client.get("/login", headers={"X-Forwarded-Prefix": PREFIX},
                       environ_overrides={"REMOTE_ADDR": "203.0.113.7"})
        assert r.status_code == 200
        assert 'window.HW_BASE = ""' in r.get_data(as_text=True)
        assert 'action="/login"' in r.get_data(as_text=True)
