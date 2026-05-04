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
    SdrConfig,
    SdrOpenError,
    _open_osmosdr_source,
    autoprobe_driver,
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


def test_supported_drivers_contains_known_set():
    # Sanity check; other modules (gain_sweep CLI help, error message) read
    # this list, so a regression here would be silently confusing.
    assert "rtlsdr" in SUPPORTED_DRIVERS
    assert "airspy" in SUPPORTED_DRIVERS
    assert "hackrf" in SUPPORTED_DRIVERS
