"""Tests for the Manager > Asterisk Updates feature: apt output parsing, log
line classification, the weekly-check state machine and the owner-gated
routes. Nothing here touches apt, sudo or systemd -- every subprocess seam
(_asl_run, _asl_upgrade_running, subprocess.run) is stubbed."""
import json
import subprocess

import pytest

import app

POLICY = """asl3-asterisk:
  Installed: 2:22.9.0+asl3-3.9.3-1.deb13
  Candidate: 2:22.10.1+asl3-3.10.5-1.deb13
  Version table:
 *** 2:22.9.0+asl3-3.9.3-1.deb13 100
        100 /var/lib/dpkg/status
     2:22.10.1+asl3-3.10.5-1.deb13 500
        500 https://repo.allstarlink.org/public trixie/main amd64 Packages
asl3-asterisk-config:
  Installed: 2:22.9.0+asl3-3.9.3-1.deb13
  Candidate: 2:22.9.0+asl3-3.9.3-1.deb13
"""

SIM = """Reading package lists...
Inst asl3-asterisk [2:22.9.0+asl3-3.9.3-1.deb13] (2:22.10.1+asl3-3.10.5-1.deb13 AllStarLink:trixie [amd64])
Inst asl3-asterisk-modules [2:22.9.0+asl3-3.9.3-1.deb13] (2:22.10.1+asl3-3.10.5-1.deb13 AllStarLink:trixie [amd64])
Inst libnewdep (1.0 AllStarLink:trixie [amd64])
Conf asl3-asterisk (2:22.10.1+asl3-3.10.5-1.deb13 AllStarLink:trixie [amd64])
"""


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    """/login carries a real rate limit whose in-memory storage outlives
    conftest's RATELIMIT_ENABLED=False (the limiter is built at import), so
    this file's many logins would otherwise trip it partway through."""
    app.limiter.reset()


def _login(client, username, password="password12345"):
    return client.post("/login", data={"username": username, "password": password})


class TestParsing:
    def test_policy(self):
        p = app.parse_apt_policy(POLICY)
        assert p["asl3-asterisk"] == ("2:22.9.0+asl3-3.9.3-1.deb13", "2:22.10.1+asl3-3.10.5-1.deb13")
        assert p["asl3-asterisk-config"][0] == p["asl3-asterisk-config"][1]

    def test_policy_none_is_normalised(self):
        p = app.parse_apt_policy("foo:\n  Installed: (none)\n  Candidate: 1.0\n")
        assert p["foo"] == (None, "1.0")

    def test_simulation_ignores_conf_lines_and_handles_new_deps(self):
        out = app.parse_apt_simulation(SIM)
        assert [u["name"] for u in out] == ["asl3-asterisk", "asl3-asterisk-modules", "libnewdep"]
        assert out[0]["old"] == "2:22.9.0+asl3-3.9.3-1.deb13"
        assert out[0]["new"] == "2:22.10.1+asl3-3.10.5-1.deb13"
        assert out[2]["old"] == "" and out[2]["new"] == "1.0"


class TestLineClassification:
    def levels(self, lines):
        return [lv for lv, _ in app.classify_asl_update_lines(lines)]

    def test_script_prefixes(self):
        assert self.levels([
            "[10:00:00] [INFO] hello",
            "[10:00:01] [WARN] careful",
            "[10:00:02] [ERROR] bad",
        ]) == ["info", "warn", "error"]

    def test_apt_passthrough_patterns(self):
        assert self.levels([
            "Unpacking asl3-asterisk (2:22.10.1) over (2:22.9.0) ...",
            "E: Sub-process /usr/bin/dpkg returned an error code (1)",
            "W: some apt warning",
            " ==> Keeping old config file as default.",
            "dpkg: error processing package asl3-asterisk (--configure):",
        ]) == ["info", "error", "warn", "warn", "error"]

    def test_indented_continuations_inherit_previous_warn_or_error(self):
        lv = self.levels([
            "[10:00:00] [WARN] new defaults saved:",
            "        /etc/asterisk/rpt.conf.dpkg-dist",
            "[10:00:01] [INFO] next",
            "        2:22.9.0",
        ])
        assert lv == ["warn", "warn", "info", "info"]

    def test_result_markers(self):
        assert self.levels(["=== RESULT: SUCCESS ===", "=== RESULT: FAILED ==="]) == ["ok", "error"]


