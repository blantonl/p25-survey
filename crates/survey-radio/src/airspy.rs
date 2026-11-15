//! Airspy (R2, Mini) through libairspy, loaded when an Airspy is opened, so
//! nothing else needs it installed. libairspy converts the ADC's real
//! samples to complex float I/Q and calls back on its own thread; the
//! callback copies each block into a bounded queue that [`Airspy::read`]
//! takes from. A full queue drops the block and counts it: the survey would
//! rather lose a block than stall the USB transfers.

use std::ffi::{CStr, c_char, c_int, c_void};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, RecvTimeoutError, SyncSender, TrySendError, sync_channel};
use std::time::Duration;

use libloading::Library;
use num_complex::Complex32;

use crate::{Gain, Radio, RadioError};

#[repr(C)]
struct Transfer {
    device: *mut c_void,
    ctx: *mut c_void,
    samples: *mut c_void,
    sample_count: c_int,
    dropped_samples: u64,
    sample_type: c_int,
}

const SAMPLE_FLOAT32_IQ: c_int = 0;
/// Blocks queued between libairspy's thread and the reader (~11 ms each at 6 MS/s).
const QUEUE_BLOCKS: usize = 64;

type Callback = extern "C" fn(*mut Transfer) -> c_int;

/// The libairspy functions used.
struct Lib {
    _lib: Library,
    list_devices: unsafe extern "C" fn(*mut u64, c_int) -> c_int,
    open: unsafe extern "C" fn(*mut *mut c_void) -> c_int,
    open_sn: unsafe extern "C" fn(*mut *mut c_void, u64) -> c_int,
    close: unsafe extern "C" fn(*mut c_void) -> c_int,
    get_samplerates: unsafe extern "C" fn(*mut c_void, *mut u32, u32) -> c_int,
    set_samplerate: unsafe extern "C" fn(*mut c_void, u32) -> c_int,
    set_sample_type: unsafe extern "C" fn(*mut c_void, c_int) -> c_int,
    set_freq: unsafe extern "C" fn(*mut c_void, u32) -> c_int,
    set_linearity_gain: unsafe extern "C" fn(*mut c_void, u8) -> c_int,
    set_sensitivity_gain: unsafe extern "C" fn(*mut c_void, u8) -> c_int,
    set_lna_agc: unsafe extern "C" fn(*mut c_void, u8) -> c_int,
    set_mixer_agc: unsafe extern "C" fn(*mut c_void, u8) -> c_int,
    start_rx: unsafe extern "C" fn(*mut c_void, Callback, *mut c_void) -> c_int,
    stop_rx: unsafe extern "C" fn(*mut c_void) -> c_int,
    error_name: unsafe extern "C" fn(c_int) -> *const c_char,
}

const NAMES: &[&str] = if cfg!(target_os = "macos") {
    &["libairspy.0.dylib", "libairspy.dylib", "/opt/homebrew/lib/libairspy.dylib", "/usr/local/lib/libairspy.dylib"]
} else if cfg!(windows) {
    &["airspy.dll"]
} else {
    &["libairspy.so.0", "libairspy.so"]
};

impl Lib {
    fn load() -> Result<Lib, RadioError> {
        let lib = NAMES.iter().find_map(|n| unsafe { Library::new(n) }.ok()).ok_or_else(|| {
            RadioError(format!(
                "libairspy isn't installed (looked for {}). On Debian/Ubuntu: apt install libairspy0; on macOS: brew install airspy",
                NAMES.join(", ")
            ))
        })?;
        macro_rules! f {
            ($name:literal) => {
                *unsafe { lib.get(concat!($name, "\0").as_bytes()) }.map_err(|e| RadioError(format!("libairspy: {}: {e}", $name)))?
            };
        }
        Ok(Lib {
            list_devices: f!("airspy_list_devices"),
            open: f!("airspy_open"),
            open_sn: f!("airspy_open_sn"),
            close: f!("airspy_close"),
            get_samplerates: f!("airspy_get_samplerates"),
            set_samplerate: f!("airspy_set_samplerate"),
            set_sample_type: f!("airspy_set_sample_type"),
            set_freq: f!("airspy_set_freq"),
            set_linearity_gain: f!("airspy_set_linearity_gain"),
            set_sensitivity_gain: f!("airspy_set_sensitivity_gain"),
            set_lna_agc: f!("airspy_set_lna_agc"),
            set_mixer_agc: f!("airspy_set_mixer_agc"),
            start_rx: f!("airspy_start_rx"),
            stop_rx: f!("airspy_stop_rx"),
            error_name: f!("airspy_error_name"),
            _lib: lib,
        })
    }

