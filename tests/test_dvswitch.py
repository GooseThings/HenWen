"""Unit tests for DVSwitch's pure helper functions (app.py) — config
validation, rpt.conf bridge-node stanza creation, and default-context
lookup. No DB or Flask app context needed for any of these, same posture
as test_rpt_conf_parser.py; the apt/systemd/AMBE-hardware parts of
dvswitch/apply.sh and check.sh need a real install to verify (see
dvswitch/README.md).
"""
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
