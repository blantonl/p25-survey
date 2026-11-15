//! The survey on the corpus recordings: every control channel in
//! corpus/expected.json is found, with its identity, and the copies are
//! called copies. Skipped (passes) when the recordings aren't there (they
//! stay out of git: `rsync -a <capture host>:p25-corpus/raw/ corpus/raw/`).

use std::path::PathBuf;
use std::process::Command;

use serde_json::Value;

fn root() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..")
}

#[test]
fn scans_find_every_expected_control_channel() {
    let expected: Value = serde_json::from_str(&std::fs::read_to_string(root().join("corpus/expected.json")).unwrap()).unwrap();
    for (band, e) in expected.as_object().unwrap() {
        let recordings: Vec<PathBuf> = e["recordings"].as_array().unwrap().iter().map(|r| root().join("corpus/raw").join(r.as_str().unwrap())).collect();
        if recordings.iter().any(|r| !PathBuf::from(format!("{}.sigmf-meta", r.display())).exists()) {
            eprintln!("{band}: corpus recordings not here, skipped");
            continue;
        }
        let out = std::env::temp_dir().join(format!("p25-survey-corpus-{band}-{}.json", std::process::id()));
        let status = Command::new(env!("CARGO_BIN_EXE_p25-survey"))
            .arg("scan")
            .args(&recordings)
            .args(["--start", &e["start_mhz"].to_string(), "--stop", &e["stop_mhz"].to_string(), "--output"])
            .arg(&out)
            .stdout(std::process::Stdio::null())
            .status()
            .unwrap();
        assert!(status.success(), "{band}: scan failed");
        let records: Vec<Value> = std::fs::read_to_string(&out).unwrap().lines().map(|l| serde_json::from_str(l).unwrap()).collect();
        let _ = std::fs::remove_file(&out);
        for cc in e["control_channels"].as_array().unwrap() {
            let f = cc["freq_hz"].as_u64().unwrap();
            let r = records.iter().find(|r| r["freq_hz"].as_u64() == Some(f)).unwrap_or_else(|| panic!("{band}: {f} not a candidate"));
            assert!(matches!(r["verdict"].as_str(), Some("complete" | "partial")), "{band}: {f} is {}", r["verdict"]);
            assert!(r["copy_of_hz"].is_null(), "{band}: {f} called a copy");
            for k in ["nac", "wacn", "sysid", "rfss_id", "site_id"] {
                if !cc[k].is_null() {
                    assert_eq!(r[k], cc[k], "{band}: {f} {k}");
                }
            }
        }
        for c in e.get("copies").and_then(Value::as_array).into_iter().flatten() {
            let f = c["freq_hz"].as_u64().unwrap();
            let r = records.iter().find(|r| r["freq_hz"].as_u64() == Some(f)).unwrap_or_else(|| panic!("{band}: copy {f} not a candidate"));
            assert_eq!(r["copy_of_hz"], c["copy_of_hz"], "{band}: {f}");
        }
    }
}
