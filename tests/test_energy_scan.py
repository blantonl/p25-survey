"""Energy scan tests using synthetic IQ.

We build complex baseband signals with one or more tones at known offsets
plus AWGN, run them through find_peaks, and assert peak locations and
deduplication behavior. No SDR or GNU Radio needed.
"""

import numpy as np
import pytest

from p25_survey.energy_scan import (
    Candidate,
    find_peaks,
    plan_scan_chunks,
    scan_range,
)


# ---------------------------------------------------------------------------
# Synthetic IQ generators
# ---------------------------------------------------------------------------


def _awgn(n: int, power: float, rng: np.random.Generator) -> np.ndarray:
    sigma = np.sqrt(power / 2.0)
    return rng.normal(0, sigma, n) + 1j * rng.normal(0, sigma, n)


def make_iq(
    tones_hz: list[float],
    sample_rate_hz: float,
    n_samples: int = 1 << 16,
    snr_db: float = 30.0,
    seed: int = 0,
) -> np.ndarray:
    """Complex IQ with one or more CW tones at given baseband offsets.

    snr_db is per-tone over the AWGN floor.
    """
    rng = np.random.default_rng(seed)
    t = np.arange(n_samples) / sample_rate_hz
    signal = np.zeros(n_samples, dtype=np.complex128)
    for f in tones_hz:
        signal += np.exp(2j * np.pi * f * t)
    signal_power = np.abs(signal[0]) ** 2 if tones_hz else 1.0
    noise_power = signal_power / (10 ** (snr_db / 10))
    return (signal + _awgn(n_samples, noise_power, rng)).astype(np.complex64)


# ---------------------------------------------------------------------------
# plan_scan_chunks
# ---------------------------------------------------------------------------


class TestPlanScanChunks:
    def test_covers_full_range(self):
        chunks = plan_scan_chunks(851_000_000, 869_000_000, 2_400_000)
        assert chunks
        # Last chunk's right edge must reach stop_hz.
        chunk_bw = int(2_400_000 * 0.8)
        assert chunks[-1] + chunk_bw // 2 >= 869_000_000

    def test_first_chunk_left_edge_at_start(self):
        chunks = plan_scan_chunks(800_000_000, 810_000_000, 2_500_000)
        chunk_bw = int(2_500_000 * 0.8)
        assert chunks[0] - chunk_bw // 2 == 800_000_000

    def test_step_equals_chunk_bw(self):
        chunks = plan_scan_chunks(770_000_000, 780_000_000, 2_400_000)
        chunk_bw = int(2_400_000 * 0.8)
        for a, b in zip(chunks, chunks[1:]):
            assert b - a == chunk_bw

    def test_invalid_args(self):
        with pytest.raises(ValueError):
            plan_scan_chunks(870_000_000, 860_000_000, 2_400_000)
        with pytest.raises(ValueError):
            plan_scan_chunks(0, 1, 0)
        with pytest.raises(ValueError):
            plan_scan_chunks(0, 1, 1, usable_bw_fraction=0)
        with pytest.raises(ValueError):
            plan_scan_chunks(0, 1, 1, usable_bw_fraction=1.5)


# ---------------------------------------------------------------------------
# find_peaks
# ---------------------------------------------------------------------------


