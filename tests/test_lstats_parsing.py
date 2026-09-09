"""Unit tests for AMIClient.get_node_status()'s parsing of `rpt lstats` and
RPT_ALINKS.

Issue #74: a Web Transceiver client connects under its *callsign*, not a node
number, and both parsers required 4-7 digits — so the client was dropped
silently and never appeared in the Connected Nodes panel. The fixtures below
are the verbatim output the reporter captured from their node.

get_node_status() only needs AMIClient.command() to return lines, so these
drive a bare instance with that one method stubbed rather than standing up a
real AMI connection.
"""
import pytest

import app


# Verbatim from issue #74, node 628280 with a Web Transceiver client attached.
LSTATS_CALLSIGN = [
    "NODE      PEER                RECONNECTS  DIRECTION  CONNECT TIME        CONNECT STATE",
    "----      ----                ----------  ---------  ------------        -------------",
    "NT0Y      73.145.245.30       0           IN         00:20:32:549        ESTABLISHED",
]
VARS_CALLSIGN = [
    "RPT_TXKEYED=0",
    "RPT_NUMLINKS=1",
    "RPT_LINKS=1,TNT0Y",
    "RPT_NUMALINKS=1",
    "RPT_ALINKS=1,NT0YTU",
    "RPT_RXKEYED=0",
]

# An ordinary numeric peer, to prove the old path is untouched.
LSTATS_NUMERIC = [
    "NODE      PEER                RECONNECTS  DIRECTION  CONNECT TIME        CONNECT STATE",
    "----      ----                ----------  ---------  ------------        -------------",
    "27664     44.1.2.3            0           OUT        01:02:03:456        ESTABLISHED",
]
VARS_NUMERIC = ["RPT_RXKEYED=1", "RPT_ALINKS=2,2324RU,666380TK"]


def _status(monkeypatch, vars_lines, lstats_lines, node="628280"):
    client = app.AMIClient.__new__(app.AMIClient)   # no socket, no connect

    def fake_command(cmd, log_level=None):
        return vars_lines if "show variables" in cmd else lstats_lines

    monkeypatch.setattr(client, "command", fake_command, raising=False)
    return client.get_node_status(node)


class TestCallsignPeer:
    """The issue #74 case."""

    def test_callsign_peer_appears_in_connected(self, monkeypatch):
        st = _status(monkeypatch, VARS_CALLSIGN, LSTATS_CALLSIGN)
        assert st["connected"] == ["NT0Y"]

    def test_header_and_separator_rows_are_not_treated_as_peers(self, monkeypatch):
        st = _status(monkeypatch, VARS_CALLSIGN, LSTATS_CALLSIGN)
        assert "NODE" not in st["connected"]
        assert "----" not in st["connected"]
        assert len(st["connected"]) == 1

    def test_direction_and_connect_time_are_parsed(self, monkeypatch):
        st = _status(monkeypatch, VARS_CALLSIGN, LSTATS_CALLSIGN)
        assert st["link_direction"]["NT0Y"] == "IN"
        assert st["link_connect_seconds"]["NT0Y"] == 20 * 60 + 32
        assert st["link_connect_state"]["NT0Y"] == "ESTABLISHED"

    def test_alinks_splits_callsign_from_mode_flags(self, monkeypatch):
        """NT0YTU -> callsign NT0Y, mode T (transceive), U (unkeyed)."""
        st = _status(monkeypatch, VARS_CALLSIGN, LSTATS_CALLSIGN)
        assert st["links"] == {"NT0Y": {"keyed": False, "mode": "T"}}


class TestNumericPeerUnchanged:
    """Everything that worked before must parse identically."""

    def test_numeric_peer_still_parsed(self, monkeypatch):
        st = _status(monkeypatch, VARS_NUMERIC, LSTATS_NUMERIC)
        assert st["connected"] == ["27664"]
        assert st["link_direction"]["27664"] == "OUT"
        assert st["link_connect_seconds"]["27664"] == 3600 + 2 * 60 + 3

    def test_alinks_numeric_entries_match_previous_behaviour(self, monkeypatch):
        """The docstring's own example: 2324 monitor/unkeyed, 666380 TX/keyed."""
        st = _status(monkeypatch, VARS_NUMERIC, LSTATS_NUMERIC)
        assert st["links"] == {
            "2324":   {"keyed": False, "mode": "R"},
            "666380": {"keyed": True,  "mode": "T"},
        }

    def test_rxkeyed_still_detected(self, monkeypatch):
        assert _status(monkeypatch, VARS_NUMERIC, LSTATS_NUMERIC)["keyed"] is True


class TestEdgeCases:
    def test_the_nodes_own_number_is_never_listed_as_a_peer(self, monkeypatch):
        lstats = [
            "NODE      PEER          RECONNECTS  DIRECTION  CONNECT TIME   CONNECT STATE",
            "628280    44.1.2.3      0           IN         00:00:10:000   ESTABLISHED",
        ]
        assert _status(monkeypatch, [], lstats, node="628280")["connected"] == []

    def test_empty_lstats_yields_no_peers(self, monkeypatch):
        st = _status(monkeypatch, [], [])
        assert st["connected"] == [] and st["links"] == {}

    def test_alinks_entry_without_mode_flags_falls_back_to_numeric(self, monkeypatch):
        st = _status(monkeypatch, ["RPT_ALINKS=1,12345"], [])
        assert st["links"] == {"12345": {"keyed": False, "mode": ""}}

    def test_duplicate_rows_are_not_double_listed(self, monkeypatch):
        lstats = LSTATS_CALLSIGN + [LSTATS_CALLSIGN[-1]]
        assert _status(monkeypatch, VARS_CALLSIGN, lstats)["connected"] == ["NT0Y"]

    @pytest.mark.parametrize("callsign", ["W1AW", "KE8WNV", "N8GMZ", "M0ABC", "VK2XYZ"])
    def test_a_range_of_real_callsign_shapes_parse(self, monkeypatch, callsign):
        lstats = [
            "NODE   PEER        RECONNECTS  DIRECTION  CONNECT TIME   CONNECT STATE",
            f"{callsign}   10.0.0.1    0           IN         00:00:05:000   ESTABLISHED",
        ]
        st = _status(monkeypatch, [f"RPT_ALINKS=1,{callsign}TU"], lstats)
        assert st["connected"] == [callsign]
        assert st["links"] == {callsign: {"keyed": False, "mode": "T"}}
