//! Microphone and control input for the worker loop.
//!
//! Linux reads `pw-record` and stdin with poll(2). Windows captures the
//! microphone with WASAPI, which converts to 16 kHz mono itself, and reads
//! stdin on a thread. `JARVIS_TEST_CAPTURE_FILE` replaces the microphone on
//! either platform with paced s16le 16 kHz PCM followed by silence, so whole-app
//! benchmarks can run without a device.
use crate::Result;
use serde_json::Value;
use std::collections::VecDeque;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::mpsc::{channel, Receiver, RecvTimeoutError, Sender};
use std::sync::Arc;
use std::time::Duration;

pub const FRAME: usize = 960;
/// About two seconds of 30 ms frames, like a full pipe from pw-record. Frames
/// beyond this while inference blocks the loop are dropped, never queued.
const QUEUED_FRAMES: usize = 67;

/// What one wait produced. `control` is `Some(empty)` when stdin closed.
#[derive(Default)]
pub struct Ready {
    pub control: Option<Vec<u8>>,
    pub audio: bool,
}

pub trait Input {
    /// Wait up to `timeout` for control bytes or a complete audio frame.
    fn wait(&mut self, timeout: Duration, control_open: bool) -> Result<Ready>;
    /// Copy the next complete frame; `false` means the stream ended.
    fn frame(&mut self, pcm: &mut [u8; FRAME]) -> Result<bool>;
    /// Diagnostic tail for an ended or failed stream.
    fn detail(&mut self) -> String;
}

/// The microphone's Windows name in a saved setting: settings store
/// "input:<name>", and a bare name (typed, or edited down to it) means the same.
#[cfg_attr(not(windows), allow(dead_code))]
fn microphone_name(id: &str) -> &str {
    id.strip_prefix("input:").unwrap_or(id).trim()
}

pub fn open(config: &Value, control: bool) -> Result<Box<dyn Input>> {
    let device = config["audio"]["device"].as_str().unwrap_or("").to_owned();
    if let Some(path) = std::env::var_os("JARVIS_TEST_CAPTURE_FILE") {
        let pcm = std::fs::read(path)?;
        return Ok(Box::new(Threaded::start(control, move |tx| {
            replay_file(pcm, tx)
        })));
    }
    #[cfg(windows)]
    {
        Ok(Box::new(Threaded::start(control, move |tx| {
            wasapi::capture(&device, tx)
        })))
    }
    #[cfg(unix)]
    {
        Ok(Box::new(unix::PipeWire::start(&device, control)?))
    }
}

enum Message {
    Audio(Box<[u8; FRAME]>),
    Control(Vec<u8>),
    Ended(String),
}

/// Frames come from a producer thread; stdin is read on another thread.
pub struct Producer {
    tx: Sender<Message>,
    queued: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    pending: Vec<u8>,
}
impl Producer {
    pub fn stopped(&self) -> bool {
        self.stop.load(Ordering::Relaxed)
    }
    /// Append captured PCM; emits complete frames.
    pub fn push(&mut self, mut pcm: &[u8]) {
        while !pcm.is_empty() {
            let take = (FRAME - self.pending.len()).min(pcm.len());
            self.pending.extend_from_slice(&pcm[..take]);
            pcm = &pcm[take..];
            if self.pending.len() == FRAME {
                let mut frame = Box::new([0; FRAME]);
                frame.copy_from_slice(&self.pending);
                self.pending.clear();
                if self.queued.load(Ordering::Relaxed) < QUEUED_FRAMES {
                    self.queued.fetch_add(1, Ordering::Relaxed);
                    let _ = self.tx.send(Message::Audio(frame));
                }
            }
        }
    }
}

