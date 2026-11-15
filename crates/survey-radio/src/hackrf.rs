//! HackRF One through libhackrf: signed 8-bit I/Q, called back on
//! libhackrf's thread. A strong DC spike (the LO leaking through) is taken
//! off. Gain: the LNA (IF) gain, 0-40 dB in 8 dB steps, is `--gain`; the
//! baseband (VGA) gain, 0-62 dB in 2 dB steps, and the 14 dB RF amplifier
//! are set at opening.

use std::ffi::{CStr, CString, c_char, c_int, c_void};
use std::sync::Mutex;
use std::sync::mpsc::Receiver;
use std::time::Duration;

use libloading::Library;
use num_complex::Complex32;

use crate::{DcBlock, Gain, Radio, RadioError, Stream};

const NAMES: &[&str] = if cfg!(target_os = "macos") {
    &["libhackrf.0.dylib", "libhackrf.dylib", "/opt/homebrew/lib/libhackrf.dylib", "/usr/local/lib/libhackrf.dylib"]
} else if cfg!(windows) {
    &["hackrf.dll"]
} else {
    &["libhackrf.so.0", "libhackrf.so", "/usr/local/lib/libhackrf.so.0", "/usr/local/lib/libhackrf.so"]
};

#[repr(C)]
struct Transfer {
    device: *mut c_void,
    buffer: *mut u8,
    buffer_length: c_int,
    valid_length: c_int,
    rx_ctx: *mut c_void,
    tx_ctx: *mut c_void,
}

#[repr(C)]
struct DeviceList {
    serial_numbers: *mut *mut c_char,
    usb_board_ids: *mut c_int,
    usb_device_index: *mut c_int,
    devicecount: c_int,
    usb_devices: *mut *mut c_void,
    usb_devicecount: c_int,
}

type Callback = extern "C" fn(*mut Transfer) -> c_int;

struct Lib {
    _lib: Library,
    init: unsafe extern "C" fn() -> c_int,
    exit: unsafe extern "C" fn() -> c_int,
    device_list: unsafe extern "C" fn() -> *mut DeviceList,
    device_list_free: unsafe extern "C" fn(*mut DeviceList),
    open: unsafe extern "C" fn(*mut *mut c_void) -> c_int,
    open_by_serial: unsafe extern "C" fn(*const c_char, *mut *mut c_void) -> c_int,
    close: unsafe extern "C" fn(*mut c_void) -> c_int,
    set_sample_rate: unsafe extern "C" fn(*mut c_void, f64) -> c_int,
    compute_baseband_filter_bw: unsafe extern "C" fn(u32) -> u32,
    set_baseband_filter_bandwidth: unsafe extern "C" fn(*mut c_void, u32) -> c_int,
    set_freq: unsafe extern "C" fn(*mut c_void, u64) -> c_int,
    set_lna_gain: unsafe extern "C" fn(*mut c_void, u32) -> c_int,
    set_vga_gain: unsafe extern "C" fn(*mut c_void, u32) -> c_int,
    set_amp_enable: unsafe extern "C" fn(*mut c_void, u8) -> c_int,
    start_rx: unsafe extern "C" fn(*mut c_void, Callback, *mut c_void) -> c_int,
    stop_rx: unsafe extern "C" fn(*mut c_void) -> c_int,
    error_name: unsafe extern "C" fn(c_int) -> *const c_char,
}

