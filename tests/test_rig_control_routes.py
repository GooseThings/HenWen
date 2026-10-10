"""Route tests for the kiosk VFO (rig_control routes in app.py): config
gating/validation, role-gated tuning, TX band limits and the keyed/linked
refusals. Uses rig_control.SimRig in place of a real rigctld."""
import pytest

import app
import rig_control


def _login(client, username):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess.update(logged_in=True, username=username, role=row["role"], user_id=row["id"],
                    password_epoch=row["password_epoch"],
                    idle_timeout=app.SESSION_IDLE_TIMEOUT, sid="sid-" + username)


def _logout(client):
    with client.session_transaction() as sess:
        sess.clear()


GOOD = {"enabled": True, "backend": "sim", "host": "127.0.0.1", "port": 4532,
        "node": "64393", "tx_bands": "144-148, 420-450", "step_khz": 5,
        "block_when_keyed": True, "block_when_linked": False,
        "memories": [{"label": "Simplex", "freq": "146.520", "mode": "FM", "ctcss": 0},
                     {"label": "Rptr", "freq": "147.000", "mode": "FM", "ctcss": "100.0",
                      "shift": "+", "offset": "0.600"}]}


@pytest.fixture
def rig(client, create_user, monkeypatch):
    """Owner logged in, rig enabled against a SimRig installed as the live client."""
    create_user("owner1", role="owner")
    create_user("admin1", role="admin")
    create_user("user1", role="user")
    _login(client, "owner1")
    assert client.post("/api/rig/config", json=GOOD).status_code == 200
    sim = rig_control.SimRig()
    monkeypatch.setitem(app._rig_client, "client", sim)
    monkeypatch.setitem(app._rig_state, "state", sim.read_state())
    monkeypatch.setitem(app._rig_state, "error", None)
    monkeypatch.setattr(app, "get_cached_status",
                        lambda node: {"keyed": False, "links": {}, "connected": []})
    return sim