struct Threaded {
    rx: Receiver<Message>,
    queued: Arc<AtomicUsize>,
    stop: Arc<AtomicBool>,
    frames: VecDeque<Box<[u8; FRAME]>>,
    ended: Option<String>,
}
impl Threaded {
    fn start(control: bool, capture: impl FnOnce(&mut Producer) -> Result<()> + Send + 'static) -> Self {
        let (tx, rx) = channel();
        let queued = Arc::new(AtomicUsize::new(0));
        let stop = Arc::new(AtomicBool::new(false));
        let mut producer = Producer {
            tx: tx.clone(),
            queued: queued.clone(),
            stop: stop.clone(),
            pending: Vec::with_capacity(FRAME),
        };
        std::thread::Builder::new()
            .name("jarvis-capture".into())
            .spawn(move || {
                let reason = match capture(&mut producer) {
                    Ok(()) => String::new(),
                    Err(error) => error.to_string(),
                };
                let _ = producer.tx.send(Message::Ended(reason));
            })
            .expect("capture thread");
        if control {
            std::thread::Builder::new()
                .name("jarvis-control".into())
                .spawn(move || {
                    use std::io::Read;
                    let mut stdin = std::io::stdin().lock();
                    let mut bytes = [0_u8; 4096];
                    loop {
                        let n = stdin.read(&mut bytes).unwrap_or(0);
                        if tx.send(Message::Control(bytes[..n].to_vec())).is_err() || n == 0 {
                            return;
                        }
                    }
                })
                .expect("control thread");
        }
        Self {
            rx,
            queued,
            stop,
            frames: VecDeque::new(),
            ended: None,
        }
    }
    fn accept(&mut self, message: Message, ready: &mut Ready) {
        match message {
            Message::Audio(frame) => {
                self.queued.fetch_sub(1, Ordering::Relaxed);
                self.frames.push_back(frame);
            }
            Message::Control(bytes) => ready.control.get_or_insert_default().extend(bytes),
            Message::Ended(reason) => self.ended = Some(reason),
        }
    }
}
impl Input for Threaded {
    fn wait(&mut self, timeout: Duration, _control_open: bool) -> Result<Ready> {
        let mut ready = Ready::default();
        if self.frames.is_empty() && self.ended.is_none() {
            match self.rx.recv_timeout(timeout) {
                Ok(message) => self.accept(message, &mut ready),
                Err(RecvTimeoutError::Timeout) => {}
                Err(RecvTimeoutError::Disconnected) => self.ended = Some(String::new()),
            }
        }
        // Drain everything already queued so control is never behind audio.
        while let Ok(message) = self.rx.try_recv() {
            self.accept(message, &mut ready);
        }
        ready.audio = !self.frames.is_empty() || self.ended.is_some();
        Ok(ready)
    }
    fn frame(&mut self, pcm: &mut [u8; FRAME]) -> Result<bool> {
        if let Some(frame) = self.frames.pop_front() {
            pcm.copy_from_slice(&frame[..]);
            return Ok(true);
        }
        Ok(false)
    }
    fn detail(&mut self) -> String {
        self.ended.clone().unwrap_or_default()
    }
}
impl Drop for Threaded {
    fn drop(&mut self) {
        self.stop.store(true, Ordering::Relaxed);
    }
}

/// Test source: the file in real time, then silence until the worker stops.
fn replay_file(pcm: Vec<u8>, out: &mut Producer) -> Result<()> {
    if pcm.len() % 2 != 0 {
        return Err("Test capture must be s16le PCM".into());
    }
    let start = std::time::Instant::now();
    let silence = [0_u8; FRAME];
    let mut sent = 0_usize;
    while !out.stopped() {
        let chunk = pcm.get(sent..(sent + FRAME).min(pcm.len())).filter(|c| !c.is_empty());
        out.push(chunk.unwrap_or(&silence));
        sent += FRAME;
        // 960 bytes of 16 kHz s16 mono are 30 ms.
        let due = Duration::from_micros(sent as u64 * 1_000_000 / 32_000);
        if let Some(wait) = due.checked_sub(start.elapsed()) {
            std::thread::sleep(wait);
        }
    }
    Ok(())
}

#[cfg(unix)]
mod unix {
    use super::{Input, Ready, FRAME};
    use crate::Result;
    use std::collections::VecDeque;
    use std::io::Read;
    use std::os::fd::AsRawFd;
    use std::os::unix::process::CommandExt;
    use std::process::{Child, ChildStderr, ChildStdout, Command, Stdio};
    use std::time::Duration;

