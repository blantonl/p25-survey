#!/usr/bin/env python3
"""Compare the Rust decoder with the op25 baseline on the corpus.

For each recording: every candidate op25 got P25 broadcasts from (either
chain), decoded by `p25-survey decode --json`. Side by side: site broadcasts
decoded (op25's TSBK counts are per TSDU, not per TSBK, so broadcasts are what
compare), the share that passed CRC, and whether the site came out the same as
op25's better chain.

  python3 corpus/compare.py [names...]   (from the repo root, after cargo build --release)
"""
import json
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(ROOT, "corpus", "raw")
BASE = os.path.join(ROOT, "corpus", "baseline")
BIN = os.path.join(ROOT, "target", "release", "p25-survey")


def op25(name):
    """freq -> {chain: record} for candidates with broadcasts."""
    out = {}
    for chain in ("cqpsk", "fsk4"):
        path = os.path.join(BASE, f"{name}.{chain}.records.ndjson")
        if not os.path.exists(path):
            continue
        for line in open(path):
            d = json.loads(line)
            if d["broadcasts"]:
                out.setdefault(d["freq_hz"], {})[chain] = d
    return out


def site(d):
    return (d.get("nac"), d.get("wacn"), d.get("sysid"), d.get("rfss_id"), d.get("site_id"))


def main():
    names = sys.argv[1:] or sorted(f[:-11] for f in os.listdir(RAW) if f.endswith(".sigmf-meta") and not f.endswith("-2s.sigmf-meta"))
    tot_ours = tot_best = 0
    for name in names:
        ref = op25(name)
        if not ref:
            print(f"{name}: op25 found no P25 broadcasts")
            continue
        freqs = sorted(ref)
        r = subprocess.run([BIN, "decode", os.path.join(RAW, name), "--freq", ",".join(f"{f / 1e6:.5f}" for f in freqs), "--json"],
                           capture_output=True, text=True, check=True)
        ours = {d["freq_hz"]: d for d in map(json.loads, r.stdout.splitlines())}
        print(f"\n{name}")
        print(f"  {'MHz':>10}   {'ours':>5} {'%':>5}   {'cqpsk':>5} {'%':>5}   {'fsk4':>5} {'%':>5}   site (vs op25's better)")
        for f in freqs:
            o = ours[f]
            chains = ref[f]
            n = {c: chains[c]["broadcasts"] if c in chains else 0 for c in ("cqpsk", "fsk4")}
            pct = {c: (chains[c].get("signal") or {}).get("decode_rate_pct") or 0 if c in chains else 0 for c in ("cqpsk", "fsk4")}
            best = max(n, key=n.get)
            att = max(o["c4fm"]["tsbk_ok"] + o["c4fm"]["tsbk_bad"], o["cqpsk"]["tsbk_ok"] + o["cqpsk"]["tsbk_bad"])
            same = site(o) == site(chains[best]) and o["idens"] == len(chains[best]["iden_up"])
            tot_ours += o["broadcasts"]
            tot_best += n[best]
            print(f"  {f / 1e6:>10.5f}   {o['broadcasts']:>5} {100 * o['tsbks'] / max(att, 1):>5.1f}   {n['cqpsk']:>5} {pct['cqpsk']:>5.1f}   {n['fsk4']:>5} {pct['fsk4']:>5.1f}   "
                  f"{'same' if same else 'differs'} {'/'.join(str(x) for x in site(o))}")
    print(f"\nsite broadcasts: ours {tot_ours}, op25's better chain {tot_best} ({100 * tot_ours / max(tot_best, 1):.1f} %)")


if __name__ == "__main__":
    main()