class TestConfig:
    def test_owner_only(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", role="admin")
        _login(client, "admin1")
        assert client.get("/api/rig/config").status_code == 403
        assert client.post("/api/rig/config", json=GOOD).status_code == 403

    def test_round_trip(self, client, rig):
        cfg = client.get("/api/rig/config").get_json()
        assert cfg["enabled"] == 1 and cfg["node"] == "64393"
        assert cfg["memories"][1]["ctcss"] == 100.0
        assert cfg["memories"][1]["shift"] == "+" and cfg["memories"][1]["offset"] == 0.6

    @pytest.mark.parametrize("patch", [
        {"backend": "bogus"}, {"host": "bad host;"}, {"port": 0}, {"step_khz": 7},
        {"node": "12"}, {"tx_bands": ""}, {"tx_bands": "148-144"},
        {"memories": [{"label": "", "freq": "146.5"}]},
        {"memories": [{"label": "X", "freq": "151.0"}]},      # outside bands
        {"memories": [{"label": "X", "freq": "146.5", "mode": "NOPE"}]},
        {"memories": [{"label": "X", "freq": "146.5", "shift": "+"}]},          # shift, no offset
        {"memories": [{"label": "X", "freq": "148.0", "shift": "+", "offset": "0.6"}]},  # TX out of band
        {"memories": [{"label": "X", "freq": "146.5", "shift": "?", "offset": "0.6"}]},
    ])
    def test_rejects_bad_input(self, client, rig, patch):
        assert client.post("/api/rig/config", json={**GOOD, **patch}).status_code == 400


class TestPublicStatusLeaks:
    def test_error_text_hides_rigctld_address_from_the_public(self, client, rig, monkeypatch):
        monkeypatch.setitem(app._rig_state, "state", None)
        monkeypatch.setitem(app._rig_state, "error", "Cannot reach rigctld at 10.9.8.7:4532 (refused)")
        _logout(client)
        body = client.get("/api/rig/status").get_json()
        assert body["error"] == "Radio not responding" and "error_detail" not in body
        assert "10.9.8.7" not in client.get("/api/rig/status").get_data(as_text=True)

    def test_owner_gets_the_detail(self, client, rig, monkeypatch):
        monkeypatch.setitem(app._rig_state, "state", None)
        monkeypatch.setitem(app._rig_state, "error", "Cannot reach rigctld at 10.9.8.7:4532 (refused)")
        _login(client, "owner1")
        assert "10.9.8.7" in client.get("/api/rig/status").get_json()["error_detail"]
        _login(client, "admin1")
        assert "error_detail" not in client.get("/api/rig/status").get_json()


class TestStatus:
    def test_public_and_hides_connection_details(self, client, rig):
        _logout(client)
        body = client.get("/api/rig/status").get_json()
        assert body["enabled"] and body["connected"]
        assert body["freq_hz"] == 146_520_000
        assert "host" not in body and "port" not in body
        assert len(body["memories"]) == 2


class TestTune:
    def test_requires_login(self, client, rig):
        _logout(client)
        assert client.post("/api/rig/tune", json={"memory": 0}).status_code == 401

    def test_user_can_recall_memory_but_not_free_tune(self, client, rig):
        _login(client, "user1")
        assert client.post("/api/rig/tune", json={"memory": 1}).status_code == 200
        assert rig.freq_hz == 147_000_000 and rig.ctcss_tenths == 1000
        assert (rig.shift, rig.offset_hz) == ("+", 600_000)
        assert client.post("/api/rig/tune", json={"freq": "146.94"}).status_code == 403

    def test_admin_free_tune(self, client, rig):
        _login(client, "admin1")
        r = client.post("/api/rig/tune", json={"freq": "146.940", "mode": "fm", "ctcss": "88.5"})
        assert r.status_code == 200
        assert (rig.freq_hz, rig.mode, rig.ctcss_tenths) == (146_940_000, "FM", 885)

    def test_outside_tx_band_refused(self, client, rig):
        _login(client, "admin1")
        r = client.post("/api/rig/tune", json={"freq": "151.0"})
        assert r.status_code == 403
        assert rig.freq_hz == 146_520_000

    def test_bad_input(self, client, rig):
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "abc"}).status_code == 400
        assert client.post("/api/rig/tune", json={"memory": 99}).status_code == 404

    def test_refused_while_node_keyed(self, client, rig, monkeypatch):
        monkeypatch.setattr(app, "get_cached_status",
                            lambda node: {"keyed": True, "links": {}, "connected": []})
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "146.94"}).status_code == 409
        assert rig.freq_hz == 146_520_000

    def test_refused_while_radio_transmitting(self, client, rig):
        rig.ptt = True
        app._rig_state["state"] = rig.read_state()
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "146.94"}).status_code == 409

    def test_refused_while_linked_only_when_configured(self, client, rig, monkeypatch):
        monkeypatch.setattr(app, "get_cached_status",
                            lambda node: {"keyed": False, "links": {}, "connected": ["1999"]})
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "146.94"}).status_code == 200
        _login(client, "owner1")
        client.post("/api/rig/config", json={**GOOD, "block_when_linked": True})
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "146.97"}).status_code == 409

    def test_disabled_returns_503(self, client, rig):
        _login(client, "owner1")
        client.post("/api/rig/config", json={**GOOD, "enabled": False})
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "146.94"}).status_code == 503

    def test_blocked_for_non_owner_while_node_locked(self, client, rig):
        db = app.get_db()
        db.execute("INSERT INTO node_lockouts (node, locked_by) VALUES (?, ?)", ("64393", "owner1"))
        db.commit()
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "146.94"}).status_code == 423


