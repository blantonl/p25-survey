"""CLI entry point for p25-survey.

Argparse-based; orchestrator hooks are stubs in this skeleton — they print
the resolved configuration and exit. Phase 1 (energy scan) and Phase 2
(decode) wire in once their modules land.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from dataclasses import dataclass
from datetime import date, datetime, timezone

from p25_survey import __version__
from p25_survey.bands import default_step_hz, describe_band


SDR_DRIVERS = ("rtlsdr", "airspy", "hackrf")


@dataclass(frozen=True)
class SurveyConfig:
    start_hz: int
    stop_hz: int
    step_hz_list: tuple[int, ...]   # one or more tuning steps; ascending
    sdr: str | None
    device_args: str | None
    sample_rate_hz: int | None  # None → auto-detect from device at scan start
    gain_db: float | None
    ppm: float
    threshold_db: float
    max_dwell_s: float
    confirm_timeout_s: float
    output_path: str
    resume: bool
    thorough: bool
    phase1_only: bool
    rr_enabled: bool
    hide_no_cc: bool
    auto_gain: bool
    gain_sweep_spec: str | None
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


def _khz_list(arg: str) -> tuple[int, ...]:
    """Parse one or more comma-separated steps in kHz. Accepts '12.5' or '6.25,7.5,12.5'.

    Returned tuple is ascending and de-duplicated. The motivation is VHF/UHF
    systems where different idens use different channel spacings — running
    one Phase 1 pass per step lets Phase 2 see CCs that would otherwise
    snap to the wrong grid.
    """
    if not arg.strip():
        raise argparse.ArgumentTypeError("step list cannot be empty")
    values: list[int] = []
    for tok in arg.split(","):
        tok = tok.strip()
        if not tok:
            continue
        try:
            khz = float(tok)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(f"not a number: {tok!r}") from exc
        if khz <= 0:
            raise argparse.ArgumentTypeError(f"step must be positive: {tok!r}")
        values.append(int(round(khz * 1_000)))
    if not values:
        raise argparse.ArgumentTypeError("step list contained no values")
    return tuple(sorted(set(values)))


def _msps(arg: str) -> int:
    """Parse a sample rate in MSPS to integer Hz. Accepts '10', '2.5', '6.0'."""
    try:
        msps = float(arg)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"not a number: {arg!r}") from exc
    if msps <= 0:
        raise argparse.ArgumentTypeError(f"sample rate must be positive: {arg!r}")
    return int(round(msps * 1_000_000))


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
    sweep.add_argument("--step", type=_khz_list, default=None, metavar="kHz",
                       help="Tuning step(s) in kHz. Single value (e.g. '12.5') or a "
                            "comma-separated list ('6.25,7.5,12.5') for VHF/UHF systems "
                            "that mix channel spacings across idens. With a list, each "
                            "detected peak is snapped to whichever grid lands closest — "
                            "Phase 1 capture cost is unchanged. Defaults to a "
                            "band-appropriate value (see README).")

    sdr = p.add_argument_group("SDR")
    sdr.add_argument("--sdr", choices=SDR_DRIVERS, default=None,
                     help="SDR driver. Autoprobed if omitted.")
    sdr.add_argument("--device-args", default=None, metavar="ARGS",
                     help="Raw gr-osmosdr device args (e.g. 'rtl=0' or 'airspy,bias=1').")
    sdr.add_argument("--sample-rate", type=_msps, default=None, metavar="MSPS",
                     help="SDR sample rate (MSPS). Auto-detected from the device "
                          "by default — Airspy R2 gets 10 MSPS, Mini gets 6, "
                          "RTL-SDR gets ~2.4. Override only if you need a specific "
                          "rate (e.g. lower CPU load).")
    sdr.add_argument("--gain", type=float, default=None, metavar="dB",
                     help="RF gain (dB). Sets the driver's default stage — for Airspy this "
                          "is the linearity preset (0-21); for RTL-SDR the tuner gain. Use "
                          "--list-gains to see what's available, or --device-args for "
                          "per-stage control.")
    sdr.add_argument("--ppm", type=float, default=0.0, metavar="PPM",
                     help="Frequency correction in PPM.")

    scan = p.add_argument_group("Scan behavior")
    scan.add_argument("--threshold", type=float, default=8.0, metavar="dB",
                      help="Phase 1 energy threshold above the noise floor (default 8). "
                           "Lower values (6-10) catch weaker signals but cost scan time "
                           "on more no-cc candidates; higher (15-20) is faster but may "
                           "skip distant or weak CCs.")
    scan.add_argument("--confirm-timeout", type=float, default=2.0, metavar="SECONDS",
                      help="Phase 2 abandon-fast threshold: a candidate that produces no "
                           "P25 control-channel broadcasts (NET_STS / RFSS_STS / IDEN_UP / "
                           "ADJ_STS) within this window is marked no-cc.")
    scan.add_argument("--max-dwell", type=float, default=12.0, metavar="SECONDS",
                      help="Hard cap on per-candidate dwell time. Adaptive logic exits "
                           "earlier once enough TSBKs have been collected.")
    scan.add_argument("--thorough", action="store_true",
                      help="Skip FFT energy scan; tune-and-decode every step (slow).")
    scan.add_argument("--phase1-only", action="store_true",
                      help="Run only Phase 1 (spectrum survey). Skip P25 decode.")
    scan.add_argument("--hide-no-cc", action="store_true",
                      help="Hide no-cc rows from the live console table. They're "
                           "still saved to the survey JSON for resume support.")
    scan.add_argument("--auto-gain", action="store_true",
                      help="After Phase 2, sweep gain values on each confirmed CC "
                           "and recommend the gain that minimizes BER. Adds "
                           "~3-5 minutes to the scan, depending on # of CCs found.")
    scan.add_argument("--gain-sweep", default=None, metavar="LIST",
                      help="Comma-separated gain values to sweep (e.g. '4,8,12,16,20'). "
                           "Defaults to driver-appropriate values.")

    out = p.add_argument_group("Output")
    out.add_argument("--output", default=None, metavar="PATH",
                     help="NDJSON survey file. Default: survey-YYYYMMDD-HHMMSS.json")
    out.add_argument("--resume", action="store_true",
                     help="Append to an existing --output file instead of "
                          "overwriting it; skip frequencies already in the file. "
                          "Without --resume, the output is truncated on startup.")
    out.add_argument("--verbose", "-v", action="store_true",
                     help="Verbose console logging.")

    rr = p.add_argument_group("RadioReference enrichment")
    rr.add_argument("--rr", action="store_true",
                    help="Cross-reference decoded systems against the RadioReference "
                         "database. Prompts for username + password at startup. "
                         "Produces a survey-*-submissions.md alongside the JSON+TXT.")

    return p


def resolve_config(args: argparse.Namespace) -> SurveyConfig:
    if args.start >= args.stop:
        raise SystemExit(f"--start ({args.start} Hz) must be less than --stop ({args.stop} Hz)")

    if args.step is not None:
        step_hz_list = args.step  # already a tuple from _khz_list
    else:
        step_hz_list = (default_step_hz(args.start, args.stop),)

    output_path = args.output
    if output_path is None:
        ts = datetime.now(tz=timezone.utc).strftime("%Y%m%d-%H%M%S")
        output_path = f"survey-{ts}.json"

    return SurveyConfig(
        start_hz=args.start,
        stop_hz=args.stop,
        step_hz_list=step_hz_list,
        sdr=args.sdr,
        device_args=args.device_args,
        sample_rate_hz=args.sample_rate,
        gain_db=args.gain,
        ppm=args.ppm,
        threshold_db=args.threshold,
        max_dwell_s=args.max_dwell,
        confirm_timeout_s=args.confirm_timeout,
        output_path=output_path,
        resume=args.resume,
        thorough=args.thorough,
        phase1_only=args.phase1_only,
        rr_enabled=args.rr,
        hide_no_cc=args.hide_no_cc,
        auto_gain=args.auto_gain,
        gain_sweep_spec=args.gain_sweep,
        verbose=args.verbose,
    )


def print_config_summary(cfg: SurveyConfig) -> None:
    span_mhz = (cfg.stop_hz - cfg.start_hz) / 1_000_000
    band_desc = describe_band(cfg.start_hz)
    finest_step = min(cfg.step_hz_list)
    n_steps_finest = (cfg.stop_hz - cfg.start_hz) // finest_step
    if len(cfg.step_hz_list) == 1:
        step_desc = f"{finest_step / 1e3:g} kHz  ({n_steps_finest} steps)"
    else:
        steps_kHz = ", ".join(f"{s / 1e3:g}" for s in cfg.step_hz_list)
        step_desc = (f"{steps_kHz} kHz  (multi-grid snap; finest "
                     f"= {n_steps_finest} steps)")
    print(f"P25 Survey — configuration")
    print(f"  range:     {cfg.start_hz / 1e6:.4f} – {cfg.stop_hz / 1e6:.4f} MHz  ({span_mhz:.3f} MHz)")
    print(f"  band:      {band_desc}")
    print(f"  step:      {step_desc}")
    print(f"  sdr:       {cfg.sdr or 'autoprobe'}"
          f"{' [' + cfg.device_args + ']' if cfg.device_args else ''}")
    if cfg.sample_rate_hz is None:
        print(f"  rate:      auto-detect from device")
    else:
        print(f"  rate:      {cfg.sample_rate_hz / 1e6:g} MSPS")
    print(f"  gain:      {cfg.gain_db if cfg.gain_db is not None else 'driver default'} dB")
    print(f"  ppm:       {cfg.ppm}")
    print(f"  threshold: {cfg.threshold_db} dB above noise floor")
    print(f"  dwell:     confirm={cfg.confirm_timeout_s}s, max={cfg.max_dwell_s}s")
    print(f"  output:    {cfg.output_path}{' (resume)' if cfg.resume else ''}")
    print(f"  mode:      {'thorough (stepwise)' if cfg.thorough else 'FFT energy scan + targeted decode'}")


def _resolve_driver(cfg: SurveyConfig) -> str | None:
    from p25_survey.sdr import autoprobe_driver
    return cfg.sdr or autoprobe_driver()


def _run_phase1_scan(cfg: "SurveyConfig", sdr, sample_rate: int, n_samples: int,
                     chunk_centers: list[int]):
    """Run the Phase 1 energy scan with a progress bar when stdout is a TTY,
    or simple per-chunk lines when piped/in --verbose mode."""
    from p25_survey.energy_scan import scan_range

    show_progress = sys.stdout.isatty()
    if show_progress:
        try:
            from rich.progress import (  # noqa: PLC0415
                BarColumn, MofNCompleteColumn, Progress, SpinnerColumn,
                TextColumn, TimeElapsedColumn,
            )
        except ImportError:
            show_progress = False

    if not show_progress:
        # Plain mode: log each chunk to stdout (verbose) or just yield candidates.
        def provider(center_hz: int):
            if cfg.verbose:
                print(f"  tune {center_hz / 1e6:.3f} MHz", flush=True)
            return sdr.capture(center_hz, n_samples)

        yield from scan_range(
            start_hz=cfg.start_hz, stop_hz=cfg.stop_hz,
            sample_rate_hz=sample_rate, iq_provider=provider,
            threshold_db=cfg.threshold_db, step_hz=cfg.step_hz_list,
        )
        return

    # Rich progress bar; transient=True clears it after Phase 1 completes.
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        MofNCompleteColumn(),
        TextColumn("•"),
        TimeElapsedColumn(),
        transient=True,
    ) as progress:
        task = progress.add_task("scanning", total=len(chunk_centers))

        def provider(center_hz: int):
            progress.update(task, description=f"tune {center_hz / 1e6:.3f} MHz")
            iq = sdr.capture(center_hz, n_samples)
            progress.advance(task)
            return iq

        yield from scan_range(
            start_hz=cfg.start_hz, stop_hz=cfg.stop_hz,
            sample_rate_hz=sample_rate, iq_provider=provider,
            threshold_db=cfg.threshold_db, step_hz=cfg.step_hz_list,
        )


# RR's getUserData renders the expiry with PHP date("m-d-Y", ...), i.e.
# "MM-DD-YYYY" (e.g. "10-03-2018"). We must parse THAT — an expired account
# whose date we can't read silently slips through as "unknown" and every RR
# lookup then fails the premium gate, surfacing as bogus "NEW SYS" rows.
# Also accept ISO "YYYY-MM-DD" (and "...T..." datetimes) as a fallback in case
# a call site ever hands us one. The 4-digit year position disambiguates.
_ISO_DATE_RE = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_US_DATE_RE = re.compile(r"(\d{2})-(\d{2})-(\d{4})")


def _parse_expire_date(stripped: str) -> date | None:
    """Parse an RR subExpireDate. Returns None if neither format matches."""
    m = _ISO_DATE_RE.search(stripped)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
    else:
        m = _US_DATE_RE.search(stripped)
        if not m:
            return None
        mo, d, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    try:
        return date(y, mo, d)
    except ValueError:
        return None


def _subscription_status(expire_str: str, today: date | None = None) -> tuple[str, str]:
    """Classify an RR subExpireDate string.

    Returns (status, friendly_description) where status is one of:
      - "active"  — subscription is current; proceed normally
      - "lifetime" — "Never" or "Never - Admin"; proceed normally
      - "expired" — parsed ISO date is in the past; refuse with diagnostic
      - "unknown" — empty or unparseable; warn but proceed (don't second-guess RR)
    """
    today = today or datetime.now(tz=timezone.utc).date()
    stripped = (expire_str or "").strip()
    if not stripped:
        return ("unknown", "RR returned no subscription expiry")
    # "Never" / "Never - Admin" — lifetime / admin accounts.
    if stripped.lower().startswith("never"):
        return ("lifetime", stripped)
    expire = _parse_expire_date(stripped)
    if expire is None:
        return ("unknown", f"could not parse expiry {stripped!r}")
    if expire < today:
        return ("expired", f"expired {expire.isoformat()}")
    return ("active", f"expires {expire.isoformat()}")


def _connect_rr(cfg: SurveyConfig):
    """Prompt for RadioReference credentials and verify with getUserData.

    Per the project's auth policy, credentials are NOT stored anywhere on
    disk — prompted fresh each scan. Returns an authenticated RRClient or
    aborts the scan if auth fails.
    """
    import getpass
    from p25_survey.radioreference import RRAuthError, RRClient, RRError, app_key

    if not app_key():
        print("error: --rr requires the bundled p25-survey appKey to be set, or "
              "P25_SURVEY_APPKEY env var. The release build should already have "
              "this baked in.", flush=True)
        raise SystemExit(2)

    print(flush=True)
    print("RadioReference authentication (credentials are not stored)", flush=True)
    sys.stdout.flush()
    sys.stderr.flush()
    username = input("  Username: ").strip()
    password = getpass.getpass("  Password: ")
    try:
        client = RRClient(username=username, password=password)
        user = client.get_user_data()
    except RRAuthError as exc:
        print(f"error: RadioReference auth failed: {exc}", flush=True)
        raise SystemExit(2)
    except RRError as exc:
        print(f"error: RadioReference: {exc}", flush=True)
        raise SystemExit(2)
    status, detail = _subscription_status(user.sub_expire_date)
    if status == "expired":
        print(file=sys.stderr)
        print(f"error: RadioReference Premium subscription required for --rr "
              f"({detail}).", file=sys.stderr)
        print(f"  Account {user.username} authenticated, but the subscription is "
              f"not current.", file=sys.stderr)
        print(f"  Renew at https://www.radioreference.com/apps/content/?cid=3, "
              f"or re-run without --rr to skip RR enrichment.", file=sys.stderr)
        raise SystemExit(2)
    if status == "unknown":
        print(f"  Logged in as {user.username} (warning: {detail})", flush=True)
        print(f"  Continuing — RR lookups may fail if the subscription is "
              f"not current.", flush=True)
    else:
        print(f"  Logged in as {user.username} ({detail})", flush=True)
    return client


def _run_scan(cfg: SurveyConfig) -> int:
    """End-to-end scan: Phase 1 candidate finder → Phase 2 P25 decode → output.

    With --phase1-only, prints candidate frequencies and stops.
    """
    from p25_survey.energy_scan import plan_scan_chunks, scan_range
    from p25_survey.sdr import (
        SampleRateError, SdrCapture, SdrConfig, probe_sample_rates, select_sample_rate,
    )

    driver = _resolve_driver(cfg)
    if driver is None:
        print("error: no SDR driver specified and gr-osmosdr is not importable", flush=True)
        return 2

    # Probe the device for its supported sample rates before doing anything
    # else expensive (RR auth, etc.). For Airspy this is the fix for the
    # Mini-vs-R2 mismatch — gr-osmosdr would otherwise raise
    # "Unsupported samplerate: 6M" deep inside Phase 1. SdrOpenError from
    # the probe propagates to main()'s handler for a friendly diagnostic.
    try:
        rate_support = probe_sample_rates(driver, device_args=cfg.device_args)
    except ImportError as exc:
        print(f"error: gr-osmosdr is not installed: {exc}", file=sys.stderr)
        print("  Install boatbod op25 (which provides gr-osmosdr) per the "
              "project README.", file=sys.stderr)
        return 2
    try:
        sample_rate = select_sample_rate(rate_support, cfg.sample_rate_hz)
    except SampleRateError as exc:
        print(f"error: {exc}", file=sys.stderr)
        print(f"       supported by this {rate_support.driver}: "
              f"{rate_support.describe()}", file=sys.stderr)
        return 2

    if cfg.sample_rate_hz is None:
        print(f"  detected:  {rate_support.driver} supports {rate_support.describe()}; "
              f"using {sample_rate / 1e6:g} MSPS")
    elif cfg.verbose:
        print(f"  detected:  {rate_support.driver} supports {rate_support.describe()}")

    # Authenticate to RR up front (after the SDR probe so a missing device
    # surfaces before we prompt for credentials, but before Phase 1 so any
    # credential failures surface immediately).
    rr_client = _connect_rr(cfg) if cfg.rr_enabled else None

    sdr_cfg = SdrConfig(
        driver=driver,
        device_args=cfg.device_args,
        sample_rate_hz=sample_rate,
        gain_db=cfg.gain_db,
        ppm=cfg.ppm,
    )
    sdr = SdrCapture(sdr_cfg)
    n_samples = 1 << 17

    if cfg.verbose:
        print(f"  driver:    {driver} @ {sample_rate / 1e6:g} MSPS, "
              f"capture {n_samples} samples per chunk")

    # ----- Phase 1 -----
    print()
    print("Phase 1 — energy scan")
    chunk_centers = plan_scan_chunks(cfg.start_hz, cfg.stop_hz, sample_rate)
    candidates = list(_run_phase1_scan(cfg, sdr, sample_rate, n_samples, chunk_centers))

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
    from p25_survey.decoder import Op25NotInstalledError, decode_candidate, ensure_op25_importable
    from p25_survey.report import render_file
    from p25_survey.survey import SurveyWriter

    # Surface op25 install problems before the first decode rather than mid-scan —
    # gives the user a single, actionable error and avoids a partial survey file.
    try:
        ensure_op25_importable()
    except Op25NotInstalledError as exc:
        print(file=sys.stderr)
        print(f"error: {exc}", file=sys.stderr)
        return 2

    writer = SurveyWriter(cfg.output_path, resume=cfg.resume)
    skipped = sum(1 for c in candidates if writer.already_characterized(c.freq_hz))
    to_do = [c for c in candidates if not writer.already_characterized(c.freq_hz)]

    enrichments: dict[int, "EnrichmentResult"] = {}
    all_records: list = []
    confirmed = 0
    with make_display(total=len(to_do), skipped=skipped,
                      hide_no_cc=cfg.hide_no_cc) as display:
        for cand in to_do:
            display.set_current(cand.freq_hz, max_dwell_s=cfg.max_dwell_s)
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
            if rr_client is not None and record.wacn is not None:
                from p25_survey.enrich import enrich_record
                enr = enrich_record(record, rr_client)
                record.rr = enr.to_json_dict()
                enrichments[record.freq_hz] = enr
            writer.append(record)
            all_records.append(record)
            if record.complete:
                confirmed += 1
                status = "complete"
            elif record.wacn is None and record.rfss_id is None:
                status = "no-cc"
            else:
                status = "partial"
            display.add(record, status)

    # ----- post-scan: per-band offsets + submission report -----
    if rr_client is not None and enrichments:
        from p25_survey.enrich import recommend_ppm, summarize_band_offsets
        from p25_survey.submission import render_file as render_submission

        offsets = summarize_band_offsets(all_records, enrichments)
        if offsets:
            print()
            print("RadioReference frequency offsets per band:")
            print(f"  {'Band':<55} {'N':>3} {'Mean ppm':>10} {'Median':>8}")
            for s in offsets:
                print(f"  {s.band_name:<55} {s.n_samples:>3} "
                      f"{s.mean_ppm:>+10.3f} {s.median_ppm:>+8.3f}")

        rec = recommend_ppm(all_records, enrichments)
        print()
        if rec.consistency == "insufficient_data":
            print(f"--ppm recommendation: need at least 3 matched sites "
                  f"(got {rec.n_samples}) for a reliable suggestion.")
        else:
            print(f"Recommended --ppm: {rec.median_ppm:+.2f}  "
                  f"(median across {rec.n_samples} sites; IQR {rec.iqr_ppm:.2f} ppm; "
                  f"consistency: {rec.consistency})")
            if rec.cross_band_warning:
                print(f"  Warning: {rec.cross_band_warning}")

        sub_path = os.path.splitext(cfg.output_path)[0] + "-submissions.md"
        render_submission(all_records, enrichments, sub_path)
        print(f"  Submission report: {sub_path}")

    # Render text report alongside the JSON survey.
    txt_path = os.path.splitext(cfg.output_path)[0] + ".txt"
    render_file(cfg.output_path, txt_path)

    # ----- Phase 3: gain sweep -----
    band_recs = []
    if cfg.auto_gain:
        band_recs = _run_gain_sweep(cfg, all_records, driver, sdr_cfg, sample_rate)

    print()
    print(f"  {confirmed} / {len(to_do)} candidates confirmed as P25 control channels.")
    print(f"  Survey JSON: {cfg.output_path}")
    print(f"  Survey TXT:  {txt_path}")

    # ----- Optional rescan with the recommended gain -----
    if cfg.auto_gain and band_recs:
        rescan_gain = _prompt_rescan_gain(cfg, band_recs)
        if rescan_gain is not None:
            return _rescan_with_gain(cfg, rescan_gain)
    return 0


def _prompt_rescan_gain(cfg: SurveyConfig, band_recs: list) -> float | None:
    """Ask the user whether to re-run the scan at the recommended gain.

    Picks the band with the most CCs as the source of the recommendation
    (most statistically meaningful). Skips the prompt entirely if stdin
    isn't a TTY (so piped runs don't hang).
    """
    if not sys.stdin.isatty():
        return None
    # Pick the band with the most matched CCs (most confidence)
    best = max(band_recs, key=lambda r: r.n_cc)
    if cfg.gain_db is not None and abs(cfg.gain_db - best.median_gain) < 0.5:
        print()
        print(f"Current --gain {cfg.gain_db:g} already matches the recommendation "
              f"({best.median_gain:g} for {best.band_name}). No rescan needed.")
        return None
    print()
    current = f"{cfg.gain_db:g}" if cfg.gain_db is not None else "AGC"
    try:
        prompt = (f"Re-run scan with --gain {best.median_gain:g} "
                  f"(was {current}; recommendation from {best.band_name})? [Y/n] ")
        response = input(prompt).strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if response in ("", "y", "yes"):
        return best.median_gain
    return None


def _rescan_with_gain(cfg: SurveyConfig, new_gain: float) -> int:
    """Re-run the scan with `new_gain`. Auto-gain is disabled to avoid an
    infinite recommend→rescan loop; the new run gets its own output filename
    so the original survey isn't overwritten."""
    from dataclasses import replace
    base, ext = os.path.splitext(cfg.output_path)
    new_output = f"{base}-rescan-gain{int(round(new_gain))}{ext}"
    new_cfg = replace(cfg, gain_db=new_gain, auto_gain=False, output_path=new_output)
    print()
    print(f"=== Rescanning with --gain {new_gain:g} → {new_output} ===")
    return _run_scan(new_cfg)


