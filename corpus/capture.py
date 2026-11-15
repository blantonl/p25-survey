#!/usr/bin/env python3
"""Record the p25-survey IQ corpus with airspy_rx.

Each capture becomes <name>.sigmf-data (ci16_le) + <name>.sigmf-meta.
Run: python3 capture.py [--only NAME ...] [--out DIR]
"""
import argparse
import datetime as dt
import hashlib
import json
import os
import subprocess
import sys

RATE = 6_000_000

# name, center MHz, seconds, airspy linearity gain, what it's for
PLAN = [
    ("800-a-853.4", 853.40, 45, 14, "multi-CC hop: DFW simulcast 851.0875, NTIRN 853.7875/853.9125, Metrocrest 854.7875, CTTRS 855.0625 (clean)"),
    ("800-b-857.6", 857.60, 45, 14, "multi-CC hop: 855.4125, 857.8375 (47%), NTIRN 858.1375/858.4375, 859.2125 (27%), 859.7125, 859.7375"),
    ("800-c-862.6", 862.60, 20, 14, "sweep hop 860.1-865.1 (not in the May survey)"),
    ("800-d-866.6", 866.60, 20, 14, "sweep hop 864.1-869.1 (not in the May survey)"),
    ("700-a-772.3", 772.30, 45, 14, "700 MHz PS downlink 769.8-774.8, incl. DFW East 774.48125"),
    ("800-a-853.4-lin8", 853.40, 10, 8, "gain study: hop A at low gain"),
    ("800-a-853.4-lin20", 853.40, 10, 20, "gain study: hop A at high gain"),
]


def sha512(path):
    h = hashlib.sha512()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 22), b""):
            h.update(chunk)
    return h.hexdigest()


def capture(out, name, center_mhz, seconds, gain, desc):
    data = os.path.join(out, name + ".sigmf-data")
    meta = os.path.join(out, name + ".sigmf-meta")
    n = RATE * seconds
    start = dt.datetime.now(dt.timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    cmd = ["airspy_rx", "-r", data, "-f", f"{center_mhz:.6f}", "-a", str(RATE), "-t", "2",
           "-g", str(gain), "-n", str(n)]
    print(f"[{name}] {' '.join(cmd)}", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True)
    got = os.path.getsize(data) // 4 if os.path.exists(data) else 0
    log = (r.stdout + r.stderr).strip().splitlines()
    if r.returncode != 0 or got != n:
        print(f"[{name}] FAILED rc={r.returncode} samples={got}/{n}\n" + "\n".join(log[-10:]), flush=True)
        return False
    m = {
        "global": {
            "core:datatype": "ci16_le",
            "core:sample_rate": RATE,
            "core:version": "1.0.0",
            "core:hw": "Airspy Mini (airspy_rx, INT16_IQ)",
            "core:recorder": "p25-survey corpus/capture.py",
            "core:description": desc,
            "core:sha512": sha512(data),
            "p25s:gain": {"mode": "linearity", "value": gain},
            "p25s:ppm": 0.0,
            "p25s:airspy_rx_log": log[-6:],
        },
        "captures": [{"core:sample_start": 0, "core:frequency": round(center_mhz * 1e6), "core:datetime": start}],
        "annotations": [],
    }
    with open(meta, "w") as f:
        json.dump(m, f, indent=2)
    print(f"[{name}] ok {got} samples, {os.path.getsize(data) / 1e9:.2f} GB", flush=True)
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=os.path.expanduser("~/p25-corpus/raw"))
    ap.add_argument("--only", nargs="*")
    a = ap.parse_args()
    os.makedirs(a.out, exist_ok=True)
    ok = True
    for row in PLAN:
        if a.only and row[0] not in a.only:
            continue
        ok &= capture(a.out, *row)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
