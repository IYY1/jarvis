//! One paced file request, multiple bounded command windows. Busy detections
//! are discarded, never queued; recovery requires fresh quiet after release.
use super::*;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Config {
    pub max_windows: usize,
    pub worker_timeout_ms: u64,
    pub recovery_quiet_ms: usize,
    /// Explicit Linux pressure diagnostic: prefer killing the command child.
    #[serde(default)]
    pub command_oom_score_adj: Option<i32>,
}
impl Config {
    pub fn validate(&self, cycles: usize, endpoint: bool) -> Result<()> {
        if cycles != 1
            || !endpoint
            || !(1..=32).contains(&self.max_windows)
            || !(1000..=15000).contains(&self.worker_timeout_ms)
            || !(300..=3000).contains(&self.recovery_quiet_ms)
            || self
                .command_oom_score_adj
                .is_some_and(|score| !(0..=1000).contains(&score))
        {
            return Err("Continuous mode needs one cycle, a quiet endpoint, 1–32 windows, 1000–15000 ms worker deadline and 300–3000 ms recovery quiet".into());
        }
        Ok(())
    }
}

struct Recovery {
    quiet_samples: usize,
    armed_after: Option<usize>,
}
impl Recovery {
    fn release(&mut self) {
        self.quiet_samples = 0;
        self.armed_after = None;
    }
    fn observe(&mut self, frame: &[u8], end: usize, rms: f64, quiet_ms: usize) -> bool {
        let energy: f64 = frame
            .as_chunks::<2>()
            .0
            .iter()
            .map(|b| (i16::from_le_bytes(*b) as f64 / 32768.0).powi(2))
            .sum();
        if energy >= rms.powi(2) * (frame.len() / 2) as f64 {
            self.quiet_samples = 0;
        } else {
            self.quiet_samples += frame.len() / 2;
        }
        if self.quiet_samples >= quiet_ms * 16 {
            self.armed_after = Some(end);
            true
        } else {
            false
        }
    }
    fn reject(
        &self,
        trigger: usize,
        busy: bool,
        count: usize,
        limit: usize,
    ) -> Option<&'static str> {
        if busy {
            Some("busy")
        } else if count >= limit {
            Some("window_limit")
        } else if self.armed_after.is_none_or(|after| trigger <= after) {
            Some("recovering_or_stale")
        } else {
            None
        }
    }
}

struct Window {
    child: Process,
    spawned: Instant,
    trigger: Value,
    trigger_sample: usize,
    endpoint: Endpoint,
    endpoint_reason: Option<&'static str>,
    endpoint_sample: Option<usize>,
    payload_sent: bool,
    submitted: bool,
    buffer_path: Option<std::path::PathBuf>,
    ready: Value,
    result: Value,
    result_elapsed: Option<f64>,
    error: Value,
}

