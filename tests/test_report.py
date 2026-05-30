"""Tests for the plain-text survey report.

Focused on the per-record "RR:" annotation line. Russ Mason flagged that a
NAC mismatch surfaces in the live console and the markdown submission report
but had no annotation in the grep-able TXT report.
"""

from __future__ import annotations

from io import StringIO

from p25_survey.report import _fmt_neighbor_site, _fmt_rr, render_record
from p25_survey.survey import IdenUpEntry, NeighborSite, SurveyRecord


def _record(nac=None, rr=None) -> SurveyRecord:
    return SurveyRecord(freq_hz=851_012_500, wacn=0xBEE00, sysid=0x1B2,
                        nac=nac, rfss_id=2, site_id=29, rr=rr)


def _full_match_rr(**overrides):
    rr = {
        "system_match": True,
        "site_match": True,
        "rr_system_name": "Kansas Statewide Interoperability",
        "rr_site_description": "Zurich",
        "rr_site_nac": "1BB",
        "cc_freq_offset": {"offset_hz": 0, "ppm": 0.0,
                           "decoded_hz": 851_012_500, "expected_hz": 851_012_500},
        "cc_freq_in_db": True,
        "neighbors_decoded_not_in_rr": [],
        "neighbor_cc_mismatches": [],
    }
    rr.update(overrides)
    return rr


class TestFmtRR:
    def test_no_enrichment(self):
        assert _fmt_rr(_record(rr=None)) == ""

    def test_clean_match_has_no_nac_annotation(self):
        line = _fmt_rr(_record(nac=0x1BB, rr=_full_match_rr()))
        assert line.startswith("RR:")
        assert "NAC MISMATCH" not in line

    def test_nac_differs_is_annotated(self):
        rr = _full_match_rr(rr_site_nac="2A0")
        line = _fmt_rr(_record(nac=0x1BB, rr=rr))
        assert "NAC MISMATCH: RR=2A0, decoded 1BB" in line

    def test_nac_missing_in_rr_is_annotated(self):
        # Russ's Zurich case: RR has no NAC listed for the site.
        rr = _full_match_rr(rr_site_nac="")
        line = _fmt_rr(_record(nac=0x1BB, rr=rr))
        assert "NAC MISMATCH: RR has none listed, decoded 1BB" in line

    def test_secondary_cc_mismatch_flagged_on_rr_line(self):
        rr = _full_match_rr(secondary_cc_mismatches=[
            {"freq_hz": 851_500_000, "kind": "missing_from_rr", "rr_use_code": None},
        ])
        line = _fmt_rr(_record(nac=0x1BB, rr=rr))
        assert line.startswith("RR:")
        assert "1 secondary CC mismatch(es)" in line


def _full_record(*, secondary_cc=None, neighbors=None, rr=None) -> SurveyRecord:
    return SurveyRecord(
        freq_hz=851_012_500, wacn=0xBEE00, sysid=0x1B2, nac=0x1BB,
        rfss_id=2, site_id=29,
        secondary_cc=secondary_cc or [],
        neighbors=neighbors or [],
        rr=rr,
    )


def _rendered(record: SurveyRecord) -> str:
    out = StringIO()
    render_record(record, out)
    return out.getvalue()


class TestSecondaryCCInReport:
    """Russ Mason's request: a secondary CC that RR is missing — or has
    but doesn't flag as control — must be visible in the TXT report."""

    def test_secondary_cc_line_marks_missing_from_rr(self):
        rr = _full_match_rr(secondary_cc_mismatches=[
            {"freq_hz": 851_500_000, "kind": "missing_from_rr", "rr_use_code": None},
        ])
        text = _rendered(_full_record(secondary_cc=[851_500_000], rr=rr))
        assert "Secondary CC:" in text
        assert "851.50000 MHz [not in RR frequency list]" in text

    def test_secondary_cc_line_marks_not_marked_control(self):
        rr = _full_match_rr(secondary_cc_mismatches=[
            {"freq_hz": 851_500_000, "kind": "not_marked_control", "rr_use_code": ""},
        ])
        text = _rendered(_full_record(secondary_cc=[851_500_000], rr=rr))
        assert 'use="(blank)" not control' in text

    def test_clean_secondary_cc_renders_plain(self):
        text = _rendered(_full_record(secondary_cc=[851_500_000],
                                      rr=_full_match_rr()))
        assert "Secondary CC: 851.50000 MHz\n" in text


