//! One application's sound, or everything except it, for a separate replay
//! track on Windows (`jarvis-voice --app-audio`).
//!
//! Windows' process loopback (build 20348 and later, so Windows 11) captures a
//! process tree's output, or all output except that tree. Request on stdin:
//!   {"process":"firefox","include":true}
//! "process" is the program's file name without ".exe"; the oldest running
//! process with that name whose parent has a different name is the tree root.
//! The stream is s16le 48 kHz stereo on stdout. Process loopback delivers no
//! packets while the tree is silent, so silence is written for those gaps and
//! the track stays in step with the video. The mode ends when stdout closes.
use crate::Result;

pub const RATE: u64 = 48000;
const FRAME_BYTES: usize = 4;
/// Pad only after this many milliseconds without packets, so ordinary packet
/// jitter never inserts silence.
const IDLE_MS: u64 = 100;

/// Frames of silence to write so the track covers `elapsed_ms` of wall time.
pub fn silence_needed(elapsed_ms: u64, written: u64, idle_ms: u64) -> u64 {
    if idle_ms < IDLE_MS {
        return 0;
    }
    (elapsed_ms * RATE / 1000).saturating_sub(written)
}

pub fn run(request: &serde_json::Value) -> Result<()> {
    let process = request["process"]
        .as_str()
        .filter(|p| !p.is_empty() && p.len() <= 64 && !p.contains(['\\', '/']))
        .ok_or("Name the program, for example firefox")?;
    let include = request["include"]
        .as_bool()
        .ok_or("Say whether to include or exclude the program")?;
    #[cfg(windows)]
    {
        windows_loopback::capture(process, include)
    }
    #[cfg(not(windows))]
    {
        let _ = (process, include);
        Err("Per-application audio capture is only implemented on Windows".into())
    }
}

