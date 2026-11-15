//! Airspy (R2, Mini) through libairspy, and HydraSDR (RFOne) through
//! libhydrasdr: the same design (libhydrasdr grew from libairspy), so one
//! driver with the library's prefix. The library turns the ADC's real
//! samples into complex float I/Q (no DC spike) and calls back on its own
//! thread with each block.

use std::ffi::{CStr, c_char, c_int, c_void};
use std::sync::mpsc::Receiver;
use std::time::Duration;

use libloading::Library;
use num_complex::Complex32;

use crate::{Gain, Radio, RadioError, Stream};

/// One of the two libraries.
#[derive(Clone, Copy, Debug)]
pub struct Family {
    pub kind: &'static str,
    prefix: &'static str,
    names: &'static [&'static str],
    install: &'static str,
    /// set_freq takes a uint64_t (libhydrasdr), not a uint32_t (libairspy).
    freq64: bool,
}

pub const AIRSPY: Family = Family {
    kind: "airspy",
    prefix: "airspy",
    names: if cfg!(target_os = "macos") {
        &["libairspy.0.dylib", "libairspy.dylib", "/opt/homebrew/lib/libairspy.dylib", "/usr/local/lib/libairspy.dylib"]
    } else if cfg!(windows) {
        &["airspy.dll"]
    } else {
        &["libairspy.so.0", "libairspy.so", "/usr/local/lib/libairspy.so.0"]
    },
    install: "Debian/Ubuntu: apt install libairspy0; macOS: brew install airspy",
    freq64: false,
};

pub const HYDRASDR: Family = Family {
    kind: "hydrasdr",
    prefix: "hydrasdr",
    names: if cfg!(target_os = "macos") {
        &["libhydrasdr.1.dylib", "libhydrasdr.0.dylib", "libhydrasdr.dylib", "/opt/homebrew/lib/libhydrasdr.dylib", "/usr/local/lib/libhydrasdr.dylib"]
    } else if cfg!(windows) {
        &["hydrasdr.dll"]
    } else {
        &["libhydrasdr.so.1", "libhydrasdr.so.0", "libhydrasdr.so", "/usr/local/lib/libhydrasdr.so.1"]
    },
    install: "Debian/Ubuntu: apt install libhydrasdr1 (or build github.com/hydrasdr/hydrasdr-host)",
    freq64: true,
};

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

type Callback = extern "C" fn(*mut Transfer) -> c_int;
type SetU8 = unsafe extern "C" fn(*mut c_void, u8) -> c_int;

enum SetFreq {
    U32(unsafe extern "C" fn(*mut c_void, u32) -> c_int),
    U64(unsafe extern "C" fn(*mut c_void, u64) -> c_int),
}

/// The library's functions used.
struct Lib {
    _lib: Library,
    fam: Family,
    list_devices: unsafe extern "C" fn(*mut u64, c_int) -> c_int,
    open: unsafe extern "C" fn(*mut *mut c_void) -> c_int,
    open_sn: unsafe extern "C" fn(*mut *mut c_void, u64) -> c_int,
    close: unsafe extern "C" fn(*mut c_void) -> c_int,
    get_samplerates: unsafe extern "C" fn(*mut c_void, *mut u32, u32) -> c_int,
    set_samplerate: unsafe extern "C" fn(*mut c_void, u32) -> c_int,
    set_sample_type: unsafe extern "C" fn(*mut c_void, c_int) -> c_int,
    set_freq: SetFreq,
    set_linearity_gain: SetU8,
    set_sensitivity_gain: SetU8,
    set_lna_agc: SetU8,
    set_mixer_agc: SetU8,
    start_rx: unsafe extern "C" fn(*mut c_void, Callback, *mut c_void) -> c_int,
    stop_rx: unsafe extern "C" fn(*mut c_void) -> c_int,
    error_name: unsafe extern "C" fn(c_int) -> *const c_char,
}

