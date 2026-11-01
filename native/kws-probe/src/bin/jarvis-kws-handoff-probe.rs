//! Separate, file-only buffered handoff experiment. No capture or actions.
//! Spawns only the explicit KWS probe and the existing voice worker's --engine
//! mode. Replaying the wake and command preserves its free decoder and matcher.
use serde::Deserialize;
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    error::Error,
    fs::File,
    io::{BufReader, Read, Write},
    path::Path,
    process::{Child, ChildStdin, Command, Stdio},
    sync::{
        atomic::{AtomicBool, Ordering},
        mpsc::{self, Receiver},
        Arc, Mutex,
    },
    thread,
    time::{Duration, Instant},
};
#[path = "../allocation.rs"]
mod allocation;
#[path = "handoff/continuous.rs"]
mod continuous;
#[path = "handoff/reader.rs"]
mod reader;

type Result<T> = std::result::Result<T, Box<dyn Error>>;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Config {
    detector_binary: String,
    detector_config: String,
    voice_binary: String,
    voice_payload: String,
    work_dir: String,
    #[serde(default = "preroll_ms")]
    preroll_ms: usize,
    #[serde(default = "max_buffer_ms")]
    max_buffer_ms: usize,
    /// Diagnostic only: withhold configuration while audio continues arriving.
    #[serde(default)]
    payload_delay_ms: u64,
    #[serde(default = "one")]
    cycles: usize,
    #[serde(default = "settle_ms")]
    settle_ms: u64,
    #[serde(default)]
    quiet_endpoint: Option<EndpointConfig>,
    #[serde(default)]
    continuous: Option<continuous::Config>,
    /// Diagnostic: stream reports without retaining their JSON trees.
    #[serde(default = "yes")]
    retain_reports: bool,
    /// Diagnostic control only: allow a fresh comparison with detached readers.
    #[serde(default = "yes")]
    join_readers: bool,
}
fn yes() -> bool {
    true
}
fn one() -> usize {
    1
}
fn settle_ms() -> u64 {
    1000
}
fn preroll_ms() -> usize {
    2000
}
fn max_buffer_ms() -> usize {
    10000
}

#[derive(Clone, Copy, Deserialize)]
#[serde(deny_unknown_fields)]
struct EndpointConfig {
    rms: f64,
    min_speech_ms: usize,
    quiet_ms: usize,
    timeout_ms: usize,
}
struct Endpoint {
    config: EndpointConfig,
    speech: usize,
    quiet: usize,
    last_speech_end: Option<usize>,
}
impl Endpoint {
    fn observe(&mut self, frame: &[u8], input_end: usize) -> bool {
        let samples = frame.len() / 2;
        let energy: f64 = frame
            .as_chunks::<2>()
            .0
            .iter()
            .map(|b| (i16::from_le_bytes(*b) as f64 / 32768.0).powi(2))
            .sum();
        if energy >= self.config.rms.powi(2) * samples as f64 {
            self.speech += samples;
            self.quiet = 0;
            self.last_speech_end = Some(input_end);
        } else {
            self.quiet += samples;
        }
        self.speech >= self.config.min_speech_ms * 16 && self.quiet >= self.config.quiet_ms * 16
    }
}

fn text(path: &str, max: u64) -> Result<String> {
    let mut value = String::new();
    File::open(path)?.take(max + 1).read_to_string(&mut value)?;
    if value.len() as u64 > max {
        return Err("Oversized configuration".into());
    }
    Ok(value)
}
fn emit(value: Value) -> Result<()> {
    let mut output = std::io::stdout().lock();
    writeln!(output, "{value}")?;
    output.flush()?;
    Ok(())
}

