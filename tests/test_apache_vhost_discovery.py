"""Tests for _find_henwen_apache_vhosts()/_apache_marker_status() -- the
generalized Apache-vhost discovery that replaced a hardcoded two-filename
candidate list (henwen-ssl.conf / henwen.conf). Confirmed live against a
real production install that a box can front HenWen through more than one
differently-named vhost at once; these tests exercise that shape directly
rather than assuming exactly zero or one vhost exists, the way the older
candidate-list tests implicitly did.
"""
import app


def _write_vhost(path, port=None, marker=None, servername=None, quoted=False):
    port = port or app.PORT
    lines = ["<VirtualHost *:443>"]
    if servername:
        lines.append(f"    ServerName {servername}")
    if marker:
        lines.append(f"    # {marker}")
    if quoted:
        lines.append(f'    ProxyPass "/" "http://127.0.0.1:{port}/"')
    else:
        lines.append(f"    ProxyPass        / http://127.0.0.1:{port}/ retry=0 timeout=120")
    lines.append("</VirtualHost>")
    path.write_text("\n".join(lines) + "\n")


class TestFindHenwenApacheVhosts:
    def test_discovers_multiple_differently_named_vhosts(self, tmp_path, monkeypatch):
        # The exact shape confirmed live: two vhosts, neither named
        # henwen-ssl.conf/henwen.conf, both proxying HenWen's real port.
        _write_vhost(tmp_path / "site-one.conf", servername="one.example.com")
        _write_vhost(tmp_path / "site-two.conf", servername="two.example.com")
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(tmp_path))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        found = app._find_henwen_apache_vhosts()
        assert len(found) == 2
        assert {str(tmp_path / "site-one.conf"), str(tmp_path / "site-two.conf")} == set(found)

    def test_ignores_conf_files_not_proxying_our_port(self, tmp_path, monkeypatch):
        _write_vhost(tmp_path / "real.conf", servername="real.example.com")
        (tmp_path / "unrelated.conf").write_text(
            "<VirtualHost *:80>\n    DocumentRoot /var/www/html\n</VirtualHost>\n"
        )
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(tmp_path))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        found = app._find_henwen_apache_vhosts()
        assert found == [str(tmp_path / "real.conf")]

    def test_matches_quoted_proxypass_syntax(self, tmp_path, monkeypatch):
        # Apache's ProxyPass "/" "http://..." form is equally valid but
        # wasn't matched by the old regex at all.
        _write_vhost(tmp_path / "quoted.conf", servername="quoted.example.com", quoted=True)
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(tmp_path))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        found = app._find_henwen_apache_vhosts()
        assert found == [str(tmp_path / "quoted.conf")]

    def test_ignores_a_vhost_proxying_a_different_port(self, tmp_path, monkeypatch):
        _write_vhost(tmp_path / "other-port.conf", port=app.PORT + 1, servername="other.example.com")
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(tmp_path))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        assert app._find_henwen_apache_vhosts() == []

    def test_resolves_symlink_to_real_sites_available_path(self, tmp_path, monkeypatch):
        available = tmp_path / "available"
        available.mkdir()
        enabled = tmp_path / "enabled"
        enabled.mkdir()
        real = available / "site.conf"
        _write_vhost(real, servername="linked.example.com")
        (enabled / "site.conf").symlink_to(real)
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(enabled))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        assert app._find_henwen_apache_vhosts() == [str(real)]

    def test_explicit_candidates_merge_with_directory_scan(self, tmp_path, monkeypatch):
        scanned_dir = tmp_path / "scanned"
        scanned_dir.mkdir()
        _write_vhost(scanned_dir / "found.conf", servername="found.example.com")
        explicit = tmp_path / "explicit.conf"
        _write_vhost(explicit, servername="explicit.example.com")
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(scanned_dir))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", (str(explicit),))
        found = set(app._find_henwen_apache_vhosts())
        assert found == {str(scanned_dir / "found.conf"), str(explicit)}

    def test_singular_helper_returns_first_or_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(tmp_path))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        assert app._find_henwen_apache_vhost() is None
        _write_vhost(tmp_path / "only.conf", servername="only.example.com")
        assert app._find_henwen_apache_vhost() == str(tmp_path / "only.conf")


class TestApacheMarkerStatus:
    def test_reports_missing_from_only_the_unpatched_vhost(self, tmp_path, monkeypatch):
        # Direct regression coverage for the live bug this hardening fixes:
        # a box with two real vhosts where only one has ever been patched
        # (e.g. apply.sh only found the first one under the old candidate
        # list) must not report the feature as fully "applied".
        _write_vhost(tmp_path / "patched.conf", marker="TEST-MARKER", servername="patched.example.com")
        _write_vhost(tmp_path / "unpatched.conf", servername="unpatched.example.com")
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(tmp_path))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        vhosts, applied_to, missing_from = app._apache_marker_status("TEST-MARKER")
        assert len(vhosts) == 2
        assert applied_to == [str(tmp_path / "patched.conf")]
        assert missing_from == [str(tmp_path / "unpatched.conf")]

    def test_reports_no_vhosts_when_none_discovered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(tmp_path))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        assert app._apache_marker_status("TEST-MARKER") == ([], [], [])

    def test_reports_fully_applied_when_every_vhost_has_the_marker(self, tmp_path, monkeypatch):
        _write_vhost(tmp_path / "a.conf", marker="TEST-MARKER", servername="a.example.com")
        _write_vhost(tmp_path / "b.conf", marker="TEST-MARKER", servername="b.example.com")
        monkeypatch.setattr(app, "HENWEN_APACHE_SITES_DIR", str(tmp_path))
        monkeypatch.setattr(app, "HENWEN_APACHE_VHOST_CANDIDATES", ())
        vhosts, applied_to, missing_from = app._apache_marker_status("TEST-MARKER")
        assert len(vhosts) == 2 and len(applied_to) == 2 and missing_from == []
