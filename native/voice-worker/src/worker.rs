use crate::{
    dictation::Dictation,
    emit,
    engine::{Engine, Recognition},
    input::{self, FRAME},
    Payload, Result,
};
use serde_json::json;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, Ordering};
use std::time::Duration;
static STOP: AtomicBool = AtomicBool::new(false);
#[cfg(unix)]
extern "C" fn stop(_: libc::c_int) {
    STOP.store(true, Ordering::Relaxed);
}
fn expected_owner(actual: u32) -> Result<u32> {
    Ok(std::env::var("JARVIS_OWNER_PID")
        .ok()
        .map(|s| s.parse())
        .transpose()?
        .unwrap_or(actual))
}
#[cfg(unix)]
pub fn bind_parent() -> Result<()> {
    let before = unsafe { libc::getppid() };
    if before <= 1 || before as u32 != expected_owner(before as u32)? {
        return Err("Voice worker owner exited".into());
    }
    if unsafe { libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM) } != 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    if unsafe { libc::getppid() } != before {
        return Err("Voice worker owner changed".into());
    }
    // Handler only writes an atomic; the service retains its three-second kill bound.
    unsafe {
        libc::signal(libc::SIGTERM, stop as *const () as libc::sighandler_t);
        libc::signal(libc::SIGINT, stop as *const () as libc::sighandler_t);
    }
    Ok(())
}
/// The service's kill-on-close job also ends this process; watching the owner
/// covers a worker started outside that job.
#[cfg(windows)]
pub fn bind_parent() -> Result<()> {
    use windows::Win32::System::Threading::{
        OpenProcess, WaitForSingleObject, INFINITE, PROCESS_SYNCHRONIZE,
    };
    let Ok(owner) = std::env::var("JARVIS_OWNER_PID") else {
        return Ok(());
    };
    let owner = expected_owner(owner.parse()?)?;
    let handle = unsafe { OpenProcess(PROCESS_SYNCHRONIZE, false, owner) }
        .map_err(|_| "Voice worker owner exited")?;
    let handle = handle.0 as usize;
    std::thread::Builder::new()
        .name("jarvis-owner".into())
        .spawn(move || {
            let handle = windows::Win32::Foundation::HANDLE(handle as *mut _);
            unsafe { WaitForSingleObject(handle, INFINITE) };
            std::process::exit(1);
        })?;
    Ok(())
}
/// Seconds on the clock the service's time.monotonic() reads, so deadlines
/// and capture times cross the process boundary unchanged.
#[cfg(unix)]
pub fn now() -> f64 {
    let mut t = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    unsafe {
        libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t);
    }
    t.tv_sec as f64 + t.tv_nsec as f64 / 1e9
}
/// Python 3.13+ uses QueryPerformanceCounter for time.monotonic() on Windows.
#[cfg(windows)]
pub fn now() -> f64 {
    use windows::Win32::System::Performance::{
        QueryPerformanceCounter, QueryPerformanceFrequency,
    };
    let (mut counter, mut frequency) = (0_i64, 0_i64);
    unsafe {
        let _ = QueryPerformanceCounter(&mut counter);
        let _ = QueryPerformanceFrequency(&mut frequency);
    }
    counter as f64 / frequency.max(1) as f64
}
pub fn rms(pcm: &[u8]) -> f64 {
    let sum: i64 = pcm
        .as_chunks::<2>()
        .0
        .iter()
        .map(|s| {
            let v = i16::from_le_bytes([s[0], s[1]]) as i64;
            v * v
        })
        .sum();
    (sum as f64 / (pcm.len() / 2).max(1) as f64).sqrt() / 32768.0
}
struct NoiseFloor {
    minimum: f64,
    adaptive: bool,
    floor: Option<f64>,
}
impl NoiseFloor {
    fn gate(&self) -> f64 {
        if self.adaptive {
            self.floor
                .map_or(self.minimum, |f| self.minimum.max(0.08_f64.min(f * 3.0)))
        } else {
            self.minimum
        }
    }
    fn update(&mut self, energy: f64) {
        self.floor = Some(self.floor.map_or(energy, |f| {
            f + (energy - f) * if energy < f { 0.05 } else { 0.0015 }
        }));
    }
}
/// The short quiet wanted after a wake name before listening for a command.
/// Speech ends it early: that is the command starting, and it must not be lost.
#[derive(Default)]
struct Settling {
    active: bool,
    quiet_frames: usize,
    /// Quiet frames held for the start of the command, which is often too soft
    /// to pass the energy gate. At most `SETTLE_FRAMES`.
    frames: VecDeque<[u8; FRAME]>,
}
const SETTLE_FRAMES: usize = 7;
impl Settling {
    fn start(&mut self) {
        self.active = true;
        self.quiet_frames = 0;
        self.frames.clear();
    }
    fn stop(&mut self) {
        self.active = false;
        self.frames.clear();
    }
    /// Returns true once listening should begin, leaving any held frames to feed first.
    fn frame(&mut self, pcm: &[u8; FRAME], quiet: bool) -> bool {
        if !quiet {
            self.active = false;
            return true;
        }
        if self.frames.len() == SETTLE_FRAMES {
            self.frames.pop_front();
        }
        self.frames.push_back(*pcm);
        self.quiet_frames += 1;
        if self.quiet_frames >= SETTLE_FRAMES {
            self.stop();
            return true;
        }
        false
    }
}
fn ready(engine: Option<&Engine>, listening: bool, deadline: Option<f64>) -> Result<()> {
    emit(
        json!({"event":"ready","listening":listening,"deadline":deadline,"unsupported":engine.map(|e| &e.unsupported).cloned().unwrap_or_default(),"engine":engine.map(|e| e.name.as_str()),"worker":"rust"}),
    )
}
fn heard(engine: &mut Option<Engine>) -> Result<()> {
    if let Some(text) = engine.as_mut().and_then(|e| e.unmatched.take()) {
        emit(json!({"event":"heard","text":text.chars().take(120).collect::<String>()}))?;
    }
    Ok(())
}
fn dictated(results: Vec<std::result::Result<crate::inference::Text, String>>) -> Result<()> {
    for result in results {
        match result {
            Ok(t) if !t.text.is_empty() => {
                emit(json!({"event":"dictation_text","text":t.text,"language":t.language}))?
            }
            Ok(_) => {}
            Err(e) => emit(json!({"event":"dictation_error","message":e}))?,
        }
    }
    Ok(())
}
/// Ends dictation: "stop" transcribes what was already said, "cancel"
/// discards it. Waiting for the last segment blocks capture briefly; the
/// worker then returns to waiting for the wake phrase.
fn end_dictation(dictation: &mut Option<Dictation>, reason: &str) -> Result<()> {
    let Some(mut d) = dictation.take() else {
        return Ok(());
    };
    let dropped = d.dropped;
    if reason == "cancel" {
        drop(d);
    } else {
        d.cut();
        dictated(d.finish())?;
    }
    emit(json!({"event":"dictation","state":"stopped","reason":reason,"dropped":dropped}))
}
fn command(result: &Recognition, captured_at: f64) -> Result<()> {
    let mut message = serde_json::to_value(result)?;
    message["event"] = json!("command");
    message["captured_at"] = json!(captured_at);
    emit(message)
}
pub fn run(payload: Payload, mut controls: Vec<u8>) -> Result<()> {
    let Payload {
        config,
        mode,
        grammar,
        native,
    } = payload;
    if config["privacy"]["onboarded"].as_bool() != Some(true) {
        return Err("Complete microphone consent in Settings first".into());
    }
    if !["wake_word", "once", "test", "dictation"].contains(&mode.as_str()) {
        return Err("Invalid worker mode".into());
    }
    let dictation_idle = config["dictation"]["idle_seconds"]
        .as_f64()
        .unwrap_or(20.0)
        .clamp(5.0, 300.0);
    let dictation_limit = config["dictation"]["max_minutes"]
        .as_f64()
        .unwrap_or(10.0)
        .clamp(1.0, 60.0)
        * 60.0;
    let timeout = config["activation"]["command_timeout"]
        .as_f64()
        .filter(|v| v.is_finite() && (2.0..=15.0).contains(v))
        .ok_or("Invalid command timeout")?;
    let mut engine = if mode == "test" {
        None
    } else {
        emit(json!({"event":"loading","message":"Loading local voice model…"}))?;
        Some(Engine::new(&native, &grammar, &config)?)
    };
    drop(native);
    drop(grammar);
    crate::inference::trim();
    if STOP.load(Ordering::Relaxed) {
        return Ok(());
    }
    let mut input = input::open(&config, mode == "wake_word")?;
    let mut control_open = mode == "wake_word" || mode == "dictation";
    let mut dictation: Option<Dictation> = None;
    let mut dictation_deadline = 0.0;
    let begin_dictation = |engine: &mut Option<Engine>,
                           dictation: &mut Option<Dictation>,
                           deadline: &mut f64,
                           language: Option<&str>|
     -> Result<()> {
        let model = engine
            .as_mut()
            .ok_or("Dictation requires a recognition engine")?
            .dictation_model(language)?;
        *dictation = Some(Dictation::start(model)?);
        *deadline = now() + dictation_limit;
        emit(json!({"event":"dictation","state":"started","idle_seconds":dictation_idle,"max_seconds":dictation_limit}))
    };
    let mut listening = mode == "once";
    let mut deadline = if ["test", "once"].contains(&mode.as_str()) {
        Some(now() + if mode == "test" { 15.0 } else { timeout })
    } else {
        None
    };
    if let Some(e) = &mut engine {
        e.begin(listening)?;
    }
    ready(engine.as_ref(), listening, deadline)?;
    if mode == "dictation" {
        begin_dictation(&mut engine, &mut dictation, &mut dictation_deadline, None)?;
    }
    let minimum = config["audio"]["vad_rms"]
        .as_f64()
        .filter(|v| v.is_finite() && (0.0001..=0.5).contains(v))
        .ok_or("Invalid VAD level")?;
    let mut noise = NoiseFloor {
        minimum,
        adaptive: config["audio"]["adaptive_gate"].as_bool().unwrap_or(true),
        floor: None,
    };
    let mut next_level = 0.0;
    let mut cooldown = 0.0;
    let mut settling = Settling::default();
    let mut last_frame = now();
    let mut pending = [0_u8; FRAME];
    while !STOP.load(Ordering::Relaxed) {
        let time = now();
        if time - last_frame >= 3.0 {
            return Err(if cfg!(windows) {
                "No microphone frames for three seconds. Check the input device and Windows microphone privacy settings, then Resume."
            } else {
                "No microphone frames for three seconds. Check the input device and PipeWire, then Resume."
            }
            .into());
        }
        if deadline.is_some_and(|d| time >= d) {
            let stamp = deadline.unwrap();
            let late = if listening {
                if let Some(e) = &mut engine {
                    e.flush()?
                } else {
                    None
                }
            } else {
                None
            };
            last_frame = now();
            if let Some(result) = late {
                command(&result, stamp)?;
                if mode == "once" {
                    return Ok(());
                }
                listening = false;
                deadline = None;
                settling.stop();
                engine.as_mut().unwrap().begin(false)?;
                cooldown = now() + 1.5;
                continue;
            }
            heard(&mut engine)?;
            if ["test", "once"].contains(&mode.as_str()) {
                emit(json!({"event":"timeout"}))?;
                return Ok(());
            }
            listening = false;
            deadline = None;
            settling.stop();
            engine.as_mut().unwrap().begin(false)?;
            emit(json!({"event":"timeout"}))?;
        }
        let next = deadline.map_or(last_frame + 3.0, |d| d.min(last_frame + 3.0));
        let wait = if controls.contains(&b'\n') {
            Duration::ZERO
        } else if dictation.as_ref().is_some_and(Dictation::pending) {
            // Collect finished segments promptly even between frames.
            Duration::from_millis(50)
        } else {
            Duration::from_secs_f64((next - now()).clamp(0.0, 3.0))
        };
        // Control wins over audio, including a continuously readable pipe.
        let arrived = input.wait(wait, control_open)?;
        if let Some(bytes) = arrived.control.filter(|_| control_open) {
            if bytes.is_empty() {
                control_open = false;
            } else {
                controls.extend_from_slice(&bytes);
            }
        }
        while let Some(end) = controls.iter().position(|&b| b == b'\n') {
            let line = controls.drain(..=end).collect::<Vec<_>>();
            // "dictate", or "dictate ar" / "dictate en" from the starting phrase.
            let dictate = match &line[..] {
                b"dictate\n" => Some(None),
                b"dictate ar\n" => Some(Some("ar")),
                b"dictate en\n" => Some(Some("en")),
                _ => None,
            };
            match &line[..] {
                _ if dictate.is_some() => {
                    if dictation.is_none() {
                        listening = false;
                        deadline = None;
                        if let Err(e) = begin_dictation(
                            &mut engine,
                            &mut dictation,
                            &mut dictation_deadline,
                            dictate.flatten(),
                        ) {
                            emit(json!({"event":"dictation_error","message":e.to_string()}))?;
                            emit(json!({"event":"dictation","state":"stopped","reason":"error","dropped":0}))?;
                            engine.as_mut().unwrap().begin(false)?;
                        }
                    }
                    continue;
                }
                b"dictate_stop\n" | b"cancel\n" if dictation.is_some() => {
                    end_dictation(
                        &mut dictation,
                        if line == b"cancel\n" { "cancel" } else { "stop" },
                    )?;
                    if mode == "dictation" {
                        return Ok(());
                    }
                    last_frame = now();
                    engine.as_mut().unwrap().begin(false)?;
                    ready(engine.as_ref(), false, None)?;
                    continue;
                }
                b"dictate_stop\n" => continue,
                _ => {}
            }
            end_dictation(&mut dictation, "cancel")?;
            listening = line == b"listen\n";
            deadline = if listening {
                Some(now() + timeout)
            } else {
                None
            };
            settling.stop();
            cooldown = 0.0;
            engine
                .as_mut()
                .ok_or("Controls require a recognition engine")?
                .begin(listening)?;
            ready(engine.as_ref(), listening, deadline)?;
        }
        if controls.len() > 4096 {
            return Err("Voice control line exceeds 4096 bytes".into());
        }
        if let Some(d) = &mut dictation {
            dictated(d.poll())?;
        }
        if !arrived.audio {
            continue;
        }
        if !input.frame(&mut pending)? {
            let detail = input.detail();
            return Err(if !detail.is_empty() {
                detail
            } else {
                "Microphone stream ended. Check the selected input, then Resume.".into()
            }
            .into());
        }
        last_frame = now();
        if mode == "test" {
            if now() >= next_level {
                let level = rms(&pending);
                noise.update(level);
                emit(json!({"event":"level","level":level,"gate":noise.gate()}))?;
                next_level = now() + 0.2;
            }
            continue;
        }
        engine.as_mut().unwrap().maintain()?;
        if let Some(d) = &mut dictation {
            let energy = rms(&pending);
            let quiet = energy < noise.gate();
            noise.update(energy);
            d.feed(&pending, quiet);
            dictated(d.poll())?;
            let idle = d.silent_frames as f64 * 0.03 >= dictation_idle && !d.pending();
            if idle || now() >= dictation_deadline {
                end_dictation(&mut dictation, if idle { "idle" } else { "limit" })?;
                if mode == "dictation" {
                    return Ok(());
                }
                last_frame = now();
                engine.as_mut().unwrap().begin(false)?;
                ready(engine.as_ref(), false, None)?;
            }
            continue;
        }
        if now() < cooldown {
            continue;
        }
        let energy = rms(&pending);
        let gate = noise.gate();
        noise.update(energy);
        let quiet = energy < gate;
        if settling.active {
            if !settling.frame(&pending, quiet) {
                continue;
            }
            let engine = engine.as_mut().unwrap();
            engine.begin_after_wake()?;
            if quiet {
                continue;
            }
            // The command has started. Quiet frames go to the engine's pre-roll.
            for frame in settling.frames.drain(..) {
                engine.feed(&frame, true)?;
            }
        }
        let mut captured_at = now();
        if listening && deadline.is_some_and(|d| captured_at >= d) {
            continue;
        }
        let result = engine.as_mut().unwrap().feed(&pending, quiet)?;
        last_frame = now();
        // What it heard so far is reported at the deadline: until then, the
        // rest of the command may still be coming.
        let Some(mut result) = result else {
            continue;
        };
        if result.kind == "wake" {
            listening = true;
            deadline = Some(now() + timeout);
            settling.start();
            emit(json!({"event":"wake","deadline":deadline}))?;
            let follow = engine.as_mut().unwrap().after_wake();
            if let Some(command) = follow {
                result = command;
                settling.stop();
                captured_at = now();
            } else {
                continue;
            }
        }
        command(&result, captured_at)?;
        if mode == "once" {
            return Ok(());
        }
        listening = false;
        deadline = None;
        engine.as_mut().unwrap().begin(false)?;
        cooldown = now() + 1.5;
    }
    Ok(())
}
#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn speech_ends_settling_at_once_and_keeps_the_quiet_before_it() {
        let mut settling = Settling::default();
        settling.start();
        assert!(!settling.frame(&[1; FRAME], true));
        assert!(!settling.frame(&[2; FRAME], true));
        assert!(settling.frame(&[3; FRAME], false));
        assert!(!settling.active);
        assert_eq!(settling.frames.iter().map(|f| f[0]).collect::<Vec<_>>(), vec![1, 2]);
    }
    #[test]
    fn enough_quiet_ends_settling_with_nothing_to_feed() {
        let mut settling = Settling::default();
        settling.start();
        for _ in 1..SETTLE_FRAMES {
            assert!(!settling.frame(&[0; FRAME], true));
        }
        assert!(settling.frame(&[0; FRAME], true));
        assert!(!settling.active);
        assert!(settling.frames.is_empty());
    }
    #[test]
    fn energy_and_adaptation() {
        assert_eq!(rms(&[0; 960]), 0.0);
        assert_eq!(rms(&[0, 128]), 1.0);
        let mut n = NoiseFloor {
            minimum: 0.005,
            adaptive: true,
            floor: None,
        };
        n.update(0.01);
        assert!((n.gate() - 0.03).abs() < 1e-12);
        n.update(0.0);
        assert!((n.gate() - 0.0285).abs() < 1e-12);
    }
}