struct Process {
    child: Child,
    input: Option<ChildStdin>,
    events: Option<Receiver<std::result::Result<Value, String>>>,
    reader: reader::Reader,
}
impl Process {
    #[cfg(test)]
    fn spawn(binary: &str, args: &[&str]) -> Result<Self> {
        Self::spawn_with_policy(binary, args, true)
    }
    fn spawn_with_policy(binary: &str, args: &[&str], join: bool) -> Result<Self> {
        let mut command = Command::new(binary);
        command
            .args(args)
            .env("MURMUR_OWNER_PID", std::process::id().to_string());
        // Attribution harness only: vary the parent's cache independently of
        // identical detector/voice child settings. Absent means normal inheritance.
        if let Some(tunables) = std::env::var_os("MURMUR_KWS_CHILD_GLIBC_TUNABLES") {
            command
                .env("GLIBC_TUNABLES", tunables)
                .env_remove("MURMUR_KWS_CHILD_GLIBC_TUNABLES");
        }
        let mut child = command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit())
            .spawn()?;
        let input = child.stdin.take();
        let output = child.stdout.take().ok_or("Missing child stdout")?;
        let (send, events) = mpsc::sync_channel(64);
        let reader = reader::Reader::spawn(output, send, join);
        Ok(Self {
            child,
            input,
            events: Some(events),
            reader,
        })
    }
    fn send(&mut self, value: &Value) -> Result<()> {
        let input = self.input.as_mut().ok_or("Child stdin closed")?;
        writeln!(input, "{value}")?;
        input.flush()?;
        Ok(())
    }
    fn events(&self) -> &Receiver<std::result::Result<Value, String>> {
        self.events
            .as_ref()
            .expect("Reader events requested after closing")
    }
    fn close_reader(&mut self) -> Result<()> {
        drop(self.events.take());
        self.reader.close()
    }
    fn receive(&self, timeout: Duration) -> Result<Value> {
        self.events().recv_timeout(timeout)?.map_err(Into::into)
    }
    fn finish(&mut self) -> Result<()> {
        self.input = None;
        let start = Instant::now();
        loop {
            if let Some(status) = self.child.try_wait()? {
                self.close_reader()?;
                return if status.success() {
                    Ok(())
                } else {
                    Err(format!("Child exited: {status}").into())
                };
            }
            if start.elapsed() > Duration::from_secs(10) {
                return Err("Child shutdown timed out".into());
            }
            thread::sleep(Duration::from_millis(10));
        }
    }
}
impl Drop for Process {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
        let _ = self.close_reader();
    }
}

