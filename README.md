# p25-survey (Rust)

A rewrite of [p25-survey](https://github.com/blantonl/p25-survey) as a single
program: no GNU Radio, no op25, no Python. Scan a band with an SDR, find the
P25 control channels, and log each site's identity, band plan, neighbours and
secondary control channels, cross-referenced against RadioReference.

**Status: early.** It surveys a band with an Airspy, or recordings
(`p25-survey scan`): every control channel op25 finds on the
[corpus](corpus/README.md), and more, with their identity, band plan,
neighbours and secondary control channels; copies (images,
intermodulation) marked; gain advice per band (`--auto-gain`); records the
Python version's reports read. Not yet: RadioReference and the reports,
RTL-SDR and HackRF.

## Run

```bash
p25-survey radios
p25-survey scan --radio airspy --start 851 --stop 869 --gain 14 \
    --hide-no-cc --output survey-800.json
```

An Airspy needs libairspy (`apt install libairspy0`, `brew install airspy`);
nothing else does.

## Layout

```
crates/survey-core    the engine, with no I/O: given samples, returns what it
                      decoded and asks to retune. Runs the same on a radio and
                      on a recording.
crates/survey-radio   sample sources: SigMF recordings; RTL-SDR, HackRF and
                      Airspy next
crates/p25-survey     the program
corpus/               the IQ recordings it is tested against (kept out of git)
                      and the scripts that make them and their op25 baseline
```

## Build

```bash
cargo build --release
cargo test
./target/release/p25-survey info corpus/raw/800-a-853.4-2s
./target/release/p25-survey decode corpus/raw/800-a-853.4 --freq 855.0625,851.0875
./target/release/p25-survey scan corpus/raw/800-{a-853.4,b-857.6,c-862.6,d-866.6} \
    --start 851 --stop 869 --hide-no-cc --output /tmp/survey-800.json
python3 corpus/compare.py          # decoding against the op25 baseline
```

See [DECISIONS.md](DECISIONS.md) for why it is built the way it is.

## License

GPL-3.0-or-later.
