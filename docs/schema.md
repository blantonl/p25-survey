# Survey record schema

A survey file is **NDJSON**: one JSON object per line, one per candidate a
scan looked at (control channels, copies, other P25, and carriers that
weren't P25). Each tuning's records are written, and synced to disk, as the
tuning ends.

`--output` truncates an existing file at the start; `--resume` keeps its
records and surveys only what they don't cover.

The schema is the Python version's (0.4), so its tools read these records and
`p25-survey report` reads its records. Fields that version doesn't have are at
the top level of each record (it builds `neighbors`, `iden_up` and `signal`
from exactly their keys). A field whose broadcast wasn't heard is `null` or
empty, and readers take missing keys as such.

## Record

| Field | Type | |
|---|---|---|
| `freq_hz` | int | The channel. Where it was found, or where it announces it is, if that is within 12.5 kHz (`heard_hz` keeps the other). |
| `ts` | string | ISO 8601 UTC, milliseconds: when the record was written. |
| `complete` | bool | A control channel of its own whose identity, band plan and own channel were heard, every channel number it named resolved, and nothing new came for 3 s (or at the dwell's end, everything resolved). `false` for copies. |
| `wacn` | string \| null | 5 hex digits (`"BEE00"`). |
| `sysid` | string \| null | 3 hex digits (`"44A"`). |
| `nac` | string \| null | 3 hex digits: the NAC most of its frames had. |
| `rfss_id` | int \| null | |
| `site_id` | int \| null | |
| `site_network_active` | bool \| null | RFSS_STS_BCST's A bit: `false` is failsoft (site trunking). |
| `site_lra` | int \| null | Location registration area. |
| `services_available` | array of string | SYS_SRV_BCST: the services the site offers now, by name. |
| `services_supported` | array of string | SYS_SRV_BCST: the services it is equipped for. |
| `utc_offset_min` | int \| null | TIME_DATE_ANN: the local time's offset from UTC, minutes. |
| `encryption_algid` | int \| null | P_PARM_BCST: the control channel is encrypted, with this algorithm. |
| `neighbors` | array | Neighbouring sites (below). |
| `secondary_cc` | array of int | The site's secondary control channels (SCCB, SCCB_EXP), Hz. |
| `iden_up` | array | The band plan (below). |
| `signal` | object | Below. |
| `dwell_ms` | int | How long it was listened to. |
| `sdr_driver` | string | `airspy`, `hydrasdr`, `rtlsdr`, `hackrf`, or `file` (a recording). |
| `sdr_gain_db` | float \| null | The gain used, as `--gain` takes it (Airspy: the linearity preset). |
| `sdr_ppm` | float | The correction applied (`--ppm`). |
| `notes` | array of string | Copies, channel numbers that didn't resolve. |
| `rr` | object \| null | With `--rr` (below). |

Fields this version adds:

| Field | Type | |
|---|---|---|
| `verdict` | string | `complete`, `partial` (a control channel the dwell ran out on), `p25-not-control` (voice, data, conventional), `not-p25`. |
| `level_db` | float | Its steady power over the local noise floor in the look, dB. |
| `heard_hz` | int | Where it was found. |
| `announced_hz` | int \| null | The frequency its NET/RFSS_STS_BCST names for it. |
| `copy_of_hz` | int \| null | Not a control channel of its own: a copy (image, intermodulation) of the one at this frequency. |
| `tsbks` | int | TSBKs that passed their CRC. |
| `carrier_offset_hz` | float | Where its carrier was against where it should be: the radio's frequency error there. |
| `receivers` | object | `c4fm` and `cqpsk`: `frames`, `tsbk_ok`, `tsbk_bad` each. |

## `neighbors[]`

| Field | Type | |
|---|---|---|
| `freq_hz` | int | Its control channel, through the band plan. |
| `rfss_id`, `site_id` | int | |
| `sysid` | string \| null | |
| `wacn` | string \| null | Only the TDMA extended form carries it. |
| `conventional` | bool \| null | ADJ_STS_BCST's C bit: a conventional channel. `null` when not decoded (MBT, TDMA). |
| `site_failure` | bool \| null | F: the neighbour is in a failure condition. |
| `valid` | bool \| null | V: current (`false`: last known). |
| `network_active` | bool \| null | A: an active network connection (`false`: failsoft). |

## `iden_up[]`

| Field | Type | |
|---|---|---|
| `iden` | int | The table (0-15). |
| `base_freq_hz` | int | Channel 0's downlink. |
| `step_hz` | int | Channel spacing. |
| `offset_hz` | int | Uplink minus downlink. |
| `is_tdma` | bool | Two or more slots a carrier (a 1-slot IDEN_UP_TDMA entry is FDMA). |
| `slots_per_carrier` | int | 1, 2 or 4. |
| `bandwidth_hz` | int \| null | IDEN_UP_VU's channel bandwidth (6250 or 12500). |

## `signal`

| Field | Type | |
|---|---|---|
| `rssi_dbfs_mean`, `rssi_dbfs_peak` | null | The Python version's; this version gives `level_db` instead. |
| `decode_rate_pct` | float \| null | TSBKs that passed their CRC, of those attempted, by the better receiver. |
| `ber_pct_mean` | float \| null | 100 − `decode_rate_pct` (the Python version's name for it). |

## `rr`

As the Python version's enrichment record, field for field.

| Field | Type | |
|---|---|---|
| `system_match` | bool | RR has the WACN and System ID. |
| `site_match` | bool | RR has the RFSS and site for that system (several subsystems sharing numbers: the NAC chooses). |
| `ambiguous_site` | bool | It can't be told which (see `notes`). |
| `rr_system_name`, `rr_sid` | string, int | |
| `rr_site_description`, `rr_site_location`, `rr_site_county`, `rr_site_nac` | string \| null | |
| `rr_site_lat`, `rr_site_lon` | float \| null | |
| `rr_site_licenses` | array of string | FCC call signs. |
| `cc_freq_offset` | object \| null | `decoded_hz`, `expected_hz` (the nearest control channel RR lists for the site), `offset_hz`, `ppm`. |
| `cc_freq_in_db` | bool | The control channel is listed (within 1 kHz). |
| `observed_neighbors` | array | Each neighbour: `rfss_id`, `site_id`, `freq_hz`, `in_rr`, and RR's `description`, `location`, `county`. |
| `neighbors_decoded_not_in_rr` | array | Those not in RR's roster: new-site candidates. |
| `neighbor_cc_mismatches` | array | Neighbours whose announced control channel RR doesn't list for them (`kind` `missing_from_rr`) or lists but not as control (`not_marked_control`, with `rr_use_code`): `rfss_id`, `site_id`, `advertised_cc_hz`, `kind`, `rr_use_code`, `rr_site_description`. |
| `secondary_cc_mismatches` | array | The same for the site's own secondary control channels: `freq_hz`, `kind`, `rr_use_code`. |
| `notes` | array of string | New system, new site, lookups that failed, a WACN taken from the survey's other control channels. |

## Example (illustrative)

```json
{"freq_hz":855062500,"ts":"2026-11-15T19:57:27.268+00:00","complete":true,
 "wacn":"DEE00","sysid":"911","nac":"910","rfss_id":1,"site_id":64,
 "site_network_active":true,"site_lra":0,"services_available":[],"services_supported":[],
 "utc_offset_min":null,"encryption_algid":null,
 "neighbors":[{"freq_hz":150830000,"rfss_id":1,"site_id":40,"sysid":"911","wacn":null,
   "conventional":null,"site_failure":null,"valid":null,"network_active":null}],
 "secondary_cc":[860012500],
 "iden_up":[{"iden":0,"base_freq_hz":851006250,"step_hz":6250,"offset_hz":-45000000,
   "is_tdma":false,"slots_per_carrier":1,"bandwidth_hz":null}],
 "signal":{"rssi_dbfs_mean":null,"rssi_dbfs_peak":null,"ber_pct_mean":0.0,"decode_rate_pct":100.0},
 "dwell_ms":3802,"sdr_driver":"airspy","sdr_gain_db":14.0,"sdr_ppm":0.0,"notes":[],
 "verdict":"complete","level_db":47.5,"heard_hz":855062500,"announced_hz":855062500,
 "copy_of_hz":null,"tsbks":540,"carrier_offset_hz":766.0,
 "receivers":{"c4fm":{"frames":172,"tsbk_ok":540,"tsbk_bad":0},"cqpsk":{"frames":172,"tsbk_ok":538,"tsbk_bad":2}}}
```

(`iden_up` cut to one entry; with `--rr` an `rr` object follows.)
