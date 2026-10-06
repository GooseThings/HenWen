"""Tests for leak_monitor.py (pure /proc + threshold logic) and app.py's
_check_leaks() alert state machine. /proc reads are exercised against a fake
proc tree under tmp_path, so nothing here needs a live Asterisk."""
import os
import pytest

import app
import leak_monitor as lm

TH = {"fd_pct": 80, "fd_growth": 300, "fd_growth_window": 3600,
      "closewait": 50, "log_bytes": 2 * 1024 ** 3}

# header + one CLOSE_WAIT AMI socket (0x13AE = 5038) + one LISTEN (0A)
PROC_NET_TCP = """  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode
   0: 0100007F:13AE 0100007F:9C4A 08 00000000:00000000 00:00000000 00000000   113        0 11111 1 0000 100 0 0 10 0
   1: 0100007F:13AE 00000000:0000 0A 00000000:00000000 00:00000000 00000000   113        0 22222 1 0000 100 0 0 10 0
   2: 0100007F:13AE 0100007F:9C4B 01 00000000:00000000 00:00000000 00000000   113        0 33333 1 0000 100 0 0 10 0
"""


def _fake_proc(tmp_path, pid, fds, limit="Max open files            1024                 524288               files"):
    d = tmp_path / str(pid)
    (d / "fd").mkdir(parents=True)
    for n, target in fds.items():
        os.symlink(target, d / "fd" / str(n))
    (d / "limits").write_text("Limit  Soft Limit  Hard Limit  Units\n" + limit + "\n")
    (d / "comm").write_text("asterisk\n")
    return d


class TestProcReaders:
    def test_snapshot_classifies_fds_and_collects_socket_inodes(self, tmp_path):
        _fake_proc(tmp_path, 42, {0: "/dev/null", 1: "socket:[11111]", 2: "socket:[22222]",
                                  3: "anon_inode:[eventfd]", 4: "pipe:[9]", 5: "weird"})
        snap = lm.snapshot_fds(42, proc=str(tmp_path))
        assert snap["count"] == 6 and snap["limit"] == 1024
        assert snap["kinds"] == {"file": 1, "socket": 2, "anon_inode": 1, "pipe": 1, "other": 1}
        assert sorted(snap["socket_inodes"]) == [11111, 22222]

    def test_count_is_true_total_even_when_classification_is_capped(self, tmp_path):
        _fake_proc(tmp_path, 7, {n: "/dev/null" for n in range(50)})
        snap = lm.snapshot_fds(7, proc=str(tmp_path), max_classify=10)
        assert snap["count"] == 50 and snap["kinds"]["file"] == 10

    def test_unreadable_process_returns_none(self, tmp_path):
        assert lm.snapshot_fds(999, proc=str(tmp_path)) is None

    def test_unlimited_limit_is_none(self, tmp_path):
        _fake_proc(tmp_path, 5, {}, limit="Max open files            unlimited            unlimited            files")
        assert lm.read_fd_limit(5, proc=str(tmp_path)) is None

    def test_find_pid_prefers_pidfile_then_scans_comm(self, tmp_path):
        _fake_proc(tmp_path, 42, {})
        pidfile = tmp_path / "ast.pid"
        pidfile.write_text("42\n")
        assert lm.find_pid("asterisk", str(pidfile), proc=str(tmp_path)) == 42
        pidfile.write_text("9999\n")                     # stale pidfile -> fall back to scan
        assert lm.find_pid("asterisk", str(pidfile), proc=str(tmp_path)) == 42
        assert lm.find_pid("nothing", None, proc=str(tmp_path)) is None

    def test_tcp_table_maps_inode_to_state_and_port(self):
        table = lm.parse_proc_net_tcp(PROC_NET_TCP)
        assert table[11111] == ("CLOSE_WAIT", 5038)
        assert table[22222] == ("LISTEN", 5038)
        counts = lm.socket_state_counts([11111, 22222, 33333, 99999], table)
        assert lm.count_state(counts, "CLOSE_WAIT") == 1
        assert lm.describe_sockets(counts, "CLOSE_WAIT") == ":5038 x1"


class TestPortFallback:
    def test_counts_server_side_sockets_by_local_port_excluding_listen(self):
        table = lm.parse_proc_net_tcp(PROC_NET_TCP)
        counts = lm.port_state_counts(table, 5038)
        assert counts == {("CLOSE_WAIT", 5038): 1, ("ESTABLISHED", 5038): 1}
        assert lm.port_state_counts(table, 8088) == {}

    def test_messages_omit_breakdown_when_fds_cannot_be_classified(self):
        snap = {"count": 900, "limit": 1000, "kinds": {}, "socket_inodes": []}
        msg = lm.evaluate({"asterisk": snap}, TH)["asterisk_fds_high"][1]
        assert "900 of 1000" in msg and "mostly" not in msg


