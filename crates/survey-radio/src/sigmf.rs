//! SigMF recordings: `<name>.sigmf-meta` (JSON) beside `<name>.sigmf-data`
//! (interleaved I/Q). One capture segment, complex samples.

use std::fs::File;
use std::io::{self, BufReader, Read};
use std::path::{Path, PathBuf};

use num_complex::Complex32;
use serde::Deserialize;

/// The sample formats read.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Datatype {
    /// Complex int16, little-endian (Airspy, HackRF converted).
    Ci16Le,
    /// Complex uint8 (RTL-SDR).
    Cu8,
    /// Complex float32, little-endian.
    Cf32Le,
}

impl Datatype {
    fn parse(s: &str) -> Option<Datatype> {
        match s {
            "ci16_le" => Some(Datatype::Ci16Le),
            "cu8" => Some(Datatype::Cu8),
            "cf32_le" => Some(Datatype::Cf32Le),
            _ => None,
        }
    }

    /// Bytes per complex sample.
    pub fn sample_bytes(self) -> usize {
        match self {
            Datatype::Ci16Le => 4,
            Datatype::Cu8 => 2,
            Datatype::Cf32Le => 8,
        }
    }

    /// The SigMF name.
    pub fn as_str(self) -> &'static str {
        match self {
            Datatype::Ci16Le => "ci16_le",
            Datatype::Cu8 => "cu8",
            Datatype::Cf32Le => "cf32_le",
        }
    }
}

#[derive(Deserialize)]
struct Meta {
    global: Global,
    captures: Vec<Capture>,
}

#[derive(Deserialize)]
struct Global {
    #[serde(rename = "core:datatype")]
    datatype: String,
    #[serde(rename = "core:sample_rate")]
    sample_rate: f64,
    #[serde(rename = "core:description", default)]
    description: Option<String>,
    #[serde(rename = "core:hw", default)]
    hw: Option<String>,
}

#[derive(Deserialize)]
struct Capture {
    #[serde(rename = "core:frequency")]
    frequency: f64,
    #[serde(rename = "core:datetime", default)]
    datetime: Option<String>,
}

/// A recording, opened by its metadata.
#[derive(Clone, Debug)]
pub struct Recording {
    pub data: PathBuf,
    pub datatype: Datatype,
    pub rate_hz: f64,
    pub center_hz: f64,
    /// Complex samples in the data file.
    pub samples: u64,
    pub datetime: Option<String>,
    pub description: Option<String>,
    pub hw: Option<String>,
}

fn invalid(path: &Path, why: impl std::fmt::Display) -> io::Error {
    io::Error::new(io::ErrorKind::InvalidData, format!("{}: {why}", path.display()))
}

impl Recording {
    /// Open by any of `<name>`, `<name>.sigmf-meta` or `<name>.sigmf-data`.
    pub fn open(path: impl AsRef<Path>) -> io::Result<Recording> {
        let p = path.as_ref();
        let base = match p.extension().and_then(|e| e.to_str()) {
            Some("sigmf-meta" | "sigmf-data") => p.with_extension(""),
            _ => p.to_path_buf(),
        };
        // Appended, not with_extension: names like 800-a-853.4 have dots.
        let with = |ext: &str| PathBuf::from(format!("{}.{ext}", base.display()));
        let meta_path = with("sigmf-meta");
        let data = with("sigmf-data");
        let meta: Meta = serde_json::from_reader(BufReader::new(File::open(&meta_path)?)).map_err(|e| invalid(&meta_path, e))?;
        let datatype = Datatype::parse(&meta.global.datatype).ok_or_else(|| invalid(&meta_path, format!("datatype {} isn't read", meta.global.datatype)))?;
        let capture = meta.captures.first().ok_or_else(|| invalid(&meta_path, "no capture segment"))?;
        if meta.captures.len() > 1 {
            return Err(invalid(&meta_path, "more than one capture segment"));
        }
        let len = std::fs::metadata(&data)?.len();
        if len % datatype.sample_bytes() as u64 != 0 {
            return Err(invalid(&data, "size isn't a whole number of samples"));
        }
        Ok(Recording {
            samples: len / datatype.sample_bytes() as u64,
            data,
            datatype,
            rate_hz: meta.global.sample_rate,
            center_hz: capture.frequency,
            datetime: capture.datetime.clone(),
            description: meta.global.description,
            hw: meta.global.hw,
        })
    }

    pub fn seconds(&self) -> f64 {
        self.samples as f64 / self.rate_hz
    }

