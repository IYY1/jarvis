mod engine;
mod history;
mod inference;
mod input;
mod matching;
mod on_demand;
mod worker;
use serde::Deserialize;
use serde_json::{json, Value};
use std::io::{BufRead, BufReader, Read, Write};
type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;
#[derive(Clone, Deserialize)]
struct WhisperFiles {
    encoder: String,
    decoder: String,
    tokens: String,
}
#[derive(Deserialize)]
struct NativeConfig {
    vosk_library: String,
    model_path: String,
    whisper: Option<WhisperFiles>,
    denoiser: Option<String>,
    threads: i32,
    /// Load Whisper on wake and release it after this many idle seconds.
    /// Absent: load it at startup and keep it for the worker's lifetime.
    #[serde(default)]
    whisper_idle_seconds: Option<f64>,
    /// Experimental: exact, confident Vosk/free agreement can skip Whisper.
    /// Off until the complete recognition acceptance corpus passes.
    #[serde(default)]
    vosk_fast_path: bool,
    /// Experimental per-utterance feature reclamation; never enabled by controllers.
    #[serde(default)]
    vosk_finalize_utterances: bool,
    candidates: Vec<(String, String)>,
    wake_forms: Vec<String>,
    greetings: Vec<String>,
    prefixes: Vec<String>,
    suffixes: Vec<String>,
}
#[derive(Deserialize)]
struct Payload {
    config: Value,
    mode: String,
    grammar: engine::Grammar,
    native: NativeConfig,
}
fn emit(event: Value) -> Result<()> {
    let mut out = std::io::stdout().lock();
    serde_json::to_writer(&mut out, &event)?;
    out.write_all(b"\n")?;
    out.flush()?;
    Ok(())
}
fn read_line(reader: &mut impl BufRead, limit: usize) -> Result<Option<Vec<u8>>> {
    let mut bytes = vec![];
    (&mut *reader)
        .take((limit + 1) as u64)
        .read_until(b'\n', &mut bytes)?;
    if bytes.len() > limit {
        return Err("Worker input exceeds its size limit".into());
    }
    Ok((!bytes.is_empty()).then_some(bytes))
}
fn read_pcm(path: &str) -> Result<Vec<u8>> {
    let file = std::fs::File::open(path)?;
    let mut bytes = vec![];
    file.take(320001).read_to_end(&mut bytes)?;
    if bytes.len() < 6400 || bytes.len() > 320000 || bytes.len() % 2 != 0 {
        return Err("Fixture must be 0.2–10 seconds of s16le PCM".into());
    }
    Ok(bytes)
}
#[cfg(unix)]
fn metrics() -> Value {
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let mib = |name| {
        status
            .lines()
            .find(|l| l.starts_with(name))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|s| s.parse::<f64>().ok())
            .unwrap_or(0.0)
            / 1024.0
    };
    json!({"rss_mib":mib("VmRSS:"), "peak_rss_mib":mib("VmHWM:")})
}
/// Working set is the RSS counterpart; private commit is reported separately.
#[cfg(windows)]
fn metrics() -> Value {
    use windows::Win32::System::ProcessStatus::{
        GetProcessMemoryInfo, PROCESS_MEMORY_COUNTERS, PROCESS_MEMORY_COUNTERS_EX,
    };
    use windows::Win32::System::Threading::GetCurrentProcess;
    let mut counters = PROCESS_MEMORY_COUNTERS_EX::default();
    let size = std::mem::size_of::<PROCESS_MEMORY_COUNTERS_EX>() as u32;
    let _ = unsafe {
        GetProcessMemoryInfo(
            GetCurrentProcess(),
            (&mut counters as *mut PROCESS_MEMORY_COUNTERS_EX).cast::<PROCESS_MEMORY_COUNTERS>(),
            size,
        )
    };
    let mib = |bytes: usize| bytes as f64 / 1048576.0;
    json!({"rss_mib":mib(counters.WorkingSetSize), "peak_rss_mib":mib(counters.PeakWorkingSetSize),
           "private_commit_mib":mib(counters.PrivateUsage)})
}
#[cfg(unix)]
fn cpu_seconds() -> f64 {
    let mut time = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    unsafe {
        libc::clock_gettime(libc::CLOCK_PROCESS_CPUTIME_ID, &mut time);
    }
    time.tv_sec as f64 + time.tv_nsec as f64 / 1e9
}
#[cfg(windows)]
fn cpu_seconds() -> f64 {
    use windows::Win32::Foundation::FILETIME;
    use windows::Win32::System::Threading::{GetCurrentProcess, GetProcessTimes};
    let mut times = [FILETIME::default(); 4];
    let [created, exited, kernel, user] = &mut times;
    let _ = unsafe { GetProcessTimes(GetCurrentProcess(), created, exited, kernel, user) };
    let ticks = |t: &FILETIME| ((t.dwHighDateTime as u64) << 32 | t.dwLowDateTime as u64) as f64;
    (ticks(&times[2]) + ticks(&times[3])) / 1e7
}
fn wake_only(request: &Value) -> Result<bool> {
    let enabled = request
        .get("wake_only")
        .map(|value| value.as_bool().ok_or("wake_only must be a boolean"))
        .transpose()?
        .unwrap_or(false);
    if enabled && request["session"].as_str() != Some("wake") {
        return Err("wake_only requires session wake".into());
    }
    Ok(enabled)
}
fn benchmark(payload: Payload, reader: &mut impl BufRead, engine_mode: bool) -> Result<()> {
    let start = std::time::Instant::now();
    let transcriber = if engine_mode {
        None
    } else {
        Some(inference::Transcriber::new(&payload.native)?)
    };
    let mut engine = if engine_mode {
        Some(engine::Engine::new(
            &payload.native,
            &payload.grammar,
            &payload.config,
        )?)
    } else {
        None
    };
    emit(
        json!({"event":"ready","load_seconds":start.elapsed().as_secs_f64(),"memory":metrics(),"whisper":"rust-tract-0.23.9","denoiser":"rust-tract-0.23.9"}),
    )?;
    while let Some(line) = read_line(reader, 16384)? {
        let request: Value = serde_json::from_slice(&line)?;
        let wake_only = wake_only(&request)?;
        if wake_only && !engine_mode {
            return Err("wake_only requires --engine".into());
        }
        let pcm = read_pcm(request["path"].as_str().ok_or("Fixture path required")?)?;
        let start = std::time::Instant::now();
        let cpu = cpu_seconds();
        let mut events = vec![];
        let mut text = String::new();
        if let Some(transcriber) = &transcriber {
            text = transcriber.transcribe(&pcm)?;
        }
        if let Some(engine) = &mut engine {
            let repeats = request
                .get("repeat")
                .map(|value| value.as_u64().ok_or("Invalid fixture repetition"))
                .transpose()?
                .unwrap_or(1);
            if !(1..=20).contains(&repeats) || pcm.len() as u64 * repeats > 32000 * 60 {
                return Err("Use 1–20 repetitions totaling at most 60 seconds".into());
            }
            engine.begin(request["session"].as_str().unwrap_or("command") == "command")?;
            let mut padded = pcm.repeat(repeats as usize);
            padded.resize((padded.len() + 64000).div_ceil(960) * 960, 0);
            for (index, frame) in padded.as_chunks::<960>().0.iter().enumerate() {
                if let Some(result) = engine.feed(frame, worker::rms(frame) < 0.005)? {
                    let wake = result.kind == "wake";
                    let mut event = serde_json::to_value(&result)?;
                    if wake_only {
                        // Diagnostic input time, including explicit padding;
                        // this is not wall time or command delivery latency.
                        event["input_seconds"] = json!((index + 1) as f64 * 0.03);
                    }
                    events.push(event);
                    if wake_only {
                        // Skip command handoff and re-arm on the next frame.
                        // begin() discards pending results from this utterance.
                        engine.begin(false)?;
                        continue;
                    }
                    if wake {
                        if let Some(command) = engine.after_wake() {
                            events.push(serde_json::to_value(command)?);
                        }
                    }
                    break;
                }
            }
        }
        let seconds = start.elapsed().as_secs_f64();
        let decode_cpu = cpu_seconds() - cpu;
        let settle = request["settle_seconds"].as_f64().unwrap_or(0.0);
        if !settle.is_finite() || !(0.0..=2.0).contains(&settle) {
            return Err("Invalid settle duration".into());
        }
        let cpu = cpu_seconds();
        std::thread::sleep(std::time::Duration::from_secs_f64(settle));
        // Explicit offline measurement only, applied equally to paired arms.
        if request["trim_heap"] == true {
            inference::trim();
        }
        let settle_cpu = cpu_seconds() - cpu;
        emit(
            json!({"event":"result","text":text,"events":events,"wake_only":wake_only,"seconds":seconds,"decode_cpu_seconds":decode_cpu,"settle_cpu_seconds":settle_cpu,"memory":metrics(),"recognition_stats":engine.as_ref().map(engine::Engine::recognition_stats)}),
        )?;
    }
    Ok(())
}
fn policy(reader: &mut impl BufRead) -> Result<()> {
    while let Some(line) = read_line(reader, 1048576)? {
        let request: Value = serde_json::from_slice(&line)?;
        let strings = |key: &str| {
            request[key]
                .as_array()
                .map(|a| {
                    a.iter()
                        .filter_map(|s| s.as_str().map(str::to_owned))
                        .collect()
                })
                .unwrap_or_default()
        };
        let candidates: Vec<(String, String)> =
            serde_json::from_value(request["candidates"].clone())?;
        let matcher = matching::Matcher {
            phrases: Default::default(),
            apps: Default::default(),
            verbs: vec![],
            prefixes: strings("prefixes"),
            suffixes: strings("suffixes"),
            candidates,
            exact_only: request["exact_only"].as_bool().unwrap_or(false),
            rules: jarvis_phrases::Rules::from_grammar(&request["grammar"]),
            blocked_apps: Default::default(),
            close_verbs: vec!["close".into()],
        };
        emit(json!({"match":matcher.closest(request["heard"].as_str().ok_or("heard required")?)}))?;
    }
    Ok(())
}
fn run() -> Result<()> {
    // One malloc arena, set before any thread starts. Whisper's loader and
    // inference threads would otherwise allocate in per-thread arenas that keep
    // their pages after on-demand Whisper is released and trimmed.
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    unsafe {
        libc::mallopt(libc::M_ARENA_MAX, 1);
    }
    // With transparent huge pages set to "always" (CachyOS and others), the
    // kernel backs sparse model allocations with whole 2 MiB pages. Opting out
    // kept the released floor 16 MiB lower over 12 Whisper load cycles at the
    // same latency (docs/benchmarks/voice-worker-thp-2026-11-05.json).
    #[cfg(target_os = "linux")]
    unsafe {
        libc::prctl(libc::PR_SET_THP_DISABLE, 1, 0, 0, 0);
    }
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args.iter().any(|s| {
        ![
            "--transcribe",
            "--engine",
            "--decoder-history",
            "--match",
            "--version",
        ]
        .contains(&s.as_str())
    }) || args.len() > 1
    {
        return Err(
            "Use --transcribe, --engine, --decoder-history, --match, --version, or no arguments for microphone worker"
                .into(),
        );
    }
    if args.first().is_some_and(|a| a == "--version") {
        println!(concat!(
            "jarvis-voice ",
            env!("CARGO_PKG_VERSION"),
            "; Rust Whisper/GTCRN tract 0.23.9; native Vosk"
        ));
        return Ok(());
    }
    worker::bind_parent()?;
    let mut reader = BufReader::new(std::io::stdin());
    if args.first().is_some_and(|a| a == "--match") {
        return policy(&mut reader);
    }
    let payload: Payload = serde_json::from_slice(
        &read_line(&mut reader, 1048576)?.ok_or("Worker configuration required")?,
    )?;
    if !(1..=32).contains(&payload.native.threads) {
        return Err("Invalid inference thread count".into());
    }
    if payload
        .native
        .whisper_idle_seconds
        .is_some_and(|s| !(1.0..=86400.0).contains(&s))
    {
        return Err("Invalid Whisper idle duration".into());
    }
    if args.first().is_some_and(|a| a == "--decoder-history") {
        return history::run(payload, &mut reader);
    }
    if args
        .first()
        .is_some_and(|a| a == "--transcribe" || a == "--engine")
    {
        return benchmark(payload, &mut reader, args[0] == "--engine");
    }
    let controls = reader.buffer().to_vec();
    drop(reader);
    worker::run(payload, controls)
}
fn main() {
    if let Err(error) = run() {
        let _ = emit(json!({"event":"error","message":error.to_string()}));
        std::process::exit(1);
    }
}

#[cfg(test)]
mod replay_tests {
    use super::*;

    #[test]
    fn wake_only_requires_an_explicit_wake_session_and_boolean() {
        assert!(!wake_only(&json!({})).unwrap());
        assert!(!wake_only(&json!({"session":"command","wake_only":false})).unwrap());
        assert!(wake_only(&json!({"session":"wake","wake_only":true})).unwrap());
        for request in [
            json!({"wake_only":true}),
            json!({"session":"command","wake_only":true}),
            json!({"session":"wake","wake_only":1}),
            json!({"session":"wake","wake_only":"true"}),
            json!({"session":"wake","wake_only":null}),
        ] {
            assert!(wake_only(&request).is_err(), "{request}");
        }
    }
}
