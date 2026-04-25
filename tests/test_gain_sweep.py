"""Tests for the gain-sweep helpers.

Driving an SDR is a hardware integration concern; these tests cover the
pure-Python decision logic: parsing the sweep grid, picking the best gain,
aggregating per-band recommendations.
"""

import pytest

from p25_survey.gain_sweep import (
    BandRecommendation,
    CcSweepResult,
    GainSample,
    cc_sweep_result,
    confirmed_ccs,
    default_sweep_grid,
    parse_sweep_grid,
    pick_best_gain,
    recommend_per_band,
)
from p25_survey.survey import SignalQuality, SurveyRecord


# ---------------------------------------------------------------------------
# Sweep grid parsing
# ---------------------------------------------------------------------------


class TestSweepGrid:
    def test_default_per_driver(self):
        assert default_sweep_grid("airspy") == [4, 8, 12, 16, 20]
        assert default_sweep_grid("rtlsdr")[0] == 10
        assert default_sweep_grid("hackrf")[-1] == 60

    def test_unknown_driver_falls_back(self):
        assert default_sweep_grid("frobozz") == [4, 8, 12, 16, 20]

    def test_parse_explicit_grid(self):
        assert parse_sweep_grid("4,8,12") == [4, 8, 12]

    def test_parse_with_whitespace(self):
        assert parse_sweep_grid(" 4 , 8 , 12 ") == [4, 8, 12]

    def test_parse_floats(self):
        assert parse_sweep_grid("3.5,10.0,49.6") == [3.5, 10.0, 49.6]

    def test_parse_rejects_too_few(self):
        with pytest.raises(ValueError):
            parse_sweep_grid("8")
        with pytest.raises(ValueError):
            parse_sweep_grid("")


# ---------------------------------------------------------------------------
# Best-gain selection
# ---------------------------------------------------------------------------


def _sample(gain: float, ber: float | None, decode_rate: float | None = None,
            rssi: float | None = None, locked: bool | None = None) -> GainSample:
    return GainSample(
        gain_db=gain,
        ber_pct=ber,
        decode_rate_pct=decode_rate if decode_rate is not None
                          else (100.0 - ber if ber is not None else None),
        rssi_dbfs=rssi,
        locked=locked if locked is not None else (ber is not None),
    )


class TestPickBestGain:
    def test_lowest_ber_wins(self):
        samples = [
            _sample(4, 5.0),
            _sample(8, 1.0),
            _sample(12, 3.0),
        ]
        best = pick_best_gain(samples)
        assert best.gain_db == 8

    def test_unlocked_samples_skipped(self):
        samples = [
            _sample(4, ber=None, locked=False),
            _sample(8, ber=2.0),
        ]
        assert pick_best_gain(samples).gain_db == 8

    def test_no_locked_returns_none(self):
        samples = [_sample(4, ber=None, locked=False)] * 3
        assert pick_best_gain(samples) is None

    def test_ties_broken_by_decode_rate(self):
        # Both BER=0 but decode_rate differs (synthetic — could happen if
        # one gain saw fewer total TSBKs but no errors).
        samples = [
            _sample(4, ber=0.0, decode_rate=80.0),
            _sample(8, ber=0.0, decode_rate=99.0),
        ]
        assert pick_best_gain(samples).gain_db == 8

    def test_full_ties_broken_by_lower_gain(self):
        # Identical metrics → prefer lower gain (less risk of front-end
        # overload on stronger days).
        samples = [
            _sample(8, ber=0.0, decode_rate=100.0),
            _sample(16, ber=0.0, decode_rate=100.0),
        ]
        assert pick_best_gain(samples).gain_db == 8


# ---------------------------------------------------------------------------
# CcSweepResult construction
# ---------------------------------------------------------------------------


