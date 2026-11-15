# p25-survey (Rust)

A rewrite of [p25-survey](https://github.com/blantonl/p25-survey) as a single
program: no GNU Radio, no op25, no Python. Scan a band with an SDR, find the
P25 control channels, and log each site's identity, band plan, neighbours and
secondary control channels, cross-referenced against RadioReference.

**Status: early.** The broadcast parsing and the recording reader are in; the
DSP (channelizer, C4FM and CQPSK receivers, framing) is next. Until it can
match the Python version on the [corpus](corpus/README.md), use that one.

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
```

See [DECISIONS.md](DECISIONS.md) for why it is built the way it is.

## License

GPL-3.0-or-later.