impl Lib {
    fn load(fam: Family) -> Result<Lib, RadioError> {
        let lib = crate::load(fam.names, &format!("lib{}", fam.prefix), fam.install)?;
        macro_rules! f {
            ($name:literal) => {{
                let sym = format!("{}_{}\0", fam.prefix, $name);
                *unsafe { lib.get(sym.as_bytes()) }.map_err(|e| RadioError(format!("lib{}: {}_{}: {e}", fam.prefix, fam.prefix, $name)))?
            }};
        }
        let set_freq = if fam.freq64 { SetFreq::U64(f!("set_freq")) } else { SetFreq::U32(f!("set_freq")) };
        Ok(Lib {
            list_devices: f!("list_devices"),
            open: f!("open"),
            open_sn: f!("open_sn"),
            close: f!("close"),
            get_samplerates: f!("get_samplerates"),
            set_samplerate: f!("set_samplerate"),
            set_sample_type: f!("set_sample_type"),
            set_freq,
            set_linearity_gain: f!("set_linearity_gain"),
            set_sensitivity_gain: f!("set_sensitivity_gain"),
            set_lna_agc: f!("set_lna_agc"),
            set_mixer_agc: f!("set_mixer_agc"),
            start_rx: f!("start_rx"),
            stop_rx: f!("stop_rx"),
            error_name: f!("error_name"),
            fam,
            _lib: lib,
        })
    }

    fn check(&self, what: &str, r: c_int) -> Result<(), RadioError> {
        if r == 0 {
            return Ok(());
        }
        let name = unsafe { (self.error_name)(r) };
        let name = if name.is_null() { format!("error {r}") } else { unsafe { CStr::from_ptr(name) }.to_string_lossy().into_owned() };
        Err(RadioError(format!("{}: {what}: {name}", self.fam.kind)))
    }
}

extern "C" fn on_samples(t: *mut Transfer) -> c_int {
    // SAFETY: the library passes a valid transfer for the call's duration;
    // ctx is the Stream given to start_rx, alive until after stop_rx.
    let t = unsafe { &*t };
    let stream = unsafe { &*(t.ctx as *const Stream) };
    if t.sample_type != SAMPLE_FLOAT32_IQ || t.samples.is_null() || t.sample_count <= 0 {
        return 0;
    }
    let samples = unsafe { std::slice::from_raw_parts(t.samples as *const Complex32, t.sample_count as usize) };
    stream.send(samples.to_vec());
    0
}

/// Serial numbers of the radios of this family plugged in.
pub fn list(fam: Family) -> Result<Vec<u64>, RadioError> {
    let lib = Lib::load(fam)?;
    let mut serials = [0u64; 16];
    let n = unsafe { (lib.list_devices)(serials.as_mut_ptr(), serials.len() as c_int) };
    if n < 0 {
        lib.check("listing devices", n)?;
    }
    Ok(serials[..n.max(0) as usize].to_vec())
}

pub struct Spy {
    lib: Lib,
    dev: *mut c_void,
    rate: f64,
    center: f64,
    stream: *const Stream,
    rx: Option<Receiver<Vec<Complex32>>>,
}

// The device handle is only used from the thread that owns the radio.
unsafe impl Send for Spy {}