class TestRepeaterTuning:
    def test_admin_sets_shift_offset_and_tone(self, client, rig):
        _login(client, "admin1")
        r = client.post("/api/rig/tune", json={"freq": "147.000", "ctcss": "100.0",
                                               "shift": "+", "offset": "0.600"})
        assert r.status_code == 200
        body = r.get_json()
        assert (rig.shift, rig.offset_hz, rig.ctcss_tenths) == ("+", 600_000, 1000)
        assert body["tx_freq_hz"] == 147_600_000

    def test_transmit_frequency_must_be_in_band(self, client, rig):
        """147.9 + 0.6 transmits on 148.5 -- outside 144-148 even though the
        dial frequency is fine. The radio must not be touched."""
        _login(client, "admin1")
        r = client.post("/api/rig/tune", json={"freq": "147.900", "shift": "+", "offset": "0.600"})
        assert r.status_code == 403 and "Transmit" in r.get_json()["error"]
        assert rig.freq_hz == 146_520_000 and rig.shift == "None"

    def test_negative_shift_below_band_refused(self, client, rig):
        _login(client, "admin1")
        r = client.post("/api/rig/tune", json={"freq": "144.100", "shift": "-", "offset": "0.600"})
        assert r.status_code == 403

    def test_existing_shift_counts_when_request_omits_it(self, client, rig):
        """Retuning without mentioning shift keeps the radio's current +0.6,
        so the band check must use it rather than assuming simplex."""
        rig.shift, rig.offset_hz = "+", 600_000
        app._rig_state["state"] = rig.read_state()
        _login(client, "admin1")
        r = client.post("/api/rig/tune", json={"freq": "147.900"})
        assert r.status_code == 403
        assert client.post("/api/rig/tune", json={"freq": "147.000"}).status_code == 200

    def test_shift_without_offset_rejected(self, client, rig):
        _login(client, "admin1")
        r = client.post("/api/rig/tune", json={"freq": "147.000", "shift": "+"})
        assert r.status_code == 400

    def test_switching_back_to_simplex(self, client, rig):
        rig.shift, rig.offset_hz = "+", 600_000
        app._rig_state["state"] = rig.read_state()
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "146.520", "shift": "None",
                                                  "offset": "0"}).status_code == 200
        assert rig.shift == "None"

    def test_bad_values(self, client, rig):
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "147.0", "shift": "x"}).status_code == 400
        assert client.post("/api/rig/tune", json={"freq": "147.0", "shift": "+",
                                                  "offset": "abc"}).status_code == 400

    def test_unknown_radio_state_refused(self, client, rig, monkeypatch):
        monkeypatch.setitem(app._rig_state, "state", None)
        _login(client, "admin1")
        assert client.post("/api/rig/tune", json={"freq": "146.94"}).status_code == 503

    def test_status_reports_shift_and_tx_freq(self, client, rig):
        rig.shift, rig.offset_hz = "-", 600_000
        app._rig_state["state"] = rig.read_state()
        body = client.get("/api/rig/status").get_json()
        assert body["shift"] == "-" and body["tx_freq_hz"] == 145_920_000


class TestSaveMemory:
    def test_saves_current_state_not_client_values(self, client, rig):
        rig.freq_hz, rig.shift, rig.offset_hz, rig.ctcss_tenths = 147_000_000, "+", 600_000, 1000
        app._rig_state["state"] = rig.read_state()
        _login(client, "admin1")
        r = client.post("/api/rig/memories", json={"label": "My Rptr", "freq_hz": 1,
                                                    "shift": "-", "offset": "9"})
        assert r.status_code == 200
        saved = [m for m in r.get_json()["memories"] if m["label"] == "My Rptr"][0]
        assert (saved["freq_hz"], saved["shift"], saved["offset_hz"], saved["ctcss_tenths"]) == \
            (147_000_000, "+", 600_000, 1000)

    def test_admin_plus_only(self, client, rig):
        _login(client, "user1")
        assert client.post("/api/rig/memories", json={"label": "x"}).status_code == 403
        _logout(client)
        assert client.post("/api/rig/memories", json={"label": "x"}).status_code == 401

    def test_name_required(self, client, rig):
        _login(client, "admin1")
        assert client.post("/api/rig/memories", json={"label": "  "}).status_code == 400

    def test_duplicate_needs_overwrite(self, client, rig):
        _login(client, "admin1")
        r = client.post("/api/rig/memories", json={"label": "simplex"})   # case-insensitive clash
        assert r.status_code == 409 and r.get_json()["exists"]
        rig.freq_hz = 146_940_000
        app._rig_state["state"] = rig.read_state()
        r = client.post("/api/rig/memories", json={"label": "simplex", "overwrite": True})
        assert r.status_code == 200 and r.get_json()["replaced"]
        mems = r.get_json()["memories"]
        assert len(mems) == 2 and mems[0]["freq_hz"] == 146_940_000

    def test_persists_and_is_recallable_by_a_plain_user(self, client, rig):
        rig.freq_hz = 146_940_000
        app._rig_state["state"] = rig.read_state()
        _login(client, "admin1")
        assert client.post("/api/rig/memories", json={"label": "Newly saved"}).status_code == 200
        _login(client, "user1")
        idx = [m["label"] for m in client.get("/api/rig/status").get_json()["memories"]].index("Newly saved")
        rig.freq_hz = 146_520_000
        assert client.post("/api/rig/tune", json={"memory": idx}).status_code == 200
        assert rig.freq_hz == 146_940_000

    def test_refuses_when_radio_outside_bands(self, client, rig):
        rig.freq_hz = 151_000_000
        app._rig_state["state"] = rig.read_state()
        _login(client, "admin1")
        assert client.post("/api/rig/memories", json={"label": "bad"}).status_code == 400

    def test_refuses_when_radio_state_unavailable(self, client, rig, monkeypatch):
        monkeypatch.setitem(app._rig_state, "state", None)
        _login(client, "admin1")
        assert client.post("/api/rig/memories", json={"label": "x"}).status_code == 503

    def test_memory_cap(self, client, rig):
        _login(client, "owner1")
        many = [{"label": f"m{i}", "freq": "146.520"} for i in range(app.RIG_MAX_MEMORIES)]
        assert client.post("/api/rig/config", json={**GOOD, "memories": many}).status_code == 200
        _login(client, "admin1")
        assert client.post("/api/rig/memories", json={"label": "one too many"}).status_code == 400