    fn check(&self, what: &str, r: c_int) -> Result<(), RadioError> {
        if r == 0 {
            return Ok(());
        }
        let name = unsafe { (self.error_name)(r) };
        let name = if name.is_null() { format!("error {r}") } else { unsafe { CStr::from_ptr(name) }.to_string_lossy().into_owned() };
        Err(RadioError(format!("airspy: {what}: {name}")))
    }
}

/// What libairspy's thread shares with us.
struct Shared {
    tx: SyncSender<Vec<Complex32>>,
    /// Samples lost: libairspy's own, and blocks our queue had no room for.
    dropped: AtomicU64,
}

extern "C" fn on_samples(t: *mut Transfer) -> c_int {
    // SAFETY: libairspy passes a valid transfer for the call's duration; ctx
    // is the Shared we gave start_rx, alive until after stop_rx.
    let t = unsafe { &*t };
    let shared = unsafe { &*(t.ctx as *const Shared) };
    if t.sample_type != SAMPLE_FLOAT32_IQ || t.samples.is_null() || t.sample_count <= 0 {
        return 0;
    }
    let n = t.sample_count as usize;
    let samples = unsafe { std::slice::from_raw_parts(t.samples as *const Complex32, n) };
    if t.dropped_samples > 0 {
        shared.dropped.fetch_add(t.dropped_samples, Ordering::Relaxed);
    }
    match shared.tx.try_send(samples.to_vec()) {
        Ok(()) => {}
        Err(TrySendError::Full(_)) | Err(TrySendError::Disconnected(_)) => {
            shared.dropped.fetch_add(n as u64, Ordering::Relaxed);
        }
    }
    0
}

pub struct Airspy {
    lib: Lib,
    dev: *mut c_void,
    rate: f64,
    center: f64,
    shared: *const Shared,
    rx: Receiver<Vec<Complex32>>,
}

// The device handle is only used from the thread that owns the Airspy.
unsafe impl Send for Airspy {}

/// Serial numbers of the Airspys plugged in.
pub fn list() -> Result<Vec<u64>, RadioError> {
    let lib = Lib::load()?;
    let mut serials = [0u64; 16];
    let n = unsafe { (lib.list_devices)(serials.as_mut_ptr(), serials.len() as c_int) };
    if n < 0 {
        lib.check("listing devices", n)?;
    }
    Ok(serials[..n.max(0) as usize].to_vec())
}

impl Airspy {
    /// Open the Airspy with `serial` (or the first), at the highest sample
    /// rate it offers no higher than `max_rate_hz`.
    pub fn open(serial: Option<u64>, max_rate_hz: f64, gain: Gain) -> Result<Airspy, RadioError> {
        let lib = Lib::load()?;
        let mut dev: *mut c_void = std::ptr::null_mut();
        let r = unsafe {
            match serial {
                Some(s) => (lib.open_sn)(&mut dev, s),
                None => (lib.open)(&mut dev),
            }
        };
        lib.check("opening (is it plugged in, and not in use by another program?)", r)?;
        let mut a = Airspy { lib, dev, rate: 0.0, center: 0.0, shared: std::ptr::null(), rx: sync_channel(1).1 };
        let mut count = 0u32;
        a.lib.check("sample rates", unsafe { (a.lib.get_samplerates)(a.dev, &mut count, 0) })?;
        let mut rates = vec![0u32; count as usize];
        a.lib.check("sample rates", unsafe { (a.lib.get_samplerates)(a.dev, rates.as_mut_ptr(), count) })?;
        let rate = rates
            .iter()
            .copied()
            .filter(|&r| r as f64 <= max_rate_hz)
            .max()
            .ok_or_else(|| RadioError(format!("airspy: no sample rate at or under {max_rate_hz} (it offers {rates:?})")))?;
        a.lib.check("sample rate", unsafe { (a.lib.set_samplerate)(a.dev, rate) })?;
        a.rate = rate as f64;
        a.lib.check("sample type", unsafe { (a.lib.set_sample_type)(a.dev, SAMPLE_FLOAT32_IQ) })?;
        a.set_gain(gain)?;
        Ok(a)
    }