@pytest.fixture()
def stubbed_apt(monkeypatch):
    """_asl_run answers dpkg-query / apt-cache / apt-get -s / the sudo check."""
    calls = []

    def fake_run(cmd, timeout):
        calls.append(cmd)
        if cmd[0] == "dpkg-query":
            out = ("ii \tasl3-asterisk\t2:22.9.0+asl3-3.9.3-1.deb13\n"
                   "ii \tasl3-asterisk-config\t2:22.9.0+asl3-3.9.3-1.deb13\n"
                   "rc \tasl3-asterisk-old\t1.0\n")
            return subprocess.CompletedProcess(cmd, 0, out, "")
        if cmd[0] == "apt-cache":
            return subprocess.CompletedProcess(cmd, 0, POLICY, "")
        if cmd[0] == "apt-get":
            return subprocess.CompletedProcess(cmd, 0, SIM, "")
        return subprocess.CompletedProcess(cmd, 0, "", "")   # sudo ... check

    monkeypatch.setattr(app, "_asl_run", fake_run)
    return calls


class TestCollectInfo:
    def test_reports_upgrade_and_skips_removed_packages(self, stubbed_apt):
        info = app._asl_collect_update_info()
        assert info["available"] is True
        assert [p["name"] for p in info["packages"]] == ["asl3-asterisk", "asl3-asterisk-config"]
        assert [p["newer"] for p in info["packages"]] == [True, False]
        assert len(info["will_upgrade"]) == 3

    def test_up_to_date_does_not_simulate(self, stubbed_apt, monkeypatch):
        monkeypatch.setattr(app, "_asl_run", lambda cmd, t: subprocess.CompletedProcess(
            cmd, 0,
            "ii \tasl3-asterisk\t1.0\n" if cmd[0] == "dpkg-query"
            else "asl3-asterisk:\n  Installed: 1.0\n  Candidate: 1.0\n", ""))
        info = app._asl_collect_update_info()
        assert info["available"] is False and info["will_upgrade"] == []


class TestCheckState:
    def test_check_persists_state(self, fresh_db, stubbed_apt):
        state = app._asl_check_for_updates(refresh=True)
        assert state["available"] is True and state["refresh_ok"] is True
        assert any(c[0] == app.SUDO_PATH and c[-1] == "check" for c in stubbed_apt)
        assert app._asl_load_state()["will_upgrade"][0]["name"] == "asl3-asterisk"

    def test_failed_refresh_is_recorded_but_result_still_computed(self, fresh_db, stubbed_apt, monkeypatch):
        real = app._asl_run

        def run(cmd, timeout):
            if cmd[0] == app.SUDO_PATH:
                return subprocess.CompletedProcess(cmd, 1, "", "sudo: a password is required")
            return real(cmd, timeout)

        monkeypatch.setattr(app, "_asl_run", run)
        state = app._asl_check_for_updates(refresh=True)
        assert state["refresh_ok"] is False
        assert "password" in state["refresh_error"]
        assert state["available"] is True

    def test_second_concurrent_check_is_refused(self, fresh_db, stubbed_apt):
        assert app._asl_check_lock.acquire(blocking=False)
        try:
            assert app._asl_check_for_updates(refresh=False) is None
        finally:
            app._asl_check_lock.release()

    def test_corrupt_saved_state_reads_as_empty(self, fresh_db):
        app.set_setting(app.ASL_UPDATE_STATE_KEY, "{not json")
        assert app._asl_load_state() == {}