def _run_gain_sweep(cfg: SurveyConfig, all_records: list, driver: str,
                    sdr_cfg, sample_rate: int) -> list:
    """Sweep gain on each confirmed CC, print per-CC + per-band picks, and
    return the per-band recommendations so the orchestrator can prompt for
    a rescan."""
    from p25_survey.decoder import decode_candidate
    from p25_survey.gain_sweep import (
        GainSample, cc_sweep_result, confirmed_ccs, default_sweep_grid,
        parse_sweep_grid, recommend_per_band,
    )

    ccs = confirmed_ccs(all_records)
    if not ccs:
        print()
        print("  --auto-gain: no confirmed CCs to sweep.")
        return []

    if cfg.gain_sweep_spec:
        try:
            sweep_values = parse_sweep_grid(cfg.gain_sweep_spec)
        except ValueError as exc:
            print(f"error: --gain-sweep: {exc}", flush=True)
            return []
    else:
        sweep_values = default_sweep_grid(driver)

    est_minutes = (len(ccs) * len(sweep_values) * 5) / 60.0
    print()
    print(f"Phase 3 — gain sweep  ({len(ccs)} CCs × {len(sweep_values)} gains "
          f"× ~5s each ≈ {est_minutes:.1f} min)")
    print(f"  sweeping: {', '.join(str(g) for g in sweep_values)} dB")
    print()

    use_rich = sys.stdout.isatty()
    if use_rich:
        try:
            from rich.console import Console  # noqa: PLC0415
            from rich.live import Live  # noqa: PLC0415
            from rich.table import Table  # noqa: PLC0415
        except ImportError:
            use_rich = False

    if use_rich:
        console = Console()
        table = Table(title="gain sweep", show_header=True, header_style="bold")
        for h, j in [("Freq (MHz)", "right"), ("Gain", "right"),
                     ("BER", "right"), ("Decode", "right"),
                     ("RSSI", "right"), ("Lock", "left")]:
            table.add_column(h, justify=j)

        results = []
        with Live(table, console=console, refresh_per_second=4) as live:
            for cc in ccs:
                samples: list[GainSample] = []
                for g in sweep_values:
                    rec = decode_candidate(
                        freq_hz=cc.freq_hz,
                        sdr_driver=driver,
                        device_args=sdr_cfg.resolved_device_args(),
                        sample_rate_hz=sample_rate,
                        gain_db=g,
                        ppm=cfg.ppm,
                        confirm_timeout_s=2.0,
                        max_dwell_s=4.0,
                    )
                    sample = GainSample(
                        gain_db=g,
                        ber_pct=rec.signal.ber_pct_mean,
                        decode_rate_pct=rec.signal.decode_rate_pct,
                        rssi_dbfs=rec.signal.rssi_dbfs_mean,
                        locked=rec.signal.ber_pct_mean is not None,
                    )
                    samples.append(sample)
                    table.add_row(
                        f"{cc.freq_hz / 1e6:.5f}",
                        f"{g:g}",
                        f"{sample.ber_pct:.2f}%" if sample.ber_pct is not None else "—",
                        f"{sample.decode_rate_pct:.1f}%" if sample.decode_rate_pct is not None else "—",
                        f"{sample.rssi_dbfs:+.1f}" if sample.rssi_dbfs is not None else "—",
                        "✓" if sample.locked else "—",
                    )
                    live.update(table)
                results.append(cc_sweep_result(
                    freq_hz=cc.freq_hz, samples=samples,
                    original_gain=cfg.gain_db,
                    original_ber=cc.signal.ber_pct_mean,
                ))
    else:
        results = []
        for cc in ccs:
            samples = []
            print(f"  {cc.freq_hz / 1e6:.5f} MHz:")
            for g in sweep_values:
                rec = decode_candidate(
                    freq_hz=cc.freq_hz, sdr_driver=driver,
                    device_args=sdr_cfg.resolved_device_args(),
                    sample_rate_hz=sample_rate, gain_db=g, ppm=cfg.ppm,
                    confirm_timeout_s=2.0, max_dwell_s=4.0,
                )
                sample = GainSample(
                    gain_db=g, ber_pct=rec.signal.ber_pct_mean,
                    decode_rate_pct=rec.signal.decode_rate_pct,
                    rssi_dbfs=rec.signal.rssi_dbfs_mean,
                    locked=rec.signal.ber_pct_mean is not None,
                )
                samples.append(sample)
                ber = f"{sample.ber_pct:.2f}%" if sample.ber_pct is not None else "—"
                rssi = f"{sample.rssi_dbfs:+.1f}" if sample.rssi_dbfs is not None else "—"
                print(f"    gain {g:>4g}  BER {ber:>7}  RSSI {rssi:>6} dBFS",
                      flush=True)
            results.append(cc_sweep_result(
                freq_hz=cc.freq_hz, samples=samples,
                original_gain=cfg.gain_db, original_ber=cc.signal.ber_pct_mean,
            ))

    # ----- summaries -----
    print()
    print("Per-CC best gain:")
    print(f"  {'Frequency':<14} {'Best gain':>10} {'BER@best':>10} "
          f"{'Original gain':>14} {'Original BER':>13} {'Δ pp':>8}")
    for r in results:
        if r.best_gain is None:
            continue
        original_ber_str = f"{r.original_ber:.2f}%" if r.original_ber is not None else "—"
        original_gain_str = f"{r.original_gain}" if r.original_gain is not None else "AGC"
        delta = f"{r.improvement_pp:+.2f}" if r.improvement_pp is not None else "—"
        print(f"  {r.freq_hz / 1e6:>10.5f} MHz {r.best_gain:>10g} "
              f"{r.best_ber:>9.2f}% {original_gain_str:>14} "
              f"{original_ber_str:>13} {delta:>8}")

    band_recs = recommend_per_band(results)
    if band_recs:
        print()
        print("Per-band recommendation:")
        for rec in band_recs:
            print(f"  {rec.band_name}")
            print(f"    suggested --gain {rec.median_gain:g}   "
                  f"(median across {rec.n_cc} CC{'s' if rec.n_cc != 1 else ''}; "
                  f"mean {rec.mean_gain:g}, range {rec.range_gain[0]:g}-{rec.range_gain[1]:g})")
    return band_recs


