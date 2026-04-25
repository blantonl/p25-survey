"""Tests for the NDJSON survey writer + reader and the text report."""

import json
from pathlib import Path

import pytest

from p25_survey.report import render, render_file, render_record
from p25_survey.survey import (
    IdenUpEntry,
    NeighborSite,
    SignalQuality,
    SurveyRecord,
    SurveyWriter,
    read_survey,
)


def _sample_record(freq_hz: int = 851_006_250, complete: bool = True) -> SurveyRecord:
    return SurveyRecord(
        freq_hz=freq_hz,
        complete=complete,
        wacn=0xBEE00,
        sysid=0x1A4,
        nac=0x293,
        rfss_id=1,
        site_id=7,
        neighbors=[
            NeighborSite(freq_hz=851_106_250, rfss_id=1, site_id=8,
                         sysid=0x1A4, wacn=0xBEE00),
            NeighborSite(freq_hz=851_206_250, rfss_id=1, site_id=9),
        ],
        iden_up=[
            IdenUpEntry(iden=0, base_freq_hz=851_006_250, step_hz=12_500,
                        offset_hz=-45_000_000),
            IdenUpEntry(iden=2, base_freq_hz=851_006_250, step_hz=12_500,
                        offset_hz=-45_000_000, is_tdma=True, slots_per_carrier=2),
        ],
        signal=SignalQuality(
            rssi_dbfs_mean=-42.1, rssi_dbfs_peak=-38.7,
            ber_pct_mean=0.4, decode_rate_pct=98.7,
        ),
        dwell_ms=4123,
        sdr_driver="rtlsdr",
        sdr_gain_db=40.0,
        sdr_ppm=0.0,
    )


# ---------------------------------------------------------------------------
# JSON round-trip
# ---------------------------------------------------------------------------


class TestJsonRoundTrip:
    def test_record_roundtrip(self):
        rec = _sample_record()
        d = rec.to_json_dict()
        # Hex strings present
        assert d["wacn"] == "BEE00"
        assert d["sysid"] == "1A4"
        assert d["nac"] == "293"
        assert d["neighbors"][0]["wacn"] == "BEE00"
        # Reload
        rec2 = SurveyRecord.from_json_dict(d)
        assert rec2.wacn == 0xBEE00
        assert rec2.sysid == 0x1A4
        assert rec2.nac == 0x293
        assert rec2.neighbors[0].wacn == 0xBEE00
        assert rec2.iden_up[1].is_tdma is True
        assert rec2.iden_up[1].slots_per_carrier == 2
        assert rec2.signal.rssi_dbfs_mean == -42.1

    def test_none_fields_stay_none(self):
        rec = SurveyRecord(freq_hz=851_000_000)  # no wacn/sysid/nac
        d = rec.to_json_dict()
        assert d["wacn"] is None
        assert d["sysid"] is None
        assert d["nac"] is None
        rec2 = SurveyRecord.from_json_dict(d)
        assert rec2.wacn is None

    def test_neighbor_without_wacn_stays_none(self):
        rec = SurveyRecord(
            freq_hz=851_000_000,
            neighbors=[NeighborSite(freq_hz=851_100_000, rfss_id=1, site_id=2)],
        )
        d = rec.to_json_dict()
        assert d["neighbors"][0]["wacn"] is None
        rec2 = SurveyRecord.from_json_dict(d)
        assert rec2.neighbors[0].wacn is None


# ---------------------------------------------------------------------------
# SurveyWriter
# ---------------------------------------------------------------------------


