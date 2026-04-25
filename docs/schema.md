# Survey JSON schema

The survey output is **NDJSON**: one JSON object per line, append-only,
fsynced after each write. A scan that crashes mid-run leaves a valid
file. Re-running with `--resume` skips frequencies already present.

## Record

| Field | Type | Notes |
|---|---|---|
| `freq_hz` | int | Detected control channel frequency in Hz. The record key for resume / dedup. |
| `ts` | string | ISO 8601 UTC timestamp (millisecond precision) of detection completion. |
| `complete` | bool | `true` if WACN, sysid, RFSS, site, and band plan were all collected before max_dwell. |
| `wacn` | string \| null | 5-hex-char WACN (e.g. `"BEE00"`). |
| `sysid` | string \| null | 3-hex-char System ID (e.g. `"1A4"`). |
| `nac` | string \| null | 3-hex-char NAC (e.g. `"293"`). |
| `rfss_id` | int \| null | RFSS ID from RFSS_STS_BCST. |
| `site_id` | int \| null | Site ID from RFSS_STS_BCST. |
| `neighbors` | array | Adjacent sites — see below. |
| `iden_up` | array | Channel-identifier (band plan) entries — see below. |
| `signal` | object | Quality metrics. |
| `dwell_ms` | int | Total ms spent decoding this candidate. |
| `sdr_driver` | string | "rtlsdr" / "airspy" / "hackrf" / etc. |
| `sdr_gain_db` | float \| null | RF gain used. |
| `sdr_ppm` | float | Frequency correction. |
| `notes` | array | Warnings (e.g. "neighbor at 0x1234 unresolvable: missing iden_up table 1"). |

## `neighbors[]`

| Field | Type | Notes |
|---|---|---|
| `freq_hz` | int | Resolved neighbor CC frequency. |
| `rfss_id` | int | |
| `site_id` | int | |
| `sysid` | string \| null | Only set when source TSBK was TDMA explicit (0xfa-family). |
| `wacn` | string \| null | Only set when source TSBK was TDMA extended explicit (0xfe). |

## `iden_up[]`

| Field | Type | Notes |
|---|---|---|
| `iden` | int | Channel-identifier table number (0–15). |
| `base_freq_hz` | int | Channel 0 downlink frequency. |
| `step_hz` | int | Channel-to-channel spacing. |
| `offset_hz` | int | Signed mobile-uplink offset. |
| `is_tdma` | bool | |
| `slots_per_carrier` | int | 1 (FDMA), 2 (Phase 2), or 4. |

## `signal`

| Field | Type | Notes |
|---|---|---|
| `rssi_dbfs_mean` | float \| null | Mean during dwell. |
| `rssi_dbfs_peak` | float \| null | Peak during dwell. |
| `ber_pct_mean` | float \| null | Bit error rate from frame_assembler stats. |
| `decode_rate_pct` | float \| null | Successfully-decoded TSBK fraction. |

## Example record

```json
{
  "complete": true,
  "dwell_ms": 4123,
  "freq_hz": 851006250,
  "iden_up": [
    {"iden": 0, "base_freq_hz": 851006250, "step_hz": 12500,
     "offset_hz": -45000000, "is_tdma": false, "slots_per_carrier": 1}
  ],
  "nac": "293",
  "neighbors": [
    {"freq_hz": 851106250, "rfss_id": 1, "site_id": 8,
     "sysid": "1A4", "wacn": "BEE00"}
  ],
  "notes": [],
  "rfss_id": 1,
  "sdr_driver": "rtlsdr",
  "sdr_gain_db": 40.0,
  "sdr_ppm": 0.0,
  "signal": {"ber_pct_mean": 0.4, "decode_rate_pct": 98.7,
             "rssi_dbfs_mean": -42.1, "rssi_dbfs_peak": -38.7},
  "site_id": 7,
  "sysid": "1A4",
  "ts": "2026-04-25T18:23:11.402+00:00",
  "wacn": "BEE00"
}
```