impl Spy {
    /// Open the radio with `serial` (or the first), at the highest sample
    /// rate it offers no higher than `max_rate_hz`.
    pub fn open(fam: Family, serial: Option<u64>, max_rate_hz: f64) -> Result<Spy, RadioError> {
        let lib = Lib::load(fam)?;
        let mut dev: *mut c_void = std::ptr::null_mut();
        let r = unsafe {
            match serial {
                Some(s) => (lib.open_sn)(&mut dev, s),
                None => (lib.open)(&mut dev),
            }
        };
        lib.check("opening (is it plugged in, and not in use by another program?)", r)?;
        let mut a = Spy { lib, dev, rate: 0.0, center: 0.0, stream: std::ptr::null(), rx: None };
        let mut count = 0u32;
        a.lib.check("sample rates", unsafe { (a.lib.get_samplerates)(a.dev, &mut count, 0) })?;
        let mut rates = vec![0u32; count as usize];
        a.lib.check("sample rates", unsafe { (a.lib.get_samplerates)(a.dev, rates.as_mut_ptr(), count) })?;
        let rate = rates
            .iter()
            .copied()
            .filter(|&r| r as f64 <= max_rate_hz)
            .max()
            .ok_or_else(|| RadioError(format!("{}: no sample rate at or under {max_rate_hz} (it offers {rates:?})", fam.kind)))?;
        a.lib.check("sample rate", unsafe { (a.lib.set_samplerate)(a.dev, rate) })?;
        a.rate = rate as f64;
        a.lib.check("sample type", unsafe { (a.lib.set_sample_type)(a.dev, SAMPLE_FLOAT32_IQ) })?;
        a.set_gain(Gain::Linearity(14))?;
        Ok(a)
    }

    fn start(&mut self) -> Result<(), RadioError> {
        let (stream, rx) = Stream::new();
        let stream = Box::into_raw(Box::new(stream));
        let r = unsafe { (self.lib.start_rx)(self.dev, on_samples, stream as *mut c_void) };
        if r != 0 {
            drop(unsafe { Box::from_raw(stream) });
            return self.lib.check("starting", r);
        }
        self.stream = stream;
        self.rx = Some(rx);
        Ok(())
    }
}

impl Radio for Spy {
    fn kind(&self) -> &'static str {
        self.lib.fam.kind
    }

    fn rate_hz(&self) -> f64 {
        self.rate
    }

    fn center_hz(&self) -> f64 {
        self.center
    }

    fn tune(&mut self, hz: f64) -> Result<(), RadioError> {
        let r = unsafe {
            match self.lib.set_freq {
                SetFreq::U32(f) => f(self.dev, hz.round() as u32),
                SetFreq::U64(f) => f(self.dev, hz.round() as u64),
            }
        };
        self.lib.check("tuning", r)?;
        self.center = hz;
        if self.stream.is_null() {
            self.start()?;
        }
        if let Some(rx) = &self.rx {
            crate::drain(rx);
        }
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
            Gain::Db(d) => self.set_gain(self.gain_from(d)),
        }
    }

    fn read(&mut self, out: &mut Vec<Complex32>, timeout: Duration) -> Result<usize, RadioError> {
        match &self.rx {
            Some(rx) => crate::take(rx, out, timeout, self.lib.fam.kind),
            None => Err(RadioError(format!("{}: read before tuning", self.lib.fam.kind))),
        }
    }

    fn dropped(&self) -> u64 {
        if self.stream.is_null() { 0 } else { unsafe { &*self.stream }.dropped() }
    }

    /// A linearity preset (0-21).
    fn gain_from(&self, g: f32) -> Gain {
        Gain::Linearity(g.round().clamp(0.0, 21.0) as u8)
    }

    fn sweep_gains(&self) -> Vec<f32> {
        vec![6.0, 9.0, 12.0, 15.0, 18.0]
    }

    fn gain_settings(&self) -> Vec<String> {
        vec![
            "--gain 0-21: linearity preset (default 14); --gain auto: LNA and mixer AGC".into(),
            "--sensitivity 0-21: sensitivity preset instead".into(),
            format!("sample rate {:.1} MS/s (--rate: the highest at or under it)", self.rate / 1e6),
        ]
    }

    fn dc_spike(&self) -> bool {
        false
    }
}

impl Drop for Spy {
    fn drop(&mut self) {
        unsafe {
            if !self.stream.is_null() {
                (self.lib.stop_rx)(self.dev);
                drop(Box::from_raw(self.stream as *mut Stream));
            }
            (self.lib.close)(self.dev);
        }
    }
}