    pub struct PipeWire {
        child: Child,
        audio: ChildStdout,
        stderr: ChildStderr,
        stderr_open: bool,
        errors: VecDeque<u8>,
        pending: [u8; FRAME],
        filled: usize,
    }
    impl PipeWire {
        pub fn start(device: &str, _control: bool) -> Result<Self> {
            let mut capture = Command::new("pw-record");
            capture.args([
                "--raw",
                "--rate",
                "16000",
                "--channels",
                "1",
                "--format",
                "s16",
                "--latency",
                "30ms",
            ]);
            if !device.is_empty() {
                capture.args(["--target", device]);
            }
            capture
                .arg("-")
                .stdin(Stdio::null())
                .stdout(Stdio::piped())
                .stderr(Stdio::piped());
            let owner = std::process::id() as i32;
            // SAFETY: only async-signal-safe libc calls run between fork and exec.
            unsafe {
                capture.pre_exec(move || {
                    if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    if libc::getppid() != owner {
                        libc::_exit(1);
                    }
                    Ok(())
                });
            }
            let mut child = capture.spawn()?;
            let audio = child.stdout.take().ok_or("Missing capture stdout")?;
            let stderr = child.stderr.take().ok_or("Missing capture stderr")?;
            Ok(Self {
                child,
                audio,
                stderr,
                stderr_open: true,
                errors: VecDeque::with_capacity(2048),
                pending: [0; FRAME],
                filled: 0,
            })
        }
    }
    impl Input for PipeWire {
        fn wait(&mut self, timeout: Duration, control_open: bool) -> Result<Ready> {
            let mut polls = [
                libc::pollfd {
                    fd: if control_open { 0 } else { -1 },
                    events: libc::POLLIN,
                    revents: 0,
                },
                libc::pollfd {
                    fd: self.audio.as_raw_fd(),
                    events: libc::POLLIN,
                    revents: 0,
                },
                libc::pollfd {
                    fd: if self.stderr_open {
                        self.stderr.as_raw_fd()
                    } else {
                        -1
                    },
                    events: libc::POLLIN,
                    revents: 0,
                },
            ];
            let wait = (timeout.as_secs_f64() * 1000.0).ceil().min(i32::MAX as f64) as i32;
            let mut ready = Ready::default();
            if unsafe { libc::poll(polls.as_mut_ptr(), 3, wait) } < 0 {
                let error = std::io::Error::last_os_error();
                if error.kind() == std::io::ErrorKind::Interrupted {
                    return Ok(ready);
                }
                return Err(error.into());
            }
            if polls[2].revents != 0 {
                let mut bytes = [0_u8; 512];
                let n = self.stderr.read(&mut bytes)?;
                if n == 0 {
                    self.stderr_open = false;
                }
                for &b in &bytes[..n] {
                    if self.errors.len() == 2048 {
                        self.errors.pop_front();
                    }
                    self.errors.push_back(b);
                }
            }
            if control_open && polls[0].revents != 0 {
                let mut bytes = [0_u8; 4096];
                let n = unsafe { libc::read(0, bytes.as_mut_ptr().cast(), bytes.len()) };
                if n < 0 {
                    let error = std::io::Error::last_os_error();
                    if error.kind() != std::io::ErrorKind::Interrupted {
                        return Err(error.into());
                    }
                } else {
                    ready.control = Some(bytes[..n as usize].to_vec());
                }
            }
            if polls[1].revents != 0 {
                let n = self.audio.read(&mut self.pending[self.filled..])?;
                if n == 0 {
                    return Err(format!(
                        "Microphone stream ended. Check PipeWire and the selected input. {}",
                        self.detail()
                    )
                    .into());
                }
                self.filled += n;
                ready.audio = self.filled == FRAME;
            }
            Ok(ready)
        }
        fn frame(&mut self, pcm: &mut [u8; FRAME]) -> Result<bool> {
            if self.filled < FRAME {
                return Ok(false);
            }
            self.filled = 0;
            pcm.copy_from_slice(&self.pending);
            Ok(true)
        }
        fn detail(&mut self) -> String {
            String::from_utf8_lossy(&self.errors.iter().copied().collect::<Vec<_>>()).into_owned()
        }
    }
    impl Drop for PipeWire {
        fn drop(&mut self) {
            unsafe {
                libc::kill(self.child.id() as i32, libc::SIGTERM);
            }
            let start = std::time::Instant::now();
            while start.elapsed().as_secs_f64() < 2.0 {
                if !matches!(self.child.try_wait(), Ok(None)) {
                    return;
                }
                std::thread::sleep(Duration::from_millis(10));
            }
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

#[cfg(windows)]
mod wasapi {
    use super::Producer;
    use crate::Result;
    use windows::core::{HRESULT, PCWSTR};
    use windows::Win32::Devices::FunctionDiscovery::PKEY_Device_FriendlyName;
    use windows::Win32::Foundation::{CloseHandle, E_ACCESSDENIED, WAIT_OBJECT_0};
    use windows::Win32::Media::Audio::{
        eCapture, eConsole, IAudioCaptureClient, IAudioClient, IMMDevice, IMMDeviceEnumerator,
        MMDeviceEnumerator, AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_E_DEVICE_INVALIDATED,
        AUDCLNT_SHAREMODE_SHARED, AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
        AUDCLNT_STREAMFLAGS_EVENTCALLBACK, AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
        DEVICE_STATE_ACTIVE, WAVEFORMATEX, WAVE_FORMAT_PCM,
    };
    use windows::Win32::System::Com::StructuredStorage::PropVariantToStringAlloc;
    use windows::Win32::System::Com::{
        CoCreateInstance, CoInitializeEx, CoTaskMemFree, CLSCTX_ALL, COINIT_MULTITHREADED,
        STGM_READ,
    };
    use windows::Win32::System::Threading::{CreateEventW, WaitForSingleObject};

    fn friendly(error: windows::core::Error) -> Box<dyn std::error::Error> {
        let code: HRESULT = error.code();
        if code == E_ACCESSDENIED {
            return "Windows blocked microphone access. Turn on Settings > Privacy > Microphone > Let desktop apps access your microphone, then Resume.".into();
        }
        if code == AUDCLNT_E_DEVICE_INVALIDATED {
            return "The microphone was disconnected or changed. Check the input device, then Resume.".into();
        }
        format!("Windows audio capture failed: {error}").into()
    }

    unsafe fn name(device: &IMMDevice) -> windows::core::Result<String> {
        let store = device.OpenPropertyStore(STGM_READ)?;
        let value = store.GetValue(&PKEY_Device_FriendlyName)?;
        let text = PropVariantToStringAlloc(&value)?;
        let result = text.to_string().unwrap_or_default();
        CoTaskMemFree(Some(text.0 as *const _));
        Ok(result)
    }

    /// Settings stores PortAudio-style ids, "input:<friendly name>".
    unsafe fn select(enumerator: &IMMDeviceEnumerator, id: &str) -> Result<IMMDevice> {
        if id.is_empty() || id == "default_input" {
            return enumerator
                .GetDefaultAudioEndpoint(eCapture, eConsole)
                .map_err(|_| "No Windows microphone is available; connect or enable an input device".into());
        }
        let wanted = super::microphone_name(id);
        let all = enumerator
            .EnumAudioEndpoints(eCapture, DEVICE_STATE_ACTIVE)
            .map_err(friendly)?;
        let mut found = vec![];
        for index in 0..all.GetCount().map_err(friendly)? {
            let device = all.Item(index).map_err(friendly)?;
            if name(&device).is_ok_and(|n| n == wanted) {
                found.push(device);
            }
        }
        match found.len() {
            1 => Ok(found.pop().unwrap()),
            _ => Err("The selected Windows audio device is unavailable or ambiguous; refresh audio devices".into()),
        }
    }

    pub fn capture(device: &str, out: &mut Producer) -> Result<()> {
        unsafe {
            CoInitializeEx(None, COINIT_MULTITHREADED).ok().map_err(friendly)?;
            let enumerator: IMMDeviceEnumerator =
                CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL).map_err(friendly)?;
            let device = select(&enumerator, device)?;
            let client: IAudioClient = device.Activate(CLSCTX_ALL, None).map_err(friendly)?;
            // The shared-mode engine converts the device mix to 16 kHz mono s16.
            let format = WAVEFORMATEX {
                wFormatTag: WAVE_FORMAT_PCM as u16,
                nChannels: 1,
                nSamplesPerSec: 16000,
                nAvgBytesPerSec: 32000,
                nBlockAlign: 2,
                wBitsPerSample: 16,
                cbSize: 0,
            };
            client
                .Initialize(
                    AUDCLNT_SHAREMODE_SHARED,
                    AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                        | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM
                        | AUDCLNT_STREAMFLAGS_SRC_DEFAULT_QUALITY,
                    2_000_000, // 200 ms in 100 ns units
                    0,
                    &format,
                    None,
                )
                .map_err(friendly)?;
            let event = CreateEventW(None, false, false, PCWSTR::null()).map_err(friendly)?;
            let result = (|| -> Result<()> {
                client.SetEventHandle(event).map_err(friendly)?;
                let capture: IAudioCaptureClient = client.GetService().map_err(friendly)?;
                client.Start().map_err(friendly)?;
                let silence = [0_u8; 3200];
                while !out.stopped() {
                    if WaitForSingleObject(event, 200) != WAIT_OBJECT_0 {
                        continue;
                    }
                    while capture.GetNextPacketSize().map_err(friendly)? > 0 {
                        let mut data = std::ptr::null_mut();
                        let mut frames = 0;
                        let mut flags = 0;
                        capture
                            .GetBuffer(&mut data, &mut frames, &mut flags, None, None)
                            .map_err(friendly)?;
                        let bytes = frames as usize * 2;
                        if flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0 || data.is_null() {
                            let mut left = bytes;
                            while left > 0 {
                                let n = left.min(silence.len());
                                out.push(&silence[..n]);
                                left -= n;
                            }
                        } else {
                            out.push(std::slice::from_raw_parts(data, bytes));
                        }
                        capture.ReleaseBuffer(frames).map_err(friendly)?;
                    }
                }
                let _ = client.Stop();
                Ok(())
            })();
            let _ = CloseHandle(event);
            result
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_microphone_is_found_with_or_without_its_id_prefix() {
        // Found on the user's PC: the setting edited down to the bare name left
        // Jarvis not listening ("unavailable or ambiguous").
        let name = "Microphone (USB Podcast Microphone)";
        assert_eq!(microphone_name("input:Microphone (USB Podcast Microphone)"), name);
        assert_eq!(microphone_name(name), name);
        assert_eq!(microphone_name(" Microphone (USB Podcast Microphone) "), name);
    }

    fn producer() -> (Producer, Receiver<Message>, Arc<AtomicUsize>) {
        let (tx, rx) = channel();
        let queued = Arc::new(AtomicUsize::new(0));
        let producer = Producer {
            tx,
            queued: queued.clone(),
            stop: Arc::new(AtomicBool::new(false)),
            pending: Vec::new(),
        };
        (producer, rx, queued)
    }

    #[test]
    fn producer_emits_whole_frames_across_packet_boundaries() {
        let (mut out, rx, queued) = producer();
        let pcm: Vec<u8> = (0..FRAME * 2 + 100).map(|i| i as u8).collect();
        for packet in pcm.chunks(333) {
            out.push(packet);
        }
        let frames: Vec<_> = rx
            .try_iter()
            .map(|m| match m {
                Message::Audio(frame) => frame,
                _ => panic!("unexpected message"),
            })
            .collect();
        assert_eq!(frames.len(), 2);
        assert_eq!(&frames[1][..], &pcm[FRAME..FRAME * 2]);
        assert_eq!(out.pending.len(), 100);
        assert_eq!(queued.load(Ordering::Relaxed), 2);
    }

    #[test]
    fn a_blocked_consumer_bounds_queued_audio() {
        let (mut out, rx, queued) = producer();
        out.push(&vec![0; FRAME * (QUEUED_FRAMES + 20)]);
        assert_eq!(rx.try_iter().count(), QUEUED_FRAMES);
        assert_eq!(queued.load(Ordering::Relaxed), QUEUED_FRAMES);
    }

    #[test]
    fn test_capture_is_paced_then_silent() {
        let mut fixture = vec![7_u8; FRAME * 2];
        fixture.extend([9_u8; 10]);
        let start = std::time::Instant::now();
        let mut input = Threaded::start(false, move |out| replay_file(fixture, out));
        let mut frames = vec![];
        while frames.len() < 4 {
            if input.wait(Duration::from_secs(1), false).unwrap().audio {
                let mut pcm = [0; FRAME];
                assert!(input.frame(&mut pcm).unwrap());
                frames.push(pcm);
            }
        }
        // Four 30 ms frames cannot arrive faster than real time (first is immediate).
        assert!(start.elapsed() >= Duration::from_millis(85));
        assert!(frames[0].iter().all(|&b| b == 7));
        assert_eq!(&frames[2][..10], &[9; 10]);
        assert!(frames[2][10..].iter().all(|&b| b == 0));
        assert!(frames[3].iter().all(|&b| b == 0));
        drop(input);
    }

    #[test]
    fn odd_length_test_capture_is_refused() {
        let mut input = Threaded::start(false, |out| replay_file(vec![0; 3], out));
        let ready = input.wait(Duration::from_secs(2), false).unwrap();
        assert!(ready.audio);
        assert!(!input.frame(&mut [0; FRAME]).unwrap());
        assert!(input.detail().contains("s16le"));
    }
}
