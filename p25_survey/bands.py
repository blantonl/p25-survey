"""US P25 band table and default tuning step lookup.

Source: see DECISIONS.md (2026-04-25 — Tuning step defaults).
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class Band:
    name: str
    low_hz: int      # inclusive
    high_hz: int     # inclusive
    default_step_hz: int
    notes: str = ""


# Ordered most-specific to least; first match wins on overlap.
US_BANDS: tuple[Band, ...] = (
    Band(
        name="700 MHz PS narrowband downlink (base TX, mobile RX)",
        low_hz=769_000_000,
        high_hz=775_000_000,
        default_step_hz=6_250,
        notes="P25 control channels broadcast here. FCC narrowband segment, 6.25 kHz raster.",
    ),
    Band(
        name="700 MHz PS narrowband uplink (mobile TX, base RX)",
        low_hz=799_000_000,
        high_hz=805_000_000,
        default_step_hz=6_250,
    ),
    Band(
        name="800 MHz public safety (rebanded)",
        low_hz=851_000_000,
        high_hz=869_000_000,
        default_step_hz=12_500,
        notes="6.25 kHz raster but P25 CCs sit on 12.5 kHz boundaries",
    ),
    Band(
        name="UHF land mobile",
        low_hz=380_000_000,
        high_hz=512_000_000,
        default_step_hz=12_500,
        notes="post-narrowband 12.5 kHz",
    ),
    Band(
        name="VHF land mobile",
        low_hz=150_000_000,
        high_hz=174_000_000,
        default_step_hz=7_500,
        notes="post-narrowband 7.5 kHz raster, 12.5 kHz channels",
    ),
)

# Used when start freq doesn't fall in any known band.
FALLBACK_STEP_HZ = 12_500


def find_band(freq_hz: int) -> Band | None:
    """Return the band containing freq_hz, or None if outside known bands."""
    for band in US_BANDS:
        if band.low_hz <= freq_hz <= band.high_hz:
            return band
    return None


def default_step_hz(start_hz: int, stop_hz: int) -> int:
    """Pick a default tuning step for a [start, stop] sweep.

    Uses the band of start_hz. If start and stop straddle two bands with
    different defaults, the start-side band wins; the user can override
    with --step on the CLI.
    """
    band = find_band(start_hz)
    if band is not None:
        return band.default_step_hz
    return FALLBACK_STEP_HZ


def describe_band(freq_hz: int) -> str:
    band = find_band(freq_hz)
    if band is None:
        return f"unknown band (defaulting to {FALLBACK_STEP_HZ} Hz step)"
    return band.name