pub(super) fn run(
    config: &super::Config,
    continuous: &Config,
    payload: &Value,
    path: &str,
    detector: &mut Process,
    sampler: &Sampler,
) -> Result<usize> {
    let file = File::open(path)?;
    let length = file.metadata()?.len() as usize;
    if length == 0 || length > 120 * 32000 || !length.is_multiple_of(2) {
        return Err("Continuous file must be nonempty complete s16le up to 120 seconds".into());
    }
    let endpoint_config = config.quiet_endpoint.ok_or("Missing endpoint")?;
    let mut reader = BufReader::new(file.take(length as u64));
    let start = Instant::now();
    detector.send(&json!({"path":path,"paced":true}))?;
    sampler.reset();
    let mut audio = Audio::new(config.preroll_ms, config.max_buffer_ms);
    let mut window: Option<Window> = None;
    let mut recovery = Recovery {
        quiet_samples: 0,
        armed_after: Some(0),
    };
    let mut detector_complete = None;
    let mut reports = Vec::new();
    let mut count = 0;
    let mut ignored = 0;
    let mut worker_failures = 0;
    let mut successful_workers = 0;
    let mut bytes = [0u8; 960];
    while audio.consumed < length || detector_complete.is_none() || window.is_some() {
        if start.elapsed() > Duration::from_secs(155) {
            return Err("Continuous deadline exceeded".into());
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
        if n > 0
            && window.is_none()
            && recovery.armed_after.is_none()
            && recovery.observe(
                &bytes[..n],
                audio.consumed / 2,
                endpoint_config.rms,
                continuous.recovery_quiet_ms,
            )
        {
            emit(json!({"event":"rearmed","input_seconds":audio.consumed as f64/32000.0}))?;
        }
        while let Ok(event) = detector.events().try_recv() {
            let event = event?;
            match event["event"].as_str() {
                Some("keyword") => {
                    let trigger_sample = (event["audio_seconds"]
                        .as_f64()
                        .ok_or("Missing input time")?
                        * 16000.0)
                        .round() as usize;
                    if let Some(reason) = recovery.reject(
                        trigger_sample,
                        window.is_some(),
                        count,
                        continuous.max_windows,
                    ) {
                        ignored += 1;
                        emit(json!({"event":"trigger_ignored","reason":reason,"trigger":event}))?;
                        continue;
                    }
                    audio.trigger_from(
                        trigger_sample,
                        config.preroll_ms,
                        recovery.armed_after.unwrap() * 2,
                    )?;
                    let child = Process::spawn_with_policy(
                        &config.voice_binary,
                        &["--engine"],
                        config.join_readers,
                    )?;
                    if let Some(score) = continuous.command_oom_score_adj {
                        std::fs::write(
                            format!("/proc/{}/oom_score_adj", child.child.id()),
                            score.to_string(),
                        )?;
                    }
                    sampler.add(child.child.id());
                    count += 1;
                    emit(
                        json!({"event":"handoff_started","window":count,"worker_pid":child.child.id(),"trigger":event,"buffer_start_seconds":audio.start.unwrap() as f64/32000.0}),
                    )?;
                    window = Some(Window {
                        child,
                        spawned: Instant::now(),
                        trigger: event,
                        trigger_sample,
                        endpoint: Endpoint {
                            config: endpoint_config,
                            speech: 0,
                            quiet: 0,
                            last_speech_end: None,
                        },
                        endpoint_reason: None,
                        endpoint_sample: None,
                        payload_sent: false,
                        submitted: false,
                        buffer_path: None,
                        ready: Value::Null,
                        result: Value::Null,
                        result_elapsed: None,
                        error: Value::Null,
                    });
                }
                Some("complete") => detector_complete = Some(event),
                _ => return Err(format!("Unexpected detector event: {event}").into()),
            }
        }
        if detector_complete.is_none() && detector.child.try_wait()?.is_some() {
            return Err("Detector exited early".into());
        }
        let mut release = None;
        if let Some(w) = &mut window {
            if !audio.frozen {
                if n > 0 && w.endpoint.observe(&bytes[..n], audio.consumed / 2) {
                    w.endpoint_reason = Some("quiet");
                } else if audio.consumed / 2 >= w.trigger_sample + endpoint_config.timeout_ms * 16 {
                    w.endpoint_reason = Some("command_timeout");
                    release = Some("command_timeout");
                } else if audio.consumed == length {
                    w.endpoint_reason = Some("input_ended_before_endpoint");
                    release = Some("input_ended_before_endpoint");
                }
                if w.endpoint_reason.is_some() {
                    audio.frozen = true;
                    w.endpoint_sample = Some(audio.consumed / 2);
                }
            }
            if w.spawned.elapsed() >= Duration::from_millis(continuous.worker_timeout_ms) {
                release = Some("worker_timeout");
            }
            if release.is_none() {
                if !w.payload_sent
                    && w.spawned.elapsed() >= Duration::from_millis(config.payload_delay_ms)
                {
                    match w.child.send(payload) {
                        Ok(()) => w.payload_sent = true,
                        Err(error) => {
                            w.error = json!({"kind":"stdin","message":error.to_string()});
                            release = Some("worker_io_error");
                        }
                    }
                }
                if release.is_none() && audio.frozen && w.payload_sent && !w.submitted {
                    let path = Path::new(&config.work_dir)
                        .join(format!("continuous-{}-{count}.raw", std::process::id()));
                    File::options()
                        .write(true)
                        .create_new(true)
                        .open(&path)?
                        .write_all(&audio.buffered)?;
                    w.buffer_path = Some(path.clone());
                    match w
                        .child
                        .send(&json!({"path":path,"session":"wake","trim_heap":false}))
                    {
                        Ok(()) => w.submitted = true,
                        Err(error) => {
                            w.error = json!({"kind":"stdin","message":error.to_string()});
                            release = Some("worker_io_error");
                        }
                    }
                }
                let mut output_closed = false;
                while release.is_none() {
                    let event = match w.child.events().try_recv() {
                        Ok(Ok(event)) => event,
                        Ok(Err(message)) => {
                            w.error = json!({"kind":"protocol","message":message});
                            release = Some("worker_protocol_error");
                            break;
                        }
                        Err(mpsc::TryRecvError::Empty) => break,
                        Err(mpsc::TryRecvError::Disconnected) => {
                            output_closed = true;
                            break;
                        }
                    };
                    match event["event"].as_str() {
                        Some("ready") => w.ready = event,
                        Some("result") if w.submitted && w.result.is_null() => {
                            w.result = event;
                            w.result_elapsed = Some(start.elapsed().as_secs_f64());
                            w.child.input = None;
                        }
                        Some("error") => {
                            w.error = event;
                            release = Some("worker_error");
                        }
                        _ => {
                            w.error = json!({"kind":"protocol","event":event});
                            release = Some("worker_protocol_error");
                        }
                    }
                }
                if release.is_none() {
                    if let Some(status) = w.child.child.try_wait()? {
                        // A successful process can exit before its stdout-reader thread
                        // delivers the final event. Drain through EOF before rejecting it.
                        if !status.success() || (w.result.is_null() && output_closed) {
                            w.error = json!({"kind":"exit","status":status.to_string()});
                            release = Some("worker_exit");
                        } else if !w.result.is_null() {
                            release = Some("result");
                        }
                    }
                }
            }
        }
        if let Some(reason) = release {
            let mut w = window.take().ok_or("Release without window")?;
            sampler.snapshot();
            // A hard deadline also covers model loading and shutdown. No queued retry.
            if reason != "result" {
                let _ = w.child.child.kill();
            }
            let status = w.child.child.wait()?;
            if !w.error.is_null() {
                worker_failures += 1;
            } else if reason == "result" {
                successful_workers += 1;
            }
            #[cfg(unix)]
            let signal = {
                use std::os::unix::process::ExitStatusExt;
                status.signal()
            };
            #[cfg(not(unix))]
            let signal: Option<i32> = None;
            sampler.remove(w.child.child.id());
            drop(w.child);
            let mut report = json!({"event":"window_complete","window":count,"trigger":w.trigger,
                "release_reason":reason,"endpoint_reason":w.endpoint_reason,"worker_error":w.error,
                "worker_exit_code":status.code(),"worker_exit_signal":signal,
                "endpoint_input_seconds":w.endpoint_sample.map(|s|s as f64/16000.0),
                "buffer_start_seconds":audio.start.map(|s|s as f64/32000.0),
                "buffer_bytes":audio.buffered.len(),"buffer_capacity_bytes":audio.buffered.capacity(),
                "ring_capacity_bytes":audio.ring.capacity(),"buffer_path":w.buffer_path,
                "command_ready":w.ready,"command_result":w.result,"result_elapsed_seconds":w.result_elapsed,
                "released_input_seconds":audio.consumed as f64/32000.0});
            audio.buffered = Vec::new();
            audio.start = None;
            audio.frozen = false;
            report["waiting_after_release"] = sampler.current();
            emit(report.clone())?;
            if config.retain_reports {
                reports.push(report);
            }
            recovery.release();
        }
    }
    thread::sleep(Duration::from_millis(config.settle_ms));
    emit(
        json!({"event":"complete","continuous":true,"windows":count,"ignored_triggers":ignored,
        "reports":reports,"retain_reports":config.retain_reports,"detector_complete":detector_complete,
        "worker_failures":worker_failures,"successful_workers":successful_workers,
        "aborted_windows":count-successful_workers-worker_failures,"all_command_workers_succeeded":successful_workers==count,
        "final_waiting":sampler.current(),"memory":sampler.snapshot(),
        "production_defaults_changed":false,"whole_application_acceptance":false}),
    )?;
    Ok(worker_failures)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn busy_and_timeout_recovery_never_queue_or_replay_stale_triggers() {
        let mut r = Recovery {
            quiet_samples: 0,
            armed_after: Some(0),
        };
        assert_eq!(r.reject(480, true, 1, 3), Some("busy"));
        r.release();
        assert_eq!(r.reject(480, false, 1, 3), Some("recovering_or_stale"));
        for i in 1..20 {
            assert!(!r.observe(&[0; 960], i * 480, 0.001, 600));
        }
        assert!(!r.observe(&1000i16.to_le_bytes().repeat(480), 20 * 480, 0.001, 600));
        for i in 21..40 {
            assert!(!r.observe(&[0; 960], i * 480, 0.001, 600));
        }
        assert!(r.observe(&[0; 960], 40 * 480, 0.001, 600));
        assert_eq!(r.reject(39 * 480, false, 1, 3), Some("recovering_or_stale"));
        assert_eq!(r.reject(41 * 480, false, 1, 3), None);
        assert_eq!(r.reject(41 * 480, false, 3, 3), Some("window_limit"));
    }
    #[test]
    fn later_windows_keep_byte_identical_onsets_and_exclude_busy_history() {
        let mut a = Audio::new(300, 1000);
        for n in 0u16..30 {
            a.push(&n.to_le_bytes().repeat(480)).unwrap();
        }
        a.trigger(30 * 480, 300).unwrap();
        a.frozen = true;
        for n in 30u16..50 {
            a.push(&n.to_le_bytes().repeat(480)).unwrap();
        }
        a.buffered = Vec::new();
        a.start = None;
        a.frozen = false;
        a.trigger_from(50 * 480, 300, 45 * 960).unwrap();
        let expected: Vec<_> = (45u16..50)
            .flat_map(|n| n.to_le_bytes().repeat(480))
            .collect();
        assert_eq!(a.buffered, expected);
        assert_eq!(a.start, Some(45 * 960));
    }
}
