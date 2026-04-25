"""Phase 1 energy scan: tune across the requested range, find spectral peaks.

The scanner walks the SDR's center frequency in chunks of `sample_rate *
usable_bw_fraction` (default 0.8 — leaves a small guardband at the rolloff).
For each chunk we capture IQ, run a Welch PSD, threshold against the median
noise floor, coalesce adjacent above-threshold bins into peaks, snap to the
tuning-step grid, and emit candidates. Phase 2 will then attempt P25 decode
on each candidate.

This module is deliberately SDR-agnostic. It accepts an `iq_provider`
callable that, given a center frequency, returns a complex IQ array. The
real SDR wiring lives in `p25_survey.sdr`; tests inject a synthetic
provider.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass

import numpy as np
from scipy.signal import welch


# Default Welch parameters. nperseg controls bin width:
#   bin_width_hz = sample_rate / nperseg
# At 2.4 Msps and nperseg=4096 → ~586 Hz/bin, fine enough to spot a 12.5 kHz
# wide P25 CC without smearing across grid lines.
DEFAULT_NPERSEG = 4096


@dataclass(frozen=True)
class Candidate:
    """A spectral peak that may be a P25 control channel.

    freq_hz is already snapped to the requested tuning-step grid.
    power_db is dB above the chunk's noise floor (relative, not absolute).
    """
    freq_hz: int
    power_db: float


# ---------------------------------------------------------------------------
# Chunk planner — tells the SDR which center frequencies to tune to.
# ---------------------------------------------------------------------------


def plan_scan_chunks(
    start_hz: int,
    stop_hz: int,
    sample_rate_hz: float,
    usable_bw_fraction: float = 0.8,
) -> list[int]:
    """Center frequencies covering [start_hz, stop_hz] at the given sample rate.

    Each chunk yields `sample_rate * usable_bw_fraction` of usable bandwidth;
    the remaining 20% is rolloff at the band edges and gets ignored by the
    peak finder via guardband filtering on the frequency axis.
    """
    if start_hz >= stop_hz:
        raise ValueError(f"start_hz ({start_hz}) must be < stop_hz ({stop_hz})")
    if sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be positive")
    if not 0 < usable_bw_fraction <= 1:
        raise ValueError("usable_bw_fraction must be in (0, 1]")

    chunk_bw = int(sample_rate_hz * usable_bw_fraction)
    # Center the first chunk so its left edge is at start_hz.
    fc = start_hz + chunk_bw // 2
    out: list[int] = []
    while fc - chunk_bw // 2 < stop_hz:
        out.append(fc)
        fc += chunk_bw
    return out


# ---------------------------------------------------------------------------
# Peak finder — pure NumPy/SciPy. Operates on one IQ chunk at a time.
# ---------------------------------------------------------------------------


def find_peaks(
    iq: np.ndarray,
    sample_rate_hz: float,
    center_freq_hz: int,
    threshold_db: float = 8.0,
    step_hz: int = 12_500,
    min_separation_hz: int = 25_000,
    usable_bw_fraction: float = 0.8,
    nperseg: int = DEFAULT_NPERSEG,
) -> list[Candidate]:
    """Find spectral peaks in one IQ capture.

    Algorithm:
      1. Welch PSD (two-sided, fftshifted to [-fs/2, +fs/2]).
      2. dB scale.
      3. Robust noise floor = median PSD across the chunk.
      4. threshold_line = noise_floor + threshold_db.
      5. Find contiguous runs of bins above threshold; for each run, take
         the bin with maximum PSD as that group's peak.
      6. Drop peaks outside the usable bandwidth (rolloff guardband).
      7. Snap each peak's center frequency to the nearest step_hz multiple.
      8. Coalesce peaks within min_separation_hz, keeping the stronger.
    """
    if len(iq) < nperseg:
        nperseg = max(64, len(iq) // 2)
    if len(iq) < 64:
        return []

    f, psd = welch(
        iq,
        fs=sample_rate_hz,
        nperseg=nperseg,
        noverlap=nperseg // 2,
        return_onesided=False,
        scaling="density",
        detrend=False,
    )
    # Welch with return_onesided=False returns frequencies in [0, fs/2, -fs/2, 0)
    # order; fftshift gives us the natural [-fs/2, +fs/2] layout.
    f = np.fft.fftshift(f)
    psd = np.fft.fftshift(psd)
    psd_db = 10.0 * np.log10(psd + 1e-20)

    noise_floor = float(np.median(psd_db))
    threshold = noise_floor + threshold_db

    # Mask to the usable bandwidth — drop rolloff bins on both sides.
    half_usable = sample_rate_hz * usable_bw_fraction / 2
    in_band = (f >= -half_usable) & (f <= half_usable)
    above = (psd_db > threshold) & in_band

    peaks: list[Candidate] = []
    i = 0
    n = len(above)
    while i < n:
        if not above[i]:
            i += 1
            continue
        j = i
        while j < n and above[j]:
            j += 1
        run_psd = psd_db[i:j]
        run_f = f[i:j]
        local = int(np.argmax(run_psd))
        peak_offset_hz = float(run_f[local])
        absolute_hz = center_freq_hz + peak_offset_hz
        snapped_hz = int(round(absolute_hz / step_hz) * step_hz)
        peaks.append(Candidate(
            freq_hz=snapped_hz,
            power_db=float(run_psd[local] - noise_floor),
        ))
        i = j

    return _coalesce(peaks, min_separation_hz)


def _coalesce(peaks: list[Candidate], min_separation_hz: int) -> list[Candidate]:
    """Merge peaks within `min_separation_hz`, keeping the stronger of each pair."""
    if min_separation_hz <= 0 or not peaks:
        return peaks
    sorted_peaks = sorted(peaks, key=lambda p: p.freq_hz)
    out: list[Candidate] = [sorted_peaks[0]]
    for p in sorted_peaks[1:]:
        if p.freq_hz - out[-1].freq_hz < min_separation_hz:
            if p.power_db > out[-1].power_db:
                out[-1] = p
        else:
            out.append(p)
    return out


# ---------------------------------------------------------------------------
# Range scanner — orchestrates chunks + peak finding into a unique candidate list.
# ---------------------------------------------------------------------------


IqProvider = Callable[[int], np.ndarray]


def scan_range(
    start_hz: int,
    stop_hz: int,
    sample_rate_hz: float,
    iq_provider: IqProvider,
    threshold_db: float = 8.0,
    step_hz: int = 12_500,
    usable_bw_fraction: float = 0.8,
    min_separation_hz: int = 25_000,
    nperseg: int = DEFAULT_NPERSEG,
) -> Iterator[Candidate]:
    """Yield deduplicated candidate peaks across the full [start_hz, stop_hz] range.

    `iq_provider(center_hz)` is called for each chunk and must return a complex
    IQ array sampled at `sample_rate_hz`. Tests pass a synthetic generator;
    the live SDR module will pass a function that retunes and reads samples.

    Yields candidates in ascending frequency order, deduplicated globally
    (a CC near a chunk boundary won't be emitted twice).
    """
    chunks = plan_scan_chunks(start_hz, stop_hz, sample_rate_hz, usable_bw_fraction)
    seen: dict[int, Candidate] = {}
    for fc in chunks:
        iq = iq_provider(fc)
        chunk_peaks = find_peaks(
            iq=iq,
            sample_rate_hz=sample_rate_hz,
            center_freq_hz=fc,
            threshold_db=threshold_db,
            step_hz=step_hz,
            min_separation_hz=min_separation_hz,
            usable_bw_fraction=usable_bw_fraction,
            nperseg=nperseg,
        )
        for p in chunk_peaks:
            if p.freq_hz < start_hz or p.freq_hz > stop_hz:
                continue
            existing = seen.get(p.freq_hz)
            if existing is None or p.power_db > existing.power_db:
                seen[p.freq_hz] = p
    for freq in sorted(seen):
        yield seen[freq]