class TestFindPeaks:
    SAMPLE_RATE = 2_400_000
    CENTER = 851_500_000

    def test_pure_noise_no_peaks(self):
        rng = np.random.default_rng(42)
        iq = _awgn(1 << 16, 1.0, rng).astype(np.complex64)
        peaks = find_peaks(iq, self.SAMPLE_RATE, self.CENTER, threshold_db=8.0, step_hz=12_500)
        # Random spectral spikes can squeak above an 8 dB threshold; keep this
        # bound loose. The real correctness test is "tone present" below.
        assert len(peaks) <= 3

    def test_finds_single_tone(self):
        # Tone at +250 kHz from center → 851.750 MHz. Snap to 12.5 kHz grid.
        iq = make_iq([250_000], self.SAMPLE_RATE, snr_db=30)
        peaks = find_peaks(iq, self.SAMPLE_RATE, self.CENTER, threshold_db=10, step_hz=12_500)
        # Should find at least the tone within 12.5 kHz of expected freq.
        target = 851_500_000 + 250_000
        assert any(abs(p.freq_hz - target) <= 12_500 for p in peaks), \
            f"expected tone near {target}, got {peaks}"

    def test_tone_snapped_to_grid(self):
        # Place tone at 251_237 Hz offset → not on a 12.5 kHz boundary.
        # After snap it must be a multiple of 12_500.
        iq = make_iq([251_237], self.SAMPLE_RATE, snr_db=30)
        peaks = find_peaks(iq, self.SAMPLE_RATE, self.CENTER, threshold_db=10, step_hz=12_500)
        for p in peaks:
            assert p.freq_hz % 12_500 == 0

    def test_two_separated_tones(self):
        # 200 kHz apart, well above min_separation_hz=25 kHz default.
        iq = make_iq([-300_000, 300_000], self.SAMPLE_RATE, snr_db=30)
        peaks = find_peaks(iq, self.SAMPLE_RATE, self.CENTER, threshold_db=10, step_hz=12_500)
        targets = {851_500_000 - 300_000, 851_500_000 + 300_000}
        found_targets = {
            t for t in targets if any(abs(p.freq_hz - t) <= 12_500 for p in peaks)
        }
        assert found_targets == targets

    def test_close_tones_coalesced(self):
        # Two tones 10 kHz apart → less than default min_separation 25 kHz.
        # Welch with 4096-bin FFT at 2.4 Msps has ~586 Hz bin width — they will
        # show up as two distinct peaks pre-coalesce, then merge to one.
        iq = make_iq([0, 10_000], self.SAMPLE_RATE, snr_db=30)
        peaks = find_peaks(
            iq, self.SAMPLE_RATE, self.CENTER,
            threshold_db=10, step_hz=12_500, min_separation_hz=25_000,
        )
        # Tones span only 10 kHz; we expect a single coalesced peak.
        # (In FFT terms, both tones land in the same Welch resolution group;
        # this test mostly verifies we don't accidentally emit duplicates.)
        target = 851_500_000
        nearby = [p for p in peaks if abs(p.freq_hz - target) <= 12_500]
        assert len(nearby) == 1

    def test_tone_in_rolloff_ignored(self):
        # Tone at +0.45 * fs (well outside usable_bw_fraction=0.8 → ±0.4 fs).
        offset = int(0.45 * self.SAMPLE_RATE)
        iq = make_iq([offset], self.SAMPLE_RATE, snr_db=40)
        peaks = find_peaks(iq, self.SAMPLE_RATE, self.CENTER, threshold_db=10, step_hz=12_500)
        # Either the rolloff filter drops it, or it lands so far out it's still
        # filtered. Assert no peak within 50 kHz of the rolloff tone.
        forbidden = self.CENTER + offset
        snapped_forbidden = round(forbidden / 12_500) * 12_500
        assert all(abs(p.freq_hz - snapped_forbidden) > 50_000 for p in peaks)

    def test_short_input_returns_empty(self):
        iq = np.zeros(10, dtype=np.complex64)
        assert find_peaks(iq, self.SAMPLE_RATE, self.CENTER) == []

    def test_power_db_is_relative(self):
        iq = make_iq([100_000], self.SAMPLE_RATE, snr_db=30)
        peaks = find_peaks(iq, self.SAMPLE_RATE, self.CENTER, threshold_db=8, step_hz=12_500)
        # Strong tone should be at least 8 dB above floor (matching threshold).
        assert peaks and all(p.power_db >= 8.0 for p in peaks)

    def test_multi_step_picks_closer_grid(self):
        # Tone at +312_500 Hz from CENTER 851.5 MHz → absolute 851.812500 MHz.
        # Compare 5 kHz, 6.25 kHz grids:
        #   851_812_500 / 5_000  = 170_362.5  → snaps to 851_810_000 (±2500 Hz)
        #   851_812_500 / 6_250  = 136_290    → snaps to 851_812_500 (0 Hz)
        # The 6.25 kHz grid is strictly closer; the picker must pick it.
        iq = make_iq([312_500], self.SAMPLE_RATE, snr_db=30)
        peaks = find_peaks(
            iq, self.SAMPLE_RATE, self.CENTER,
            threshold_db=10, step_hz=(5_000, 6_250),
        )
        nearby = [p for p in peaks if abs(p.freq_hz - 851_812_500) <= 10_000]
        assert nearby, f"expected a peak near 851.8125 MHz, got {peaks}"
        assert nearby[0].freq_hz == 851_812_500

    def test_multi_step_falls_back_to_only_grid_in_list(self):
        # Single-element tuple should behave identically to passing the int.
        iq = make_iq([100_000], self.SAMPLE_RATE, snr_db=30)
        single = find_peaks(iq, self.SAMPLE_RATE, self.CENTER, threshold_db=10, step_hz=12_500)
        tuple_arg = find_peaks(iq, self.SAMPLE_RATE, self.CENTER, threshold_db=10,
                               step_hz=(12_500,))
        assert [p.freq_hz for p in single] == [p.freq_hz for p in tuple_arg]

    def test_invalid_step_argument_rejected(self):
        iq = make_iq([100_000], self.SAMPLE_RATE, snr_db=30)
        import pytest
        with pytest.raises(ValueError):
            find_peaks(iq, self.SAMPLE_RATE, self.CENTER, step_hz=())
        with pytest.raises(ValueError):
            find_peaks(iq, self.SAMPLE_RATE, self.CENTER, step_hz=(12_500, 0))
        with pytest.raises(ValueError):
            find_peaks(iq, self.SAMPLE_RATE, self.CENTER, step_hz=0)


