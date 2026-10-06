"""Unit tests for DVSwitch's pure helper functions (app.py) — config
validation, rpt.conf bridge-node stanza creation, and default-context
lookup. No DB or Flask app context needed for any of these, same posture
as test_rpt_conf_parser.py; the apt/systemd/AMBE-hardware parts of
dvswitch/apply.sh and check.sh need a real install to verify (see
dvswitch/README.md).

TestDvswitchStatusRoute/TestDvswitchTuneRoute cover the live-status and
talkgroup-switch routes added after a real deploy surfaced two things: no
Kiosk-facing way to see current TG, and dvswitch.sh needing to be invoked
correctly. Tuning was originally gated to the owner's curated preset list;
since the Browse Talkgroups popup shipped it accepts any numeric TG from
any logged-in role instead (see api_dvswitch_tune()'s own docstring).
"""
import json
from unittest.mock import MagicMock, patch

import app


VALID_CONFIG = {
    "enabled": True,
    "dmr_id": "3123456",
    "callsign": "N8GMZ",
    "dmr_network": "brandmeister",
    "network_host": "master.example.org",
    "network_port": 62031,
    "network_password": "s3cret",
    "static_talkgroups": "3123",
    "bridge_node": "1999",
    "allstar_gain": 1.0,
    "dmr_gain": 1.0,
    "ambe_source": "software",
    "ambe_device": "",
    "ambe_host": "",
    "ambe_port": 2460,
}


class TestValidateDvswitchConfig:
    def test_valid_config_passes(self):
        cleaned, err = app._validate_dvswitch_config(VALID_CONFIG)
        assert err is None
        assert cleaned["dmr_id"] == "3123456"
        assert cleaned["callsign"] == "N8GMZ"

    def test_disabled_config_needs_nothing(self):
        cleaned, err = app._validate_dvswitch_config({"enabled": False})
        assert err is None
        assert cleaned["enabled"] is False

    def test_enabling_without_dmr_id_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["dmr_id"] = ""
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None
        assert "required" in err

    def test_enabling_without_bridge_node_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["bridge_node"] = ""
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None

    def test_bad_dmr_id_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["dmr_id"] = "abc"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "DMR ID" in err

    def test_short_dmr_id_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["dmr_id"] = "123"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None

    def test_bad_callsign_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["callsign"] = "n8"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "Callsign" in err

    def test_callsign_lowercased_input_is_uppercased(self):
        cfg = dict(VALID_CONFIG); cfg["callsign"] = "n8gmz"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is None
        assert cleaned["callsign"] == "N8GMZ"

    def test_bad_bridge_node_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["bridge_node"] = "12"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "Bridge node" in err

    def test_bad_dmr_network_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["dmr_network"] = "not-a-network"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "dmr_network" in err

    def test_out_of_range_network_port_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["network_port"] = 70000
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "network_port" in err

    def test_gain_out_of_range_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["allstar_gain"] = 50
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "gain" in err

    def test_ambe_source_defaults_to_software_no_hardware_required(self):
        cfg = dict(VALID_CONFIG)
        cfg.pop("ambe_source")
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is None
        assert cleaned["ambe_source"] == "software"

    def test_hardware_ambe_source_requires_device_when_enabled(self):
        cfg = dict(VALID_CONFIG)
        cfg["ambe_source"] = "hardware"
        cfg["ambe_device"] = ""
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "hardware" in err

    def test_hardware_ambe_source_ok_with_device(self):
        cfg = dict(VALID_CONFIG)
        cfg["ambe_source"] = "hardware"
        cfg["ambe_device"] = "/dev/ttyUSB0"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is None
        assert cleaned["ambe_device"] == "/dev/ttyUSB0"

    def test_network_ambe_source_requires_host_when_enabled(self):
        cfg = dict(VALID_CONFIG)
        cfg["ambe_source"] = "network"
        cfg["ambe_host"] = ""
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "network" in err

    def test_bad_ambe_source_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["ambe_source"] = "quantum"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None and "ambe_source" in err

    def test_non_numeric_network_port_rejected(self):
        cfg = dict(VALID_CONFIG); cfg["network_port"] = "not-a-number"
        cleaned, err = app._validate_dvswitch_config(cfg)
        assert err is not None