    /// Samples lost so far.
    pub fn dropped(&self) -> u64 {
        if self.shared.is_null() { 0 } else { unsafe { &*self.shared }.dropped.load(Ordering::Relaxed) }
    }

    fn start(&mut self) -> Result<(), RadioError> {
        let (tx, rx) = sync_channel(QUEUE_BLOCKS);
        let shared = Box::into_raw(Box::new(Shared { tx, dropped: AtomicU64::new(0) }));
        let r = unsafe { (self.lib.start_rx)(self.dev, on_samples, shared as *mut c_void) };
        if r != 0 {
            drop(unsafe { Box::from_raw(shared) });
            return self.lib.check("starting", r);
        }
        self.shared = shared;
        self.rx = rx;
        Ok(())
    }
}

impl Radio for Airspy {
    fn rate_hz(&self) -> f64 {
        self.rate
    }

    fn center_hz(&self) -> f64 {
        self.center
    }

    fn tune(&mut self, hz: f64) -> Result<(), RadioError> {
        self.lib.check("tuning", unsafe { (self.lib.set_freq)(self.dev, hz.round() as u32) })?;
        self.center = hz;
        if self.shared.is_null() {
            self.start()?;
        }
        // What's queued is from the old frequency.
        while self.rx.try_recv().is_ok() {}
        Ok(())
    }

    fn set_gain(&mut self, gain: Gain) -> Result<(), RadioError> {
        match gain {
            Gain::Auto => {
                self.lib.check("LNA AGC", unsafe { (self.lib.set_lna_agc)(self.dev, 1) })?;
                self.lib.check("mixer AGC", unsafe { (self.lib.set_mixer_agc)(self.dev, 1) })
            }
            Gain::Linearity(v) => self.lib.check("linearity gain", unsafe { (self.lib.set_linearity_gain)(self.dev, v.min(21)) }),
            Gain::Sensitivity(v) => self.lib.check("sensitivity gain", unsafe { (self.lib.set_sensitivity_gain)(self.dev, v.min(21)) }),
        }
    }

    fn read(&mut self, out: &mut Vec<Complex32>, timeout: Duration) -> Result<usize, RadioError> {
        out.clear();
        if self.shared.is_null() {
            return Err(RadioError("airspy: read before tuning".into()));
        }
        match self.rx.recv_timeout(timeout) {
            Ok(block) => {
                *out = block;
                // Take what else is waiting too, so the reader keeps up.
                while out.len() < 1 << 18 {
                    match self.rx.try_recv() {
                        Ok(b) => out.extend_from_slice(&b),
                        Err(_) => break,
                    }
                }
                Ok(out.len())
            }
            Err(RecvTimeoutError::Timeout) => Ok(0),
            Err(RecvTimeoutError::Disconnected) => Err(RadioError("airspy: stopped streaming".into())),
        }
    }

    fn dropped(&self) -> u64 {
        Airspy::dropped(self)
    }
}

impl Drop for Airspy {
    fn drop(&mut self) {
        unsafe {
            if !self.shared.is_null() {
                (self.lib.stop_rx)(self.dev);
                drop(Box::from_raw(self.shared as *mut Shared));
            }
            (self.lib.close)(self.dev);
        }
    }
}
