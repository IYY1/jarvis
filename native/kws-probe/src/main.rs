//! Isolated file-only KWS experiment. Rust owns I/O, pacing and native handles;
//! sherpa-onnx/ONNX Runtime perform inference. Never linked into the voice worker.
use libloading::Library;
mod allocation;
mod gate;
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::HashSet,
    error::Error,
    ffi::{c_char, c_void, CStr, CString},
    fs::File,
    io::{BufRead, BufReader, Read, Write},
    path::Path,
    ptr::NonNull,
    time::{Duration, Instant},
};

type Result<T> = std::result::Result<T, Box<dyn Error>>;
type Handle = *const c_void;
type Create = unsafe extern "C" fn(Handle) -> Handle;
type Unary = unsafe extern "C" fn(Handle);
type Pair = unsafe extern "C" fn(Handle, Handle);
type StreamCreate = unsafe extern "C" fn(Handle) -> Handle;
type Ready = unsafe extern "C" fn(Handle, Handle) -> i32;
type Accept = unsafe extern "C" fn(Handle, i32, *const f32, i32);
type GetJson = unsafe extern "C" fn(Handle, Handle) -> *const c_char;
type FreeJson = unsafe extern "C" fn(*const c_char);

unsafe extern "C" {
    fn jarvis_create_kws(
        create: Create,
        encoder: *const c_char,
        decoder: *const c_char,
        joiner: *const c_char,
        tokens: *const c_char,
        keywords: *const c_char,
        score: f32,
        threshold: f32,
    ) -> Handle;
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    library: String,
    encoder: String,
    decoder: String,
    joiner: String,
    tokens: String,
    keywords: String,
    #[serde(default = "default_score")]
    score: f32,
    #[serde(default = "default_threshold")]
    threshold: f32,
    // Absent in normal/ungated experiments. No application setting is added.
    #[serde(default)]
    quiet_gate: Option<gate::Config>,
}
fn default_score() -> f32 {
    1.0
}
fn default_threshold() -> f32 {
    0.25
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Request {
    path: String,
    #[serde(default)]
    paced: bool,
}

fn limited_text(path: &str, limit: u64) -> Result<String> {
    let mut text = String::new();
    File::open(path)?
        .take(limit + 1)
        .read_to_string(&mut text)?;
    if text.len() as u64 > limit || text.contains('\0') {
        return Err("Text file too large or contains NUL".into());
    }
    Ok(text)
}

fn validate_keywords(tokens: &str, keywords: &str) -> Result<()> {
    let vocab: HashSet<_> = tokens
        .lines()
        .filter_map(|l| l.split_whitespace().next())
        .collect();
    let mut aliases = HashSet::new();
    for line in keywords.lines().filter(|l| !l.trim().is_empty()) {
        let pieces: Vec<_> = line.split_whitespace().collect();
        if !(2..=33).contains(&pieces.len()) {
            return Err("Use 1–32 tokens and an @alias per keyword".into());
        }
        let alias = pieces
            .last()
            .unwrap()
            .strip_prefix('@')
            .ok_or("Missing @alias")?;
        if alias.is_empty()
            || alias.len() > 64
            || !alias
                .bytes()
                .all(|c| c.is_ascii_alphanumeric() || c == b'_')
            || !aliases.insert(alias)
        {
            return Err(
                "Keyword aliases must be unique ASCII letters, digits or underscores".into(),
            );
        }
        for token in &pieces[..pieces.len() - 1] {
            if *token == "<unk>" || *token == "<blk>" || !vocab.contains(token) {
                return Err(format!("Unknown/disallowed keyword token: {token}").into());
            }
        }
    }
    if aliases.is_empty() || aliases.len() > 16 {
        return Err("Use 1–16 keywords".into());
    }
    Ok(())
}

impl Config {
    fn validate(&self) -> Result<()> {
        if let Some(gate) = self.quiet_gate {
            gate.validate()?;
        }
        if !self.score.is_finite()
            || !(0.0..=10.0).contains(&self.score)
            || self.score == 0.0
            || !self.threshold.is_finite()
            || !(0.0..=1.0).contains(&self.threshold)
            || self.threshold == 0.0
        {
            return Err("score must be (0,10], threshold (0,1]".into());
        }
        for name in [
            &self.library,
            &self.encoder,
            &self.decoder,
            &self.joiner,
            &self.tokens,
            &self.keywords,
        ] {
            if !Path::new(name).is_absolute() || !Path::new(name).is_file() {
                return Err("Supply explicit absolute paths to existing local files".into());
            }
            CString::new(name.as_str())?;
        }
        validate_keywords(
            &limited_text(&self.tokens, 1024 * 1024)?,
            &limited_text(&self.keywords, 8192)?,
        )
    }
}

struct Api {
    create: Create,
    destroy: Unary,
    create_stream: StreamCreate,
    destroy_stream: Unary,
    accept: Accept,
    finished: Unary,
    ready: Ready,
    decode: Pair,
    reset: Pair,
    result: GetJson,
    free_json: FreeJson,
    _library: Library,
}
impl Api {
    fn load(path: &str) -> Result<Self> {
        // All symbol types follow vendor/c-api.h. Pin the ABI before passing config.
        unsafe {
            let lib = Library::new(path)?;
            let version =
                lib.get::<unsafe extern "C" fn() -> *const c_char>(b"SherpaOnnxGetVersionStr\0")?();
            if version.is_null() || CStr::from_ptr(version).to_bytes() != b"1.13.8" {
                return Err("This probe requires the pinned sherpa-onnx 1.13.8 ABI".into());
            }
            macro_rules! sym {
                ($ty:ty, $name:literal) => {
                    *lib.get::<$ty>(concat!($name, "\0").as_bytes())?
                };
            }
            Ok(Self {
                create: sym!(Create, "SherpaOnnxCreateKeywordSpotter"),
                destroy: sym!(Unary, "SherpaOnnxDestroyKeywordSpotter"),
                create_stream: sym!(StreamCreate, "SherpaOnnxCreateKeywordStream"),
                destroy_stream: sym!(Unary, "SherpaOnnxDestroyOnlineStream"),
                accept: sym!(Accept, "SherpaOnnxOnlineStreamAcceptWaveform"),
                finished: sym!(Unary, "SherpaOnnxOnlineStreamInputFinished"),
                ready: sym!(Ready, "SherpaOnnxIsKeywordStreamReady"),
                decode: sym!(Pair, "SherpaOnnxDecodeKeywordStream"),
                reset: sym!(Pair, "SherpaOnnxResetKeywordStream"),
                result: sym!(GetJson, "SherpaOnnxGetKeywordResultAsJson"),
                free_json: sym!(FreeJson, "SherpaOnnxFreeKeywordResultJson"),
                _library: lib,
            })
        }
    }
}

struct Spotter {
    handle: NonNull<c_void>,
    api: Api,
    _paths: Vec<CString>,
}
impl Spotter {
    fn new(config: &Config) -> Result<Self> {
        config.validate()?;
        let paths: Vec<_> = [
            &config.encoder,
            &config.decoder,
            &config.joiner,
            &config.tokens,
            &config.keywords,
        ]
        .iter()
        .map(|s| CString::new(s.as_str()))
        .collect::<std::result::Result<_, _>>()?;
        let api = Api::load(&config.library)?;
        // Config layout is built by C. Paths and library outlive the owned handle.
        let ptr = unsafe {
            jarvis_create_kws(
                api.create,
                paths[0].as_ptr(),
                paths[1].as_ptr(),
                paths[2].as_ptr(),
                paths[3].as_ptr(),
                paths[4].as_ptr(),
                config.score,
                config.threshold,
            )
        };
        Ok(Self {
            handle: NonNull::new(ptr.cast_mut()).ok_or("KWS model could not be loaded")?,
            api,
            _paths: paths,
        })
    }
    fn stream(&self) -> Result<Stream<'_>> {
        // The returned stream borrows its spotter and is destroyed first.
        let ptr = unsafe { (self.api.create_stream)(self.handle.as_ptr()) };
        Ok(Stream {
            handle: NonNull::new(ptr.cast_mut()).ok_or("KWS stream creation failed")?,
            spotter: self,
        })
    }
}
impl Drop for Spotter {
    fn drop(&mut self) {
        unsafe { (self.api.destroy)(self.handle.as_ptr()) };
    }
}
struct Stream<'a> {
    handle: NonNull<c_void>,
    spotter: &'a Spotter,
}
impl Drop for Stream<'_> {
    fn drop(&mut self) {
        unsafe { (self.spotter.api.destroy_stream)(self.handle.as_ptr()) };
    }
}
impl Stream<'_> {
    fn accept(&self, samples: &[f32]) {
        // Only fixed <=480-sample buffers are passed by the file replay loop.
        unsafe {
            (self.spotter.api.accept)(
                self.handle.as_ptr(),
                16000,
                samples.as_ptr(),
                samples.len() as i32,
            )
        };
    }
    fn drain(&self, samples: u64, origin: u64, start: Instant) -> Result<usize> {
        let api = &self.spotter.api;
        let kws = self.spotter.handle.as_ptr();
        let stream = self.handle.as_ptr();
        let mut count = 0;
        // Every call is guarded by the library's stream-ready predicate.
        unsafe {
            let mut iterations = 0;
            while (api.ready)(kws, stream) != 0 {
                iterations += 1;
                if iterations > 10000 {
                    return Err("KWS decode made no bounded progress".into());
                }
                (api.decode)(kws, stream);
                let ptr = (api.result)(kws, stream);
                if ptr.is_null() {
                    return Err("Null KWS JSON result".into());
                }
                // Free the native snapshot even when JSON parsing fails.
                let result = serde_json::from_slice::<Value>(CStr::from_ptr(ptr).to_bytes());
                (api.free_json)(ptr);
                let result = result?;
                if result["keyword"].as_str().is_some_and(|s| !s.is_empty()) {
                    count += 1;
                    emit(
                        json!({"event":"keyword", "audio_seconds":samples as f64/16000.0, "stream_origin_seconds":origin as f64/16000.0, "elapsed_seconds":start.elapsed().as_secs_f64(), "result":result}),
                    )?;
                    (api.reset)(kws, stream);
                }
            }
        }
        Ok(count)
    }
}

