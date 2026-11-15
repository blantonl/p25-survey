//! RTL-SDR through librtlsdr. Its async read blocks, so it runs on a thread
//! of ours; the callback turns each buffer of unsigned 8-bit I/Q into
//! complex floats (less the DC offset: the RTL2832's LO leaks through as a
//! spike at the centre) and queues it. Retuning from the reader's thread
//! while the read runs is how librtlsdr is used everywhere.

use std::ffi::{CStr, c_char, c_int, c_uchar, c_void};
use std::sync::Arc;
use std::sync::mpsc::Receiver;
use std::thread::JoinHandle;
use std::time::Duration;

use libloading::Library;
use num_complex::Complex32;

use crate::{DcBlock, Gain, Radio, RadioError, Stream};

const NAMES: &[&str] = if cfg!(target_os = "macos") {
    &["librtlsdr.2.dylib", "librtlsdr.0.dylib", "librtlsdr.dylib", "/opt/homebrew/lib/librtlsdr.dylib", "/usr/local/lib/librtlsdr.dylib"]
} else if cfg!(windows) {
    &["rtlsdr.dll"]
} else {
    &["librtlsdr.so.2", "librtlsdr.so.0", "librtlsdr.so", "/usr/local/lib/librtlsdr.so.0"]
};

/// Buffers librtlsdr cycles through, and their size (16 KiB multiples).
const BUF_NUM: u32 = 15;
const BUF_LEN: u32 = 16 * 16384;

type Callback = extern "C" fn(*mut c_uchar, u32, *mut c_void);

struct Lib {
    _lib: Library,
    get_device_count: unsafe extern "C" fn() -> u32,
    get_device_name: unsafe extern "C" fn(u32) -> *const c_char,
    get_device_usb_strings: unsafe extern "C" fn(u32, *mut c_char, *mut c_char, *mut c_char) -> c_int,
    get_index_by_serial: unsafe extern "C" fn(*const c_char) -> c_int,
    open: unsafe extern "C" fn(*mut *mut c_void, u32) -> c_int,
    close: unsafe extern "C" fn(*mut c_void) -> c_int,
    set_center_freq: unsafe extern "C" fn(*mut c_void, u32) -> c_int,
    set_sample_rate: unsafe extern "C" fn(*mut c_void, u32) -> c_int,
    get_tuner_gains: unsafe extern "C" fn(*mut c_void, *mut c_int) -> c_int,
    set_tuner_gain_mode: unsafe extern "C" fn(*mut c_void, c_int) -> c_int,
    set_tuner_gain: unsafe extern "C" fn(*mut c_void, c_int) -> c_int,
    reset_buffer: unsafe extern "C" fn(*mut c_void) -> c_int,
    read_async: unsafe extern "C" fn(*mut c_void, Callback, *mut c_void, u32, u32) -> c_int,
    cancel_async: unsafe extern "C" fn(*mut c_void) -> c_int,
}

impl Lib {
    fn load() -> Result<Lib, RadioError> {
        let lib = crate::load(NAMES, "librtlsdr", "Debian/Ubuntu: apt install librtlsdr0 (or librtlsdr2); macOS: brew install librtlsdr")?;
        macro_rules! f {
            ($name:literal) => {
                *unsafe { lib.get(concat!("rtlsdr_", $name, "\0").as_bytes()) }.map_err(|e| RadioError(format!("librtlsdr: rtlsdr_{}: {e}", $name)))?
            };
        }
        Ok(Lib {
            get_device_count: f!("get_device_count"),
            get_device_name: f!("get_device_name"),
            get_device_usb_strings: f!("get_device_usb_strings"),
            get_index_by_serial: f!("get_index_by_serial"),
            open: f!("open"),
            close: f!("close"),
            set_center_freq: f!("set_center_freq"),
            set_sample_rate: f!("set_sample_rate"),
            get_tuner_gains: f!("get_tuner_gains"),
            set_tuner_gain_mode: f!("set_tuner_gain_mode"),
            set_tuner_gain: f!("set_tuner_gain"),
            reset_buffer: f!("reset_buffer"),
            read_async: f!("read_async"),
            cancel_async: f!("cancel_async"),
            _lib: lib,
        })
    }
}

