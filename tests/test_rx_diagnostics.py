"""Tests for /api/rx/diagnostics -- the RX-side counterpart to
api_tx_diagnostics(), backing the Manager's RX Diagnostics page. Each
check's dependency is monkeypatched independently rather than exercised for
real (no real Asterisk/AMI/ffmpeg/modules.conf in this sandbox), mirroring
tests/test_rx_audio_config.py's TestForceTeardownOnChange spy style.
"""
import app


def _login(client, username, password=None):
    row = app.get_db().execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    with client.session_transaction() as sess:
        sess["logged_in"]    = True
        sess["username"]     = username
        sess["role"]         = row["role"]
        sess["user_id"]      = row["id"]
        sess["password_epoch"] = row["password_epoch"]
        sess["idle_timeout"] = (row["session_idle_timeout"] if row["session_idle_timeout"] is not None
                                 else app.SESSION_IDLE_TIMEOUT)
        sess["sid"]          = "test-sid-" + username


def _find(checks, label_substr):
    for c in checks:
        if label_substr in c["label"]:
            return c
    raise AssertionError(f"no check with label containing {label_substr!r} in {checks}")


class TestRxDiagnosticsGating:
    def test_requires_login(self, client, create_user):
        create_user("owner1", role="owner")
        assert client.get("/api/rx/diagnostics").status_code == 401

    def test_plain_user_forbidden(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("user1", password="password12345", role="user")
        _login(client, "user1")
        assert client.get("/api/rx/diagnostics").status_code == 403

    def test_admin_allowed(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("admin1", password="password12345", role="admin")
        _login(client, "admin1")
        assert client.get("/api/rx/diagnostics").status_code == 200


class TestRxDiagnosticsChecks:
    def test_returns_checks_and_summary(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/rx/diagnostics").get_json()
        assert "checks" in body and "summary" in body
        for key in ("pass", "fail", "warn"):
            assert key in body["summary"]
        assert body["summary"]["pass"] + body["summary"]["fail"] + body["summary"]["warn"] == len(body["checks"])

    def test_ffmpeg_check_fails_when_ffmpeg_missing(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        monkeypatch.setattr(app.shutil, "which", lambda name: None)
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "ffmpeg installed")
        assert c["status"] == "fail"

    def test_ffmpeg_check_passes_when_libopus_present(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        monkeypatch.setattr(app.shutil, "which", lambda name: "/usr/bin/ffmpeg")

        class _Proc:
            stdout = "... libopus ..."

        monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: _Proc())
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "libopus")
        assert c["status"] == "pass"

    def test_audiosocket_tap_warns_when_not_applied(self, client, create_user, tmp_path, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        conf = tmp_path / "modules.conf"
        conf.write_text("; nothing here\n")
        monkeypatch.setattr(app, "MODULES_CONF_PATH", str(conf))
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "AudioSocket tap applied")
        assert c["status"] == "warn"

    def test_audiosocket_tap_passes_when_applied(self, client, create_user, tmp_path, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        conf = tmp_path / "modules.conf"
        conf.write_text("; " + app.AUDIOSOCKET_TAP_MARKER + "\n")
        monkeypatch.setattr(app, "MODULES_CONF_PATH", str(conf))
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "AudioSocket tap applied")
        assert c["status"] == "pass"

    def test_mixmonitor_check_reflects_ami_result(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        monkeypatch.setattr(app, "ami_send_command", lambda fn: {"loaded": True})
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "MixMonitor module loaded")
        assert c["status"] == "pass"

    def test_mixmonitor_check_fails_on_ami_error(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")

        def _boom(fn):
            raise RuntimeError("no AMI connection")

        monkeypatch.setattr(app, "ami_send_command", _boom)
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "MixMonitor module loaded")
        assert c["status"] == "fail"

    def test_relay_process_check_fails_when_not_running(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        monkeypatch.setattr(app, "_audio_ws_relay_proc", None)
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "Low-latency relay process running")
        assert c["status"] == "fail"

    def test_relay_process_check_passes_when_running(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")

        class _FakeProc:
            def poll(self):
                return None

        monkeypatch.setattr(app, "_audio_ws_relay_proc", _FakeProc())
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "Low-latency relay process running")
        assert c["status"] == "pass"

    def test_proxy_check_only_warns_when_lowlatency_selected(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "legacy"})
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "Apache proxy applied")
        assert c["status"] == "pass"

        client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "Apache proxy applied")
        assert c["status"] == "warn"

    def test_no_nodes_configured_fails_channel_check(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "Node channel resolution")
        assert c["status"] == "fail"

    def test_node_channel_check_reflects_resolution(self, client, create_user, monkeypatch):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        monkeypatch.setattr(app, "read_conf_file", lambda path: "[546054]\n")
        monkeypatch.setattr(app, "get_node_numbers", lambda content: ["546054"])
        monkeypatch.setattr(app, "_find_node_channel", lambda node: "Zap/1-1" if node == "546054" else None)
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "Node 546054 channel resolves")
        assert c["status"] == "pass"

    def test_https_check_only_warns_when_lowlatency_selected(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "legacy"})
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "arrived over HTTPS")
        assert c["status"] == "pass"

        client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "arrived over HTTPS")
        assert c["status"] == "warn"

    def test_https_check_passes_over_the_apache_tls_proxy(self, client, create_user):
        # _LocalProxyFix restores the real scheme from X-Forwarded-Proto for
        # a request that reached us via Apache's HTTPS vhost. It is the
        # same header Apache itself sets, so this exercises the actual
        # production path rather than just Werkzeug's own (unproxied)
        # is_secure detection.
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "lowlatency"})
        body = client.get("/api/rx/diagnostics",
                           headers={"X-Forwarded-Proto": "https"}).get_json()
        c = _find(body["checks"], "arrived over HTTPS")
        assert c["status"] == "pass"

    def test_current_settings_row_reports_config(self, client, create_user):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        client.put("/api/rx-audio/config", json={"path": "lowlatency", "agc_enabled": True})
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "Current RX audio settings")
        assert c["status"] == "pass"
        assert "lowlatency" in c["detail"]
        assert "on" in c["detail"]