fn memory() -> Value {
    let rust_allocations = allocation::snapshot();
    let status = std::fs::read_to_string("/proc/self/status").unwrap_or_default();
    let smaps = std::fs::read_to_string("/proc/self/smaps_rollup").unwrap_or_default();
    let mib = |text: &str, key: &str| {
        text.lines()
            .find(|l| l.starts_with(key))
            .and_then(|l| l.split_whitespace().nth(1))
            .and_then(|s| s.parse::<f64>().ok())
            .map(|n| n / 1024.0)
    };
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    let heap = unsafe {
        let info = libc::mallinfo2();
        json!({"in_use_bytes":info.uordblks,"free_arena_bytes":info.fordblks,"mmap_bytes":info.hblkhd})
    };
    #[cfg(not(all(target_os = "linux", target_env = "gnu")))]
    let heap = Value::Null;
    json!({"rss_mib":mib(&status,"VmRSS:"),"peak_rss_mib":mib(&status,"VmHWM:"),"pss_mib":mib(&smaps,"Pss:"),
        "anonymous_mib":mib(&smaps,"Anonymous:"),"heap":heap,"rust_allocations":rust_allocations})
}
fn cpu_seconds() -> Option<f64> {
    #[cfg(unix)]
    unsafe {
        let mut usage: libc::rusage = std::mem::zeroed();
        if libc::getrusage(libc::RUSAGE_SELF, &mut usage) == 0 {
            return Some(
                usage.ru_utime.tv_sec as f64
                    + usage.ru_stime.tv_sec as f64
                    + (usage.ru_utime.tv_usec + usage.ru_stime.tv_usec) as f64 / 1e6,
            );
        }
    }
    None
}
fn emit(value: Value) -> Result<()> {
    let mut out = std::io::stdout().lock();
    writeln!(out, "{value}")?;
    out.flush()?;
    Ok(())
}
fn validate_pcm(bytes: u64) -> Result<()> {
    if bytes == 0 || bytes > 120 * 32000 || !bytes.is_multiple_of(2) {
        return Err("Use 0–120 seconds of nonempty complete s16le 16 kHz mono PCM".into());
    }
    Ok(())
}
fn replay(spotter: &Spotter, request: Request, quiet_gate: Option<gate::Config>) -> Result<()> {
    let file = File::open(&request.path)?;
    let length = file.metadata()?.len();
    validate_pcm(length)?;
    // New stream per request; concatenated cases preserve state within one file.
    let mut stream = if quiet_gate.is_none() {
        Some(spotter.stream()?)
    } else {
        None
    };
    let mut gate = quiet_gate.map(gate::Gate::new);
    let mut origin = 0;
    let mut accounting = gate::Accounting::default();
    let mut stream_origins = if stream.is_some() {
        vec![0]
    } else {
        Vec::new()
    };
    let mut stream_count = u64::from(stream.is_some());
    let start = Instant::now();
    let cpu_start = cpu_seconds();
    let mut reader = BufReader::new(file.take(length));
    let mut bytes = [0u8; 960];
    let mut buffer = [0f32; 480];
    let mut consumed = 0;
    let mut hits = 0;
    while consumed < length {
        let n = (length - consumed).min(960) as usize;
        reader.read_exact(&mut bytes[..n])?;
        for (out, pair) in buffer.iter_mut().zip(bytes[..n].as_chunks::<2>().0) {
            *out = i16::from_le_bytes([pair[0], pair[1]]) as f32 / 32768.0;
        }
        consumed += n as u64;
        // A chunk becomes available at its end, not at its start.
        if request.paced {
            std::thread::sleep(
                Duration::from_secs_f64(consumed as f64 / 32000.0).saturating_sub(start.elapsed()),
            );
        }
        let frame = gate::Frame {
            samples: buffer,
            len: n / 2,
            end: consumed / 2,
        };
        let action = gate
            .as_mut()
            .map_or(gate::Action::Feed, |g| g.observe(&frame));
        if action == gate::Action::Skip {
            gate.as_mut().unwrap().remember(frame);
            continue;
        }
        if action == gate::Action::Open {
            let g = gate.as_mut().unwrap();
            origin = g.origin(&frame);
            if g.preroll
                .front()
                .is_some_and(|first| origin < first.start())
            {
                return Err("Aligned origin fell outside bounded original-audio context".into());
            }
            stream = Some(spotter.stream()?);
            stream_count += 1;
            stream_origins.push(origin);
            for prior in &g.preroll {
                let begin = origin.saturating_sub(prior.start()).min(prior.len as u64) as usize;
                if begin == prior.len {
                    continue;
                }
                stream.as_ref().unwrap().accept(&prior.audio()[begin..]);
                accounting.accept(prior.start() + begin as u64, prior.end);
                // Replayed original audio is available only at this input time.
                hits += stream
                    .as_ref()
                    .unwrap()
                    .drain(consumed / 2, origin, start)?;
            }
            if !g.retains_active_context() {
                g.preroll.clear();
            }
        }
        let active = stream.as_ref().ok_or("Gate opened without a stream")?;
        active.accept(frame.audio());
        accounting.accept(frame.start(), frame.end);
        hits += active.drain(consumed / 2, origin, start)?;
        if gate
            .as_ref()
            .is_some_and(gate::Gate::retains_active_context)
        {
            gate.as_mut().unwrap().remember(frame);
        }
        if action == gate::Action::Close {
            // Drop feature history as well as hypotheses. Do not finalize into a
            // synthetic wake at a quiet boundary (the Vosk missed-wake fix).
            stream = None;
        }
    }
    // No hidden trailing audio. The caller includes silence explicitly in PCM.
    if let Some(active) = &stream {
        unsafe { (spotter.api.finished)(active.handle.as_ptr()) };
        hits += active.drain(consumed / 2, origin, start)?;
    }
    drop(stream);
    emit(
        json!({"event":"complete","path":request.path,"paced":request.paced,"audio_seconds":length as f64/32000.0,"elapsed_seconds":start.elapsed().as_secs_f64(),"cpu_seconds":cpu_seconds().zip(cpu_start).map(|(a,b)|a-b),"detections":hits,"decoded_samples":accounting.decoded,"replayed_samples":accounting.replayed,"accepted_samples":accounting.decoded+accounting.replayed,
        "skipped_samples":consumed/2-accounting.decoded,"stream_count":stream_count,"stream_origins_samples":stream_origins,"quiet_gate":quiet_gate,"memory":memory()}),
    )
}
fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--version"] {
        println!("jarvis-kws-probe 0.1.0; experimental sherpa-onnx 1.13.8 C API / CPU");
        return Ok(());
    }
    if args.len() != 1 {
        return Err("Use jarvis-kws-probe CONFIG.json, then JSON lines {\"path\":\"/absolute/clip.s16le\",\"paced\":false}".into());
    }
    let config: Config = serde_json::from_str(&limited_text(&args[0], 16384)?)?;
    let start = Instant::now();
    let spotter = Spotter::new(&config)?;
    emit(
        json!({"event":"ready","backend":"sherpa-onnx-1.13.8-c-api","threads":1,"score":config.score,"threshold":config.threshold,"quiet_gate":config.quiet_gate,"load_seconds":start.elapsed().as_secs_f64(),"memory":memory()}),
    )?;
    let mut input = std::io::stdin().lock();
    loop {
        let mut line = String::new();
        if (&mut input).take(16385).read_line(&mut line)? == 0 {
            break;
        }
        if line.len() > 16384 {
            return Err("Request line too large".into());
        }
        replay(&spotter, serde_json::from_str(&line)?, config.quiet_gate)?;
    }
    Ok(())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("jarvis-kws-probe: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn keywords_require_known_tokens_and_unique_labels() {
        let tokens = "<blk> 0\n<unk> 1\n▁GO 2\n▁FOR 3\nWARD 4\n";
        assert!(validate_keywords(tokens, "▁GO ▁FOR WARD @go_forward\n").is_ok());
        for bad in [
            "",
            "▁GO",
            "<unk> @wake",
            "<blk> @wake",
            "missing @wake",
            "▁GO :3 @wake",
            "▁GO @",
            "▁GO @a/b",
            "▁GO @wake\n▁GO @wake",
        ] {
            assert!(validate_keywords(tokens, bad).is_err(), "{bad}");
        }
    }
    #[test]
    fn pcm_is_bounded_and_complete() {
        for n in [2, 960, 120 * 32000] {
            assert!(validate_pcm(n).is_ok());
        }
        for n in [0, 1, 961, 120 * 32000 + 2] {
            assert!(validate_pcm(n).is_err());
        }
    }
    #[test]
    fn protocol_rejects_unknown_fields() {
        assert!(serde_json::from_str::<Request>(r#"{"path":"clip","microphone":true}"#).is_err());
        assert!(serde_json::from_str::<Config>(r#"{}"#).is_err());
    }
}