#[cfg(windows)]
mod windows_loopback {
    use super::{silence_needed, FRAME_BYTES, RATE};
    use crate::Result;
    use std::io::Write;
    use std::time::Instant;
    use windows::core::{implement, IUnknown, Interface, Ref, HRESULT, PCWSTR};
    use windows::Win32::Foundation::{CloseHandle, HANDLE, WAIT_OBJECT_0};
    use windows::Win32::Media::Audio::{
        ActivateAudioInterfaceAsync, IActivateAudioInterfaceAsyncOperation,
        IActivateAudioInterfaceCompletionHandler, IActivateAudioInterfaceCompletionHandler_Impl,
        IAudioCaptureClient, IAudioClient, AUDCLNT_BUFFERFLAGS_SILENT, AUDCLNT_SHAREMODE_SHARED,
        AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM, AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
        AUDCLNT_STREAMFLAGS_LOOPBACK, AUDIOCLIENT_ACTIVATION_PARAMS,
        AUDIOCLIENT_ACTIVATION_PARAMS_0, AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
        AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS, PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE,
        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE, VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
        WAVEFORMATEX, WAVE_FORMAT_PCM,
    };
    use windows::Win32::System::Com::StructuredStorage::PROPVARIANT;
    use windows::Win32::System::Com::{CoInitializeEx, BLOB, COINIT_MULTITHREADED};
    use windows::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };
    use windows::Win32::System::Threading::{CreateEventW, SetEvent, WaitForSingleObject};
    use windows::Win32::System::Variant::VT_BLOB;

    /// False once FFmpeg's reader has closed, which is how capture stops.
    fn send(out: &mut impl Write, bytes: &[u8]) -> bool {
        out.write_all(bytes).and_then(|_| out.flush()).is_ok()
    }
    fn send_silence(out: &mut impl Write, mut bytes: usize) -> bool {
        let zeros = [0_u8; 3840];
        while bytes > 0 {
            let n = bytes.min(zeros.len());
            if !send(out, &zeros[..n]) {
                return false;
            }
            bytes -= n;
        }
        true
    }

    #[implement(IActivateAudioInterfaceCompletionHandler)]
    struct Activated(HANDLE);
    impl IActivateAudioInterfaceCompletionHandler_Impl for Activated_Impl {
        fn ActivateCompleted(
            &self,
            _operation: Ref<'_, IActivateAudioInterfaceAsyncOperation>,
        ) -> windows::core::Result<()> {
            unsafe { SetEvent(self.0) }
        }
    }

    /// The root of the named program's process tree.
    fn root_process(name: &str) -> Result<u32> {
        let wanted = format!("{}.exe", name.trim_end_matches(".exe")).to_lowercase();
        let mut all = vec![];
        unsafe {
            let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)?;
            let mut entry = PROCESSENTRY32W {
                dwSize: std::mem::size_of::<PROCESSENTRY32W>() as u32,
                ..Default::default()
            };
            let mut more = Process32FirstW(snapshot, &mut entry).is_ok();
            while more {
                let length = entry.szExeFile.iter().position(|&c| c == 0).unwrap_or(260);
                let exe = String::from_utf16_lossy(&entry.szExeFile[..length]).to_lowercase();
                all.push((entry.th32ProcessID, entry.th32ParentProcessID, exe));
                more = Process32NextW(snapshot, &mut entry).is_ok();
            }
            let _ = CloseHandle(snapshot);
        }
        all.iter()
            .filter(|(_, parent, exe)| {
                *exe == wanted && !all.iter().any(|(id, _, e)| id == parent && *e == wanted)
            })
            .map(|(id, _, _)| *id)
            .min()
            .ok_or_else(|| format!("{name} is not running. Start it before instant replay.").into())
    }

    unsafe fn activate(pid: u32, include: bool) -> Result<IAudioClient> {
        let params = AUDIOCLIENT_ACTIVATION_PARAMS {
            ActivationType: AUDIOCLIENT_ACTIVATION_TYPE_PROCESS_LOOPBACK,
            Anonymous: AUDIOCLIENT_ACTIVATION_PARAMS_0 {
                ProcessLoopbackParams: AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS {
                    TargetProcessId: pid,
                    ProcessLoopbackMode: if include {
                        PROCESS_LOOPBACK_MODE_INCLUDE_TARGET_PROCESS_TREE
                    } else {
                        PROCESS_LOOPBACK_MODE_EXCLUDE_TARGET_PROCESS_TREE
                    },
                },
            },
        };
        // A VT_BLOB that points at `params`; it is borrowed, never cleared.
        let mut variant: PROPVARIANT = std::mem::zeroed();
        let inner = &mut *variant.Anonymous.Anonymous;
        inner.vt = VT_BLOB;
        inner.Anonymous.blob = BLOB {
            cbSize: std::mem::size_of::<AUDIOCLIENT_ACTIVATION_PARAMS>() as u32,
            pBlobData: &params as *const _ as *mut u8,
        };
        let done = CreateEventW(None, false, false, PCWSTR::null())?;
        let handler: IActivateAudioInterfaceCompletionHandler = Activated(done).into();
        let result = (|| -> Result<IAudioClient> {
            let operation = ActivateAudioInterfaceAsync(
                VIRTUAL_AUDIO_DEVICE_PROCESS_LOOPBACK,
                &IAudioClient::IID,
                Some(&variant),
                &handler,
            )
            .map_err(|e| format!("Windows refused per-app audio ({e}). It needs Windows 11."))?;
            if WaitForSingleObject(done, 10_000) != WAIT_OBJECT_0 {
                return Err("Windows did not start per-app audio capture in time".into());
            }
            let mut status = HRESULT(0);
            let mut unknown: Option<IUnknown> = None;
            operation.GetActivateResult(&mut status, &mut unknown)?;
            status.ok().map_err(|e| {
                format!("Windows refused per-app audio ({e}). It needs Windows 11.")
            })?;
            Ok(unknown.ok_or("Windows returned no audio client")?.cast()?)
        })();
        let _ = CloseHandle(done);
        result
    }

    pub fn capture(name: &str, include: bool) -> Result<()> {
        unsafe {
            CoInitializeEx(None, COINIT_MULTITHREADED).ok()?;
            let client = activate(root_process(name)?, include)?;
            let format = WAVEFORMATEX {
                wFormatTag: WAVE_FORMAT_PCM as u16,
                nChannels: 2,
                nSamplesPerSec: RATE as u32,
                nAvgBytesPerSec: RATE as u32 * FRAME_BYTES as u32,
                nBlockAlign: FRAME_BYTES as u16,
                wBitsPerSample: 16,
                cbSize: 0,
            };
            client.Initialize(
                AUDCLNT_SHAREMODE_SHARED,
                AUDCLNT_STREAMFLAGS_LOOPBACK
                    | AUDCLNT_STREAMFLAGS_EVENTCALLBACK
                    | AUDCLNT_STREAMFLAGS_AUTOCONVERTPCM,
                2_000_000, // 200 ms in 100 ns units
                0,
                &format,
                None,
            )?;
            let event = CreateEventW(None, false, false, PCWSTR::null())?;
            let result = (|| -> Result<()> {
                client.SetEventHandle(event)?;
                let capture: IAudioCaptureClient = client.GetService()?;
                let mut out = std::io::stdout().lock();
                client.Start()?;
                let start = Instant::now();
                let mut last_packet = Instant::now();
                let mut written: u64 = 0;
                loop {
                    let _ = WaitForSingleObject(event, 20);
                    while capture.GetNextPacketSize()? > 0 {
                        let mut data = std::ptr::null_mut();
                        let mut frames = 0;
                        let mut flags = 0;
                        capture.GetBuffer(&mut data, &mut frames, &mut flags, None, None)?;
                        let bytes = frames as usize * FRAME_BYTES;
                        let ok =
                            if flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0 || data.is_null() {
                                send_silence(&mut out, bytes)
                            } else {
                                send(&mut out, std::slice::from_raw_parts(data, bytes))
                            };
                        capture.ReleaseBuffer(frames)?;
                        if !ok {
                            return Ok(());
                        }
                        written += frames as u64;
                        last_packet = Instant::now();
                    }
                    let gap = silence_needed(
                        start.elapsed().as_millis() as u64,
                        written,
                        last_packet.elapsed().as_millis() as u64,
                    );
                    if !send_silence(&mut out, gap as usize * FRAME_BYTES) {
                        return Ok(());
                    }
                    written += gap;
                }
            })();
            let _ = client.Stop();
            let _ = CloseHandle(event);
            result
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn silence_fills_only_real_gaps() {
        // Packets are flowing: never pad, even if slightly behind.
        assert_eq!(silence_needed(1000, 47000, 20), 0);
        // Silent for 500 ms after one second of sound: pad to wall time.
        assert_eq!(silence_needed(1500, 48000, 500), 24000);
        // Never negative when ahead of the clock.
        assert_eq!(silence_needed(1000, 50000, 500), 0);
    }

    #[test]
    fn requests_name_a_program_and_a_mode() {
        for bad in [
            serde_json::json!({"include": true}),
            serde_json::json!({"process": "C:\\x\\firefox", "include": true}),
            serde_json::json!({"process": "firefox"}),
        ] {
            assert!(run(&bad).is_err(), "{bad}");
        }
    }
}