#[derive(Default)]
struct Peaks {
    samples: usize,
    rss_kib: u64,
    pss_kib: u64,
    hwm_sum_kib: u64,
    overlap_samples: usize,
    overlap_rss_kib: u64,
    overlap_pss_kib: u64,
}
fn field(text: &str, name: &str) -> u64 {
    text.lines()
        .find(|l| l.starts_with(name))
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse().ok())
        .unwrap_or(0)
}
fn sample(pids: &[u32], peaks: &mut Peaks) {
    let mut rss = 0;
    let mut pss = 0;
    let mut hwm = 0;
    for pid in pids {
        let status = std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap_or_default();
        let smaps =
            std::fs::read_to_string(format!("/proc/{pid}/smaps_rollup")).unwrap_or_default();
        rss += field(&status, "VmRSS:");
        pss += field(&smaps, "Pss:");
        hwm += field(&status, "VmHWM:");
    }
    peaks.samples += 1;
    peaks.rss_kib = peaks.rss_kib.max(rss);
    peaks.pss_kib = peaks.pss_kib.max(pss);
    peaks.hwm_sum_kib = peaks.hwm_sum_kib.max(hwm);
    if pids.len() == 3 {
        peaks.overlap_samples += 1;
        peaks.overlap_rss_kib = peaks.overlap_rss_kib.max(rss);
        peaks.overlap_pss_kib = peaks.overlap_pss_kib.max(pss);
    }
}
struct Sampler {
    pids: Arc<Mutex<Vec<u32>>>,
    peaks: Arc<Mutex<Peaks>>,
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
}
impl Sampler {
    fn new() -> Self {
        let pids = Arc::new(Mutex::new(vec![std::process::id()]));
        let peaks = Arc::new(Mutex::new(Peaks::default()));
        let stop = Arc::new(AtomicBool::new(false));
        let (p, m, s) = (pids.clone(), peaks.clone(), stop.clone());
        let thread = thread::spawn(move || {
            while !s.load(Ordering::Relaxed) {
                sample(&p.lock().unwrap(), &mut m.lock().unwrap());
                thread::sleep(Duration::from_millis(20));
            }
        });
        Self {
            pids,
            peaks,
            stop,
            thread: Some(thread),
        }
    }
    fn add(&self, pid: u32) {
        self.pids.lock().unwrap().push(pid);
    }
    fn remove(&self, pid: u32) {
        self.pids.lock().unwrap().retain(|p| *p != pid);
    }
    fn reset(&self) {
        *self.peaks.lock().unwrap() = Peaks::default();
    }
    fn current(&self) -> Value {
        let pids = self.pids.lock().unwrap();
        let rust_allocations = allocation::snapshot();
        let mut rss = 0;
        let mut pss = 0;
        let processes: Vec<_> = pids
            .iter()
            .enumerate()
            .map(|(index, pid)| {
                let status =
                    std::fs::read_to_string(format!("/proc/{pid}/status")).unwrap_or_default();
                let smaps = std::fs::read_to_string(format!("/proc/{pid}/smaps_rollup"))
                    .unwrap_or_default();
                let r = field(&status, "VmRSS:");
                let p = field(&smaps, "Pss:");
                rss += r;
                pss += p;
                json!({"pid":pid,"role":match index {0=>"parent",1=>"detector",_=>"command"},
                "rss_mib":r as f64/1024.0,"pss_mib":p as f64/1024.0,
                "anonymous_mib":field(&smaps,"Anonymous:") as f64/1024.0,
                "private_dirty_mib":field(&smaps,"Private_Dirty:") as f64/1024.0,
                "threads":field(&status,"Threads:")})
            })
            .collect();
        #[cfg(all(target_os = "linux", target_env = "gnu"))]
        let heap = unsafe {
            let info = libc::mallinfo2();
            json!({"in_use_bytes":info.uordblks,"free_arena_bytes":info.fordblks,"mmap_bytes":info.hblkhd})
        };
        #[cfg(not(all(target_os = "linux", target_env = "gnu")))]
        let heap = Value::Null;
        json!({"processes":pids.len(),"rss_mib":rss as f64/1024.0,"pss_mib":pss as f64/1024.0,"by_process":processes,"parent_heap":heap,"parent_rust_allocations":rust_allocations,"child_readers":reader::counters()})
    }
    fn snapshot(&self) -> Value {
        sample(&self.pids.lock().unwrap(), &mut self.peaks.lock().unwrap());
        let p = self.peaks.lock().unwrap();
        json!({"sample_interval_ms":20,"samples":p.samples,"peak_combined_rss_mib":p.rss_kib as f64/1024.0,
            "peak_combined_pss_mib":p.pss_kib as f64/1024.0,"sum_process_hwm_upper_bound_mib":p.hwm_sum_kib as f64/1024.0,
            "overlap_samples":p.overlap_samples,"overlap_peak_rss_mib":p.overlap_rss_kib as f64/1024.0,
            "overlap_peak_pss_mib":p.overlap_pss_kib as f64/1024.0})
    }
}
impl Drop for Sampler {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
    }
}