class TestAppendNodeStanza:
    def test_appends_new_stanza_to_empty_content(self):
        out = app.append_node_stanza("", "1999", {"rxchannel": "usrp/127.0.0.1:34001:32001", "duplex": "0"})
        assert "[1999]" in out
        assert "rxchannel = usrp/127.0.0.1:34001:32001" in out
        assert "duplex = 0" in out

    def test_appends_after_existing_content_with_blank_line_separator(self):
        existing = "[64393]\nrxchannel = Local/64393@nodes\n"
        out = app.append_node_stanza(existing, "1999", {"duplex": "0"})
        assert out.startswith(existing)
        assert "\n\n[1999]" in out

    def test_handles_content_missing_trailing_newline(self):
        existing = "[64393]\nrxchannel = Local/64393@nodes"
        out = app.append_node_stanza(existing, "1999", {"duplex": "0"})
        assert "[64393]\nrxchannel = Local/64393@nodes\n\n[1999]" in out

    def test_raises_on_existing_node_number(self):
        existing = "[1999]\nrxchannel = something\n"
        try:
            app.append_node_stanza(existing, "1999", {"duplex": "0"})
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_raises_on_existing_template_name(self):
        existing = "[node-main](!)\nduplex = 2\n"
        try:
            app.append_node_stanza(existing, "node-main", {"duplex": "0"})
            assert False, "expected ValueError"
        except ValueError:
            pass

    def test_writes_template_header_when_given(self):
        out = app.append_node_stanza("", "1999", {"duplex": "0"}, template="node-main")
        assert "[1999](node-main)" in out

    def test_new_stanza_is_parseable_afterwards(self):
        out = app.append_node_stanza("", "1999", {"rxchannel": "usrp/127.0.0.1:34001:32001", "duplex": "0"})
        assert app.get_node_numbers(out) == ["1999"]
        settings = app.parse_stanza_settings(out, "1999")
        assert settings["duplex"]["value"] == "0"


class TestDvswitchBridgeNodeSettings:
    def test_uses_fixed_usrp_loopback_ports(self):
        settings = app._dvswitch_bridge_node_settings("radio-secure")
        assert settings["rxchannel"] == (
            f"usrp/127.0.0.1:{app.DVSWITCH_USRP_ASTERISK_RXPORT}:{app.DVSWITCH_USRP_ASTERISK_TXPORT}")
        assert settings["duplex"] == "0"
        assert settings["context"] == "radio-secure"


