#!/usr/bin/env python3
"""Reference results for the corpus from the current Python p25-survey (op25).

For each capture:
  * Phase 1: p25_survey.energy_scan.find_peaks on 1<<17-sample snapshots
    (what a live scan would see), at several points in the capture.
  * Phase 2: every candidate (Phase 1 union the known CCs given with --cc),
    decoded over the WHOLE capture with op25's p25_demod_cb, once with the
    CQPSK chain (what p25-survey uses) and once with FSK4 (C4FM). Every
    frame_assembler message is logged; the dwell state is built with
    p25_survey.decoder._process_msg and turned into a SurveyRecord.

Outputs under --out:
  <capture>.phase1.json
  <capture>.<demod>.records.ndjson   one SurveyRecord (+ fec stats) per candidate
  <capture>.<demod>.msgs.ndjson      every message, in order, per candidate

Run with the system python3 on the capture host (GNU Radio + gr-op25_repeater), against
a clean copy of the p25-survey release in ~/p25-corpus/p25-survey-v0.3.5
(`git archive --prefix=p25-survey-v0.3.5/ v0.3.5 | ssh <capture host> "cd ~/p25-corpus && tar xf -"`):
  python3 baseline.py ~/p25-corpus/raw/800-a-853.4 --cc 851.0875,855.0625 ...
"""
import argparse
import ctypes
import json
import os
import sys
import threading
import time

# The p25-survey release the baseline is of: a clean `git archive` of a tag,
# not a working tree (P25_SURVEY_SRC to override).
P25_SURVEY_SRC = os.environ.get("P25_SURVEY_SRC", os.path.expanduser("~/p25-corpus/p25-survey-v0.3.5"))
sys.path.insert(0, P25_SURVEY_SRC)
sys.path.insert(0, os.path.join(P25_SURVEY_SRC, "p25_survey/_vendored/op25"))

import numpy as np  # noqa: E402
from gnuradio import blocks, gr  # noqa: E402
from gnuradio import op25_repeater  # noqa: E402

import p25_demodulator  # noqa: E402  (vendored)
from p25_survey import energy_scan  # noqa: E402
from p25_survey.bands import find_band  # noqa: E402
from p25_survey.decoder import _DwellState, _process_msg, _state_to_record  # noqa: E402

USABLE = 0.9  # p25_demod_cb refuses offsets beyond rate*usable/2 - if1/2
# op25 filter per chain: CQPSK as p25-survey runs it; FSK4 needs "rc" (with "rrc" it decodes nothing).
FILTER = {"cqpsk": "rrc", "fsk4": "rc"}


def load(base):
    meta = json.load(open(base + ".sigmf-meta"))
    g = meta["global"]
    assert g["core:datatype"] == "ci16_le", g["core:datatype"]
    return meta, g["core:sample_rate"], meta["captures"][0]["core:frequency"]


def phase1(base, rate, center, threshold_db):
    raw = np.memmap(base + ".sigmf-data", dtype=np.int16, mode="r")
    n_total = len(raw) // 2
    step = (find_band(center).default_step_hz if find_band(center) else 12_500)
    snaps = []
    for t in (0.5, 5.0, 15.0, 30.0):
        start = int(t * rate)
        if start + (1 << 17) > n_total:
            continue
        x = np.asarray(raw[2 * start: 2 * (start + (1 << 17))], dtype=np.float32) / 32768.0
        iq = (x[0::2] + 1j * x[1::2]).astype(np.complex64)
        c = energy_scan.find_peaks(iq, rate, center, threshold_db=threshold_db, step_hz=step)
        snaps.append({"t_s": t, "candidates": [{"freq_hz": k.freq_hz, "power_db": round(k.power_db, 2)} for k in c]})
    union = sorted({k["freq_hz"] for s in snaps for k in s["candidates"]})
    return {"threshold_db": threshold_db, "step_hz": step, "n_samples": 1 << 17, "snapshots": snaps, "union_hz": union}


def drain(q, out, stop):
    while True:
        if not q.empty_p():
            out.append(q.delete_head())
        elif stop.is_set():
            if q.empty_p():
                return
        else:
            time.sleep(0.01)