impl Lib {
    fn load() -> Result<Lib, RadioError> {
        let lib = crate::load(NAMES, "libhackrf", "Debian/Ubuntu: apt install libhackrf0; macOS: brew install hackrf")?;
        macro_rules! f {
            ($name:literal) => {
                *unsafe { lib.get(concat!("hackrf_", $name, "\0").as_bytes()) }.map_err(|e| RadioError(format!("libhackrf: hackrf_{}: {e}", $name)))?
            };
        }
        let l = Lib {
            init: f!("init"),
            exit: f!("exit"),
            device_list: f!("device_list"),
            device_list_free: f!("device_list_free"),
            open: f!("open"),
            open_by_serial: f!("open_by_serial"),
            close: f!("close"),
            set_sample_rate: f!("set_sample_rate"),
            compute_baseband_filter_bw: f!("compute_baseband_filter_bw"),
            set_baseband_filter_bandwidth: f!("set_baseband_filter_bandwidth"),
            set_freq: f!("set_freq"),
            set_lna_gain: f!("set_lna_gain"),
            set_vga_gain: f!("set_vga_gain"),
            set_amp_enable: f!("set_amp_enable"),
            start_rx: f!("start_rx"),
            stop_rx: f!("stop_rx"),
            error_name: f!("error_name"),
            _lib: lib,
        };
        l.check("init", unsafe { (l.init)() })?;
        Ok(l)
    }

    fn check(&self, what: &str, r: c_int) -> Result<(), RadioError> {
        if r == 0 {
            return Ok(());
        }
        let name = unsafe { (self.error_name)(r) };
        let name = if name.is_null() { format!("error {r}") } else { unsafe { CStr::from_ptr(name) }.to_string_lossy().into_owned() };
        Err(RadioError(format!("hackrf: {what}: {name}")))
    }
}

impl Drop for Lib {
    fn drop(&mut self) {
        unsafe { (self.exit)() };
    }
}

/// Serial numbers of the HackRFs plugged in.
pub fn list() -> Result<Vec<String>, RadioError> {
    let lib = Lib::load()?;
    let l = unsafe { (lib.device_list)() };
    if l.is_null() {
        return Ok(Vec::new());
    }
    let d = unsafe { &*l };
    let serials = (0..d.devicecount.max(0) as usize)
        .map(|i| {
            let p = unsafe { *d.serial_numbers.add(i) };
            if p.is_null() { format!("#{i}") } else { unsafe { CStr::from_ptr(p) }.to_string_lossy().trim_start_matches('0').to_string() }
        })
        .collect();
    unsafe { (lib.device_list_free)(l) };
    Ok(serials)
}

struct Ctx {
    stream: Stream,
    dc: Mutex<DcBlock>,
}

extern "C" fn on_samples(t: *mut Transfer) -> c_int {
    // SAFETY: libhackrf passes a valid transfer for the call; rx_ctx is our
    // Ctx, alive until after stop_rx.
    let t = unsafe { &*t };
    let ctx = unsafe { &*(t.rx_ctx as *const Ctx) };
    if t.buffer.is_null() || t.valid_length <= 0 {
        return 0;
    }
    let bytes = unsafe { std::slice::from_raw_parts(t.buffer as *const i8, t.valid_length as usize) };
    let mut dc = ctx.dc.lock().unwrap();
    let block = bytes.as_chunks::<2>().0.iter().map(|c| dc.apply(Complex32::new(c[0] as f32 / 128.0, c[1] as f32 / 128.0))).collect();
    ctx.stream.send(block);
    0
}

pub struct HackRf {
    lib: Lib,
    dev: *mut c_void,
    rate: f64,
    center: f64,
    ctx: *const Ctx,
    rx: Option<Receiver<Vec<Complex32>>>,
}

unsafe impl Send for HackRf {}