struct Audio {
    ring: VecDeque<u8>,
    ring_capacity: usize,
    limit: usize,
    consumed: usize,
    start: Option<usize>,
    buffered: Vec<u8>,
    frozen: bool,
}
impl Audio {
    fn new(preroll_ms: usize, max_ms: usize) -> Self {
        // 300 ms of bounded delivery slack, in addition to requested pre-roll.
        let ring_capacity = (preroll_ms + 300).div_ceil(30) * 960;
        Self {
            ring: VecDeque::with_capacity(ring_capacity),
            ring_capacity,
            limit: max_ms * 32,
            consumed: 0,
            start: None,
            buffered: Vec::new(),
            frozen: false,
        }
    }
    fn push(&mut self, frame: &[u8]) -> Result<()> {
        if self.start.is_some() && !self.frozen {
            if self.buffered.len() + frame.len() > self.limit {
                return Err("Command audio buffer overflow; no truncated replay".into());
            }
            self.buffered.extend_from_slice(frame);
        }
        // Keep recovery pre-roll rolling even while a command is frozen/busy.
        let discard = (self.ring.len() + frame.len()).saturating_sub(self.ring_capacity);
        self.ring.drain(..discard);
        self.ring.extend(frame);
        self.consumed += frame.len();
        Ok(())
    }
    fn trigger(&mut self, input_sample: usize, preroll_ms: usize) -> Result<()> {
        self.trigger_from(input_sample, preroll_ms, 0)
    }
    fn trigger_from(
        &mut self,
        input_sample: usize,
        preroll_ms: usize,
        minimum: usize,
    ) -> Result<()> {
        let start = ((input_sample * 2).saturating_sub(preroll_ms * 32) / 960 * 960).max(minimum);
        let earliest = self.consumed - self.ring.len();
        if start < earliest || start > self.consumed {
            return Err("Detector delivery exceeded bounded onset ring".into());
        }
        let len = self.consumed - start;
        if len > self.limit {
            return Err("Onset ring exceeds command buffer".into());
        }
        // Reserve the fixed cap once: subsequent audio never causes unbounded growth.
        self.buffered = Vec::with_capacity(self.limit);
        self.buffered
            .extend(self.ring.iter().skip(start - earliest));
        self.ring.clear();
        self.start = Some(start);
        Ok(())
    }
}

fn run() -> Result<()> {
    let args: Vec<_> = std::env::args().skip(1).collect();
    if args == ["--version"] {
        println!("jarvis-kws-handoff-probe 0.1.0; file-only Rust buffered experiment");
        return Ok(());
    }
    if args.len() != 2 {
        return Err("Use jarvis-kws-handoff-probe CONFIG.json INPUT.s16le".into());
    }
    if !cfg!(target_os = "linux") {
        return Err("This experimental measurement requires Linux /proc".into());
    }
    let config: Config = serde_json::from_str(&text(&args[0], 16384)?)?;
    if !(300..=5000).contains(&config.preroll_ms)
        || !(1000..=10000).contains(&config.max_buffer_ms)
        || config.preroll_ms > config.max_buffer_ms
        || config.payload_delay_ms > 5000
    {
        return Err(
            "Use 300–5000 ms preroll within a 1000–10000 ms buffer, and 0–5000 ms diagnostic delay"
                .into(),
        );
    }
    for path in [
        &config.detector_binary,
        &config.detector_config,
        &config.voice_binary,
        &config.voice_payload,
        &args[1],
    ] {
        if !Path::new(path).is_absolute() || !Path::new(path).is_file() {
            return Err("Explicit absolute local file paths required".into());
        }
    }
    if !Path::new(&config.work_dir).is_absolute() || !Path::new(&config.work_dir).is_dir() {
        return Err("Explicit existing absolute work directory required".into());
    }
    let payload: Value = serde_json::from_str(&text(&config.voice_payload, 1048576)?)?;
    if payload["config"]["recognition"]["closest_match"] != true
        || payload["native"]["vosk_fast_path"] == true
    {
        return Err("Keep the free Vosk decoder and normal matcher enabled".into());
    }
    if !(1..=32).contains(&config.cycles) || config.settle_ms > 5000 {
        return Err("Use 1–32 cycles and 0–5000 ms settling".into());
    }
    if let Some(e) = config.quiet_endpoint {
        if !e.rms.is_finite()
            || !(0.0001..=0.05).contains(&e.rms)
            || !(120..=1000).contains(&e.min_speech_ms)
            || !(300..=1500).contains(&e.quiet_ms)
            || !(1000..=10000).contains(&e.timeout_ms)
            || e.quiet_ms >= e.timeout_ms
        {
            return Err("Invalid experimental quiet endpoint bounds".into());
        }
    }
    if let Some(continuous) = &config.continuous {
        continuous.validate(config.cycles, config.quiet_endpoint.is_some())?;
    }
    let sampler = Sampler::new();
    let mut detector = Process::spawn_with_policy(
        &config.detector_binary,
        &[&config.detector_config],
        config.join_readers,
    )?;
    sampler.add(detector.child.id());
    let ready = detector.receive(Duration::from_secs(30))?;
    if ready["event"] != "ready" {
        return Err(format!("Detector failed: {ready}").into());
    }
    let initial_waiting = sampler.current();
    emit(
        json!({"event":"ready","detector":ready,"memory":sampler.snapshot(),"waiting":initial_waiting}),
    )?;
    if let Some(continuous) = &config.continuous {
        let failures = continuous::run(
            &config,
            continuous,
            &payload,
            &args[1],
            &mut detector,
            &sampler,
        )?;
        detector.finish()?;
        if failures > 0 {
            return Err(format!("{failures} command window(s) failed; later wakes were handled without retrying failed audio").into());
        }
        return Ok(());
    }
    let mut reports = Vec::new();
    for cycle in 1..=config.cycles {
        sampler.reset();
        let before = sampler.current();
        let mut report = run_cycle(&config, &payload, &args[1], &mut detector, &sampler, cycle)?;
        // All audio and command-process ownership have left run_cycle before
        // the waiting floor is sampled. Never request an allocator trim.
        thread::sleep(Duration::from_millis(config.settle_ms));
        report["waiting_before"] = before;
        report["waiting_after_release"] = sampler.current();
        report["cycle"] = cycle.into();
        report["event"] = if config.cycles == 1 {
            "complete"
        } else {
            "cycle_complete"
        }
        .into();
        emit(report.clone())?;
        if config.retain_reports {
            reports.push(report);
        }
    }
    if config.cycles > 1 {
        emit(
            json!({"event":"complete","cycles":reports,"initial_waiting":initial_waiting,
            "final_waiting":sampler.current(),"retain_reports":config.retain_reports,"retained_report_bytes":serde_json::to_vec(&reports)?.len(),"production_defaults_changed":false,"whole_application_acceptance":false}),
        )?;
    }
    detector.finish()?;
    Ok(())
}

