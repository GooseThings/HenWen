"""Unit tests for _log_board_health(), the journal explanation behind the
kiosk's ONLINE/OFFLINE badge.

The badge itself is computed client-side in status.html from three
server-reported flags (asterisk_active && ami_connected && !stale). When it
flapped, nothing in the journal said which one had dropped -- issue #69, where
an operator reported the badge flashing OFFLINE roughly every 15 seconds and
had no way to narrow it down. These cover the two properties that make the new
logging actually usable: it fires only on a *transition* (the board is polled
every 2s by every open kiosk, so a line per request would bury the journal),
and every OFFLINE line names the specific flag responsible.
"""
import pytest

import app


@pytest.fixture()
def logged(monkeypatch):
    """Capture log() calls and reset the module-wide transition state, which
    would otherwise leak between tests (it's deliberately process-wide so the
    dedup holds across however many kiosks are polling)."""
    calls = []
    monkeypatch.setattr(app, "log", lambda level, msg: calls.append((level, msg)))
    monkeypatch.setattr(app, "_board_health_prev", {})
    return calls


HEALTHY_CACHE = {"stale": False, "age": 1.0}
STALE_CACHE = {"stale": True, "age": 42.5}
EMPTY_CACHE = {"stale": True, "age": None, "error": "No data yet"}


def test_logs_online_on_first_observation(logged):
    app._log_board_health("1234", True, True, HEALTHY_CACHE)
    assert len(logged) == 1
    level, msg = logged[0]
    assert level == "INFO"
    assert "ONLINE" in msg and "1234" in msg


def test_repeat_calls_in_the_same_state_log_nothing(logged):
    """The whole point: 2s polling per viewer must not produce 2s of journal."""
    for _ in range(10):
        app._log_board_health("1234", True, True, HEALTHY_CACHE)
    assert len(logged) == 1


def test_transition_to_offline_and_back_logs_both(logged):
    app._log_board_health("1234", True, True, HEALTHY_CACHE)
    app._log_board_health("1234", True, True, STALE_CACHE)
    app._log_board_health("1234", True, True, HEALTHY_CACHE)
    assert [lvl for lvl, _ in logged] == ["INFO", "WARN", "INFO"]


class TestOfflineNamesTheReason:
    def test_stale_cache_reports_age_and_ttl(self, logged):
        app._log_board_health("1234", True, True, STALE_CACHE)
        level, msg = logged[-1]
        assert level == "WARN"
        assert "stale" in msg
        assert "42.5" in msg                       # the actual age
        assert str(app.CACHE_TTL) in msg           # what it was measured against

    def test_asterisk_down_is_named(self, logged):
        app._log_board_health("1234", False, True, HEALTHY_CACHE)
        _, msg = logged[-1]
        assert "asterisk" in msg.lower()
        assert "AMI pool disconnected" not in msg

    def test_ami_disconnected_is_named(self, logged):
        app._log_board_health("1234", True, False, HEALTHY_CACHE)
        _, msg = logged[-1]
        assert "AMI pool disconnected" in msg

    def test_empty_cache_is_distinguished_from_a_stale_one(self, logged):
        """age=None means the poller has never written this node, which is a
        different problem from a write that has gone quiet."""
        app._log_board_health("1234", True, True, EMPTY_CACHE)
        _, msg = logged[-1]
        assert "no AMI data yet" in msg
        assert "age=" not in msg

    def test_several_reasons_are_all_listed(self, logged):
        app._log_board_health("1234", False, False, STALE_CACHE)
        _, msg = logged[-1]
        assert "asterisk" in msg.lower()
        assert "AMI pool disconnected" in msg
        assert "stale" in msg


def test_nodes_are_tracked_independently(logged):
    """One node going stale must not suppress another node's own transition."""
    app._log_board_health("1234", True, True, HEALTHY_CACHE)
    app._log_board_health("5678", True, True, HEALTHY_CACHE)
    assert len(logged) == 2
    app._log_board_health("1234", True, True, STALE_CACHE)
    assert len(logged) == 3
    assert "1234" in logged[-1][1]


def test_int_and_str_node_ids_are_the_same_node(logged):
    """get_node_numbers() and the cache disagree on type in places; a flip
    between the two must not read as two different nodes flapping."""
    app._log_board_health(1234, True, True, HEALTHY_CACHE)
    app._log_board_health("1234", True, True, HEALTHY_CACHE)
    assert len(logged) == 1