class TestNeighborNamesInReport:
    """Russ Mason's request: neighbor site name/location, already in
    submissions.md, should also appear in the TXT neighbor table."""

    def test_neighbor_row_shows_rr_site_description(self):
        rr = _full_match_rr(observed_neighbors=[
            {"rfss_id": 2, "site_id": 30, "freq_hz": 851_100_000, "in_rr": True,
             "description": "Hilltop", "location": "Anytown", "county": "County A"},
        ])
        rec = _full_record(
            neighbors=[NeighborSite(freq_hz=851_100_000, rfss_id=2, site_id=30)],
            rr=rr,
        )
        text = _rendered(rec)
        assert "RR site" in text  # column header
        assert "Hilltop / Anytown / County A" in text

    def test_neighbor_not_in_rr_is_marked(self):
        rr = _full_match_rr(observed_neighbors=[
            {"rfss_id": 2, "site_id": 99, "freq_hz": 860_000_000, "in_rr": False,
             "description": None, "location": None, "county": None},
        ])
        rec = _full_record(
            neighbors=[NeighborSite(freq_hz=860_000_000, rfss_id=2, site_id=99)],
            rr=rr,
        )
        assert "(not in RR roster)" in _rendered(rec)

    def test_fmt_neighbor_site_handles_no_enrichment(self):
        assert _fmt_neighbor_site(None) == ""


class TestStatusFlagRendering:
    def test_failsoft_site_annotated(self):
        rec = _full_record()
        rec.site_network_active = False
        text = _rendered(rec)
        assert "FAILSOFT" in text

    def test_active_site_not_annotated(self):
        rec = _full_record()
        rec.site_network_active = True
        assert "FAILSOFT" not in _rendered(rec)

    def test_band_plan_shows_bandwidth(self):
        rec = _full_record()
        rec.iden_up = [IdenUpEntry(iden=1, base_freq_hz=150_815_000, step_hz=7_500,
                                   offset_hz=0, bandwidth_hz=12_500)]
        text = _rendered(rec)
        assert "FDMA, 12.5 kHz BW" in text

    def test_neighbor_flags_rendered(self):
        rec = _full_record(neighbors=[
            NeighborSite(freq_hz=851_100_000, rfss_id=2, site_id=30,
                         conventional=True, network_active=False, valid=False),
        ])
        text = _rendered(rec)
        assert "conventional" in text
        assert "failsoft" in text
        assert "stale" in text

    def test_healthy_neighbor_has_no_flag_tag(self):
        rec = _full_record(neighbors=[
            NeighborSite(freq_hz=851_100_000, rfss_id=2, site_id=30,
                         conventional=False, site_failure=False,
                         valid=True, network_active=True),
        ])
        # No bracketed flag tag for a clean, active, valid trunked neighbor.
        line = [ln for ln in _rendered(rec).splitlines() if "851.10000" in ln][0]
        assert "[" not in line


class TestSystemMetadataRendering:
    def test_encryption_line(self):
        rec = _full_record()
        rec.encryption_algid = 0x84
        text = _rendered(rec)
        assert "encrypted control channel" in text
        assert "0x84" in text

    def test_lra_and_services_and_offset(self):
        rec = _full_record()
        rec.site_lra = 0x0A
        rec.services_available = ["group voice", "encryption"]
        rec.utc_offset_min = -300
        text = _rendered(rec)
        assert "LRA:" in text and "0x0A" in text
        assert "group voice, encryption" in text
        assert "-05:00" in text

    def test_absent_metadata_renders_nothing(self):
        text = _rendered(_full_record())
        for marker in ("Protected CC", "LRA:", "Services:", "UTC offset:"):
            assert marker not in text