def _print_sdr_open_error(exc: "SdrOpenError", user_specified_driver: bool) -> None:
    """Friendly diagnostic for an SDR-open failure.

    Always prints the driver/args/underlying-message; appends an autoprobe
    hint when the driver came from autoprobe rather than --sdr (so the
    "you may have a different SDR plugged in" suggestion only shows up
    when it's actually applicable).
    """
    print(file=sys.stderr)
    print("error: failed to open SDR", file=sys.stderr)
    print(f"  driver:      {exc.driver}", file=sys.stderr)
    print(f"  device-args: {exc.device_args}", file=sys.stderr)
    print(f"  underlying:  {exc.underlying}", file=sys.stderr)
    print(file=sys.stderr)
    print("Likely causes:", file=sys.stderr)
    if not user_specified_driver:
        print("  - Different SDR connected. Autoprobe defaulted to rtlsdr without "
              "actually detecting hardware. If you have an Airspy or HackRF, "
              "pass --sdr airspy or --sdr hackrf explicitly.", file=sys.stderr)
    print("  - SDR is not plugged in, or busy with another process "
          "(rtl_tcp, rtl_fm, op25, etc.).", file=sys.stderr)
    print("  - Custom args needed (specific serial, bias-tee, sample type). "
          "Use --device-args, e.g. --device-args 'rtl=00000001'.", file=sys.stderr)