class TestDvswitchBridgeNodeExcludedFromBoard:
    """The DVSwitch bridge node is a real rpt.conf node stanza (Analog_Bridge's
    USRP channel needs one to attach to), but it's internal plumbing, not a
    real repeater -- confirmed live that showing it as its own hosted-node
    card on the public kiosk board produced a confusing "node X connected to
    node Y" / "node Y connected to node X" pair that read as an erroneous
    self-connection. api_status_board() filters it out of the top-level node
    list (while the real node's own "connected" sub-list, driven by live AMI
    state rather than this list, still correctly shows the bridge link)."""

    def test_bridge_node_hidden_from_board_when_enabled(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        rpt_conf = tmp_path / "rpt.conf"
        rpt_conf.write_text(
            "[643930]\ncontext = radio-secure\nrxchannel = SimpleUSB/643930\n\n"
            "[1999]\nrxchannel = usrp/127.0.0.1:34001:32001\nduplex = 0\n"
        )
        monkeypatch.setattr(app, "RPT_CONF_PATH", str(rpt_conf))
        db = app.get_db()
        db.execute("INSERT OR REPLACE INTO dvswitch_config (id, enabled, bridge_node) VALUES (1, 1, '1999')")
        db.commit()

        resp = client.get("/api/status/board")
        assert resp.status_code == 200
        node_numbers = [n["node"] for n in resp.get_json()["nodes"]]
        assert "1999" not in node_numbers
        assert "643930" in node_numbers

    def test_bridge_node_shown_when_dvswitch_disabled(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        rpt_conf = tmp_path / "rpt.conf"
        rpt_conf.write_text(
            "[643930]\nrxchannel = SimpleUSB/643930\n\n[1999]\nrxchannel = usrp/127.0.0.1:34001:32001\n"
        )
        monkeypatch.setattr(app, "RPT_CONF_PATH", str(rpt_conf))

        resp = client.get("/api/status/board")
        assert resp.status_code == 200
        node_numbers = [n["node"] for n in resp.get_json()["nodes"]]
        assert "1999" in node_numbers

    def test_bridge_node_shown_when_no_dvswitch_config_row(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        rpt_conf = tmp_path / "rpt.conf"
        rpt_conf.write_text(
            "[643930]\nrxchannel = SimpleUSB/643930\n\n[1999]\nrxchannel = usrp/127.0.0.1:34001:32001\n"
        )
        monkeypatch.setattr(app, "RPT_CONF_PATH", str(rpt_conf))

        resp = client.get("/api/status/board")
        assert resp.status_code == 200
        node_numbers = [n["node"] for n in resp.get_json()["nodes"]]
        assert "1999" in node_numbers


class TestDvswitchDefaultContext:
    def test_reuses_existing_node_context(self):
        content = "[64393]\ncontext = radio-secure\nrxchannel = Local/64393@nodes\n"
        assert app._dvswitch_default_context(content) == "radio-secure"

    def test_falls_back_when_no_node_has_a_context(self):
        content = "[64393]\nrxchannel = Local/64393@nodes\n"
        assert app._dvswitch_default_context(content) == "radio-secure"

    def test_falls_back_on_empty_conf(self):
        assert app._dvswitch_default_context("") == "radio-secure"


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


class TestValidateTalkgroupPresets:
    def test_none_returns_empty_list(self):
        cleaned, err = app._validate_talkgroup_presets(None)
        assert err is None
        assert cleaned == []

    def test_valid_list_passes(self):
        cleaned, err = app._validate_talkgroup_presets([{"label": "Local", "tg": "9"}, {"label": "Statewide", "tg": "3120"}])
        assert err is None
        assert cleaned == [{"label": "Local", "tg": "9"}, {"label": "Statewide", "tg": "3120"}]

    def test_not_a_list_rejected(self):
        cleaned, err = app._validate_talkgroup_presets({"label": "Local", "tg": "9"})
        assert err is not None

    def test_entry_not_a_dict_rejected(self):
        cleaned, err = app._validate_talkgroup_presets(["9"])
        assert err is not None

    def test_missing_label_rejected(self):
        cleaned, err = app._validate_talkgroup_presets([{"label": "", "tg": "9"}])
        assert err is not None and "label" in err

    def test_non_numeric_tg_rejected(self):
        cleaned, err = app._validate_talkgroup_presets([{"label": "Local", "tg": "not-a-number"}])
        assert err is not None

    def test_duplicate_tg_rejected(self):
        cleaned, err = app._validate_talkgroup_presets([{"label": "A", "tg": "9"}, {"label": "B", "tg": "9"}])
        assert err is not None and "more than once" in err

    def test_too_many_presets_rejected(self):
        presets = [{"label": f"TG{i}", "tg": str(i)} for i in range(21)]
        cleaned, err = app._validate_talkgroup_presets(presets)
        assert err is not None and "20" in err

    def test_labels_and_tgs_are_stripped(self):
        cleaned, err = app._validate_talkgroup_presets([{"label": "  Local  ", "tg": " 9 "}])
        assert err is None
        assert cleaned == [{"label": "Local", "tg": "9"}]


class TestDvswitchStatusRoute:
    def test_unavailable_when_not_enabled(self, client, create_user):
        create_user("owner1", role="owner")
        resp = client.get("/api/dvswitch/status")
        assert resp.status_code == 200
        assert resp.get_json() == {"available": False}

    def test_unavailable_when_enabled_but_no_abinfo_file(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute("INSERT OR REPLACE INTO dvswitch_config (id, enabled, dmr_network, network_host) "
                   "VALUES (1, 1, 'brandmeister', 'master.example.org')")
        db.commit()
        monkeypatch.setattr(app, "DVSWITCH_ABINFO_PATH", str(tmp_path / "nonexistent.json"))

        resp = client.get("/api/dvswitch/status")
        assert resp.status_code == 200
        assert resp.get_json() == {"available": False}

    def test_available_with_live_abinfo_data(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        db = app.get_db()
        db.execute(
            "INSERT OR REPLACE INTO dvswitch_config (id, enabled, dmr_network, network_host, bridge_node, talkgroup_presets) "
            "VALUES (1, 1, 'brandmeister', 'master.example.org', '1999', ?)",
            (json.dumps([{"label": "Local", "tg": "9"}]),)
        )
        db.commit()
        abinfo = tmp_path / "ABInfo.json"
        abinfo.write_text(json.dumps({
            "digital": {"gw": "3206012", "tg": "9", "ts": "2", "cc": "1", "call": "N8GMZ"},
            "tlv": {"ambe_mode": "DMR"},
            "last_tune": "",
        }))
        monkeypatch.setattr(app, "DVSWITCH_ABINFO_PATH", str(abinfo))

        resp = client.get("/api/dvswitch/status")
        assert resp.status_code == 200
        d = resp.get_json()
        assert d["available"] is True
        assert d["tg"] == "9"
        assert d["mode"] == "DMR"
        assert d["network"] == "brandmeister"
        assert d["bridge_node"] == "1999"
        assert d["presets"] == [{"label": "Local", "tg": "9"}]

    def test_public_no_login_required(self, client, create_user):
        # No _login() call -- confirms api_dvswitch_status is in check_auth()'s
        # _PUBLIC set, matching /api/status/board's own public status data.
        create_user("owner1", role="owner")
        resp = client.get("/api/dvswitch/status")
        assert resp.status_code == 200


class TestDvswitchTuneRoute:
    def _enable_with_preset(self, tg="9", label="Local"):
        db = app.get_db()
        db.execute(
            "INSERT OR REPLACE INTO dvswitch_config (id, enabled, dmr_network, network_host, talkgroup_presets) "
            "VALUES (1, 1, 'brandmeister', 'master.example.org', ?)",
            (json.dumps([{"label": label, "tg": tg}]),)
        )
        db.commit()

    def test_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        self._enable_with_preset()
        resp = client.post("/api/dvswitch/tune", json={"tg": "9"})
        assert resp.status_code in (401, 403)

    def test_any_logged_in_role_can_tune(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("user1", role="user")
        _login(client, "user1")
        self._enable_with_preset()
        with patch("app.os.path.isfile", return_value=True), \
             patch("app.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            resp = client.post("/api/dvswitch/tune", json={"tg": "9"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_accepts_tg_not_in_preset_list(self, client, create_user):
        # The Browse Talkgroups popup lets any logged-in role tune to any
        # numeric TG from BrandMeister's full directory, not just the
        # owner's curated presets -- see api_dvswitch_tune()'s docstring
        # for why this was loosened from an earlier preset-only allowlist.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._enable_with_preset(tg="9")
        with patch("app.os.path.isfile", return_value=True), \
             patch("app.subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")):
            resp = client.post("/api/dvswitch/tune", json={"tg": "4000"})
        assert resp.status_code == 200
        assert resp.get_json()["ok"] is True

    def test_rejects_non_numeric_tg(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._enable_with_preset(tg="9")
        resp = client.post("/api/dvswitch/tune", json={"tg": "not-a-number"})
        assert resp.status_code == 400

    def test_rejects_when_not_enabled(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        resp = client.post("/api/dvswitch/tune", json={"tg": "9"})
        assert resp.status_code == 400

    def test_invokes_dvswitch_sh_with_correct_args(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._enable_with_preset(tg="3120")
        mock_run = MagicMock(return_value=MagicMock(returncode=0, stdout="", stderr=""))
        with patch("app.os.path.isfile", return_value=True), patch("app.subprocess.run", mock_run):
            client.post("/api/dvswitch/tune", json={"tg": "3120"})
        args = mock_run.call_args[0][0]
        assert args == [app.DVSWITCH_TUNE_SCRIPT_PATH, "tune", "3120"]

    def test_surfaces_script_failure(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._enable_with_preset(tg="9")
        with patch("app.os.path.isfile", return_value=True), \
             patch("app.subprocess.run", return_value=MagicMock(returncode=1, stdout="", stderr="boom")):
            resp = client.post("/api/dvswitch/tune", json={"tg": "9"})
        assert resp.status_code == 500
        assert "boom" in resp.get_json()["error"]


class TestLookupNodeDvswitchBridge:
    """lookup_node()'s special-case display for the DVSwitch bridge node.
    Regression coverage for a real bug: after switching talkgroups, the
    displayed TG used to come only from _dvswitch_caller_cache, which is
    traffic-driven and can sit on the *previous* TG indefinitely if nobody
    has keyed up on the new one yet -- a successful switch looked like it
    had silently failed. current_tg (Analog_Bridge's own live-tuned value,
    via _dvswitch_current_tg()) must now win over that stale caller tg."""

    def _caller(self, active=False, callsign=None, tg=None):
        return {"active": active, "id": "3218133" if callsign else None,
                "callsign": callsign, "tg": tg, "ts": 0}

    def test_shows_current_tg_even_with_no_caller_yet(self, monkeypatch):
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        monkeypatch.setattr(app, "_dvswitch_current_tg", lambda: "91")
        monkeypatch.setattr(app, "_dvswitch_caller_cache", self._caller())
        d = app.lookup_node("1999")
        assert d["desc"] == "DMR · TG 91"
        assert d["callsign"] == app._DVSWITCH_BRIDGE_NODE_INFO["callsign"]

    def test_stale_caller_tg_does_not_override_current_tg(self, monkeypatch):
        # Caller cache still shows the *previous* talkgroup (3100) because
        # nobody has talked on the newly-tuned one (91) yet -- this is the
        # exact scenario that used to make a switch look like it failed.
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        monkeypatch.setattr(app, "_dvswitch_current_tg", lambda: "91")
        monkeypatch.setattr(app, "_dvswitch_caller_cache",
                             self._caller(active=False, callsign="K9OSU", tg="3100"))
        d = app.lookup_node("1999")
        assert d["desc"] == "DMR · TG 91"
        assert "3100" not in d["desc"]
        assert d["callsign"] == app._DVSWITCH_BRIDGE_NODE_INFO["callsign"]

    def test_active_caller_matching_current_tg_shown_live(self, monkeypatch):
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        monkeypatch.setattr(app, "_dvswitch_current_tg", lambda: "91")
        monkeypatch.setattr(app, "_dvswitch_caller_cache",
                             self._caller(active=True, callsign="K9OSU", tg="91"))
        d = app.lookup_node("1999")
        assert d["callsign"] == "K9OSU"
        assert d["desc"] == "DMR · TG 91"
        assert "Last heard" not in d["desc"]

    def test_inactive_caller_matching_current_tg_shown_as_last_heard(self, monkeypatch):
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        monkeypatch.setattr(app, "_dvswitch_current_tg", lambda: "91")
        monkeypatch.setattr(app, "_dvswitch_caller_cache",
                             self._caller(active=False, callsign="K9OSU", tg="91"))
        d = app.lookup_node("1999")
        assert d["callsign"] == "K9OSU"
        assert d["desc"] == "Last heard — DMR · TG 91"

    def test_falls_back_to_caller_cache_when_current_tg_unavailable(self, monkeypatch):
        # e.g. Analog_Bridge's ABInfo.json hasn't been written yet -- the
        # pre-existing caller-only behavior is the best available fallback.
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        monkeypatch.setattr(app, "_dvswitch_current_tg", lambda: "")
        monkeypatch.setattr(app, "_dvswitch_caller_cache",
                             self._caller(active=True, callsign="K9OSU", tg="3100"))
        d = app.lookup_node("1999")
        assert d["callsign"] == "K9OSU"
        assert d["desc"] == "DMR · TG 3100"

    def test_falls_back_to_generic_label_when_nothing_known(self, monkeypatch):
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        monkeypatch.setattr(app, "_dvswitch_current_tg", lambda: "")
        monkeypatch.setattr(app, "_dvswitch_caller_cache", self._caller())
        d = app.lookup_node("1999")
        assert d == app._DVSWITCH_BRIDGE_NODE_INFO


class TestDvswitchCurrentTg:
    def test_reads_digital_tg_from_abinfo(self, monkeypatch, tmp_path):
        abinfo = tmp_path / "ABInfo.json"
        abinfo.write_text(json.dumps({"digital": {"tg": "91"}}))
        monkeypatch.setattr(app, "DVSWITCH_ABINFO_PATH", str(abinfo))
        assert app._dvswitch_current_tg.__wrapped__() == "91"  # __wrapped__ bypasses the 2s TTL cache

    def test_empty_string_when_file_missing(self, monkeypatch, tmp_path):
        monkeypatch.setattr(app, "DVSWITCH_ABINFO_PATH", str(tmp_path / "nonexistent.json"))
        assert app._dvswitch_current_tg.__wrapped__() == ""


class TestValidateDvswitchLocation:
    """latitude/longitude: written into MMDVM_Bridge.ini's [Info] by
    dvswitch/apply.sh. Blank is legal on save (so an unrelated edit isn't
    blocked); apply is what refuses to run without them."""

    def _v(self, **kw):
        return app._validate_dvswitch_config({**VALID_CONFIG, **kw})

    def test_blank_is_none_not_zero(self):
        cleaned, err = self._v()
        assert err is None and cleaned["latitude"] is None and cleaned["longitude"] is None
        cleaned, err = self._v(latitude="  ", longitude="")
        assert err is None and cleaned["latitude"] is None and cleaned["longitude"] is None

    def test_valid_coordinates_parsed_and_rounded(self):
        cleaned, err = self._v(latitude=" 43.07312345 ", longitude="-86.2012")
        assert err is None
        assert cleaned["latitude"] == 43.073123 and cleaned["longitude"] == -86.2012

    def test_numbers_accepted_as_well_as_strings(self):
        cleaned, err = self._v(latitude=43.0731, longitude=-86.2012)
        assert err is None and cleaned["latitude"] == 43.0731

    def test_zero_zero_is_a_real_location(self):
        cleaned, err = self._v(latitude="0", longitude="0")
        assert err is None
        assert cleaned["latitude"] == 0.0 and cleaned["latitude"] is not None

    def test_half_entered_location_rejected(self):
        assert "both" in self._v(latitude="43.0")[1].lower()
        assert "both" in self._v(longitude="-86.0")[1].lower()

    def test_out_of_range_rejected(self):
        assert "Latitude" in self._v(latitude="91", longitude="0")[1]
        assert "Latitude" in self._v(latitude="-90.5", longitude="0")[1]
        assert "Longitude" in self._v(latitude="0", longitude="180.1")[1]

    def test_non_numeric_and_non_finite_rejected(self):
        assert "number" in self._v(latitude="north", longitude="0")[1]
        assert self._v(latitude="nan", longitude="0")[1] is not None
        assert self._v(latitude="0", longitude="inf")[1] is not None

    def test_disabled_config_still_validates_coordinates(self):
        assert app._validate_dvswitch_config({"enabled": False, "latitude": "200", "longitude": "0"})[1] is not None


class TestDvswitchLocationRoutes:
    def _owner(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")

    def test_defaults_report_no_location(self, client, create_user):
        self._owner(client, create_user)
        body = client.get("/api/dvswitch/config").get_json()
        assert body["latitude"] is None and body["longitude"] is None

    def test_config_round_trips_location(self, client, create_user):
        self._owner(client, create_user)
        r = client.post("/api/dvswitch/config", json={**VALID_CONFIG, "latitude": "43.0731", "longitude": "-86.2012"})
        assert r.status_code == 200
        body = client.get("/api/dvswitch/config").get_json()
        assert body["latitude"] == 43.0731 and body["longitude"] == -86.2012

    def test_save_without_location_still_allowed_and_stored_as_null(self, client, create_user):
        self._owner(client, create_user)
        assert client.post("/api/dvswitch/config", json=VALID_CONFIG).status_code == 200
        body = client.get("/api/dvswitch/config").get_json()
        assert body["latitude"] is None and body["longitude"] is None

    def test_bad_location_rejected_with_message(self, client, create_user):
        self._owner(client, create_user)
        r = client.post("/api/dvswitch/config", json={**VALID_CONFIG, "latitude": "95", "longitude": "0"})
        assert r.status_code == 400 and "Latitude" in r.get_json()["error"]

    def _save(self, client, **kw):
        assert client.post("/api/dvswitch/config", json={**VALID_CONFIG, **kw}).status_code == 200

    def test_apply_refuses_until_location_entered(self, client, create_user):
        self._owner(client, create_user)
        self._save(client)
        resp = client.post("/api/dvswitch/apply")
        assert resp.status_code == 400
        assert "latitude" in resp.get_json()["error"].lower()

    def test_apply_status_not_ready_without_location(self, client, create_user):
        self._owner(client, create_user)
        self._save(client)
        assert client.get("/api/dvswitch/apply-status").get_json()["ready"] is False
        self._save(client, latitude="0", longitude="0")      # 0,0 counts as entered
        assert client.get("/api/dvswitch/apply-status").get_json()["ready"] is True

    def test_apply_exports_location_to_the_script(self, client, create_user, monkeypatch, tmp_path):
        self._owner(client, create_user)
        self._save(client, latitude="43.0731", longitude="-86.2012")
        rpt = tmp_path / "rpt.conf"
        rpt.write_text("[643930]\ncontext = radio-secure\n\n[1999]\nrxchannel = usrp/127.0.0.1:34001:32001\n")
        export = tmp_path / "export.json"
        monkeypatch.setattr(app, "RPT_CONF_PATH", str(rpt))
        monkeypatch.setattr(app, "DVSWITCH_EXPORT_PATH", str(export))
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            return MagicMock(returncode=0, stdout="", stderr="")
        monkeypatch.setattr(app.subprocess, "run", fake_run)
        resp = client.post("/api/dvswitch/apply")
        assert resp.status_code == 200
        exported = json.loads(export.read_text())
        assert exported["latitude"] == 43.0731 and exported["longitude"] == -86.2012
        assert calls[0][-2:] == [app.DVSWITCH_APPLY_SCRIPT_PATH, str(export)]


class TestCallerBlipFilter:
    """Sub-second streams on a busy static TG must not flicker through the
    displayed caller (see DVSWITCH_MIN_CALLER_FRAMES)."""

    def _setup(self, monkeypatch):
        cache = {"active": False, "id": None, "callsign": None, "name": None, "tg": None, "ts": 0}
        monkeypatch.setattr(app, "_dvswitch_caller_cache", cache)
        looked = []
        monkeypatch.setattr(app, "_dvswitch_lookup_dmr",
                            lambda i: (looked.append(i) or ("CALL" + i, "Name")))
        return cache, looked, {"pending": None}

    @staticmethod
    def _begin(i, tg="91"):
        return f"I: 2026-10-06 20:15:35.749 DMR, ODMR Begin Tx: src = {i}, dst = {tg} (GROUP)"

    @staticmethod
    def _end(n):
        return f"I: 2026-10-06 20:15:35.924 DMR, ODMR End Tx:DMR frame count was {n} frames"

    def test_blip_never_becomes_caller_or_triggers_lookup(self, monkeypatch):
        cache, looked, st = self._setup(monkeypatch)
        app._dvswitch_process_caller_lines([self._begin("111"), self._end(3)], st, now=100)
        assert cache["id"] is None and looked == []

    def test_blip_after_real_talker_leaves_them_as_last_heard(self, monkeypatch):
        cache, looked, st = self._setup(monkeypatch)
        app._dvswitch_process_caller_lines([self._begin("222"), self._end(117)], st, now=100)
        app._dvswitch_process_caller_lines([self._begin("333"), self._end(3)], st, now=110)
        assert cache["id"] == "222" and cache["active"] is False

    def test_long_stream_commits_on_end(self, monkeypatch):
        cache, looked, st = self._setup(monkeypatch)
        app._dvswitch_process_caller_lines([self._begin("222"), self._end(117)], st, now=100)
        assert cache["id"] == "222" and cache["callsign"] == "CALL222" and cache["active"] is False

    def test_running_stream_waits_out_blip_window(self, monkeypatch):
        cache, looked, st = self._setup(monkeypatch)
        app._dvswitch_process_caller_lines([self._begin("222")], st, now=100)
        assert cache["id"] is None
        app._dvswitch_process_caller_lines([], st, now=101.2)
        assert cache["id"] == "222" and cache["active"] is True

    def test_ta_line_commits_and_sets_alias(self, monkeypatch):
        cache, looked, st = self._setup(monkeypatch)
        app._dvswitch_process_caller_lines([self._begin("222"), "TA = YT7VNV"], st, now=100)
        assert cache["id"] == "222" and cache["callsign"] == "YT7VNV"

    def test_lost_end_tx_does_not_drop_real_talker(self, monkeypatch):
        cache, looked, st = self._setup(monkeypatch)
        app._dvswitch_process_caller_lines([self._begin("222"), self._begin("333"), self._end(3)], st, now=100)
        assert cache["id"] == "222"


class TestBridgeKeyedGate:
    """The kiosk only shows a DMR caller while the bridge link is keyed."""

    def _with_cache(self, monkeypatch, cache):
        monkeypatch.setattr(app, "_dvswitch_bridge_node_cached", lambda: "1999")
        monkeypatch.setattr(app, "_ami_cache", cache)

    def test_keyed_link_true(self, monkeypatch):
        self._with_cache(monkeypatch, {"643930": {"links": {"1999": {"keyed": True}}}})
        assert app._dvswitch_bridge_keyed() is True

    def test_unkeyed_link_false(self, monkeypatch):
        self._with_cache(monkeypatch, {"643930": {"links": {"1999": {"keyed": False}}}})
        assert app._dvswitch_bridge_keyed() is False

    def test_no_ami_data_fails_open(self, monkeypatch):
        self._with_cache(monkeypatch, {})
        assert app._dvswitch_bridge_keyed() is True


class TestCallerKeyedPromotion:
    """A long-enough stream is only promoted to caller if the bridge link was keyed."""

    BEGIN = "I: 2026-10-06 20:00:00.000 DMR, ODMR Begin Tx: src = 3333, dst = 91 (GROUP)"
    END = "I: 2026-10-06 20:00:03.000 DMR, ODMR End Tx:DMR frame count was 50 frames"

    def _run(self, monkeypatch, keyed):
        monkeypatch.setattr(app, "_dvswitch_lookup_dmr", lambda i: ("W1AW", "Test"))
        monkeypatch.setitem(app._dvswitch_caller_cache, "id", None)
        monkeypatch.setitem(app._dvswitch_caller_cache, "active", False)
        state = {"pending": None}
        app._dvswitch_process_caller_lines([self.BEGIN, self.END], state, keyed_now=keyed)
        return dict(app._dvswitch_caller_cache)

    def test_unkeyed_long_stream_never_becomes_caller(self, monkeypatch):
        c = self._run(monkeypatch, keyed=False)
        assert c["id"] is None and c["active"] is False

    def test_keyed_long_stream_commits(self, monkeypatch):
        c = self._run(monkeypatch, keyed=True)
        assert c["id"] == "3333"

    def test_keyed_seen_mid_stream_counts(self, monkeypatch):
        monkeypatch.setattr(app, "_dvswitch_lookup_dmr", lambda i: ("W1AW", "Test"))
        monkeypatch.setitem(app._dvswitch_caller_cache, "id", None)
        state = {"pending": None}
        app._dvswitch_process_caller_lines([self.BEGIN], state, keyed_now=False)
        app._dvswitch_process_caller_lines([], state, keyed_now=True)
        app._dvswitch_process_caller_lines([self.END], state, keyed_now=False)
        assert app._dvswitch_caller_cache["id"] == "3333"
