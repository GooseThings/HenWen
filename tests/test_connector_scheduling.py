"""Unit tests for _connector_should_fire(), the Smart Connector scheduler's
pure decision function: given one connector DB row and a `now` datetime,
should it fire this minute? All schedule_type variants are covered.

Row is accessed via row["key"] in app.py, so plain dicts stand in fine for
sqlite3.Row here.
"""
from datetime import datetime

import app


def _row(**overrides):
    base = {
        "connect_time": "14:30",
        "schedule_type": "daily",
        "schedule_days": "",
    }
    base.update(overrides)
    return base


AT_TIME = datetime(2024, 1, 1, 14, 30)      # matches connect_time "14:30", a Monday, week 1
OFF_TIME = datetime(2024, 1, 1, 14, 31)


class TestBasicGating:
    def test_no_connect_time_never_fires(self):
        assert app._connector_should_fire(_row(connect_time=""), AT_TIME) is False
        assert app._connector_should_fire(_row(connect_time=None), AT_TIME) is False

    def test_time_must_match_exactly_to_the_minute(self):
        assert app._connector_should_fire(_row(), OFF_TIME) is False
        assert app._connector_should_fire(_row(), AT_TIME) is True

    def test_manual_schedule_never_fires_automatically(self):
        assert app._connector_should_fire(_row(schedule_type="manual"), AT_TIME) is False


class TestDaily:
    def test_fires_every_day_at_the_right_time(self):
        assert app._connector_should_fire(_row(schedule_type="daily"), AT_TIME) is True


class TestWeekly:
    def test_fires_on_an_allowed_weekday(self):
        row = _row(schedule_type="weekly", schedule_days="0,2,4")  # Mon/Wed/Fri
        assert app._connector_should_fire(row, AT_TIME) is True  # 2024-01-01 is a Monday

    def test_does_not_fire_on_a_non_allowed_weekday(self):
        row = _row(schedule_type="weekly", schedule_days="1,3")  # Tue/Thu
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_blank_days_means_never(self):
        row = _row(schedule_type="weekly", schedule_days="")
        assert app._connector_should_fire(row, AT_TIME) is False


class TestBiweekly:
    def test_fires_on_matching_weekday_and_parity(self):
        # 2024-01-01: Monday, ISO week 1 (odd -> parity 1)
        row = _row(schedule_type="biweekly", schedule_days="0:1")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_skips_the_off_week(self):
        row = _row(schedule_type="biweekly", schedule_days="0:0")
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_fires_two_weeks_later_on_the_matching_parity(self):
        two_weeks_later = datetime(2024, 1, 15, 14, 30)  # Monday, ISO week 3 (odd)
        row = _row(schedule_type="biweekly", schedule_days="0:1")
        assert app._connector_should_fire(row, two_weeks_later) is True

    def test_malformed_days_field_does_not_raise(self):
        row = _row(schedule_type="biweekly", schedule_days="not-a-number")
        assert app._connector_should_fire(row, AT_TIME) is False


class TestMonthly:
    def test_fires_on_the_configured_day_of_month(self):
        row = _row(schedule_type="monthly", schedule_days="1")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_does_not_fire_on_other_days(self):
        row = _row(schedule_type="monthly", schedule_days="15")
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_malformed_day_does_not_raise(self):
        row = _row(schedule_type="monthly", schedule_days="abc")
        assert app._connector_should_fire(row, AT_TIME) is False


class TestBimonthly:
    def test_fires_in_a_matching_month(self):
        # start_month=1: matches months 1, 3, 5, 7, 9, 11
        row = _row(schedule_type="bimonthly", schedule_days="1:1")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_skips_a_non_matching_month(self):
        row = _row(schedule_type="bimonthly", schedule_days="1:1")
        feb = datetime(2024, 2, 1, 14, 30)
        assert app._connector_should_fire(row, feb) is False


class TestQuarterly:
    def test_fires_every_third_month_from_start(self):
        row = _row(schedule_type="quarterly", schedule_days="1:1")
        assert app._connector_should_fire(row, AT_TIME) is True
        april = datetime(2024, 4, 1, 14, 30)
        assert app._connector_should_fire(row, april) is True

    def test_skips_non_quarter_months(self):
        row = _row(schedule_type="quarterly", schedule_days="1:1")
        feb = datetime(2024, 2, 1, 14, 30)
        assert app._connector_should_fire(row, feb) is False


class TestYearly:
    def test_fires_on_the_configured_month_and_day(self):
        row = _row(schedule_type="yearly", schedule_days="01-01")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_does_not_fire_on_a_different_day(self):
        row = _row(schedule_type="yearly", schedule_days="07-04")
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_malformed_value_does_not_raise(self):
        row = _row(schedule_type="yearly", schedule_days="not-a-date")
        assert app._connector_should_fire(row, AT_TIME) is False


class TestOnetime:
    def test_fires_on_the_exact_date(self):
        row = _row(schedule_type="onetime", schedule_days="2024-01-01")
        assert app._connector_should_fire(row, AT_TIME) is True

    def test_does_not_fire_on_other_dates(self):
        row = _row(schedule_type="onetime", schedule_days="2024-01-02")
        assert app._connector_should_fire(row, AT_TIME) is False

    def test_malformed_date_does_not_raise(self):
        row = _row(schedule_type="onetime", schedule_days="not-a-date")
        assert app._connector_should_fire(row, AT_TIME) is False


def test_unknown_schedule_type_never_fires():
    row = _row(schedule_type="bogus")
    assert app._connector_should_fire(row, AT_TIME) is False