class TestLowLatencyReadyRollup:
    """The 'Low-latency path ready to use' rollup check passes only when
    ffmpeg/libopus, the relay process, the Apache proxy, and HTTPS are all
    in place, warning and naming whichever prerequisite(s) are missing
    otherwise. HTTPS matters because the browser side decodes with
    WebCodecs (AudioDecoder), a secure-context-only API. Every other
    prerequisite here could be satisfied and the path would still never
    actually work for a real visitor without it, which is exactly what
    this rollup is supposed to catch."""

    def _make_ready(self, monkeypatch, tmp_path):
        """Satisfies all four underlying conditions."""
        monkeypatch.setattr(app.shutil, "which", lambda name: "/usr/bin/ffmpeg")

        class _Proc:
            stdout = "... libopus ..."

        monkeypatch.setattr(app.subprocess, "run", lambda *a, **k: _Proc())

        class _FakeProc:
            def poll(self):
                return None

        monkeypatch.setattr(app, "_audio_ws_relay_proc", _FakeProc())

        conf = tmp_path / "henwen.conf"
        conf.write_text("; " + app.WS_AUDIO_MARKER + "\n")
        monkeypatch.setattr(app, "WS_AUDIO_APACHE_CONF_CANDIDATES", (str(conf),))

    def _get(self, client):
        # X-Forwarded-Proto: https stands in for the request having actually
        # arrived over Apache's TLS proxy. See _LocalProxyFix.
        return client.get("/api/rx/diagnostics", headers={"X-Forwarded-Proto": "https"}).get_json()

    def test_passes_when_all_prerequisites_met(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._make_ready(monkeypatch, tmp_path)
        body = self._get(client)
        c = _find(body["checks"], "Low-latency path ready to use")
        assert c["status"] == "pass"

    def test_warns_naming_missing_ffmpeg(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._make_ready(monkeypatch, tmp_path)
        monkeypatch.setattr(app.shutil, "which", lambda name: None)
        body = self._get(client)
        c = _find(body["checks"], "Low-latency path ready to use")
        assert c["status"] == "warn"
        assert "ffmpeg/libopus" in c["detail"]

    def test_warns_naming_missing_relay(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._make_ready(monkeypatch, tmp_path)
        monkeypatch.setattr(app, "_audio_ws_relay_proc", None)
        body = self._get(client)
        c = _find(body["checks"], "Low-latency path ready to use")
        assert c["status"] == "warn"
        assert "relay process" in c["detail"]

    def test_warns_naming_missing_proxy(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._make_ready(monkeypatch, tmp_path)
        monkeypatch.setattr(app, "WS_AUDIO_APACHE_CONF_CANDIDATES", (str(tmp_path / "nope.conf"),))
        body = self._get(client)
        c = _find(body["checks"], "Low-latency path ready to use")
        assert c["status"] == "warn"
        assert "Apache /ws-audio proxy" in c["detail"]

    def test_warns_naming_missing_https(self, client, create_user, monkeypatch, tmp_path):
        create_user("owner1", role="owner")
        _login(client, "owner1")
        self._make_ready(monkeypatch, tmp_path)
        # Plain request, no X-Forwarded-Proto. Everything else is ready,
        # only HTTPS is missing.
        body = client.get("/api/rx/diagnostics").get_json()
        c = _find(body["checks"], "Low-latency path ready to use")
        assert c["status"] == "warn"
        assert "HTTPS" in c["detail"]