class TestSurveyWriter:
    def test_writes_one_line_per_record(self, tmp_path: Path):
        out = tmp_path / "survey.json"
        w = SurveyWriter(out)
        w.append(_sample_record(freq_hz=851_006_250))
        w.append(_sample_record(freq_hz=852_018_750))
        lines = out.read_text().strip().split("\n")
        assert len(lines) == 2
        # Each line is valid JSON
        for line in lines:
            json.loads(line)

    def test_resume_picks_up_existing_freqs(self, tmp_path: Path):
        out = tmp_path / "survey.json"
        w1 = SurveyWriter(out)
        w1.append(_sample_record(freq_hz=851_006_250))
        w1.append(_sample_record(freq_hz=852_018_750))

        w2 = SurveyWriter(out, resume=True)
        assert w2.already_characterized(851_006_250)
        assert w2.already_characterized(852_018_750)
        assert not w2.already_characterized(853_000_000)

    def test_resume_no_file_is_clean(self, tmp_path: Path):
        out = tmp_path / "fresh.json"
        w = SurveyWriter(out, resume=True)
        assert w.existing_freqs == set()
        assert not w.already_characterized(851_000_000)

    def test_resume_skips_corrupt_lines(self, tmp_path: Path):
        out = tmp_path / "survey.json"
        out.write_text(
            json.dumps(_sample_record(freq_hz=851_000_000).to_json_dict()) + "\n"
            + "this is not json\n"
            + json.dumps(_sample_record(freq_hz=852_000_000).to_json_dict()) + "\n"
        )
        w = SurveyWriter(out, resume=True)
        assert w.already_characterized(851_000_000)
        assert w.already_characterized(852_000_000)

    def test_append_updates_existing_set(self, tmp_path: Path):
        out = tmp_path / "survey.json"
        w = SurveyWriter(out, resume=True)
        rec = _sample_record(freq_hz=851_006_250)
        w.append(rec)
        assert w.already_characterized(851_006_250)


class TestReadSurvey:
    def test_round_trip_via_disk(self, tmp_path: Path):
        out = tmp_path / "survey.json"
        w = SurveyWriter(out)
        rec = _sample_record()
        w.append(rec)
        loaded = read_survey(out)
        assert len(loaded) == 1
        assert loaded[0].freq_hz == rec.freq_hz
        assert loaded[0].wacn == rec.wacn
        assert loaded[0].neighbors == rec.neighbors


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


class TestReport:
    def test_empty_survey(self):
        text = render([])
        assert "no control channels detected" in text.lower()

    def test_renders_summary_line(self):
        text = render([_sample_record()])
        assert "1 control channels detected" in text
        assert "1 complete, 0 partial" in text

    def test_no_cc_candidates_listed_separately(self):
        from p25_survey.survey import SurveyRecord
        records = [
            _sample_record(),
            SurveyRecord(freq_hz=851_500_000, complete=False),  # no-cc
            SurveyRecord(freq_hz=851_600_000, complete=False),  # no-cc
        ]
        text = render(records)
        assert "1 control channels detected" in text
        assert "3 candidates scanned" in text
        assert "2 non-CC candidates" in text
        assert "Non-CC candidates" in text
        assert "851.50000 MHz" in text
        assert "851.60000 MHz" in text

    def test_includes_key_fields(self):
        text = render([_sample_record()])
        assert "851.00625 MHz" in text
        assert "BEE00" in text     # WACN
        assert "1A4" in text        # SYSID
        assert "293" in text        # NAC
        assert "RFSS" in text
        assert "Neighbor" in text

    def test_lists_neighbors(self):
        text = render([_sample_record()])
        # Both neighbor frequencies show up at 10 Hz precision
        assert "851.10625 MHz" in text
        assert "851.20625 MHz" in text

    def test_partial_record_marked(self):
        text = render([_sample_record(complete=False)])
        assert "partial" in text

    def test_band_plan_shows_tdma(self):
        text = render([_sample_record()])
        assert "TDMA" in text
        assert "FDMA" in text

    def test_render_file_round_trip(self, tmp_path: Path):
        json_path = tmp_path / "s.json"
        txt_path = tmp_path / "s.txt"
        w = SurveyWriter(json_path)
        w.append(_sample_record(freq_hz=851_006_250))
        w.append(_sample_record(freq_hz=852_018_750, complete=False))
        render_file(json_path, txt_path)
        text = txt_path.read_text()
        assert "851.00625" in text
        assert "852.01875" in text
        assert "1 complete, 1 partial" in text
        assert "2 control channels detected" in text
