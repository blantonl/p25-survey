# p25-survey (Rust)

A rewrite of [p25-survey](https://github.com/blantonl/p25-survey) as a single
program: no GNU Radio, no op25, no Python. Scan a band with an SDR, find the
P25 control channels, and log each site's identity, band plan, neighbours and
secondary control channels, cross-referenced against RadioReference.

**Status: early.** It decodes control channels from recordings
(`p25-survey decode`) with a C4FM and a CQPSK receiver side by side: 115 % of
what op25 decodes on the [corpus](corpus/README.md), and sites op25 misses.
Next: the shared channelizer, the scan, then radios. Until it can do a
survey, use the Python version.

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
python3 corpus/compare.py          # against the op25 baseline
```

See [DECISIONS.md](DECISIONS.md) for why it is built the way it is.

## License

GPL-3.0-or-later.