class TestRoutes:
    def _owner(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")

    @pytest.mark.parametrize("method,path", [
        ("get", "/api/asl-update/status"), ("post", "/api/asl-update/check"),
        ("post", "/api/asl-update/install"), ("get", "/api/asl-update/log"),
    ])
    def test_non_owner_is_refused(self, client, create_user, method, path):
        create_user("owner1", role="owner")
        create_user("admin1", password="admin1s-password1", role="superuser")
        _login(client, "admin1", "admin1s-password1")
        assert getattr(client, method)(path).status_code == 403

    def test_install_requires_confirm(self, client, create_user, monkeypatch):
        self._owner(client, create_user)
        monkeypatch.setattr(app, "_asl_upgrade_running", lambda: False)
        assert client.post("/api/asl-update/install", json={}).status_code == 400

    def test_install_refuses_when_up_to_date(self, client, create_user, monkeypatch):
        self._owner(client, create_user)
        monkeypatch.setattr(app, "_asl_upgrade_running", lambda: False)
        monkeypatch.setattr(app, "_asl_collect_update_info",
                            lambda: {"available": False, "packages": [], "will_upgrade": [], "error": ""})
        r = client.post("/api/asl-update/install", json={"confirm": True})
        assert r.status_code == 409

    def test_install_refuses_while_running(self, client, create_user, monkeypatch):
        self._owner(client, create_user)
        monkeypatch.setattr(app, "_asl_upgrade_running", lambda: True)
        assert client.post("/api/asl-update/install", json={"confirm": True}).status_code == 409

    def test_install_launches_exact_sudoers_command(self, client, create_user, monkeypatch):
        self._owner(client, create_user)
        monkeypatch.setattr(app, "_asl_upgrade_running", lambda: False)
        monkeypatch.setattr(app, "_asl_collect_update_info", lambda: {
            "available": True, "packages": [], "error": "",
            "will_upgrade": [{"name": "asl3-asterisk", "old": "1", "new": "2"}]})
        seen = {}

        def fake_run(cmd, **kw):
            seen["cmd"] = cmd
            return subprocess.CompletedProcess(cmd, 0, "", "")

        monkeypatch.setattr(app.subprocess, "run", fake_run)
        r = client.post("/api/asl-update/install", json={"confirm": True})
        assert r.status_code == 200
        # Must match provision-sudoers.sh's rule argument-for-argument, or
        # sudo -n refuses it.
        assert seen["cmd"] == [app.SUDO_PATH, "-n", app.SYSTEMD_RUN_PATH,
                               "--unit=henwen-asl-upgrade", "--collect",
                               app.ASL_UPDATE_SCRIPT_PATH, "install"]

    def test_sudo_failure_is_reported_with_hint(self, client, create_user, monkeypatch):
        self._owner(client, create_user)
        monkeypatch.setattr(app, "_asl_upgrade_running", lambda: False)
        monkeypatch.setattr(app, "_asl_collect_update_info", lambda: {
            "available": True, "packages": [], "error": "",
            "will_upgrade": [{"name": "a", "old": "1", "new": "2"}]})
        monkeypatch.setattr(app.subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(
            cmd, 1, "", "sudo: a password is required"))
        r = client.post("/api/asl-update/install", json={"confirm": True})
        assert r.status_code == 500 and "sudoers" in r.get_json()["hint"]


class TestLogRoute:
    @pytest.fixture()
    def logfile(self, tmp_path, monkeypatch, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        path = tmp_path / "asl.log"
        monkeypatch.setattr(app, "ASL_UPDATE_LOG_PATH", str(path))
        monkeypatch.setattr(app, "_asl_upgrade_running", lambda: False)
        return path

    def test_no_log_yet(self, client, logfile):
        d = client.get("/api/asl-update/log").get_json()
        assert d["exists"] is False and d["lines"] == []

    def test_incremental_reads_and_result(self, client, logfile):
        logfile.write_text("[10:00:00] [INFO] a\n[10:00:01] [WARN] b\n=== RESULT: SUCCESS ===\n")
        d = client.get("/api/asl-update/log").get_json()
        assert [l["level"] for l in d["lines"]] == ["info", "warn", "ok"]
        assert d["result"] == "SUCCESS"
        again = client.get(f"/api/asl-update/log?offset={d['offset']}").get_json()
        assert again["lines"] == []

    def test_partial_line_held_back_while_running(self, client, logfile, monkeypatch):
        monkeypatch.setattr(app, "_asl_upgrade_running", lambda: True)
        logfile.write_text("[10:00:00] [INFO] whole\n[10:00:01] [INF")
        d = client.get("/api/asl-update/log").get_json()
        assert [l["text"] for l in d["lines"]] == ["[10:00:00] [INFO] whole"]
        logfile.write_text("[10:00:00] [INFO] whole\n[10:00:01] [INFO] done\n")
        d2 = client.get(f"/api/asl-update/log?offset={d['offset']}").get_json()
        assert [l["text"] for l in d2["lines"]] == ["[10:00:01] [INFO] done"]

    def test_new_run_replacing_the_file_resets_the_offset(self, client, logfile):
        logfile.write_text("[10:00:00] [INFO] new run\n")
        d = client.get("/api/asl-update/log?offset=99999").get_json()
        assert d["reset"] is True and len(d["lines"]) == 1
