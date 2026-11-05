//! Offline validation: transcribe s16le 16 kHz files. No microphone or service.
use jarvis_moonshine::Recognizer;
use serde_json::{json, Value};
use std::io::{BufRead, Read};
use std::time::Instant;
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::format_err;

fn memory() -> Value {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let mib = |key: &str| {
        status
            .lines()
            .find(|l| l.starts_with(key))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|v| v.parse::<f64>().ok())
            .unwrap_or(0.0)
            / 1024.0
    };
    json!({"rss_mib":mib("VmRSS:"),"peak_rss_mib":mib("VmHWM:")})
}

fn run() -> TractResult<()> {
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--version"] {
        println!(concat!(
            "jarvis-moonshine ",
            env!("CARGO_PKG_VERSION"),
            "; tract 0.23.9"
        ));
        return Ok(());
    }
    let [flag, dir] = &args[..] else {
        return Err(format_err!(
            "Use jarvis-moonshine --batch MODEL_DIR, then {{\"path\": FILE.s16le}} lines"
        ));
    };
    if flag != "--batch" {
        return Err(format_err!("Use --batch MODEL_DIR"));
    }
    let start = Instant::now();
    let model = Recognizer::load(dir)?;
    println!(
        "{}",
        json!({"event":"ready","load_seconds":start.elapsed().as_secs_f64(),"memory":memory()})
    );
    for line in std::io::stdin().lock().lines() {
        let request: Value = serde_json::from_str(&line?)?;
        let path = request["path"]
            .as_str()
            .ok_or_else(|| format_err!("path required"))?;
        let mut bytes = vec![];
        std::fs::File::open(path)?
            .take(jarvis_moonshine::MAX_SAMPLES as u64 * 2)
            .read_to_end(&mut bytes)?;
        let samples: Vec<f32> = bytes
            .as_chunks::<2>()
            .0
            .iter()
            .map(|b| i16::from_le_bytes(*b) as f32 / 32768.0)
            .collect();
        let start = Instant::now();
        let text = model.transcribe(&samples)?;
        println!(
            "{}",
            json!({"event":"result","text":text,"seconds":start.elapsed().as_secs_f64(),"memory":memory()})
        );
    }
    Ok(())
}

fn main() {
    if let Err(e) = run() {
        eprintln!("{e:?}");
        std::process::exit(1);
    }
}