fn run_cycle(
    config: &Config,
    payload: &Value,
    path: &str,
    detector: &mut Process,
    sampler: &Sampler,
    cycle: usize,
) -> Result<Value> {
    let file = File::open(path)?;
    let length = file.metadata()?.len() as usize;
    if length == 0 || length > 30 * 32000 || !length.is_multiple_of(2) {
        return Err("Use nonempty, complete s16le 16 kHz mono PCM up to 30 seconds".into());
    }
    let mut reader = BufReader::new(file.take(length as u64));
    let start = Instant::now();
    detector.send(&json!({"path":path,"paced":true}))?;
    let mut audio = Audio::new(config.preroll_ms, config.max_buffer_ms);
    let mut command: Option<Process> = None;
    let mut trigger: Option<Value> = None;
    let mut trigger_sample = 0;
    let mut spawned = None;
    let mut payload_sent = false;
    let mut submitted = false;
    let mut detector_complete = None;
    let mut command_ready = Value::Null;
    let mut result = Value::Null;
    let mut result_elapsed = None;
    let mut release_elapsed = None;
    let mut endpoint = config.quiet_endpoint.map(|config| Endpoint {
        config,
        speech: 0,
        quiet: 0,
        last_speech_end: None,
    });
    let mut endpoint_reason = None;
    let mut endpoint_sample = None;
    let mut timed_out = false;
    let mut buffer_path = None;
    let mut bytes = [0u8; 960];
    while audio.consumed < length || detector_complete.is_none() || command.is_some() {
        if start.elapsed() > Duration::from_secs(120) {
            return Err("Cycle deadline exceeded".into());
        }
        let mut n = 0;
        if audio.consumed < length {
            n = (length - audio.consumed).min(960);
            reader.read_exact(&mut bytes[..n])?;
            thread::sleep(
                Duration::from_secs_f64((audio.consumed + n) as f64 / 32000.0)
                    .saturating_sub(start.elapsed()),
            );
            audio.push(&bytes[..n])?;
        } else {
            thread::sleep(Duration::from_millis(5));
        }
        while let Ok(event) = detector.events().try_recv() {
            let event = event?;
            match event["event"].as_str() {
                Some("keyword") if trigger.is_none() => {
                    trigger_sample = (event["audio_seconds"]
                        .as_f64()
                        .ok_or("Missing trigger input time")?
                        * 16000.0)
                        .round() as usize;
                    audio.trigger(trigger_sample, config.preroll_ms)?;
                    let child = Process::spawn_with_policy(
                        &config.voice_binary,
                        &["--engine"],
                        config.join_readers,
                    )?;
                    sampler.add(child.child.id());
                    spawned = Some(start.elapsed());
                    emit(
                        json!({"event":"handoff_started","cycle":cycle,"trigger":event,"elapsed_seconds":start.elapsed().as_secs_f64(),
                        "buffer_start_seconds":audio.start.unwrap() as f64/32000.0,"buffer_bytes":audio.buffered.len()}),
                    )?;
                    trigger = Some(event);
                    command = Some(child);
                }
                Some("keyword") => { /* One command window per request. */ }
                Some("complete") => detector_complete = Some(event),
                _ => return Err(format!("Unexpected detector event: {event}").into()),
            }
        }
        if detector_complete.is_none() && detector.child.try_wait()?.is_some() {
            return Err("Detector exited before completing input".into());
        }
        let mut release = false;
        if trigger.is_some() && !audio.frozen {
            if let Some(e) = &mut endpoint {
                if n > 0 && e.observe(&bytes[..n], audio.consumed / 2) {
                    endpoint_reason = Some("quiet");
                } else if audio.consumed / 2 >= trigger_sample + e.config.timeout_ms * 16 {
                    endpoint_reason = Some("command_timeout");
                    timed_out = true;
                    release = true;
                } else if audio.consumed == length {
                    endpoint_reason = Some("input_ended_before_endpoint");
                    timed_out = true;
                    release = true;
                }
            } else if audio.consumed == length {
                endpoint_reason = Some("file");
            }
            if endpoint_reason.is_some() {
                audio.frozen = true;
                endpoint_sample = Some(audio.consumed / 2);
            }
        }
        if let (Some(child), Some(spawned)) = (&mut command, spawned) {
            if !payload_sent
                && start.elapsed().saturating_sub(spawned)
                    >= Duration::from_millis(config.payload_delay_ms)
            {
                child.send(payload)?;
                payload_sent = true;
            }
            if audio.frozen && !timed_out && payload_sent && !submitted {
                let path = Path::new(&config.work_dir)
                    .join(format!("buffer-{}-{cycle}.raw", std::process::id()));
                let mut saved = File::options().write(true).create_new(true).open(&path)?;
                saved.write_all(&audio.buffered)?;
                drop(saved);
                child.send(&json!({"path":path,"session":"wake","trim_heap":false}))?;
                buffer_path = Some(path);
                submitted = true;
            }
            while let Ok(event) = child.events().try_recv() {
                let event = event?;
                match event["event"].as_str() {
                    Some("ready") => {
                        command_ready = json!({"event":event,"observed_elapsed_seconds":start.elapsed().as_secs_f64()});
                        emit(
                            json!({"event":"recognizer_ready","cycle":cycle,"ready":command_ready}),
                        )?;
                    }
                    Some("result") if submitted => {
                        result = event;
                        result_elapsed = Some(start.elapsed().as_secs_f64());
                        release = true;
                    }
                    _ => return Err(format!("Command worker failed: {event}").into()),
                }
            }
            if !release && child.child.try_wait()?.is_some() {
                return Err("Command worker exited before its result".into());
            }
        }
        if release {
            let mut child = command.take().ok_or("Release without command worker")?;
            // Snapshot while detector, parent and command model still overlap.
            sampler.snapshot();
            if timed_out {
                let _ = child.child.kill();
                let _ = child.child.wait();
            } else {
                child.finish()?;
            }
            sampler.remove(child.child.id());
            release_elapsed = Some(start.elapsed().as_secs_f64());
        }
    }
    Ok(
        json!({"trigger":trigger,"detector_complete":detector_complete,"command_ready":command_ready,
        "command_result":result,"result_elapsed_seconds":result_elapsed.unwrap_or_else(||start.elapsed().as_secs_f64()),
        "released_elapsed_seconds":release_elapsed,"input_seconds":length as f64/32000.0,
        "buffer_start_seconds":audio.start.map(|s|s as f64/32000.0),"buffer_bytes":audio.buffered.len(),
        "buffer_capacity_bytes":audio.buffered.capacity(),"ring_capacity_bytes":audio.ring.capacity(),"buffer_path":buffer_path,
        "payload_delay_ms":config.payload_delay_ms,"endpoint_reason":endpoint_reason,
        "endpoint_input_seconds":endpoint_sample.map(|s|s as f64/16000.0),
        "last_speech_input_seconds":endpoint.and_then(|e|e.last_speech_end).map(|s|s as f64/16000.0),
        "timed_out":timed_out,"memory":sampler.snapshot(),"full_recognition_acceptance":false,
        "whole_application_acceptance":false,"production_defaults_changed":false}),
    )
}