def _run_list_gains(args: argparse.Namespace) -> int:
    """Probe the SDR and print its gain stage table."""
    from p25_survey.sdr import SdrOpenError, autoprobe_driver, probe_gains

    user_driver = args.sdr is not None
    driver = args.sdr or autoprobe_driver()
    if driver is None:
        print("error: --list-gains needs an SDR driver — pass --sdr "
              "rtlsdr|airspy|hackrf, or install gr-osmosdr.", flush=True)
        return 2

    try:
        info = probe_gains(driver, device_args=args.device_args)
    except SdrOpenError as exc:
        _print_sdr_open_error(exc, user_specified_driver=user_driver)
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

    from p25_survey.sdr import SdrOpenError  # noqa: PLC0415
    try:
        return _run_scan(cfg)
    except SdrOpenError as exc:
        _print_sdr_open_error(exc, user_specified_driver=args.sdr is not None)
        return 2
    except KeyboardInterrupt:
        # Per-record fsync means whatever was decoded is already on disk.
        # Print a clear marker so the user knows the run was cut short.
        print()
        print()
        print("Interrupted (Ctrl-C). Records decoded up to this point have been "
              "saved to:", flush=True)
        print(f"  {cfg.output_path}")
        print("Resume with --resume to continue without re-decoding what's there.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