# ---------------------------------------------------------------------------
# scan_range — full pipeline with synthetic SDR
# ---------------------------------------------------------------------------


class TestScanRange:
    SAMPLE_RATE = 2_400_000

    def _make_provider(self, tones_at: dict[int, float]):
        """Returns a callable that synthesizes IQ for a given chunk center.

        tones_at: {absolute_freq_hz: snr_db}. Each call gives back a chunk
        containing only the tones that fall within ±0.5 fs of the requested
        center.
        """
        def provider(center_hz: int) -> np.ndarray:
            half_bw = self.SAMPLE_RATE / 2
            offsets = [
                (f - center_hz, snr) for f, snr in tones_at.items()
                if abs(f - center_hz) < half_bw
            ]
            if not offsets:
                rng = np.random.default_rng(center_hz % 10_000)
                return _awgn(1 << 16, 1.0, rng).astype(np.complex64)
            # All tones share one IQ buffer; use mean SNR for simplicity.
            tones = [o[0] for o in offsets]
            avg_snr = sum(o[1] for o in offsets) / len(offsets)
            return make_iq(tones, self.SAMPLE_RATE, snr_db=avg_snr,
                           seed=int(abs(center_hz)) % 1_000_000)
        return provider

    def test_finds_tone_in_first_chunk(self):
        target = 851_750_000
        provider = self._make_provider({target: 30.0})
        peaks = list(scan_range(
            start_hz=851_000_000, stop_hz=853_000_000,
            sample_rate_hz=self.SAMPLE_RATE,
            iq_provider=provider, threshold_db=10, step_hz=12_500,
        ))
        assert any(abs(p.freq_hz - target) <= 12_500 for p in peaks)

    def test_dedup_across_chunks(self):
        # Tone near a chunk boundary could appear in two adjacent chunks.
        # scan_range must emit it once.
        target = 853_500_000
        provider = self._make_provider({target: 30.0})
        peaks = list(scan_range(
            start_hz=851_000_000, stop_hz=856_000_000,
            sample_rate_hz=self.SAMPLE_RATE,
            iq_provider=provider, threshold_db=10, step_hz=12_500,
        ))
        # No duplicate freq entries — check via set comparison.
        freqs = [p.freq_hz for p in peaks]
        assert len(freqs) == len(set(freqs)), f"duplicates in {freqs}"
        assert any(abs(p.freq_hz - target) <= 12_500 for p in peaks)

    def test_multiple_tones_across_range(self):
        targets = {852_000_000: 30.0, 855_500_000: 30.0, 858_750_000: 30.0}
        provider = self._make_provider(targets)
        peaks = list(scan_range(
            start_hz=851_000_000, stop_hz=860_000_000,
            sample_rate_hz=self.SAMPLE_RATE,
            iq_provider=provider, threshold_db=10, step_hz=12_500,
        ))
        for t in targets:
            assert any(abs(p.freq_hz - t) <= 12_500 for p in peaks), \
                f"missed tone at {t}: peaks={[p.freq_hz for p in peaks]}"

    def test_emitted_peaks_within_range(self):
        provider = self._make_provider({852_500_000: 30.0})
        peaks = list(scan_range(
            start_hz=851_000_000, stop_hz=854_000_000,
            sample_rate_hz=self.SAMPLE_RATE,
            iq_provider=provider, threshold_db=10, step_hz=12_500,
        ))
        for p in peaks:
            assert 851_000_000 <= p.freq_hz <= 854_000_000

    def test_provider_called_for_each_chunk(self):
        calls: list[int] = []

        def tracking_provider(center_hz: int) -> np.ndarray:
            calls.append(center_hz)
            rng = np.random.default_rng(0)
            return _awgn(1 << 14, 1.0, rng).astype(np.complex64)

        list(scan_range(
            start_hz=851_000_000, stop_hz=856_000_000,
            sample_rate_hz=self.SAMPLE_RATE,
            iq_provider=tracking_provider, threshold_db=20, step_hz=12_500,
        ))
        expected_chunks = plan_scan_chunks(851_000_000, 856_000_000, self.SAMPLE_RATE)
        assert calls == expected_chunks