class TestRfBadge:
    def test_status_names_the_matching_memory(self, client, rig):
        body = client.get("/api/rig/status").get_json()
        assert body["memory"] == "Simplex"            # sim starts on 146.520 FM, simplex, no PL

    def test_memory_name_requires_the_whole_setup_to_match(self, client, rig):
        rig.ctcss_tenths = 885                          # same freq, different PL
        app._rig_state["state"] = rig.read_state()
        assert client.get("/api/rig/status").get_json()["memory"] is None
        rig.freq_hz, rig.ctcss_tenths, rig.shift, rig.offset_hz = 147_000_000, 1000, "+", 600_000
        app._rig_state["state"] = rig.read_state()
        assert client.get("/api/rig/status").get_json()["memory"] == "Rptr"

    def test_offset_ignored_when_simplex(self, client, rig):
        rig.offset_hz = 123_000                         # stale offset, but simplex
        app._rig_state["state"] = rig.read_state()
        assert client.get("/api/rig/status").get_json()["memory"] == "Simplex"

    def test_no_memory_after_retune_away(self, client, rig):
        _login(client, "admin1")
        client.post("/api/rig/tune", json={"freq": "146.940"})
        assert client.get("/api/rig/status").get_json()["memory"] is None

    def test_badge_present_in_node_card_markup(self, client, rig):
        html = client.get("/").get_data(as_text=True)
        assert 'id="rig-badge"' in html and "renderRigBadge" in html
        node_body = html.index('id="node-body"')
        assert node_body < html.index('id="rig-row"') < html.index('id="node-controls"')
        row = html[html.index('id="rig-row"'):html.index('id="node-controls"')]
        assert 'id="rig-badge"' in row and 'id="rig-btn"' in row     # same row

    def test_vfo_button_is_labelled_vfo_and_not_in_the_header(self, client, rig):
        html = client.get("/").get_data(as_text=True)
        btn = html[html.index('id="rig-btn"'):]
        assert btn[:btn.index("</button>")].rstrip().endswith("VFO")
        assert html.count('id="rig-btn"') == 1


class TestTemplates:
    """The kiosk/Manager pages render with the new VFO bits, and every inline
    script in the *rendered* pages still parses (the raw templates contain
    Jinja, so they can only be syntax-checked after rendering)."""

    def _render(self, client, path):
        r = client.get(path)
        assert r.status_code == 200
        return r.get_data(as_text=True)

    def test_kiosk_has_vfo_ui(self, client, rig):
        html = self._render(client, "/")
        for needle in ("id=\"rig-btn\"", "id=\"rig-modal-overlay\"", "/api/rig/tune"):
            assert needle in html

    def test_manager_has_rig_page(self, client, rig):
        html = self._render(client, "/henwen-manager")
        assert "id=\"page-rig-control\"" in html and "rigControlSave" in html

    @pytest.mark.parametrize("path", ["/", "/henwen-manager"])
    def test_inline_scripts_parse(self, client, rig, path, tmp_path):
        import re, shutil, subprocess
        node = shutil.which("node")
        if not node:
            pytest.skip("node not installed")
        html = re.sub(r"<!--.*?-->", "", self._render(client, path), flags=re.S)
        for i, body in enumerate(re.findall(r"<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>", html, re.S)):
            if len(body) < 500:
                continue
            f = tmp_path / f"s{i}.js"
            f.write_text(body)
            r = subprocess.run([node, "--check", str(f)], capture_output=True, text=True)
            assert r.returncode == 0, r.stderr[:500]
