"""Tests for the IRC chat relay: irc_relay.py's pure line-parsing/formatting
helpers (no socket I/O), the owner-gating on /api/irc-relay/config (mirroring
tests/test_discord_relay.py's TestDiscordRelayConfigRoute pattern), and the
inbound/outbound chat_messages wiring. The actual IRCClient socket I/O
(connect/identify/join/read_loop) is not covered here, consistent with this
project's documented testing boundary -- no live-network background loops
under pytest.
"""
import app
import irc_relay


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["idle_timeout"] = app.SESSION_IDLE_TIMEOUT
        sess["sid"]          = "test-sid-" + username


class TestParseIrcLine:
    def test_privmsg_with_prefix_and_trailing(self):
        parsed = irc_relay.parse_irc_line(":alice!user@host PRIVMSG #chan :hello there\r\n")
        assert parsed["prefix"] == "alice!user@host"
        assert parsed["command"] == "PRIVMSG"
        assert parsed["params"] == ["#chan", "hello there"]
        assert parsed["trailing"] == "hello there"

    def test_ping_with_no_prefix(self):
        parsed = irc_relay.parse_irc_line("PING :tungsten.example.net")
        assert parsed["prefix"] is None
        assert parsed["command"] == "PING"
        assert parsed["trailing"] == "tungsten.example.net"

    def test_numeric_reply(self):
        parsed = irc_relay.parse_irc_line(":server.example.net 001 HenWen :Welcome to the network")
        assert parsed["command"] == "001"
        assert parsed["params"] == ["HenWen", "Welcome to the network"]

    def test_trailing_containing_colon(self):
        parsed = irc_relay.parse_irc_line(":bob!u@h PRIVMSG #chan :hello :) there")
        assert parsed["trailing"] == "hello :) there"


class TestExtractPrivmsg:
    def test_extracts_nick_target_text(self):
        parsed = irc_relay.parse_irc_line(":alice!user@host PRIVMSG #chan :hello there")
        result = irc_relay.extract_privmsg(parsed)
        assert result == ("alice", "#chan", "hello there")

    def test_none_for_non_privmsg(self):
        parsed = irc_relay.parse_irc_line("PING :server")
        assert irc_relay.extract_privmsg(parsed) is None

    def test_none_without_prefix(self):
        parsed = irc_relay.parse_irc_line("PRIVMSG #chan :hi")
        assert irc_relay.extract_privmsg(parsed) is None


class TestFormatPrivmsg:
    def test_basic_shape(self):
        assert irc_relay.format_privmsg("#chan", "hello") == "PRIVMSG #chan :hello"

    def test_strips_embedded_crlf(self):
        line = irc_relay.format_privmsg("#chan", "hello\r\nQUIT")
        assert "\r" not in line and "\n" not in line
        assert line == "PRIVMSG #chan :hello  QUIT"


class TestSplitForIrc:
    def test_short_text_single_line(self):
        assert irc_relay.split_for_irc("hello world") == ["hello world"]

    def test_empty_text_no_lines(self):
        assert irc_relay.split_for_irc("   ") == []

    def test_long_text_splits_on_whitespace(self):
        text = ("word " * 200).strip()
        lines = irc_relay.split_for_irc(text, max_len=50)
        assert len(lines) > 1
        assert all(len(l) <= 50 for l in lines)
        # Rejoining recovers the original words in order
        assert " ".join(lines).split() == text.split()


class TestIsSelf:
    def test_case_insensitive_match(self):
        assert irc_relay.is_self("HenWen", "henwen") is True

    def test_different_nick_not_self(self):
        assert irc_relay.is_self("alice", "henwen") is False


class TestIrcRelayConfigRoute:
    def test_get_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        assert client.get("/api/irc-relay/config").status_code == 401

    def test_get_rejects_admin(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.get("/api/irc-relay/config").status_code == 403

    def test_get_rejects_plain_user(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        assert client.get("/api/irc-relay/config").status_code == 403

    def test_get_allows_owner_and_returns_defaults(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/irc-relay/config").get_json()
        assert body["enabled"] == 0
        assert body["host"] == ""
        assert body["port"] == 6697
        assert body["use_tls"] == 1

    def test_save_rejects_non_owner(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        resp = client.put("/api/irc-relay/config",
                           json={"enabled": True, "host": "irc.example.net",
                                 "channel": "#test", "nick": "HenWen"})
        assert resp.status_code == 403

    def test_owner_can_save_and_it_persists(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/irc-relay/config", json={
            "enabled": True, "host": "irc.geekshed.net", "port": 6697,
            "use_tls": True, "channel": "Node643930", "nick": "HenWen",
        })
        assert resp.status_code == 200
        cfg = app._get_irc_relay_config()
        assert cfg["enabled"] == 1
        assert cfg["host"] == "irc.geekshed.net"
        # channel auto-prefixed with '#'
        assert cfg["channel"] == "#Node643930"

    def test_save_rejects_enabled_without_required_fields(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/irc-relay/config", json={"enabled": True, "host": "", "channel": "", "nick": ""})
        assert resp.status_code == 400

    def test_save_allows_disabling_with_blank_fields(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/irc-relay/config", json={"enabled": False})
        assert resp.status_code == 200

    def test_save_rejects_non_numeric_port(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.put("/api/irc-relay/config",
                           json={"enabled": False, "port": "not-a-number"})
        assert resp.status_code == 400


class TestIrcRelayStatusRoute:
    def test_requires_owner(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        assert client.get("/api/irc-relay/status").status_code == 403

    def test_owner_sees_disconnected_by_default(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/irc-relay/status").get_json()
        assert body["connected"] is False


class TestIrcRelayOnMessage:
    def test_inserts_chat_row_with_irc_role_and_sentinel_user_id(self, client, create_user):
        create_user("owner1", role="owner")
        app._irc_relay_on_message("SomeIrcNick", "hello from IRC")
        row = app.get_db().execute(
            "SELECT user_id, username, role, message FROM chat_messages "
            "WHERE username=? ORDER BY id DESC LIMIT 1",
            ("SomeIrcNick",)
        ).fetchone()
        assert row is not None
        assert row["user_id"] == 0
        assert row["role"] == "irc"
        assert row["message"] == "hello from IRC"


class TestChatPostFeedsIrcRelayQueue:
    def test_posting_chat_message_enqueues_for_irc(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("alice", password="password12345", role="user")
        _login(client, "alice")
        # Drain anything left over from other tests sharing the module-level queue.
        while not app._irc_relay_queue.empty():
            app._irc_relay_queue.get_nowait()
        resp = client.post("/api/chat/messages", json={"message": "hello everyone"})
        assert resp.status_code == 201
        username, message = app._irc_relay_queue.get_nowait()
        assert username == "alice"
        assert message == "hello everyone"
