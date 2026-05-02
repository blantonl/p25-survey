# Airspy gain preset tables

`--gain N` for an Airspy maps to the gr-osmosdr `linearity` preset, which
in turn calls `airspy_set_linearity_gain(N)` in libairspy. That function
is a lookup into three pre-tuned tables (LNA, MIX, IF/VGA) — one entry
per preset value 0–21.

This page snapshots those tables so you can predict what `--gain N`
actually programs into the device, and so you can build an equivalent
`--device-args` string for per-stage control (see "Per-stage control"
below).

> **Note — the source arrays appear backwards, but the API is not.**
> - Preset **0** = LNA off, mixer off, IF/VGA at minimum (lowest gain).
> - Preset **21** = all stages at maximum (highest gain).
>> Higher preset value → more gain. The raw C arrays in `airspy.c` seem
> reversed because they are stored highest-first. The driver inverts the
> index before lookup using `GAIN_COUNT - 1 - N`. The tables on this
> page reflect the corrected mappings as experienced by the user.

Per-stage register ranges (from `airspy_rx.c`): LNA 0–14, MIX 0–15,
IF/VGA 0–15. Each row below is what the preset programs into those
three stages.

## Linearity preset (default for `--gain N`)

Tuned to favor IMD performance over raw sensitivity. Best general-purpose
preset for crowded bands like 700/800 MHz public-safety.

| Preset | LNA | MIX | IF/VGA |
|-------:|----:|----:|-------:|
|  0     |  0  |  0  |  4     |
|  1     |  0  |  0  |  5     |
|  2     |  0  |  1  |  6     |
|  3     |  0  |  1  |  7     |
|  4     |  0  |  1  |  8     |
|  5     |  0  |  1  |  9     |
|  6     |  0  |  2  | 10     |
|  7     |  1  |  2  | 10     |
|  8     |  3  |  0  | 10     |
|  9     |  5  |  0  | 10     |
| 10     |  6  |  1  | 10     |
| 11     |  8  |  0  | 10     |
| 12     |  9  |  0  | 10     |
| 13     |  8  |  5  | 10     |
| 14     |  9  |  6  | 10     |
| 15     |  9  |  6  | 11     |
| 16     | 10  |  7  | 11     |
| 17     | 12  |  8  | 11     |
| 18     | 13  |  9  | 11     |
| 19     | 14  | 11  | 11     |
| 20     | 14  | 12  | 12     |
| 21     | 14  | 12  | 13     |

## Sensitivity preset

Same 0–21 input range but tuned to maximize sensitivity at the cost of
linearity. Useful for weak rural signals where IMD isn't the limiting
factor. Select by passing `sensitivity=N` instead of `linearity=N`
in `--device-args` — e.g. `--device-args "airspy=0,sensitivity=12"`.

| Preset | LNA | MIX | IF/VGA |
|-------:|----:|----:|-------:|
|  0     |  0  |  0  |  4     |
|  1     |  1  |  0  |  4     |
|  2     |  2  |  0  |  4     |
|  3     |  3  |  0  |  4     |
|  4     |  5  |  1  |  4     |
|  5     |  6  |  2  |  4     |
|  6     |  7  |  2  |  4     |
|  7     |  8  |  3  |  4     |
|  8     |  9  |  4  |  4     |
|  9     |  9  |  4  |  5     |
| 10     | 12  |  4  |  5     |
| 11     | 12  |  7  |  5     |
| 12     | 13  |  8  |  5     |
| 13     | 14  |  9  |  5     |
| 14     | 14  |  9  |  6     |
| 15     | 14  | 10  |  7     |
| 16     | 14  | 10  |  8     |
| 17     | 14  | 11  |  9     |
| 18     | 14  | 12  | 10     |
| 19     | 14  | 12  | 11     |
| 20     | 14  | 12  | 12     |
| 21     | 14  | 12  | 13     |

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
cold" (preset 4: LNA 0, MIX 1, IF 8) to "hot" (preset 20: LNA 14, MIX 12, 
IF 12), which gives BER-vs-gain enough dynamic range to find
the actual decoder sweet spot for your antenna and your local RF
environment without burning dwell time on near-duplicate rows.