    /// Read the samples from the start, scaled to ±1.
    pub fn reader(&self) -> io::Result<Reader> {
        Ok(Reader { file: BufReader::with_capacity(1 << 20, File::open(&self.data)?), datatype: self.datatype, raw: Vec::new() })
    }
}

/// Reads a recording's samples in blocks.
pub struct Reader {
    file: BufReader<File>,
    datatype: Datatype,
    raw: Vec<u8>,
}

impl Reader {
    /// Replace `out` with up to `max` samples; 0 at the end.
    pub fn read(&mut self, out: &mut Vec<Complex32>, max: usize) -> io::Result<usize> {
        let sb = self.datatype.sample_bytes();
        self.raw.resize(max * sb, 0);
        let mut got = 0;
        while got < self.raw.len() {
            match self.file.read(&mut self.raw[got..])? {
                0 => break,
                n => got += n,
            }
        }
        let raw = &self.raw[..got - got % sb];
        out.clear();
        match self.datatype {
            Datatype::Ci16Le => out.extend(raw.chunks_exact(4).map(|c| {
                let i = i16::from_le_bytes([c[0], c[1]]) as f32;
                let q = i16::from_le_bytes([c[2], c[3]]) as f32;
                Complex32::new(i / 32768.0, q / 32768.0)
            })),
            Datatype::Cu8 => out.extend(raw.chunks_exact(2).map(|c| Complex32::new((c[0] as f32 - 127.5) / 127.5, (c[1] as f32 - 127.5) / 127.5))),
            Datatype::Cf32Le => out.extend(
                raw.chunks_exact(8).map(|c| Complex32::new(f32::from_le_bytes([c[0], c[1], c[2], c[3]]), f32::from_le_bytes([c[4], c[5], c[6], c[7]]))),
            ),
        }
        Ok(out.len())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn write(dir: &Path, name: &str, datatype: &str, data: &[u8]) -> PathBuf {
        let base = dir.join(name);
        std::fs::write(dir.join(format!("{name}.sigmf-data")), data).unwrap();
        let meta = format!(
            r#"{{"global":{{"core:datatype":"{datatype}","core:sample_rate":6000000,"core:version":"1.0.0"}},
               "captures":[{{"core:sample_start":0,"core:frequency":853400000,"core:datetime":"2026-11-15T15:50:00Z"}}],"annotations":[]}}"#
        );
        std::fs::write(dir.join(format!("{name}.sigmf-meta")), meta).unwrap();
        base
    }

    fn tmp(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("survey-radio-{name}-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    #[test]
    fn ci16() {
        let d = tmp("ci16");
        let mut bytes = Vec::new();
        for (i, q) in [(16384i16, -16384i16), (i16::MIN, 0), (0, i16::MAX)] {
            bytes.extend(i.to_le_bytes());
            bytes.extend(q.to_le_bytes());
        }
        let base = write(&d, "800-a-853.4", "ci16_le", &bytes);
        let r = Recording::open(&base).unwrap();
        assert_eq!(r.data, d.join("800-a-853.4.sigmf-data"));
        let r = Recording::open(d.join("800-a-853.4.sigmf-meta")).unwrap();
        assert_eq!((r.datatype, r.samples, r.rate_hz, r.center_hz), (Datatype::Ci16Le, 3, 6e6, 853.4e6));
        let mut rd = r.reader().unwrap();
        let mut out = Vec::new();
        assert_eq!(rd.read(&mut out, 2).unwrap(), 2);
        assert_eq!(out, vec![Complex32::new(0.5, -0.5), Complex32::new(-1.0, 0.0)]);
        assert_eq!(rd.read(&mut out, 2).unwrap(), 1);
        assert_eq!(rd.read(&mut out, 2).unwrap(), 0);
        std::fs::remove_dir_all(d).unwrap();
    }

    #[test]
    fn cu8_and_bad_sizes() {
        let d = tmp("cu8");
        let base = write(&d, "b", "cu8", &[255, 0, 127, 128]);
        let r = Recording::open(&base).unwrap();
        let mut out = Vec::new();
        r.reader().unwrap().read(&mut out, 10).unwrap();
        assert_eq!(out.len(), 2);
        assert!((out[0].re - 1.0).abs() < 1e-6 && (out[0].im + 1.0).abs() < 1e-6);
        let base = write(&d, "c", "ci16_le", &[0, 0, 0]);
        assert!(Recording::open(&base).is_err());
        let base = write(&d, "e", "ri16_le", &[0, 0]);
        assert!(Recording::open(&base).is_err());
        std::fs::remove_dir_all(d).unwrap();
    }
}
