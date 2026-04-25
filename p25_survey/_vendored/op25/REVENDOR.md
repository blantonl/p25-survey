# Re-vendoring op25 Python files

These four files come from `boatbod/op25` (the fork at
`~/dev/bcfy-clients/op25/`). They're imported as
`p25_survey._vendored.op25.<module>`. The C++ extension `op25_repeater` is
**not** vendored — it must come from the host's installed gr-op25_repeater.

## What's vendored

| File | Upstream path |
|---|---|
| `p25_demodulator.py` | `op25/gr-op25_repeater/apps/p25_demodulator.py` |
| `p25_decoder.py`     | `op25/gr-op25_repeater/apps/p25_decoder.py` |
| `helper_funcs.py`    | `op25/gr-op25_repeater/apps/helper_funcs.py` |
| `log_ts.py`          | `op25/gr-op25_repeater/apps/log_ts.py` |
| `rms_agc.py`         | `op25/gr-op25_repeater/apps/rms_agc.py` (transitive: imported by p25_demodulator) |
| `op25_c4fm_mod.py`   | `op25/gr-op25_repeater/apps/tx/op25_c4fm_mod.py` (transitive: c4fm filter taps) |

Each file has a header comment with the upstream commit SHA.

## How to refresh

Run from the repo root:

```bash
SHA=$(cd ../op25 && git rev-parse HEAD)
SRC=../op25/op25/gr-op25_repeater/apps
DST=p25_survey/_vendored/op25
copy() {
  local fname="$1" upath="$2" srcpath="$3"
  HEADER="# Vendored from boatbod/op25 (commit ${SHA})
# Upstream: op25/gr-op25_repeater/${upath}
# DO NOT EDIT directly — see _vendored/REVENDOR.md to refresh.
"
  { echo "$HEADER"; cat "$srcpath"; } > "$DST/$fname"
}
copy p25_demodulator.py apps/p25_demodulator.py "$SRC/p25_demodulator.py"
copy p25_decoder.py     apps/p25_decoder.py     "$SRC/p25_decoder.py"
copy helper_funcs.py    apps/helper_funcs.py    "$SRC/helper_funcs.py"
copy log_ts.py          apps/log_ts.py          "$SRC/log_ts.py"
copy rms_agc.py         apps/rms_agc.py         "$SRC/rms_agc.py"
copy op25_c4fm_mod.py   apps/tx/op25_c4fm_mod.py "$SRC/tx/op25_c4fm_mod.py"
```

Then run `make test` and visually scan the diff for breaking changes
(removed function signatures, renamed kwargs in `p25_demod_*` constructors,
etc.). Update `decoder.py` if the demod/decoder API moved.

## When to refresh

- Pulling in op25 upstream changes that touch the demodulator chain or the
  P25 frame assembler block.
- Bug fixes in the FSK4/CQPSK demod.
- New symbol_rate / filter type support that we want to expose.