impl HackRf {
    /// Open the HackRF with `serial` (or the first) at `rate_hz` (2-20 MS/s),
    /// with this baseband gain and the RF amplifier on or off.
    pub fn open(serial: Option<&str>, rate_hz: f64, vga_db: u32, amp: bool) -> Result<HackRf, RadioError> {
        let lib = Lib::load()?;
        let mut dev: *mut c_void = std::ptr::null_mut();
        let r = match serial {
            Some(s) => {
                let c = CString::new(s).map_err(|_| RadioError("hackrf: serial?".into()))?;
                unsafe { (lib.open_by_serial)(c.as_ptr(), &mut dev) }
            }
            None => unsafe { (lib.open)(&mut dev) },
        };
        lib.check("opening (is it plugged in, and not in use by another program?)", r)?;
        let h = HackRf { lib, dev, rate: rate_hz, center: 0.0, ctx: std::ptr::null(), rx: None };
        h.lib.check("sample rate", unsafe { (h.lib.set_sample_rate)(h.dev, rate_hz) })?;
        let bw = unsafe { (h.lib.compute_baseband_filter_bw)((rate_hz * 0.75) as u32) };
        h.lib.check("baseband filter", unsafe { (h.lib.set_baseband_filter_bandwidth)(h.dev, bw) })?;
        h.lib.check("VGA gain", unsafe { (h.lib.set_vga_gain)(h.dev, vga_db.min(62) & !1) })?;
        h.lib.check("amplifier", unsafe { (h.lib.set_amp_enable)(h.dev, amp as u8) })?;
        h.lib.check("LNA gain", unsafe { (h.lib.set_lna_gain)(h.dev, 16) })?;
        Ok(h)
    }

    fn start(&mut self) -> Result<(), RadioError> {
        let (stream, rx) = Stream::new();
        let ctx = Box::into_raw(Box::new(Ctx { stream, dc: Default::default() }));
        let r = unsafe { (self.lib.start_rx)(self.dev, on_samples, ctx as *mut c_void) };
        if r != 0 {
            drop(unsafe { Box::from_raw(ctx) });
            return self.lib.check("starting", r);
        }
        self.ctx = ctx;
        self.rx = Some(rx);
        Ok(())
    }
}

impl Radio for HackRf {
    fn kind(&self) -> &'static str {
        "hackrf"
    }

    fn rate_hz(&self) -> f64 {
        self.rate
    }

    fn center_hz(&self) -> f64 {
        self.center
    }

    fn tune(&mut self, hz: f64) -> Result<(), RadioError> {
        self.lib.check("tuning", unsafe { (self.lib.set_freq)(self.dev, hz.round() as u64) })?;
        self.center = hz;
        if self.ctx.is_null() {
            self.start()?;
        }
        if let Some(rx) = &self.rx {
            crate::drain(rx);
        }
        Ok(())
    }

    fn set_gain(&mut self, gain: Gain) -> Result<(), RadioError> {
        match gain {
            // No AGC: the middle of the LNA's range.
            Gain::Auto => self.set_gain(Gain::Db(16.0)),
            Gain::Db(db) => {
                let lna = ((db.clamp(0.0, 40.0) / 8.0).round() * 8.0) as u32;
                self.lib.check("LNA gain", unsafe { (self.lib.set_lna_gain)(self.dev, lna) })
            }
            Gain::Linearity(v) | Gain::Sensitivity(v) => self.set_gain(Gain::Db(v as f32)),
        }
    }

    fn read(&mut self, out: &mut Vec<Complex32>, timeout: Duration) -> Result<usize, RadioError> {
        match &self.rx {
            Some(rx) => crate::take(rx, out, timeout, "hackrf"),
            None => Err(RadioError("hackrf: read before tuning".into())),
        }
    }

    fn dropped(&self) -> u64 {
        if self.ctx.is_null() { 0 } else { unsafe { &*self.ctx }.stream.dropped() }
    }

    /// LNA (IF) gain, dB: 0-40 in 8 dB steps.
    fn gain_from(&self, g: f32) -> Gain {
        Gain::Db(g)
    }

    fn sweep_gains(&self) -> Vec<f32> {
        vec![0.0, 8.0, 16.0, 24.0, 32.0, 40.0]
    }

    fn dc_spike(&self) -> bool {
        true
    }
}

impl Drop for HackRf {
    fn drop(&mut self) {
        unsafe {
            if !self.ctx.is_null() {
                (self.lib.stop_rx)(self.dev);
                drop(Box::from_raw(self.ctx as *mut Ctx));
            }
            (self.lib.close)(self.dev);
        }
    }
}