def phase2(base, rate, center, cands, demod_type):
    if not cands:
        return [], 0.0
    tb = gr.top_block()
    src = blocks.file_source(gr.sizeof_short, base + ".sigmf-data", False)
    i2c = blocks.interleaved_short_to_complex(False, False, 32768.0)
    tb.connect(src, i2c)
    chans = []
    for k, f in enumerate(cands):
        q = gr.msg_queue(0)
        demod = p25_demodulator.p25_demod_cb(
            msgq_id=k, debug=0, input_rate=rate, demod_type=demod_type,
            filter_type=FILTER[demod_type], usable_bw=USABLE, excess_bw=0.2,
            relative_freq=center - f, offset=0, if_rate=24_000, symbol_rate=4_800)
        fa = op25_repeater.frame_assembler("", 0, k, q)
        tb.connect(i2c, demod, fa)
        chans.append((f, q, fa, []))
    stop = threading.Event()
    threads = [threading.Thread(target=drain, args=(c[1], c[3], stop), daemon=True) for c in chans]
    for t in threads:
        t.start()
    t0 = time.monotonic()
    tb.start()
    tb.wait()
    stop.set()
    for t in threads:
        t.join()
    wall = time.monotonic() - t0
    results = []
    for f, q, fa, msgs in chans:
        st = _DwellState()
        log = []
        for m in msgs:
            payload = m.to_string()
            log.append({"duid": ctypes.c_int16(m.type() & 0xFFFF).value, "hex": payload.hex()})
            _process_msg(m, st)
        fec = json.loads(fa.control('{"cmd":"fec_stats"}'))["data"]
        st.tsbk_attempted = int(fec["control"]["tsbk_attempted"])
        st.tsbk_passed = int(fec["control"]["tsbk_crc_passed"])
        complete = st.has_core_id() and len(st.freq_table) > 0
        rec = _state_to_record(st, freq_hz=f, dwell_ms=0, sdr_driver="file", sdr_gain_db=None, sdr_ppm=0.0, complete=complete)
        d = rec.to_json_dict()
        d.pop("ts", None)
        d["demod"] = demod_type
        d["p25_survey_src"] = os.path.basename(P25_SURVEY_SRC)
        d["frames"] = st.frame_count
        d["broadcasts"] = st.broadcast_count
        d["fec_stats"] = fec
        results.append((d, log))
    return results, wall


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", help="capture path without .sigmf-data/.sigmf-meta")
    ap.add_argument("--cc", default="", help="known control channels, MHz, comma-separated")
    ap.add_argument("--threshold", type=float, default=8.0)
    ap.add_argument("--demods", default="cqpsk,fsk4")
    ap.add_argument("--out", default=os.path.expanduser("~/p25-corpus/baseline"))
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    name = os.path.basename(a.base)
    meta, rate, center = load(a.base)

    p1 = phase1(a.base, rate, center, a.threshold)
    json.dump(p1, open(os.path.join(a.out, name + ".phase1.json"), "w"), indent=1)
    known = [round(float(x) * 1e6) for x in a.cc.split(",") if x.strip()]
    lim = rate * USABLE / 2 - 25_000
    cands = sorted({f for f in p1["union_hz"] + known if abs(f - center) <= lim})
    print(f"[{name}] phase1: {len(p1['union_hz'])} candidates; decoding {len(cands)}", flush=True)

    for demod in a.demods.split(","):
        results, wall = phase2(a.base, rate, center, cands, demod)
        with open(os.path.join(a.out, f"{name}.{demod}.records.ndjson"), "w") as fr, \
             open(os.path.join(a.out, f"{name}.{demod}.msgs.ndjson"), "w") as fm:
            for d, log in results:
                fr.write(json.dumps(d) + "\n")
                for m in log:
                    fm.write(json.dumps({"freq_hz": d["freq_hz"], **m}) + "\n")
        hits = [d for d, _ in results if d["broadcasts"]]
        print(f"[{name}] {demod}: {wall:.0f}s wall, {len(hits)}/{len(results)} with P25 broadcasts", flush=True)
        for d in hits:
            s = d.get("signal") or {}
            print(f"    {d['freq_hz'] / 1e6:.5f} nac={d.get('nac')} wacn={d.get('wacn')} sys={d.get('sysid')} "
                  f"{d.get('rfss_id')}/{d.get('site_id')} complete={d['complete']} bcast={d['broadcasts']} "
                  f"dec={s.get('decode_rate_pct')} nb={len(d.get('neighbors') or [])}", flush=True)


if __name__ == "__main__":
    main()
