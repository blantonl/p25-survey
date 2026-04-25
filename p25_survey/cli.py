"""CLI entry point for p25-survey.

Argparse-based; orchestrator hooks are stubs in this skeleton — they print
the resolved configuration and exit. Phase 1 (energy scan) and Phase 2
(decode) wire in once their modules land.
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone

from p25_survey import __version__
from p25_survey.bands import default_step_hz, describe_band


SDR_DRIVERS = ("rtlsdr", "airspy", "hackrf")


@dataclass(frozen=True)
class SurveyConfig:
    start_hz: int
    stop_hz: int
    step_hz: int
    sdr: str | None
    device_args: str | None
    gain_db: float | None
    ppm: float
    threshold_db: float
    max_dwell_s: float
    confirm_timeout_s: float
    output_path: str
    resume: bool
    thorough: bool
    phase1_only: bool
    verbose: bool


def _mhz(arg: str) -> int:
    """Parse a frequency in MHz to integer Hz. Accepts '851', '851.0125'."""
    try:
        mhz = float(arg)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {arg!r}") from exc
    if mhz <= 0:
        raise argparse.ArgumentTypeError(f"frequency must be positive: {arg!r}")
    return int(round(mhz * 1_000_000))


def _khz(arg: str) -> int:
    """Parse a tuning step in kHz to integer Hz. Accepts '12.5', '6.25'."""
    try:
        khz = float(arg)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {arg!r}") from exc
    if khz <= 0:
        raise argparse.ArgumentTypeError(f"step must be positive: {arg!r}")
    return int(round(khz * 1_000))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="p25-survey",
        description="Scan a frequency range for P25 control channels and log system metadata.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    p.add_argument("--list-gains", action="store_true",
                   help="Probe the SDR and print available gain stages, then exit. "
                        "Use with --sdr (and optionally --device-args).")

    sweep = p.add_argument_group("Frequency sweep")
    sweep.add_argument("--start", type=_mhz, required=False, metavar="MHz",
                       help="Start frequency (MHz, e.g. 851.0)")
    sweep.add_argument("--stop", type=_mhz, required=False, metavar="MHz",
                       help="Stop frequency (MHz, e.g. 870.0)")
    sweep.add_argument("--step", type=_khz, default=None, metavar="kHz",
                       help="Tuning step (kHz). Defaults to band-appropriate value (see README).")

    sdr = p.add_argument_group("SDR")
    sdr.add_argument("--sdr", choices=SDR_DRIVERS, default=None,
                     help="SDR driver. Autoprobed if omitted.")
    sdr.add_argument("--device-args", default=None, metavar="ARGS",
                     help="Raw gr-osmosdr device args (e.g. 'rtl=0' or 'airspy,bias=1').")
    sdr.add_argument("--gain", type=float, default=None, metavar="dB",
                     help="RF gain (dB). Sets the driver's default stage — for Airspy this "
                          "is the linearity preset (0-21); for RTL-SDR the tuner gain. Use "
                          "--list-gains to see what's available, or --device-args for "
                          "per-stage control.")
    sdr.add_argument("--ppm", type=float, default=0.0, metavar="PPM",
                     help="Frequency correction in PPM.")

    scan = p.add_argument_group("Scan behavior")
    scan.add_argument("--threshold", type=float, default=8.0, metavar="dB",
                      help="Energy threshold above noise floor for candidate selection.")
    scan.add_argument("--confirm-timeout", type=float, default=2.0, metavar="SECONDS",
                      help="Bail on a candidate if no P25 sync within this window.")
    scan.add_argument("--max-dwell", type=float, default=12.0, metavar="SECONDS",
                      help="Hard cap on per-candidate dwell time.")
    scan.add_argument("--thorough", action="store_true",
                      help="Skip FFT energy scan; tune-and-decode every step (slow).")
    scan.add_argument("--phase1-only", action="store_true",
                      help="Run only Phase 1 (spectrum survey). Skip P25 decode.")

    out = p.add_argument_group("Output")
    out.add_argument("--output", default=None, metavar="PATH",
                     help="NDJSON survey file. Default: survey-YYYYMMDD-HHMMSS.json")
    out.add_argument("--resume", action="store_true",
                     help="Skip frequencies already characterized in --output.")
    out.add_argument("--verbose", "-v", action="store_true",
                     help="Verbose console logging.")

    return p


def resolve_config(args: argparse.Namespace) -> SurveyConfig:
    if args.start >= args.stop:
        raise SystemExit(f"--start ({args.start} Hz) must be less than --stop ({args.stop} Hz)")

    step_hz = args.step if args.step is not None else default_step_hz(args.start, args.stop)

    output_path = args.output
    if output_path is None:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_path = f"survey-{ts}.json"

    return SurveyConfig(
        start_hz=args.start,
        stop_hz=args.stop,
        step_hz=step_hz,
        sdr=args.sdr,
        device_args=args.device_args,
        gain_db=args.gain,
        ppm=args.ppm,
        threshold_db=args.threshold,
        max_dwell_s=args.max_dwell,
        confirm_timeout_s=args.confirm_timeout,
        output_path=output_path,
        resume=args.resume,
        thorough=args.thorough,
        phase1_only=args.phase1_only,
        verbose=args.verbose,
    )


def print_config_summary(cfg: SurveyConfig) -> None:
    span_mhz = (cfg.stop_hz - cfg.start_hz) / 1_000_000
    band_desc = describe_band(cfg.start_hz)
    n_steps = (cfg.stop_hz - cfg.start_hz) // cfg.step_hz
    print(f"P25 Survey — configuration")
    print(f"  range:     {cfg.start_hz / 1e6:.4f} – {cfg.stop_hz / 1e6:.4f} MHz  ({span_mhz:.3f} MHz)")
    print(f"  band:      {band_desc}")
    print(f"  step:      {cfg.step_hz / 1e3:g} kHz  ({n_steps} steps)")
    print(f"  sdr:       {cfg.sdr or 'autoprobe'}"
          f"{' [' + cfg.device_args + ']' if cfg.device_args else ''}")
    print(f"  gain:      {cfg.gain_db if cfg.gain_db is not None else 'driver default'} dB")
    print(f"  ppm:       {cfg.ppm}")
    print(f"  threshold: {cfg.threshold_db} dB above noise floor")
    print(f"  dwell:     confirm={cfg.confirm_timeout_s}s, max={cfg.max_dwell_s}s")
    print(f"  output:    {cfg.output_path}{' (resume)' if cfg.resume else ''}")
    print(f"  mode:      {'thorough (stepwise)' if cfg.thorough else 'FFT energy scan + targeted decode'}")


def _resolve_driver(cfg: SurveyConfig) -> str | None:
    from p25_survey.sdr import autoprobe_driver
    return cfg.sdr or autoprobe_driver()


def _run_scan(cfg: SurveyConfig) -> int:
    """End-to-end scan: Phase 1 candidate finder → Phase 2 P25 decode → output.

    With --phase1-only, prints candidate frequencies and stops.
    """
    from p25_survey.energy_scan import scan_range
    from p25_survey.sdr import SdrCapture, SdrConfig

    driver = _resolve_driver(cfg)
    if driver is None:
        print("error: no SDR driver specified and gr-osmosdr is not importable", flush=True)
        return 2

    sdr_cfg = SdrConfig(
        driver=driver,
        device_args=cfg.device_args,
        gain_db=cfg.gain_db,
        ppm=cfg.ppm,
    )
    sdr = SdrCapture(sdr_cfg)
    sample_rate = sdr.sample_rate_hz
    n_samples = 1 << 17

    if cfg.verbose:
        print(f"  driver:    {driver} @ {sample_rate / 1e6:g} MSPS, "
              f"capture {n_samples} samples per chunk")

    # ----- Phase 1 -----
    print()
    print("Phase 1 — energy scan")

    def provider(center_hz: int):
        if cfg.verbose:
            print(f"  tune {center_hz / 1e6:.3f} MHz", flush=True)
        return sdr.capture(center_hz, n_samples)

    candidates = list(scan_range(
        start_hz=cfg.start_hz,
        stop_hz=cfg.stop_hz,
        sample_rate_hz=sample_rate,
        iq_provider=provider,
        threshold_db=cfg.threshold_db,
        step_hz=cfg.step_hz,
    ))

    if not candidates:
        print(f"  No spectral peaks above threshold ({cfg.threshold_db} dB).")
        return 0
    print(f"  found {len(candidates)} candidate(s)")
    if cfg.verbose or cfg.phase1_only:
        for c in candidates:
            print(f"    {c.freq_hz / 1e6:>10.5f} MHz  ({c.power_db:>5.1f} dB above floor)")

    if cfg.phase1_only:
        return 0

    # ----- Phase 2 -----
    from p25_survey.console import make_display
    from p25_survey.decoder import decode_candidate
    from p25_survey.report import render_file
    from p25_survey.survey import SurveyWriter

    writer = SurveyWriter(cfg.output_path, resume=cfg.resume)
    skipped = sum(1 for c in candidates if writer.already_characterized(c.freq_hz))
    to_do = [c for c in candidates if not writer.already_characterized(c.freq_hz)]

    confirmed = 0
    with make_display(total=len(to_do), skipped=skipped) as display:
        for cand in to_do:
            record = decode_candidate(
                freq_hz=cand.freq_hz,
                sdr_driver=driver,
                device_args=sdr_cfg.resolved_device_args(),
                sample_rate_hz=sample_rate,
                gain_db=cfg.gain_db,
                ppm=cfg.ppm,
                confirm_timeout_s=cfg.confirm_timeout_s,
                max_dwell_s=cfg.max_dwell_s,
                debug=10 if cfg.verbose else 0,
            )
            writer.append(record)
            if record.complete:
                confirmed += 1
                status = "complete"
            elif record.wacn is None and record.rfss_id is None:
                status = "no-cc"
            else:
                status = "partial"
            display.add(record, status)

    # Render text report alongside the JSON survey.
    txt_path = os.path.splitext(cfg.output_path)[0] + ".txt"
    render_file(cfg.output_path, txt_path)

    print()
    print(f"  {confirmed} / {len(to_do)} candidates confirmed as P25 control channels.")
    print(f"  Survey JSON: {cfg.output_path}")
    print(f"  Survey TXT:  {txt_path}")
    return 0


def _run_list_gains(args: argparse.Namespace) -> int:
    """Probe the SDR and print its gain stage table."""
    from p25_survey.sdr import autoprobe_driver, probe_gains

    driver = args.sdr or autoprobe_driver()
    if driver is None:
        print("error: --list-gains needs an SDR driver — pass --sdr "
              "rtlsdr|airspy|hackrf, or install gr-osmosdr.", flush=True)
        return 2

    try:
        info = probe_gains(driver, device_args=args.device_args)
    except Exception as exc:  # noqa: BLE001 — surface whatever osmosdr threw
        print(f"error: could not open SDR ({driver}, args={args.device_args!r}): {exc}",
              flush=True)
        return 2

    print(f"SDR driver: {info.driver}")
    print(f"Device args: {info.device_args}")
    print()
    r = info.default_range
    print(f"Default gain (used by --gain N):  {r.start:g} – {r.stop:g} dB,"
          f" step {r.step:g}")
    if not info.stages:
        print("  (no named per-stage gains exposed by this driver)")
    else:
        print()
        print("Per-stage gains (advanced, set via --device-args):")
        for s in info.stages:
            print(f"  {s.name:<8} {s.start:>5g} – {s.stop:<5g} dB  step {s.step:g}"
                  f"   e.g. --device-args \"{info.device_args},{s.name}={int(s.stop) // 2}\"")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.list_gains:
        return _run_list_gains(args)

    if args.start is None or args.stop is None:
        parser.error("--start and --stop are required (unless using --list-gains)")

    cfg = resolve_config(args)
    print_config_summary(cfg)

    if cfg.thorough:
        print()
        print("error: --thorough mode not yet implemented")
        return 2

    return _run_scan(cfg)


if __name__ == "__main__":
    sys.exit(main())
