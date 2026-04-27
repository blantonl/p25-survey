# Airspy gain preset tables

`--gain N` for an Airspy maps to the gr-osmosdr `linearity` preset, which
in turn calls `airspy_set_linearity_gain(N)` in libairspy. That function
is a lookup into three pre-tuned tables (LNA, MIX, IF/VGA) — one entry
per preset value 0–21.

This page snapshots those tables so you can predict what `--gain N`
actually programs into the device, and so you can build an equivalent
`--device-args` string for per-stage control (see "Per-stage control"
below).

> **Heads-up — the preset is inversely ordered.**
> Preset **0** = all stages at their maximum (highest gain).
> Preset **21** = LNA off, mixer off, IF/VGA at minimum (lowest gain).
> Lower preset value → more gain. This trips up almost everyone the
> first time. The libairspy help text just says "0–21" without naming
> the direction.

Per-stage register ranges (from `airspy_rx.c`): LNA 0–14, MIX 0–15,
IF/VGA 0–15. Each row below is what the preset programs into those
three stages.

## Linearity preset (default for `--gain N`)

Tuned to favor IMD performance over raw sensitivity. Best general-purpose
preset for crowded bands like 700/800 MHz public-safety.

| Preset | LNA | MIX | IF/VGA |
|-------:|----:|----:|-------:|
|  0     | 14  | 12  | 13     |
|  1     | 14  | 12  | 12     |
|  2     | 14  | 11  | 11     |
|  3     | 13  |  9  | 11     |
|  4     | 12  |  8  | 11     |
|  5     | 10  |  7  | 11     |
|  6     |  9  |  6  | 11     |
|  7     |  9  |  6  | 10     |
|  8     |  8  |  5  | 10     |
|  9     |  9  |  0  | 10     |
| 10     |  8  |  0  | 10     |
| 11     |  6  |  1  | 10     |
| 12     |  5  |  0  | 10     |
| 13     |  3  |  0  | 10     |
| 14     |  1  |  2  | 10     |
| 15     |  0  |  2  | 10     |
| 16     |  0  |  1  |  9     |
| 17     |  0  |  1  |  8     |
| 18     |  0  |  1  |  7     |
| 19     |  0  |  1  |  6     |
| 20     |  0  |  0  |  5     |
| 21     |  0  |  0  |  4     |

## Sensitivity preset

Same 0–21 input range but tuned to maximize sensitivity at the cost of
linearity. Useful for weak rural signals where IMD isn't the limiting
factor. Select by passing `sensitivity=N` instead of `linearity=N`
in `--device-args` — e.g. `--device-args "airspy=0,sensitivity=12"`.

| Preset | LNA | MIX | IF/VGA |
|-------:|----:|----:|-------:|
|  0     | 14  | 12  | 13     |
|  1     | 14  | 12  | 12     |
|  2     | 14  | 12  | 11     |
|  3     | 14  | 12  | 10     |
|  4     | 14  | 11  |  9     |
|  5     | 14  | 10  |  8     |
|  6     | 14  | 10  |  7     |
|  7     | 14  |  9  |  6     |
|  8     | 14  |  9  |  5     |
|  9     | 13  |  8  |  5     |
| 10     | 12  |  7  |  5     |
| 11     | 12  |  4  |  5     |
| 12     |  9  |  4  |  5     |
| 13     |  9  |  4  |  4     |
| 14     |  8  |  3  |  4     |
| 15     |  7  |  2  |  4     |
| 16     |  6  |  2  |  4     |
| 17     |  5  |  1  |  4     |
| 18     |  3  |  0  |  4     |
| 19     |  2  |  0  |  4     |
| 20     |  1  |  0  |  4     |
| 21     |  0  |  0  |  4     |

## Per-stage control via `--device-args`

If neither preset gives you what you want, set the three stages directly.
gr-osmosdr's airspy backend accepts these keys:

```
--device-args "airspy=0,LNA=10,MIX=15,IF=12"
```

`--gain` is ignored when per-stage args are present. Use this when:

- You're chasing a specific BER curve and want finer granularity than 22
  preset rows.
- You're characterizing front-end overload — pin LNA low and sweep MIX
  to find where IMD starts mattering.
- A specific Airspy / antenna combination has a known sweet spot that
  doesn't map cleanly onto either preset's diagonal.

`--list-gains` will print the LNA / MIX / IF ranges your gr-osmosdr
build actually advertises for the connected device, which can differ
from the 0–14 / 0–15 / 0–15 from `airspy_rx.c` if you're on an unusual
build.

## Where these numbers come from

Tables snapshot from
[`airspy/airspyone_host` `libairspy/src/airspy.c`](https://github.com/airspy/airspyone_host/blob/master/libairspy/src/airspy.c)
— search for `airspy_linearity_*_gains` and `airspy_sensitivity_*_gains`.
Last upstream change to these arrays predates 2024-01; recent commits
to that file have been thread/USB fixes only. Re-check upstream if
you suspect a tuning change has shipped.

## Related: how `--auto-gain` uses these

The default gain sweep grid for Airspy is `[4, 8, 12, 16, 20]` (see
`p25_survey/gain_sweep.py`). That spans the linearity preset from "very
hot" (preset 4: LNA 12, MIX 8, IF 11) to "cold" (preset 20: LNA 0,
MIX 0, IF 5), which gives BER-vs-gain enough dynamic range to find
the actual decoder sweet spot for your antenna and your local RF
environment without burning dwell time on near-duplicate rows.
