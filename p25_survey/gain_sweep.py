"""BER-driven gain sweep on confirmed control channels.

After Phase 2 finishes and we know which frequencies are real P25 CCs,
sweep through a small set of gain values on each one and measure the
resulting BER. The gain that minimizes BER on each CC is the
"best for that link"; aggregating across CCs in a band gives a
per-band recommended `--gain` setting.

Why BER and not RSSI: RSSI just says "the receiver sees signal" — it
doesn't say "the receiver decodes cleanly". A too-hot front end can have
strong RSSI and terrible BER (compression / IMD); too-low gain has weak
RSSI and BER from noise. BER finds the sweet spot.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from p25_survey.bands import find_band
from p25_survey.survey import SurveyRecord


# Default sweep grids per driver. Five evenly-spaced values, biased away
# from the very bottom of the range (where decode usually fails entirely).
_DEFAULT_SWEEPS: dict[str, list[float]] = {
    "airspy": [4, 8, 12, 16, 20],     # 0-21 linearity preset
    "rtlsdr": [10, 20, 30, 40, 49.6], # tuner gain (snapped to nearest valid)
    "hackrf": [12, 24, 36, 48, 60],   # IF/VGA, 0-62
}


def default_sweep_grid(driver: str) -> list[float]:
    return _DEFAULT_SWEEPS.get(driver, [4, 8, 12, 16, 20])


def parse_sweep_grid(spec: str) -> list[float]:
    """Parse a comma-separated user-supplied sweep grid like "4,8,12,16,20"."""
    out: list[float] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        out.append(float(part))
    if len(out) < 2:
        raise ValueError("--gain-sweep needs at least two values")
    return out


# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GainSample:
    """One (gain, BER) measurement on a single CC."""
    gain_db: float
    ber_pct: float | None
    decode_rate_pct: float | None
    rssi_dbfs: float | None
    locked: bool                 # True if at least one TSBK decoded


@dataclass
class CcSweepResult:
    """All sweep samples for one control channel, plus the chosen best."""
    freq_hz: int
    samples: list[GainSample]
    band_name: str
    best_gain: float | None      # gain with lowest BER among locked samples
    best_ber: float | None
    original_gain: float | None
    original_ber: float | None   # BER recorded during the main Phase 2 scan

    @property
    def improvement_pp(self) -> float | None:
        """Drop in BER (percentage points) versus the user's original gain."""
        if self.original_ber is None or self.best_ber is None:
            return None
        return round(self.original_ber - self.best_ber, 2)


@dataclass
class BandRecommendation:
    band_name: str
    n_cc: int
    median_gain: float
    mean_gain: float
    range_gain: tuple[float, float]


# ---------------------------------------------------------------------------
# Best-gain selection
# ---------------------------------------------------------------------------


def pick_best_gain(samples: Iterable[GainSample]) -> GainSample | None:
    """Lowest BER wins. Ties broken by highest decode_rate, then by lowest gain
    (lower gain = less front-end strain, and a wider safety margin if signal
    levels rise on a different day)."""
    locked = [s for s in samples if s.locked and s.ber_pct is not None]
    if not locked:
        return None
    locked.sort(key=lambda s: (
        s.ber_pct,
        -(s.decode_rate_pct or 0),
        s.gain_db,
    ))
    return locked[0]


# ---------------------------------------------------------------------------
# Per-band aggregation
# ---------------------------------------------------------------------------


def recommend_per_band(results: Iterable[CcSweepResult]) -> list[BandRecommendation]:
    """Aggregate per-CC best gains into per-band recommendations.

    Median is the headline because it's robust to a single outlier CC. Mean
    + range are also reported so the user can see if the band is consistent
    or noisy.
    """
    by_band: dict[str, list[float]] = defaultdict(list)
    for r in results:
        if r.best_gain is not None:
            by_band[r.band_name].append(r.best_gain)

    out: list[BandRecommendation] = []
    for band_name, gains in by_band.items():
        if not gains:
            continue
        out.append(BandRecommendation(
            band_name=band_name,
            n_cc=len(gains),
            median_gain=statistics.median(gains),
            mean_gain=round(statistics.mean(gains), 2),
            range_gain=(min(gains), max(gains)),
        ))
    out.sort(key=lambda r: r.band_name)
    return out


def cc_sweep_result(freq_hz: int, samples: list[GainSample],
                    original_gain: float | None,
                    original_ber: float | None) -> CcSweepResult:
    """Construct a CcSweepResult, computing best_gain from samples."""
    best = pick_best_gain(samples)
    band = find_band(freq_hz)
    return CcSweepResult(
        freq_hz=freq_hz,
        samples=samples,
        band_name=band.name if band else "unknown",
        best_gain=best.gain_db if best else None,
        best_ber=best.ber_pct if best else None,
        original_gain=original_gain,
        original_ber=original_ber,
    )


# ---------------------------------------------------------------------------
# Driving the sweep on a live SDR
# ---------------------------------------------------------------------------


def confirmed_ccs(records: Iterable[SurveyRecord]) -> list[SurveyRecord]:
    """Filter records to only those that locked enough to produce BER data
    (i.e. we know they're real CCs and the gain sweep will mean something)."""
    return [
        r for r in records
        if r.complete
        and r.signal.ber_pct_mean is not None
        and r.wacn is not None
    ]
