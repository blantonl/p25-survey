"""Live SDR capture via gr-osmosdr.

Provides a thin wrapper that opens an SDR (rtlsdr / airspy / hackrf), sets
gain / sample rate / ppm once, and exposes `capture(center_hz, n_samples)`
that returns complex64 IQ. Used as the `iq_provider` callable for the
Phase 1 energy scanner.

GNU Radio + gr-osmosdr are imported lazily inside the methods so the
package remains importable on hosts without GNU Radio (Mac dev box, CI).

The implementation rebuilds the GNU Radio flowgraph per capture. This is
~50 ms of overhead per chunk on top of the SDR's own retune+settle time;
acceptable for Phase 1 where we tune in MHz-wide chunks.

Driver name → osmosdr device-args mapping (when --device-args not given):
    rtlsdr  → "rtl=0"
    airspy  → "airspy=0"
    hackrf  → "hackrf=0"

Gain semantics — what `--gain N` actually does:
    rtlsdr  → tuner gain, snapped to nearest discrete value (e.g. 0, 0.9,
              1.4, ..., 49.6 dB). Range device-specific.
    airspy  → "linearity" preset (0-21 dB), gr-osmosdr's optimized blend of
              LNA/MIX/IF stages. Useful values: 8-18.
    hackrf  → IF/VGA gain (0-62 dB). Set LNA + AMP separately via
              --device-args (e.g. "hackrf=0,bias=1,amp=1") if needed.
For per-stage control on any device, bypass --gain and use
--device-args, e.g. "airspy=0,linearity=14" or "rtl=0,bias=1".
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


# Default sample rate per driver. The Airspy Mini supports 6 / 3 MSPS only;
# we default to 6 (wider chunks → faster Phase 1).
_DEFAULT_SAMPLE_RATE_HZ: dict[str, int] = {
    "rtlsdr": 2_400_000,
    "airspy": 6_000_000,
    "hackrf": 8_000_000,
}

_DEFAULT_DEVICE_ARGS: dict[str, str] = {
    "rtlsdr": "rtl=0",
    "airspy": "airspy=0",
    "hackrf": "hackrf=0",
}


class SdrOpenError(Exception):
    """Raised when gr-osmosdr can't open the requested SDR.

    Carries enough context (driver, args, and the underlying message) for
    `main()` to print a friendly diagnostic instead of an osmosdr traceback.
    Most commonly: wrong driver passed (or autoprobe defaulted to rtlsdr
    when an Airspy/HackRF is actually plugged in), device in use by
    another process, or the device just isn't connected.
    """

    def __init__(self, driver: str, device_args: str, underlying: str) -> None:
        super().__init__(f"failed to open {driver} ({device_args}): {underlying}")
        self.driver = driver
        self.device_args = device_args
        self.underlying = underlying


def _open_osmosdr_source(driver: str, device_args: str):
    """Open an osmosdr.source, converting RuntimeError into SdrOpenError.

    osmosdr raises bare RuntimeError with messages like "Wrong rtlsdr
    device index given." which give no hint that the user might just need
    --sdr airspy. Wrapping at this single boundary lets the CLI emit a
    useful error.
    """
    import osmosdr  # noqa: PLC0415

    try:
        return osmosdr.source(args=device_args)
    except RuntimeError as exc:
        raise SdrOpenError(driver=driver, device_args=device_args, underlying=str(exc)) from exc


@dataclass(frozen=True)
class SdrConfig:
    driver: str                       # rtlsdr | airspy | hackrf
    device_args: str | None = None    # raw osmosdr args; auto if None
    sample_rate_hz: int | None = None # auto-default per driver if None
    gain_db: float | None = None
    ppm: float = 0.0
    settle_samples: int = 65_536      # discarded after each retune (DC settle, AGC)

    def resolved_device_args(self) -> str:
        if self.device_args:
            return self.device_args
        if self.driver in _DEFAULT_DEVICE_ARGS:
            return _DEFAULT_DEVICE_ARGS[self.driver]
        raise ValueError(f"no default device-args for driver {self.driver!r}")

    def resolved_sample_rate(self) -> int:
        if self.sample_rate_hz:
            return self.sample_rate_hz
        if self.driver in _DEFAULT_SAMPLE_RATE_HZ:
            return _DEFAULT_SAMPLE_RATE_HZ[self.driver]
        raise ValueError(f"no default sample rate for driver {self.driver!r}")


class SdrCapture:
    """Live SDR capture wrapper. Construct, then call capture() per chunk.

    Cleanup is handled by garbage collection; no explicit close() needed.
    """

    def __init__(self, config: SdrConfig) -> None:
        self.config = config
        self._sample_rate = config.resolved_sample_rate()
        self._device_args = config.resolved_device_args()
        # Defer GNU Radio imports + device open until first capture so we can
        # construct an SdrCapture in places that don't need an open device.

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate

    def capture(self, center_hz: int, n_samples: int) -> np.ndarray:
        """Tune to center_hz, capture n_samples complex IQ, return as complex64."""
        # Lazy imports — first capture brings GNU Radio in.
        from gnuradio import blocks, gr  # noqa: PLC0415  (intentional lazy import)
        from p25_survey._stderr import suppress_c_stderr  # noqa: PLC0415

        total = n_samples + max(0, self.config.settle_samples)

        with suppress_c_stderr():
            tb = gr.top_block()
            src = _open_osmosdr_source(self.config.driver, self._device_args)
            src.set_sample_rate(self._sample_rate)
            src.set_center_freq(int(center_hz))
            src.set_freq_corr(float(self.config.ppm), 0)
            if self.config.gain_db is not None:
                src.set_gain_mode(False, 0)            # disable AGC when gain set
                src.set_gain(float(self.config.gain_db), 0)
            else:
                src.set_gain_mode(True, 0)             # AGC

            head = blocks.head(gr.sizeof_gr_complex, total)
            sink = blocks.vector_sink_c()
            tb.connect(src, head, sink)
            tb.run()

        data = np.asarray(sink.data(), dtype=np.complex64)
        # Drop settle samples from the front.
        if self.config.settle_samples > 0 and len(data) > self.config.settle_samples:
            data = data[self.config.settle_samples:]
        return data


#: Drivers we know how to open, in the order autoprobe will guess them.
SUPPORTED_DRIVERS: tuple[str, ...] = ("rtlsdr", "airspy", "hackrf")


def autoprobe_driver() -> str | None:
    """Default SDR driver when --sdr is omitted.

    Note: this does *not* actually detect what hardware is plugged in —
    gr-osmosdr's only way to do that is to try opening each candidate,
    which is slow and clobbers the device for any other process. We just
    confirm gr-osmosdr is importable and return rtlsdr as the most common
    case. If the user has an Airspy/HackRF, the open will fail with
    `SdrOpenError` and the CLI will tell them to pass --sdr explicitly.
    """
    try:
        import osmosdr  # noqa: F401, PLC0415
    except ImportError:
        return None
    return "rtlsdr"


@dataclass(frozen=True)
class GainRange:
    name: str        # "default" / "LNA" / "MIX" / "IF" / etc.
    start: float
    stop: float
    step: float


@dataclass(frozen=True)
class GainInfo:
    driver: str
    device_args: str
    default_range: GainRange    # what `set_gain(N, 0)` controls
    stages: list[GainRange]     # named stages from get_gain_names()


def probe_gains(driver: str, device_args: str | None = None) -> GainInfo:
    """Open the SDR briefly and report its available gain stages.

    Caller must pass a driver that gr-osmosdr understands. Raises if the
    device cannot be opened.
    """
    from p25_survey._stderr import suppress_c_stderr  # noqa: PLC0415

    args = device_args or _DEFAULT_DEVICE_ARGS.get(driver, driver)
    with suppress_c_stderr():
        src = _open_osmosdr_source(driver, args)
        chan = 0

        def _to_range(r) -> tuple[float, float, float]:
            return (float(r.start()), float(r.stop()), float(r.step()) or 1.0)

        default_lo, default_hi, default_step = _to_range(src.get_gain_range(chan))
        stages: list[GainRange] = []
        for name in src.get_gain_names(chan):
            lo, hi, step = _to_range(src.get_gain_range(name, chan))
            stages.append(GainRange(name=name, start=lo, stop=hi, step=step))

    return GainInfo(
        driver=driver,
        device_args=args,
        default_range=GainRange("default", default_lo, default_hi, default_step),
        stages=stages,
    )