fn check(what: &str, r: c_int) -> Result<(), RadioError> {
    if r < 0 { Err(RadioError(format!("rtlsdr: {what}: error {r}"))) } else { Ok(()) }
}

/// (serial, name) of each RTL-SDR plugged in.
pub fn list() -> Result<Vec<(String, String)>, RadioError> {
    let lib = Lib::load()?;
    let n = unsafe { (lib.get_device_count)() };
    Ok((0..n)
        .map(|i| {
            let (mut m, mut p, mut s) = ([0 as c_char; 256], [0 as c_char; 256], [0 as c_char; 256]);
            unsafe { (lib.get_device_usb_strings)(i, m.as_mut_ptr(), p.as_mut_ptr(), s.as_mut_ptr()) };
            let serial = unsafe { CStr::from_ptr(s.as_ptr()) }.to_string_lossy().into_owned();
            let name = unsafe { CStr::from_ptr((lib.get_device_name)(i)) }.to_string_lossy().into_owned();
            (if serial.is_empty() { i.to_string() } else { serial }, name)
        })
        .collect())
}

/// What the read thread's callback needs.
struct Ctx {
    stream: Stream,
    dc: std::sync::Mutex<DcBlock>,
}

extern "C" fn on_samples(buf: *mut c_uchar, len: u32, ctx: *mut c_void) {
    // SAFETY: librtlsdr passes its buffer for the call; ctx is our Ctx,
    // alive until the read thread is joined.
    let ctx = unsafe { &*(ctx as *const Ctx) };
    let bytes = unsafe { std::slice::from_raw_parts(buf, len as usize) };
    let mut dc = ctx.dc.lock().unwrap();
    let block = bytes.as_chunks::<2>().0.iter().map(|c| dc.apply(Complex32::new((c[0] as f32 - 127.5) / 127.5, (c[1] as f32 - 127.5) / 127.5))).collect();
    ctx.stream.send(block);
}

struct Dev(*mut c_void);
// librtlsdr's handle: the read thread reads, ours tunes; librtlsdr allows that.
unsafe impl Send for Dev {}
unsafe impl Sync for Dev {}

pub struct Rtl {
    lib: Arc<Lib>,
    dev: Arc<Dev>,
    rate: f64,
    center: f64,
    /// Tuner gains, tenths of a dB.
    gains: Vec<c_int>,
    ctx: Option<Arc<Ctx>>,
    rx: Option<Receiver<Vec<Complex32>>>,
    thread: Option<JoinHandle<()>>,
}

unsafe impl Send for Lib {}
unsafe impl Sync for Lib {}

impl Rtl {
    /// Open the RTL-SDR with `serial` (or index, or the first) at `rate_hz`.
    pub fn open(serial: Option<&str>, rate_hz: f64) -> Result<Rtl, RadioError> {
        let lib = Lib::load()?;
        let index = match serial {
            None => 0,
            Some(s) => {
                let c = std::ffi::CString::new(s).map_err(|_| RadioError("rtlsdr: serial?".into()))?;
                let i = unsafe { (lib.get_index_by_serial)(c.as_ptr()) };
                if i >= 0 { i as u32 } else { s.parse().map_err(|_| RadioError(format!("rtlsdr: no device with serial {s}")))? }
            }
        };
        if unsafe { (lib.get_device_count)() } == 0 {
            return Err(RadioError("rtlsdr: none plugged in".into()));
        }
        let mut dev: *mut c_void = std::ptr::null_mut();
        check("opening (in use by another program, or the DVB driver has it?)", unsafe { (lib.open)(&mut dev, index) })?;
        let mut r = Rtl { lib: Arc::new(lib), dev: Arc::new(Dev(dev)), rate: rate_hz, center: 0.0, gains: Vec::new(), ctx: None, rx: None, thread: None };
        check("sample rate", unsafe { (r.lib.set_sample_rate)(r.dev.0, rate_hz.round() as u32) })?;
        let n = unsafe { (r.lib.get_tuner_gains)(r.dev.0, std::ptr::null_mut()) };
        if n > 0 {
            r.gains = vec![0; n as usize];
            unsafe { (r.lib.get_tuner_gains)(r.dev.0, r.gains.as_mut_ptr()) };
        }
        Ok(r)
    }

