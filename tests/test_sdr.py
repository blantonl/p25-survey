"""Tests for the SDR module's pure-Python pieces.

Hardware open paths obviously can't run without a real device, but the
RuntimeError → `SdrOpenError` translation is testable via a fake
osmosdr module — that's what reproduced the unhelpful traceback Lindsay
hit on dragon1, and what we want to lock down.
"""

from __future__ import annotations

import sys
import types

import pytest

from p25_survey.sdr import (
    SUPPORTED_DRIVERS,
    SampleRateError,
    SampleRateSupport,
    SdrConfig,
    SdrOpenError,
    _open_osmosdr_source,
    autoprobe_driver,
    select_sample_rate,
)


def _install_fake_osmosdr(monkeypatch: pytest.MonkeyPatch, exc: Exception | None):
    """Inject a fake osmosdr module that raises (or returns a sentinel)."""
    fake = types.ModuleType("osmosdr")

    def source(args: str):
        if exc is not None:
            raise exc
        return ("fake-source", args)

    fake.source = source  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "osmosdr", fake)


class TestOpenOsmosdrSource:
    def test_passes_through_when_open_succeeds(self, monkeypatch):
        _install_fake_osmosdr(monkeypatch, exc=None)
        result = _open_osmosdr_source("rtlsdr", "rtl=0")
        assert result == ("fake-source", "rtl=0")

    def test_wraps_runtime_error(self, monkeypatch):
        # The exact gr-osmosdr message Lindsay hit on dragon1 when an
        # Airspy was plugged in but rtlsdr was attempted.
        _install_fake_osmosdr(monkeypatch,
                              exc=RuntimeError("Wrong rtlsdr device index given."))
        with pytest.raises(SdrOpenError) as info:
            _open_osmosdr_source("rtlsdr", "rtl=0")
        assert info.value.driver == "rtlsdr"
        assert info.value.device_args == "rtl=0"
        assert "Wrong rtlsdr device index given." in info.value.underlying
        # The string representation is what main() prints on the first
        # line — it should name the driver and the underlying message.
        assert "rtlsdr" in str(info.value)
        assert "Wrong rtlsdr device index given." in str(info.value)

    def test_passes_through_non_runtime_errors(self, monkeypatch):
        # ImportError or similar shouldn't be flattened to SdrOpenError —
        # those are different classes of failure (gr-osmosdr not installed).
        _install_fake_osmosdr(monkeypatch, exc=ImportError("no such lib"))
        with pytest.raises(ImportError):
            _open_osmosdr_source("rtlsdr", "rtl=0")


class TestSdrConfig:
    def test_resolved_device_args_default(self):
        cfg = SdrConfig(driver="airspy")
        assert cfg.resolved_device_args() == "airspy=0"

    def test_resolved_device_args_override(self):
        cfg = SdrConfig(driver="airspy", device_args="airspy=0,linearity=14")
        assert cfg.resolved_device_args() == "airspy=0,linearity=14"

    def test_resolved_device_args_unknown_driver_raises(self):
        cfg = SdrConfig(driver="bladerf")
        with pytest.raises(ValueError):
            cfg.resolved_device_args()


class TestAutoprobeDriver:
    def test_returns_rtlsdr_when_osmosdr_importable(self, monkeypatch):
        _install_fake_osmosdr(monkeypatch, exc=None)
        assert autoprobe_driver() == "rtlsdr"

    def test_returns_none_when_osmosdr_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "osmosdr", None)
        # `import osmosdr` with sys.modules entry of None raises ImportError.
        assert autoprobe_driver() is None


class TestSampleRateSupport:
    def test_supports_discrete(self):
        s = SampleRateSupport(driver="airspy", discrete=(2_500_000, 10_000_000))
        assert s.supports(10_000_000)
        assert s.supports(2_500_000)
        assert not s.supports(6_000_000)

    def test_supports_continuous(self):
        s = SampleRateSupport(driver="hackrf", range_hz=(2_000_000, 20_000_000))
        assert s.supports(8_000_000)
        assert s.supports(2_000_000)
        assert s.supports(20_000_000)
        assert not s.supports(1_999_999)
        assert not s.supports(20_000_001)

    def test_describe_discrete(self):
        s = SampleRateSupport(driver="airspy", discrete=(2_500_000, 10_000_000))
        assert "2.5 MSPS" in s.describe()
        assert "10 MSPS" in s.describe()

    def test_describe_continuous(self):
        s = SampleRateSupport(driver="hackrf", range_hz=(2_000_000, 20_000_000))
        assert "2–20 MSPS" in s.describe()


class TestSelectSampleRate:
    def test_auto_picks_highest_discrete_under_cap_airspy_r2(self):
        # R2: discrete {2.5M, 10M}. Cap is 10M → pick 10M.
        s = SampleRateSupport(driver="airspy", discrete=(2_500_000, 10_000_000))
        assert select_sample_rate(s, None) == 10_000_000

    def test_auto_picks_highest_discrete_under_cap_airspy_mini(self):
        # Mini: discrete {3M, 6M}. Both ≤ cap → pick 6M.
        s = SampleRateSupport(driver="airspy", discrete=(3_000_000, 6_000_000))
        assert select_sample_rate(s, None) == 6_000_000

    def test_auto_falls_back_to_lowest_when_all_exceed_cap(self):
        # Hypothetical device where every supported rate is above 10 MSPS;
        # picking the lowest is the safe choice (and beats raising).
        s = SampleRateSupport(driver="airspy", discrete=(12_000_000, 20_000_000))
        assert select_sample_rate(s, None) == 12_000_000

    def test_auto_continuous_uses_driver_default(self):
        # RTL-SDR continuous range covers the 2.4M default → returned as-is.
        s = SampleRateSupport(driver="rtlsdr", range_hz=(250_000, 3_200_000))
        assert select_sample_rate(s, None) == 2_400_000

    def test_auto_continuous_clamps_to_range(self):
        # Span ends below the driver default — clamp to the high end.
        s = SampleRateSupport(driver="rtlsdr", range_hz=(250_000, 2_000_000))
        assert select_sample_rate(s, None) == 2_000_000

    def test_user_override_validates_against_discrete(self):
        s = SampleRateSupport(driver="airspy", discrete=(2_500_000, 10_000_000))
        assert select_sample_rate(s, 2_500_000) == 2_500_000
        with pytest.raises(SampleRateError) as info:
            select_sample_rate(s, 6_000_000)
        assert "6 MSPS not supported" in str(info.value)
        assert "airspy" in str(info.value)

    def test_user_override_validates_against_continuous(self):
        s = SampleRateSupport(driver="hackrf", range_hz=(2_000_000, 20_000_000))
        assert select_sample_rate(s, 8_000_000) == 8_000_000
        with pytest.raises(SampleRateError):
            select_sample_rate(s, 1_000_000)

    def test_no_support_with_no_default_raises(self):
        s = SampleRateSupport(driver="bladerf")  # no discrete, no range
        with pytest.raises(ValueError):
            select_sample_rate(s, None)


def test_supported_drivers_contains_known_set():
    # Sanity check; other modules (gain_sweep CLI help, error message) read
    # this list, so a regression here would be silently confusing.
    assert "rtlsdr" in SUPPORTED_DRIVERS
    assert "airspy" in SUPPORTED_DRIVERS
    assert "hackrf" in SUPPORTED_DRIVERS