class TestGrowth:
    def test_needs_history_spanning_half_the_window(self):
        assert lm.growth_over_window([(0, 100), (600, 900)], 3600, 600) == 0

    def test_steady_climb_reports_total_gain(self):
        samples = [(t, 100 + t // 6) for t in range(0, 3601, 60)]
        assert lm.growth_over_window(samples, 3600, 3600) == 600

    def test_spike_that_decays_is_not_growth(self):
        samples = [(0, 100), (900, 800), (1800, 120), (3600, 105)]
        assert lm.growth_over_window(samples, 3600, 3600) == 5

    def test_old_samples_outside_window_ignored(self):
        assert lm.growth_over_window([(0, 10), (5000, 500), (7000, 520)], 3600, 7000) == 20


class TestEvaluate:
    def _snap(self, count, limit=1000, kinds=None):
        return {"count": count, "limit": limit, "kinds": kinds or {"socket": count}, "socket_inodes": []}

    def test_quiet_system_has_every_key_inactive(self):
        out = lm.evaluate({"asterisk": self._snap(90, 65536), "henwen": self._snap(9, 1024),
                           "asterisk_sockets": {}, "orphan_taps": [], "log_bytes": 5 * 1024 ** 2}, TH)
        assert out and all(not active for active, _ in out.values())

    def test_fd_pct_trips_at_threshold_and_names_the_numbers(self):
        out = lm.evaluate({"asterisk": self._snap(800, 1000)}, TH)
        active, msg = out["asterisk_fds_high"]
        assert active and "800 of 1000" in msg and "80%" in msg
        assert not lm.evaluate({"asterisk": self._snap(799, 1000)}, TH)["asterisk_fds_high"][0]

    def test_unlimited_process_never_trips_pct(self):
        assert not lm.evaluate({"asterisk": self._snap(5000, None)}, TH)["asterisk_fds_high"][0]

    def test_growth_trips(self):
        out = lm.evaluate({"henwen": self._snap(500, 1024), "henwen_growth": 350}, TH)
        assert out["henwen_fds_growing"][0] and "350" in out["henwen_fds_growing"][1]

    def test_closewait_names_the_port(self):
        counts = {("CLOSE_WAIT", 5038): 456, ("ESTABLISHED", 5038): 2}
        active, msg = lm.evaluate({"asterisk_sockets": counts}, TH)["asterisk_closewait"]
        assert active and "456" in msg and ":5038 x456" in msg

    def test_orphan_taps_and_log_size(self):
        out = lm.evaluate({"orphan_taps": ["henwen-tap-1-aa"], "log_bytes": 3 * 1024 ** 3}, TH)
        assert out["tap_orphans"][0] and "henwen-tap-1-aa" in out["tap_orphans"][1]
        assert out["asterisk_log_big"][0] and "3.0 GB" in out["asterisk_log_big"][1]

    def test_unavailable_readings_contribute_no_keys(self):
        # AMI down -> orphan_taps absent -> its condition keeps whatever state it had
        assert lm.evaluate({}, TH) == {}


@pytest.fixture
def leak_env(client, create_user, monkeypatch):
    """alert_config on + a captured _send_alert + fresh monitor state."""
    create_user("owner1", role="owner")
    db = app.get_db()
    db.execute("INSERT OR REPLACE INTO alert_config (id, enabled) VALUES (1, 1)")
    db.commit()
    calls = []
    monkeypatch.setattr(app, "_send_alert", lambda title, msg, priority="default": calls.append((title, msg, priority)))
    app._leak_last_check[0] = 0.0
    app._leak_active.clear()
    state = {"readings": {}}
    monkeypatch.setattr(app, "_leak_collect_readings", lambda now: state["readings"])
    return calls, state


def _run(now_offset=0):
    app._leak_last_check[0] = 0.0     # defeat the self-throttle between calls
    app._check_leaks()


class TestCheckLeaks:
    def test_alert_fires_once_batched_then_clears_once(self, leak_env):
        calls, state = leak_env
        state["readings"] = {"asterisk": {"count": 900, "limit": 1000, "kinds": {"socket": 900}, "socket_inodes": []},
                             "asterisk_sockets": {("CLOSE_WAIT", 5038): 456}}
        _run()
        assert len(calls) == 1                       # two conditions tripped, ONE notification
        title, msg, prio = calls[0]
        assert title == "HenWen: Resource Leak Suspected" and prio == "high"
        assert "900 of 1000" in msg and "456 sockets in CLOSE_WAIT" in msg

        _run()                                       # still tripped -> no repeat
        assert len(calls) == 1

        state["readings"] = {"asterisk": {"count": 90, "limit": 1000, "kinds": {"socket": 90}, "socket_inodes": []},
                             "asterisk_sockets": {}}
        _run()
        assert len(calls) == 2 and calls[1][0] == "HenWen: Resource Leak Cleared"

    def test_second_condition_tripping_later_alerts_separately(self, leak_env):
        calls, state = leak_env
        state["readings"] = {"log_bytes": 3 * 1024 ** 3}
        _run()
        state["readings"] = {"log_bytes": 3 * 1024 ** 3, "orphan_taps": ["henwen-tap-1-aa"]}
        _run()
        assert [c[0] for c in calls] == ["HenWen: Resource Leak Suspected"] * 2
        assert "messages.log" not in calls[1][1] and "henwen-tap-1-aa" in calls[1][1]

    def test_no_clear_notice_until_every_condition_resolves(self, leak_env):
        calls, state = leak_env
        state["readings"] = {"log_bytes": 3 * 1024 ** 3, "orphan_taps": ["henwen-tap-1-aa"]}
        _run()
        state["readings"] = {"log_bytes": 3 * 1024 ** 3, "orphan_taps": []}
        _run()
        assert len(calls) == 1                       # log still big -> not "cleared" yet
        state["readings"] = {"log_bytes": 1024, "orphan_taps": []}
        _run()
        assert calls[-1][0] == "HenWen: Resource Leak Cleared"

    def test_unavailable_reading_does_not_falsely_clear(self, leak_env):
        calls, state = leak_env
        state["readings"] = {"orphan_taps": ["henwen-tap-1-aa"]}
        _run()
        state["readings"] = {}                       # AMI went away: unknown, not "fixed"
        _run()
        assert len(calls) == 1 and app._leak_active["tap_orphans"] is True

    def test_toggle_off_still_records_but_never_alerts(self, leak_env):
        calls, state = leak_env
        db = app.get_db()
        db.execute("UPDATE alert_config SET on_leak_detected=0 WHERE id=1")
        db.commit()
        state["readings"] = {"log_bytes": 3 * 1024 ** 3}
        _run()
        assert calls == []
        assert app._leak_latest["findings"]["asterisk_log_big"][0] is True

    def test_self_throttled_between_intervals(self, leak_env):
        calls, state = leak_env
        state["readings"] = {"log_bytes": 3 * 1024 ** 3}
        app._check_leaks()
        state["readings"] = {"log_bytes": 3 * 1024 ** 3, "orphan_taps": ["henwen-tap-1-aa"]}
        app._check_leaks()                            # inside the interval -> ignored
        assert len(calls) == 1


class TestOrphanTapDetection:
    LINES = [
        "Local/tap@henwen-audiosocket-tap-000000ce;2!henwen-audiosocket-tap!tap!2!Up!AudioSocket!u,127.0.0.1:1!!!!3!{age}!!henwen-tap-643930-aaaa;2",
        "Local/tap@henwen-audiosocket-tap-000000ce;1!henwen-audiosocket-tap!tap!1!Up!ChanSpy!SimpleUSB/643930,q!!!!3!{age}!!henwen-tap-643930-aaaa",
        "SimpleUSB/643930!default!!1!Up!Rpt!643930|P!!!!3!99999!!1791292431.0",
    ]

    def _setup(self, monkeypatch, age, owned=()):
        lines = [l.format(age=age) for l in self.LINES]
        monkeypatch.setattr(app, "_ami_connected", True)
        monkeypatch.setattr(app, "ami_send_command", lambda fn: {"lines": lines})
        monkeypatch.setattr(app, "_audio_active", {})
        monkeypatch.setattr(app, "_capture_only_active", {})
        if owned:
            class B: _tap_channel_id = owned[0]
            monkeypatch.setattr(app, "_audio_active", {"643930": B()})

    def test_unowned_old_tap_is_an_orphan(self, monkeypatch):
        self._setup(monkeypatch, age=300)
        assert app._leak_find_orphan_taps() == ["henwen-tap-643930-aaaa"]

    def test_young_tap_gets_a_grace_period(self, monkeypatch):
        self._setup(monkeypatch, age=5)
        assert app._leak_find_orphan_taps() == []

    def test_owned_tap_is_not_an_orphan(self, monkeypatch):
        self._setup(monkeypatch, age=300, owned=("henwen-tap-643930-aaaa",))
        assert app._leak_find_orphan_taps() == []

    def test_ami_down_means_unknown_not_clean(self, monkeypatch):
        monkeypatch.setattr(app, "_ami_connected", False)
        assert app._leak_find_orphan_taps() is None


class TestAlertConfigAndRoute:
    def test_config_roundtrips_on_leak_detected(self, client, create_user):
        create_user("owner1", role="owner")
        from tests.test_rx_diagnostics import _login
        _login(client, "owner1")
        assert client.get("/api/alerts/config").get_json()["on_leak_detected"] == 1   # default on
        r = client.post("/api/alerts/config", json={"enabled": 1, "on_leak_detected": 0})
        assert r.status_code == 200
        assert client.get("/api/alerts/config").get_json()["on_leak_detected"] == 0

    def test_status_route_is_admin_gated_and_reports_thresholds(self, client, create_user):
        create_user("owner1", role="owner")
        create_user("plain", role="user")
        from tests.test_rx_diagnostics import _login
        assert client.get("/api/leak-monitor").status_code in (401, 403)
        _login(client, "plain")
        assert client.get("/api/leak-monitor").status_code == 403
        _login(client, "owner1")
        body = client.get("/api/leak-monitor").get_json()
        assert body["thresholds"]["closewait"] == app.LEAK_THRESHOLDS["closewait"]
        assert "findings" in body and "readings" in body
