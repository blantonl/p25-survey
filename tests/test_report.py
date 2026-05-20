"""Tests for the plain-text survey report.

Focused on the per-record "RR:" annotation line. Russ Mason flagged that a
NAC mismatch surfaces in the live console and the markdown submission report
but had no annotation in the grep-able TXT report.
"""

from __future__ import annotations

from p25_survey.report import _fmt_rr
from p25_survey.survey import SurveyRecord


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

    def test_nac_annotation_grepable_via_rr_prefix(self):
        # The whole finding rides on the "RR:" line Russ greps for.
        rr = _full_match_rr(rr_site_nac="2A0")
        line = _fmt_rr(_record(nac=0x1BB, rr=rr))
        assert line.startswith("RR:") and "NAC MISMATCH" in line
