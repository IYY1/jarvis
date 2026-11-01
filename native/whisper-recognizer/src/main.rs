//! Offline command-line validation. No microphone, service or desktop actions.
use jarvis_whisper::Recognizer;
use serde_json::{json, Value};
use std::{
    io::{BufRead, BufReader, Read},
    path::Path,
    time::Instant,
};
use tract_onnx::prelude::*;
use tract_onnx::tract_core::internal::{ensure, format_err};
fn pcm(path: &str) -> TractResult<Vec<f32>> {
    let mut bytes = Vec::new();
    std::fs::File::open(path)?
        .take(320001)
        .read_to_end(&mut bytes)?;
    ensure!(
        (6400..=320000).contains(&bytes.len()) && bytes.len().is_multiple_of(2),
        "Expected 0.2–10 seconds of complete s16le PCM samples"
    );
    Ok(bytes
        .as_chunks::<2>()
        .0
        .iter()
        .map(|b| i16::from_le_bytes(*b) as f32 / 32768.0)
        .collect())
}
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
fn trim() {
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    unsafe {
        libc::malloc_trim(0);
    }
}
fn run() -> TractResult<()> {
    let mut args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--version"] {
        println!(concat!(
            "jarvis-whisper ",
            env!("CARGO_PKG_VERSION"),
            "; Rust Whisper tiny.en/base.en/small.en / tract 0.23.9"
        ));
        return Ok(());
    }
    let threads = if let Some(i) = args.iter().position(|a| a == "--threads") {
        ensure!(i + 1 < args.len(), "--threads requires a number");
        let n = args.remove(i + 1).parse::<usize>()?;
        args.remove(i);
        n
    } else {
        8
    };
    let model_name = if let Some(i) = args.iter().position(|a| a == "--model-name") {
        ensure!(
            i + 1 < args.len(),
            "--model-name requires tiny.en, base.en or small.en"
        );
        let name = args.remove(i + 1);
        args.remove(i);
        name
    } else {
        "small.en".into()
    };
    ensure!(
        ["tiny.en", "base.en", "small.en"].contains(&model_name.as_str()),
        "Unsupported model name; use tiny.en, base.en or small.en"
    );
    let batch = args.first().is_some_and(|a| a == "--batch");
    let diagnose = args.first().is_some_and(|a| a == "--diagnose");
    if batch || diagnose {
        args.remove(0);
    }
    ensure!(if batch {args.len()==1} else if diagnose {args.len()==3} else {args.len()==2},"Use jarvis-whisper [--threads N] [--model-name tiny.en|base.en|small.en] MODEL_DIR INPUT.s16le; --batch MODEL_DIR; or --diagnose MODEL_DIR INPUT.s16le NEW_TRACE_DIR");
    let root = Path::new(&args[0]);
    let start = Instant::now();
    let model = Recognizer::load(
        root.join(format!("{model_name}-encoder.int8.onnx")),
        root.join(format!("{model_name}-decoder.int8.onnx")),
        root.join(format!("{model_name}-tokens.txt")),
        threads,
    )?;
    ensure!(
        model.model_name() == model_name,
        "Requested model name does not match its metadata"
    );
    trim();
    println!(
        "{}",
        json!({"event":"ready","implementation":"rust-tract-0.23.9","model":model.model_name(),"load_seconds":start.elapsed().as_secs_f64(),"memory":memory(),"embedding":{"storage":model.embedding_storage(),"bytes":model.embedding_bytes()}})
    );
    let infer = |path: &str, trace: Option<&str>| -> TractResult<()> {
        let samples = pcm(path)?;
        let start = Instant::now();
        let text = if let Some(trace) = trace {
            model.diagnose(&samples, Path::new(trace))?
        } else {
            model.transcribe(&samples)?
        };
        let seconds = start.elapsed().as_secs_f64();
        trim();
        println!(
            "{}",
            json!({"event":"result","text":text,"seconds":seconds,"memory":memory()})
        );
        Ok(())
    };
    if batch {
        let mut reader = BufReader::new(std::io::stdin());
        loop {
            let mut line = String::new();
            if reader.by_ref().take(16385).read_line(&mut line)? == 0 {
                break;
            }
            ensure!(line.len() <= 16384, "Request exceeds 16 KiB");
            let req: Value = serde_json::from_str(&line)?;
            infer(
                req["path"]
                    .as_str()
                    .ok_or_else(|| format_err!("Fixture path required"))?,
                None,
            )?;
        }
    } else {
        infer(&args[1], diagnose.then(|| args[2].as_str()))?;
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("{error:?}");
        std::process::exit(1);
    }
}
