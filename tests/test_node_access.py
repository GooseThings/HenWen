"""Tests for Manager > Node Access: editing the AstDB allowlist/denylist
keys that ASL3's stock [radio-secure] dialplan enforces. Real AMI is
replaced by a fake that records what would have been sent."""
import pytest

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


class _FakeAmi:
    def __init__(self, show=None, ok=True):
        self.show = show or {}
        self.ok = ok
        self.actions = []

    def command(self, cmd, log_level="INFO"):
        return self.show.get(cmd.split()[-1], ["0 results found."])

    def db_action(self, action, family, key=None, val=None):
        self.actions.append((action, family, key, val))
        return self.ok


@pytest.fixture
def ami(monkeypatch, tmp_path):
    fake = _FakeAmi()
    monkeypatch.setattr(app, "ami_send_command", lambda fn: fn(fake))
    monkeypatch.setattr(app, "lookup_node", lambda n: {"callsign": f"CALL{n}", "location": "Somewhere"})
    conf = tmp_path / "rpt.conf"
    conf.write_text("[node-main](!)\nduplex = 0\n\n[643933](node-main)\nrxchannel = SimpleUSB/643933\n")
    monkeypatch.setattr(app, "RPT_CONF_PATH", str(conf))
    return fake


@pytest.fixture
def owner(client, create_user):
    create_user("owner1", role="owner")
    _login(client, "owner1")
    return client


class TestParsingAndMode:
    def test_parse_show_output(self):
        lines = ["/allowlist/643933/643930                          : Node 643930 is allowed",
                 "/allowlist/643933/2000                            : Club hub",
                 "/denylist/643933/3123456                          : EchoLink pest",
                 "/allowlist/1999/50                                :",
                 "2 results found."]
        out = app._parse_node_access_show(lines)
        assert out["643933"]["allowlist"] == [{"node": "2000", "note": "Club hub"},
                                              {"node": "643930", "note": "Node 643930 is allowed"}]
        assert out["643933"]["denylist"] == [{"node": "3123456", "note": "EchoLink pest"}]
        assert out["1999"]["allowlist"] == [{"node": "50", "note": ""}]

    def test_mode_matches_dialplan_precedence(self):
        assert app._node_access_mode({"allowlist": [], "denylist": []}) == "open"
        assert app._node_access_mode({"allowlist": [], "denylist": [{"node": "1"}]}) == "denylist"
        # Any allowlist entry wins, denylist then never consulted.
        assert app._node_access_mode({"allowlist": [{"node": "2"}], "denylist": [{"node": "1"}]}) == "allowlist"

    def test_note_cannot_inject_ami_headers(self):
        assert app._clean_node_access_note("ok\r\nAction: Command\r\nCommand: core stop now") == \
            "ok  Action: Command  Command: core stop now"[:app.NODE_ACCESS_NOTE_MAX]
        assert "\n" not in app._clean_node_access_note("a\nb") and "\r" not in app._clean_node_access_note("a\rb")
        assert len(app._clean_node_access_note("x" * 500)) == app.NODE_ACCESS_NOTE_MAX

    def test_dialplan_check(self, tmp_path, monkeypatch):
        # Two files, not one rewritten: read_conf_file() caches by mtime.
        plain = tmp_path / "plain.conf"
        plain.write_text("[radio-secure]\nexten => _XXXX!,1,rpt(${EXTEN})\n")
        monkeypatch.setattr(app, "EXTENSIONS_CONF_PATH", str(plain))
        assert not app._node_access_dialplan_ok()
        ext = tmp_path / "extensions.conf"
        monkeypatch.setattr(app, "EXTENSIONS_CONF_PATH", str(ext))
        ext.write_text("same => n,GotoIf(${DB_EXISTS(allowlist/${EXTEN}/${NODENUM})}?connect)\n"
                       "same => n(denylist),GotoIf(${DB_EXISTS(denylist/${EXTEN}/${NODENUM})}?:connect)\n")
        assert app._node_access_dialplan_ok()


class TestRoutes:
    def test_list_reports_mode_and_callsigns(self, owner, ami):
        ami.show = {"allowlist": ["/allowlist/643933/643930 : old note", "1 results found."]}
        d = owner.get("/api/node-access").get_json()
        n = d["nodes"]["643933"]
        assert n["mode"] == "allowlist"
        assert n["allowlist"] == [{"node": "643930", "note": "old note",
                                   "callsign": "CALL643930", "location": "Somewhere"}]
        assert n["denylist"] == []

    def test_add_sends_exact_dialplan_key(self, owner, ami):
        resp = owner.post("/api/node-access", json={
            "list": "allowlist", "local": "643933", "node": "2000", "note": "Club\r\nhub"})
        assert resp.status_code == 200
        assert ami.actions == [("DBPut", "allowlist/643933", "2000", "Club  hub")]

    def test_add_blank_note_still_stores_a_value(self, owner, ami):
        owner.post("/api/node-access", json={"list": "denylist", "local": "643933", "node": "3123456"})
        assert ami.actions == [("DBPut", "denylist/643933", "3123456", "-")]

    @pytest.mark.parametrize("body", [
        {"list": "graylist", "local": "643933", "node": "2000"},
        {"list": "allowlist", "local": "999999", "node": "2000"},      # not a node on this box
        {"list": "allowlist", "local": "643933", "node": "12345678"},  # too long
        {"list": "allowlist", "local": "643933", "node": "20 00"},
        {"list": "allowlist", "local": "643933", "node": "643933"},    # itself
    ])
    def test_add_rejects_bad_input(self, owner, ami, body):
        assert owner.post("/api/node-access", json=body).status_code == 400
        assert ami.actions == []

    def test_delete(self, owner, ami):
        assert owner.delete("/api/node-access/allowlist/643933/643930").status_code == 200
        assert ami.actions == [("DBDel", "allowlist/643933", "643930", None)]

    def test_delete_missing_entry_is_404(self, owner, ami):
        ami.ok = False
        assert owner.delete("/api/node-access/denylist/643933/1").status_code == 404

    def test_delete_rejects_bad_path(self, owner, ami):
        assert owner.delete("/api/node-access/graylist/643933/1").status_code == 400
        assert ami.actions == []

    def test_admin_is_refused(self, client, create_user, ami):
        create_user("owner1", role="owner")
        create_user("admin1", role="admin")
        _login(client, "admin1")
        assert client.get("/api/node-access").status_code == 403
        assert client.post("/api/node-access", json={"list": "allowlist", "local": "643933",
                                                     "node": "2000"}).status_code == 403
        assert client.delete("/api/node-access/allowlist/643933/2000").status_code == 403
        assert ami.actions == []