fn main() {
    if let Err(error) = run() {
        eprintln!("jarvis-kws-handoff-probe: {error}");
        std::process::exit(1);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn retains_precise_onset_and_every_sample_during_load() {
        let mut audio = Audio::new(300, 1000);
        for n in 0u16..30 {
            audio.push(&n.to_le_bytes().repeat(480)).unwrap();
        }
        assert!(audio.ring.len() <= audio.ring_capacity);
        audio.trigger(30 * 480, 300).unwrap();
        assert_eq!(audio.start, Some(20 * 960));
        for n in 30u16..40 {
            audio.push(&n.to_le_bytes().repeat(480)).unwrap();
        }
        let expected: Vec<_> = (20u16..40)
            .flat_map(|n| n.to_le_bytes().repeat(480))
            .collect();
        assert_eq!(audio.buffered, expected);
    }
    #[test]
    fn overflow_and_late_delivery_fail_instead_of_losing_audio() {
        let mut audio = Audio::new(300, 1000);
        for _ in 0..100 {
            audio.push(&[0; 960]).unwrap();
        }
        assert!(audio.trigger(10 * 480, 300).is_err());
        audio.trigger(100 * 480, 300).unwrap();
        for _ in 0..23 {
            audio.push(&[0; 960]).unwrap();
        }
        assert!(audio.push(&[0; 960]).is_err());
        assert!(audio.buffered.len() <= audio.limit);
    }
    #[test]
    fn quiet_endpoint_requires_speech_and_preserves_short_pauses() {
        let mut endpoint = Endpoint {
            config: EndpointConfig {
                rms: 0.005,
                min_speech_ms: 240,
                quiet_ms: 600,
                timeout_ms: 5000,
            },
            speech: 0,
            quiet: 0,
            last_speech_end: None,
        };
        for _ in 0..30 {
            assert!(!endpoint.observe(&[0; 960], 480));
        }
        let loud = 1000i16.to_le_bytes().repeat(480);
        for i in 1..=8 {
            assert!(!endpoint.observe(&loud, i * 480));
        }
        for _ in 0..10 {
            assert!(!endpoint.observe(&[0; 960], 480));
        }
        assert!(!endpoint.observe(&loud, 9000));
        for _ in 0..19 {
            assert!(!endpoint.observe(&[0; 960], 480));
        }
        assert!(endpoint.observe(&[0; 960], 480));
        assert_eq!(endpoint.last_speech_end, Some(9000));
    }
    #[test]
    fn endpoint_freezes_audio_without_collapsing_input_time() {
        let mut audio = Audio::new(300, 1000);
        for _ in 0..20 {
            audio.push(&[1; 960]).unwrap();
        }
        audio.trigger(20 * 480, 300).unwrap();
        let bytes = audio.buffered.clone();
        audio.frozen = true;
        for _ in 0..100 {
            audio.push(&[2; 960]).unwrap();
        }
        assert_eq!(audio.buffered, bytes);
        assert_eq!(audio.consumed, 120 * 960);
    }
    #[test]
    #[cfg(target_os = "linux")]
    fn early_return_reaps_the_owned_child() {
        let pid;
        {
            let child = Process::spawn("/bin/sleep", &["30"]).unwrap();
            pid = child.child.id();
        }
        assert_eq!(unsafe { libc::kill(pid as i32, 0) }, -1);
        assert_eq!(
            std::io::Error::last_os_error().raw_os_error(),
            Some(libc::ESRCH)
        );
    }
}