class TestCcSweepResult:
    def test_assembles_band_and_best(self):
        samples = [
            _sample(4, ber=10.0),
            _sample(8, ber=2.0),
            _sample(12, ber=5.0),
        ]
        r = cc_sweep_result(
            freq_hz=851_006_250, samples=samples,
            original_gain=14, original_ber=15.0,
        )
        assert r.best_gain == 8
        assert r.best_ber == 2.0
        assert "800 MHz" in r.band_name
        assert r.improvement_pp == 13.0  # 15 - 2

    def test_no_locked_samples(self):
        samples = [_sample(g, ber=None, locked=False) for g in (4, 8, 12)]
        r = cc_sweep_result(851_006_250, samples, 14, 15.0)
        assert r.best_gain is None
        assert r.improvement_pp is None


# ---------------------------------------------------------------------------
# Per-band recommendation
# ---------------------------------------------------------------------------


def _result(freq_hz: int, best_gain: float) -> CcSweepResult:
    return CcSweepResult(
        freq_hz=freq_hz,
        samples=[_sample(best_gain, 0.5)],
        band_name="800 MHz" if 850_000_000 <= freq_hz <= 870_000_000 else "700 MHz",
        best_gain=best_gain, best_ber=0.5,
        original_gain=14, original_ber=2.0,
    )


class TestRecommendPerBand:
    def test_groups_by_band(self):
        results = [
            CcSweepResult(freq_hz=851_006_250, samples=[],
                          band_name="800 MHz PS rebanded",
                          best_gain=8.0, best_ber=0.0,
                          original_gain=14, original_ber=2.0),
            CcSweepResult(freq_hz=853_780_000, samples=[],
                          band_name="800 MHz PS rebanded",
                          best_gain=12.0, best_ber=0.5,
                          original_gain=14, original_ber=1.0),
            CcSweepResult(freq_hz=771_368_750, samples=[],
                          band_name="700 MHz PS downlink",
                          best_gain=16.0, best_ber=0.0,
                          original_gain=14, original_ber=0.5),
        ]
        recs = recommend_per_band(results)
        assert len(recs) == 2
        bands = {r.band_name: r for r in recs}
        assert bands["800 MHz PS rebanded"].n_cc == 2
        assert bands["800 MHz PS rebanded"].median_gain == 10.0  # median of [8, 12]
        assert bands["700 MHz PS downlink"].n_cc == 1
        assert bands["700 MHz PS downlink"].median_gain == 16.0

    def test_skips_results_without_best(self):
        results = [
            CcSweepResult(freq_hz=851_006_250, samples=[],
                          band_name="800", best_gain=None, best_ber=None,
                          original_gain=14, original_ber=None),
        ]
        assert recommend_per_band(results) == []

    def test_median_with_three_values(self):
        results = [_result(851_000_000 + i * 100_000, g) for i, g in enumerate([4, 8, 16])]
        recs = recommend_per_band(results)
        assert recs[0].median_gain == 8
        assert recs[0].mean_gain == round((4 + 8 + 16) / 3, 2)
        assert recs[0].range_gain == (4, 16)


# ---------------------------------------------------------------------------
# confirmed_ccs filter
# ---------------------------------------------------------------------------


class TestConfirmedCcs:
    def test_filters_to_complete_with_ber(self):
        good = SurveyRecord(freq_hz=851_006_250, complete=True,
                            wacn=0xBEE00, sysid=0x1A4,
                            signal=SignalQuality(ber_pct_mean=0.5))
        partial = SurveyRecord(freq_hz=853_000_000, complete=False,
                               wacn=0xBEE00, sysid=0x1A4,
                               signal=SignalQuality(ber_pct_mean=0.5))
        no_wacn = SurveyRecord(freq_hz=854_000_000, complete=True,
                               signal=SignalQuality(ber_pct_mean=0.5))
        no_ber = SurveyRecord(freq_hz=855_000_000, complete=True,
                              wacn=0xBEE00, sysid=0x1A4,
                              signal=SignalQuality())
        result = confirmed_ccs([good, partial, no_wacn, no_ber])
        assert result == [good]
