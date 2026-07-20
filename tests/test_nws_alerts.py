"""Unit tests for the NWS severe-weather-alert helper functions in app.py:
VTEC identity parsing, area-description cleanup, and the spoken-text
templating used by the TTS announcement. All pure functions -- no network,
DB, or Flask context involved.
"""
from datetime import datetime

import app


class TestParseVtecKey:
    def test_extracts_stable_identity_from_vtec_string(self):
        props = {
            "id": "urn:oid:2.49.0.1.840.0.abc123.1",
            "parameters": {
                "VTEC": ["/O.CON.KMKX.TO.W.0075.000000T0000Z-260703T1815Z/"],
            },
        }
        assert app._nws_parse_vtec_key(props) == "KMKX.TO.W.0075"

    def test_ignores_the_embedded_timestamp_range(self):
        """Two updates of the same warning with different time-extension
        ranges must resolve to the identical key -- that's the whole point
        of not using the raw CAP id (which changes on every update)."""
        first = {"parameters": {"VTEC": ["/O.CON.KMKX.TO.W.0075.000000T0000Z-260703T1815Z/"]}}
        second = {"parameters": {"VTEC": ["/O.EXT.KMKX.TO.W.0075.000000T0000Z-260703T1900Z/"]}}
        assert app._nws_parse_vtec_key(first) == app._nws_parse_vtec_key(second)

    def test_falls_back_to_cap_id_when_no_vtec(self):
        props = {"id": "urn:oid:2.49.0.1.840.0.abc123.1", "parameters": {}}
        assert app._nws_parse_vtec_key(props) == "urn:oid:2.49.0.1.840.0.abc123.1"

    def test_missing_parameters_key_falls_back_cleanly(self):
        assert app._nws_parse_vtec_key({"id": "xyz"}) == "xyz"

    def test_malformed_vtec_falls_back_to_id(self):
        props = {"id": "fallback-id", "parameters": {"VTEC": ["/too.few.parts/"]}}
        assert app._nws_parse_vtec_key(props) == "fallback-id"


class TestStripStateNames:
    def test_strips_trailing_state_code_from_each_county(self):
        assert app._strip_state_names("Kenosha, WI; Racine, WI") == "Kenosha; Racine"

    def test_leaves_text_without_state_codes_untouched(self):
        assert app._strip_state_names("Kenosha; Racine") == "Kenosha; Racine"

    def test_empty_string(self):
        assert app._strip_state_names("") == ""


class TestJoinWithAnd:
    def test_single_item(self):
        assert app._join_with_and(["Kenosha"]) == "Kenosha"

    def test_two_items(self):
        assert app._join_with_and(["Kenosha", "Racine"]) == "Kenosha and Racine"

    def test_three_or_more_items_oxford_comma(self):
        assert app._join_with_and(["Kenosha", "Racine", "Walworth"]) == "Kenosha, Racine, and Walworth"

    def test_blank_and_whitespace_entries_are_dropped(self):
        assert app._join_with_and(["Kenosha", "  ", "", "Racine"]) == "Kenosha and Racine"

    def test_empty_list(self):
        assert app._join_with_and([]) == ""


class TestFormatClockTime:
    def test_on_the_hour_drops_minutes(self):
        assert app._format_clock_time(datetime(2026, 7, 20, 22, 0)) == "10 PM"

    def test_off_the_hour_keeps_minutes(self):
        assert app._format_clock_time(datetime(2026, 7, 20, 13, 15)) == "1:15 PM"


class TestNwsAlertSpokenText:
    def test_full_alert_with_expiry_and_counties(self):
        alert = {
            "event": "Tornado Warning",
            "areaDesc": "Kenosha, WI; Racine, WI",
            "expires": "2026-07-20T22:00:00-05:00",
        }
        text = app._nws_alert_spoken_text(alert)
        assert text == (
            "The National Weather Service has issued a Tornado Warning. "
            "In effect for the following counties: Kenosha and Racine, until 10 PM."
        )

    def test_no_expiry_omits_until_clause(self):
        alert = {"event": "Flood Advisory", "areaDesc": "Kenosha, WI"}
        text = app._nws_alert_spoken_text(alert)
        assert text == (
            "The National Weather Service has issued a Flood Advisory. "
            "In effect for the following counties: Kenosha."
        )

    def test_missing_area_desc_falls_back_to_your_area(self):
        alert = {"event": "Severe Thunderstorm Warning", "areaDesc": ""}
        text = app._nws_alert_spoken_text(alert)
        assert "In effect for your area." in text

    def test_missing_event_falls_back_to_generic_label(self):
        alert = {"areaDesc": "Kenosha, WI"}
        text = app._nws_alert_spoken_text(alert)
        assert text.startswith("The National Weather Service has issued a Severe Weather Alert.")

    def test_unparseable_expiry_is_ignored_not_raised(self):
        alert = {"event": "Flood Advisory", "areaDesc": "Kenosha, WI", "expires": "not-a-date"}
        text = app._nws_alert_spoken_text(alert)
        assert "until" not in text
