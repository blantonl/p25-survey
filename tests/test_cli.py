"""CLI helper tests.

The argparse plumbing and orchestration are tested via end-to-end smoke
runs, but the pure-Python helpers (subscription parsing, sample-rate
parser) get unit tests here.
"""

from __future__ import annotations

from datetime import date

from p25_survey.cli import _msps, _subscription_status


class TestSubscriptionStatus:
    today = date(2026, 5, 28)

    def test_lifetime_admin(self):
        status, detail = _subscription_status("Never - Admin", today=self.today)
        assert status == "lifetime"
        assert "Never" in detail

    def test_lifetime_plain(self):
        status, _ = _subscription_status("Never", today=self.today)
        assert status == "lifetime"

    def test_active_future_date(self):
        status, detail = _subscription_status("2027-01-15", today=self.today)
        assert status == "active"
        assert "2027-01-15" in detail

    def test_active_same_day(self):
        # The day the subscription expires should still be considered active —
        # RR's day-of-expiry semantics: access until end of that day.
        status, _ = _subscription_status("2026-05-28", today=self.today)
        assert status == "active"

    def test_expired_past_date(self):
        status, detail = _subscription_status("2025-12-31", today=self.today)
        assert status == "expired"
        assert "2025-12-31" in detail

    def test_empty_string_unknown(self):
        status, _ = _subscription_status("", today=self.today)
        assert status == "unknown"

    def test_unparseable_unknown(self):
        status, _ = _subscription_status("garbage value", today=self.today)
        assert status == "unknown"

    def test_iso_datetime_with_time_component(self):
        # If RR ever returns "2027-01-15T00:00:00Z" — the regex still
        # extracts the date portion.
        status, _ = _subscription_status("2027-01-15T00:00:00Z", today=self.today)
        assert status == "active"


class TestMspsParser:
    def test_integer_msps(self):
        assert _msps("10") == 10_000_000

    def test_fractional_msps(self):
        assert _msps("2.5") == 2_500_000

    def test_rejects_zero(self):
        import argparse
        import pytest
        with pytest.raises(argparse.ArgumentTypeError):
            _msps("0")

    def test_rejects_negative(self):
        import argparse
        import pytest
        with pytest.raises(argparse.ArgumentTypeError):
            _msps("-1")

    def test_rejects_non_numeric(self):
        import argparse
        import pytest
        with pytest.raises(argparse.ArgumentTypeError):
            _msps("fast")