    fn start(&mut self) -> Result<(), RadioError> {
        check("reset", unsafe { (self.lib.reset_buffer)(self.dev.0) })?;
        let (stream, rx) = Stream::new();
        let ctx = Arc::new(Ctx { stream, dc: Default::default() });
        let (lib, dev, c) = (self.lib.clone(), self.dev.clone(), ctx.clone());
        self.thread = Some(
            std::thread::Builder::new()
                .name("rtlsdr".into())
                .spawn(move || {
                    let r = unsafe { (lib.read_async)(dev.0, on_samples, Arc::as_ptr(&c) as *mut c_void, BUF_NUM, BUF_LEN) };
                    if r < 0 {
                        eprintln!("rtlsdr: reading stopped: error {r}");
                    }
                })
                .map_err(|e| RadioError(format!("rtlsdr: {e}")))?,
        );
        self.ctx = Some(ctx);
        self.rx = Some(rx);
        Ok(())
    }
}

impl Radio for Rtl {
    fn kind(&self) -> &'static str {
        "rtlsdr"
    }

    fn rate_hz(&self) -> f64 {
        self.rate
    }

    fn center_hz(&self) -> f64 {
        self.center
    }

    fn tune(&mut self, hz: f64) -> Result<(), RadioError> {
        check("tuning", unsafe { (self.lib.set_center_freq)(self.dev.0, hz.round() as u32) })?;
        self.center = hz;
        if self.thread.is_none() {
            self.start()?;
        }
        if let Some(rx) = &self.rx {
            crate::drain(rx);
        }
        Ok(())
    }

    fn set_gain(&mut self, gain: Gain) -> Result<(), RadioError> {
        match gain {
            Gain::Auto => check("AGC", unsafe { (self.lib.set_tuner_gain_mode)(self.dev.0, 0) }),
            Gain::Db(db) => {
                check("manual gain", unsafe { (self.lib.set_tuner_gain_mode)(self.dev.0, 1) })?;
                let tenths = (db * 10.0).round() as c_int;
                let g = self.gains.iter().copied().min_by_key(|g| (g - tenths).abs()).unwrap_or(tenths);
                check("gain", unsafe { (self.lib.set_tuner_gain)(self.dev.0, g) })
            }
            Gain::Linearity(v) | Gain::Sensitivity(v) => self.set_gain(Gain::Db(v as f32)),
        }
    }

    fn read(&mut self, out: &mut Vec<Complex32>, timeout: Duration) -> Result<usize, RadioError> {
        match &self.rx {
            Some(rx) => crate::take(rx, out, timeout, "rtlsdr"),
            None => Err(RadioError("rtlsdr: read before tuning".into())),
        }
    }

    fn dropped(&self) -> u64 {
        self.ctx.as_ref().map_or(0, |c| c.stream.dropped())
    }

    /// Tuner gain, dB (the nearest the tuner has).
    fn gain_from(&self, g: f32) -> Gain {
        Gain::Db(g)
    }

    fn sweep_gains(&self) -> Vec<f32> {
        vec![20.0, 28.0, 34.0, 39.0, 44.0, 49.6]
    }

    fn gain_settings(&self) -> Vec<String> {
        let gains: Vec<String> = self.gains.iter().map(|g| format!("{}", *g as f32 / 10.0)).collect();
        vec![
            format!("--gain dB: tuner gain, the nearest of {}", gains.join(", ")),
            "--gain auto: the tuner's AGC (the default)".into(),
            format!("sample rate {:.2} MS/s (--rate)", self.rate / 1e6),
        ]
    }

    fn dc_spike(&self) -> bool {
        true
    }
}

impl Drop for Rtl {
    fn drop(&mut self) {
        if let Some(t) = self.thread.take() {
            unsafe { (self.lib.cancel_async)(self.dev.0) };
            let _ = t.join();
        }
        unsafe { (self.lib.close)(self.dev.0) };
    }
}
