"""Tests for the markdown submission report."""

from __future__ import annotations

from pathlib import Path

from p25_survey.enrich import EnrichmentResult, FreqOffset
from p25_survey.submission import render, render_file
from p25_survey.survey import IdenUpEntry, NeighborSite, SignalQuality, SurveyRecord


def _record(freq_hz=851_006_250, wacn=0xBEE00, sysid=0x1A4, nac=0x293,
            rfss_id=1, site_id=7, neighbors_hz=None, idens=0):
    iden_up = [
        IdenUpEntry(iden=i, base_freq_hz=851_006_250, step_hz=12_500, offset_hz=-45_000_000)
        for i in range(idens)
    ]
    return SurveyRecord(
        freq_hz=freq_hz, complete=True,
        wacn=wacn, sysid=sysid, nac=nac,
        rfss_id=rfss_id, site_id=site_id,
        iden_up=iden_up,
        neighbors=[
            NeighborSite(freq_hz=f, rfss_id=1, site_id=8) for f in (neighbors_hz or [])
        ],
        signal=SignalQuality(rssi_dbfs_mean=-15.0, ber_pct_mean=0.0),
    )


# ---------------------------------------------------------------------------
# Empty / clean cases
# ---------------------------------------------------------------------------


class TestNothingToSubmit:
    def test_empty_records(self):
        text = render([], {})
        assert "Records scanned: **0**" in text
        assert "Nothing to submit" in text

    def test_all_match_clean(self):
        rec = _record()
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X", rr_sid=1,
            cc_freq_offset=FreqOffset(decoded_hz=851_006_250,
                                      expected_hz=851_006_250,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "Nothing to submit" in text
        assert "## New systems" not in text
        assert "## New sites" not in text


# ---------------------------------------------------------------------------
# Section: new systems
# ---------------------------------------------------------------------------


class TestNewSystems:
    def test_renders_unmatched_system(self):
        rec = _record(neighbors_hz=[851_106_250, 851_206_250])
        enr = EnrichmentResult(system_match=False)
        text = render([rec], {rec.freq_hz: enr})
        assert "## New systems" in text
        assert "WACN BEE00" in text
        assert "SYSID 1A4" in text
        assert "NAC 293" in text
        assert "851.10625 MHz" in text  # neighbor listed
        assert "851.20625 MHz" in text

    def test_skips_partial_records(self):
        rec = _record(wacn=None)  # partial: no WACN
        enr = EnrichmentResult()
        text = render([rec], {rec.freq_hz: enr})
        assert "## New systems" not in text


# ---------------------------------------------------------------------------
# Section: new sites
# ---------------------------------------------------------------------------


class TestNewSites:
    def test_renders_known_system_unknown_site(self):
        rec = _record(idens=2)
        enr = EnrichmentResult(
            system_match=True, site_match=False,
            rr_system_name="State Public Safety", rr_sid=42,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## New sites" in text
        assert "State Public Safety" in text
        assert "RFSS 1 / Site 7" in text
        assert "851.00625 MHz" in text
        assert "Band plan (2 entries)" in text


# ---------------------------------------------------------------------------
# Section: frequency mismatches
# ---------------------------------------------------------------------------


class TestFreqMismatch:
    def test_renders_offset(self):
        rec = _record(freq_hz=851_009_000)
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="State Public Safety",
            rr_site_description="Downtown",
            cc_freq_offset=FreqOffset(decoded_hz=851_009_000,
                                      expected_hz=851_006_250,
                                      offset_hz=2750, ppm=3.231),
            cc_freq_in_db=False,
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Frequency mismatches" in text
        assert "Downtown" in text
        assert "RR has:" in text
        assert "851.00625 MHz" in text
        assert "851.00900 MHz" in text
        assert "+2750 Hz" in text
        assert "+3.231 ppm" in text


# ---------------------------------------------------------------------------
# Section: neighbor diffs
# ---------------------------------------------------------------------------


class TestNeighborDiff:
    def test_renders_extra_decoded_neighbor(self):
        rec = _record(neighbors_hz=[851_106_250, 860_500_000])
        enr = EnrichmentResult(
            system_match=True, site_match=True,
            rr_system_name="X",
            cc_freq_offset=FreqOffset(decoded_hz=rec.freq_hz, expected_hz=rec.freq_hz,
                                      offset_hz=0, ppm=0.0),
            cc_freq_in_db=True,
            neighbors_decoded_not_in_rr=[860_500_000],
        )
        text = render([rec], {rec.freq_hz: enr})
        assert "## Neighbor differences" in text
        assert "Neighbors we observed but RR doesn't list" in text
        assert "860.50000 MHz" in text


# ---------------------------------------------------------------------------
# Per-band offset summary
# ---------------------------------------------------------------------------


class TestBandSummary:
    def test_renders_per_band_table(self):
        rec1 = _record(freq_hz=851_007_000)  # 800 MHz, +750 Hz
        rec2 = _record(freq_hz=771_369_500)  # 700 MHz, +750 Hz
        e1 = EnrichmentResult(
            system_match=True, site_match=True,
            cc_freq_offset=FreqOffset(decoded_hz=851_007_000,
                                      expected_hz=851_006_250,
                                      offset_hz=750, ppm=0.881),
            cc_freq_in_db=True,
        )
        e2 = EnrichmentResult(
            system_match=True, site_match=True,
            cc_freq_offset=FreqOffset(decoded_hz=771_369_500,
                                      expected_hz=771_368_750,
                                      offset_hz=750, ppm=0.972),
            cc_freq_in_db=True,
        )
        text = render([rec1, rec2], {rec1.freq_hz: e1, rec2.freq_hz: e2})
        assert "SDR frequency offsets observed" in text
        assert "800 MHz" in text
        assert "700 MHz" in text


# ---------------------------------------------------------------------------
# File round-trip
# ---------------------------------------------------------------------------


class TestRenderFile:
    def test_writes_file(self, tmp_path: Path):
        rec = _record()
        enr = EnrichmentResult(system_match=False)
        path = tmp_path / "submissions.md"
        render_file([rec], {rec.freq_hz: enr}, str(path))
        text = path.read_text()
        assert "## New systems" in text
