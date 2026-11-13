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
//!
//! The program may start late or close: its track is silent while it isn't
//! running (everything else, for "include": false) and follows it once it starts.
//!
//! {"list":true} prints {"apps":[{"name","label","playing","game"}]}: programs
//! with a sound session on an active output, playing now or not. {"watch":true}
//! prints that line again whenever it changes, until stdout closes.
//!
//! "process": "@game" follows whichever game is running: a program with sound
//! under steamapps\common, Epic Games or GOG Galaxy\Games (as games.py names
//! games), the one playing first.
use crate::Result;

/// The process name that means "the game running now".
pub const GAME: &str = "@game";

/// A program installed where Steam, Epic or GOG put games.
pub fn is_game_path(path: &str) -> bool {
    let path = path.to_lowercase();
    ["\\steamapps\\common\\", "\\epic games\\", "\\gog galaxy\\games\\"].iter().any(|m| path.contains(m))
        && !NOT_GAMES.iter().any(|m| path.contains(m))
}

/// Store programs installed beside the games that are not games: Epic's launcher, its
/// web helper and overlay, and Steam tools. Any sound from one of them made the game
/// track follow it instead of the game.
const NOT_GAMES: [&str; 5] = [
    "\\epic games\\launcher\\",
    "\\epic games\\epic online services\\",
    "\\steamapps\\common\\wallpaper_engine\\",
    "\\steamapps\\common\\steamworks shared\\",
    "\\steamapps\\common\\steamvr\\",
];

pub const RATE: u64 = 48000;
const FRAME_BYTES: usize = 4;
/// Pad only after this many milliseconds without packets, so ordinary packet
/// jitter never inserts silence. Well under 100 ms: FFmpeg holds video back, and
/// loses its frames, once it runs that far ahead of the sound.
const IDLE_MS: u64 = 60;

/// Frames of silence to write so the track covers `elapsed_ms` of wall time.
pub fn silence_needed(elapsed_ms: u64, written: u64, idle_ms: u64) -> u64 {
    if idle_ms < IDLE_MS {
        return 0;
    }
    (elapsed_ms * RATE / 1000).saturating_sub(written)
}

/// Sound starting again after a quiet spell is only noticed when its packet
/// arrives, up to IDLE_MS after the last padding: without this it would land
/// that much early. Frames of silence to write first so a packet of `frames`
/// ends at wall time; nothing for ordinary packet jitter.
pub const LATE_FRAMES: u64 = RATE * 30 / 1000;
pub fn silence_before(elapsed_ms: u64, written: u64, frames: u64) -> u64 {
    let behind = (elapsed_ms * RATE / 1000).saturating_sub(written + frames);
    if behind > LATE_FRAMES { behind } else { 0 }
}

pub fn run(request: &serde_json::Value) -> Result<()> {
    if request["list"] == true || request["watch"] == true {
        #[cfg(windows)]
        {
            use std::io::Write;
            let mut out = std::io::stdout().lock();
            let mut last = String::new();
            loop {
                let line = serde_json::json!({"apps": windows_loopback::playing()?}).to_string();
                // Unchanged: an empty line, so a window that went away is noticed.
                let shown = if line != last { line.as_str() } else { "" };
                if writeln!(out, "{shown}").and_then(|_| out.flush()).is_err() {
                    return Ok(()); // the settings window closed its end
                }
                last = line;
                if request["watch"] != true {
                    return Ok(());
                }
                std::thread::sleep(std::time::Duration::from_secs(2));
            }
        }
        #[cfg(not(windows))]
        return Err("Listing apps playing sound is only implemented on Windows".into());
    }
    let process = request["process"]
        .as_str()
        .filter(|p| !p.is_empty() && p.len() <= 64 && !p.contains(['\\', '/']))
        .ok_or("Name the program, for example firefox")?;
    let include = request["include"]
        .as_bool()
        .ok_or("Say whether to include or exclude the program")?;
    let pipe = match request.get("pipe") {
        None => None,
        Some(pipe) => Some(
            pipe.as_str()
                .filter(|p| p.starts_with(r"\\.\pipe\Jarvis-") && p.len() <= 240)
                .ok_or("The program's sound needs a private pipe")?,
        ),
    };
    #[cfg(windows)]
    {
        windows_loopback::capture(process, include, pipe)
    }
    #[cfg(not(windows))]
    {
        let _ = (process, include, pipe);
        Err("Per-application audio capture is only implemented on Windows".into())
    }
}

#[cfg(windows)]
mod windows_loopback {
    use super::{silence_before, silence_needed, FRAME_BYTES, RATE};
    use crate::Result;
    use std::io::Write;
    use std::time::{Duration, Instant};
    use windows::core::{implement, IUnknown, Interface, Ref, HRESULT, PCWSTR, PWSTR};
    use windows::Win32::Foundation::{CloseHandle, ERROR_PIPE_CONNECTED, HANDLE, S_OK, WAIT_OBJECT_0};
    use windows::Win32::Media::Audio::{
        eRender, AudioSessionStateActive, IAudioSessionControl2, IAudioSessionManager2,
        IMMDeviceEnumerator, MMDeviceEnumerator, DEVICE_STATE_ACTIVE,
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
    use windows::Win32::Storage::FileSystem::{GetFileVersionInfoSizeW, GetFileVersionInfoW, VerQueryValueW, WriteFile};
    use windows::Win32::System::Pipes::ConnectNamedPipe;
    use windows::Win32::System::Com::{CoCreateInstance, CoInitializeEx, IAgileObject, IAgileObject_Impl, BLOB, CLSCTX_ALL, COINIT_MULTITHREADED};
    use windows::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Process32FirstW, Process32NextW, PROCESSENTRY32W,
        TH32CS_SNAPPROCESS,
    };
    use windows::Win32::System::Threading::{
        CreateEventW, OpenProcess, QueryFullProcessImageNameW, SetEvent, WaitForSingleObject,
        PROCESS_NAME_WIN32, PROCESS_QUERY_LIMITED_INFORMATION, PROCESS_SYNCHRONIZE,
    };
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

    #[implement(IActivateAudioInterfaceCompletionHandler, IAgileObject)]
    struct Activated(HANDLE);
    impl IActivateAudioInterfaceCompletionHandler_Impl for Activated_Impl {
        fn ActivateCompleted(
            &self,
            _operation: Ref<'_, IActivateAudioInterfaceAsyncOperation>,
        ) -> windows::core::Result<()> {
            unsafe { SetEvent(self.0) }
        }
    }
    impl IAgileObject_Impl for Activated_Impl {}

    /// The root of the named program's process tree.
    fn root_process(name: &str) -> Option<u32> {
        let wanted = format!("{}.exe", name.trim_end_matches(".exe")).to_lowercase();
        let mut all = vec![];
        unsafe {
            let snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0).ok()?;
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
    }

    /// A handle that is signalled when the process ends.
    fn watch(pid: u32) -> Option<HANDLE> {
        unsafe { OpenProcess(PROCESS_SYNCHRONIZE, false, pid).ok() }
    }
    fn ended(handle: Option<HANDLE>) -> bool {
        handle.is_some_and(|h| unsafe { WaitForSingleObject(h, 0) } == WAIT_OBJECT_0)
    }

    fn exe_path(pid: u32) -> Option<String> {
        unsafe {
            let process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, false, pid).ok()?;
            let mut buffer = [0u16; 1024];
            let mut size = buffer.len() as u32;
            let ok = QueryFullProcessImageNameW(
                process,
                PROCESS_NAME_WIN32,
                PWSTR(buffer.as_mut_ptr()),
                &mut size,
            );
            let _ = CloseHandle(process);
            ok.ok()?;
            Some(String::from_utf16_lossy(&buffer[..size as usize]))
        }
    }

    /// The file's own description ("Discord", "Space Racer"), if it has one.
    fn description(path: &str) -> Option<String> {
        unsafe {
            let wide: Vec<u16> = path.encode_utf16().chain([0]).collect();
            let size = GetFileVersionInfoSizeW(PCWSTR(wide.as_ptr()), None);
            if size == 0 {
                return None;
            }
            let mut data = vec![0u8; size as usize];
            GetFileVersionInfoW(PCWSTR(wide.as_ptr()), None, size, data.as_mut_ptr().cast()).ok()?;
            let query = |key: &str| -> Option<(*const u8, u32)> {
                let key: Vec<u16> = key.encode_utf16().chain([0]).collect();
                let mut value = std::ptr::null_mut();
                let mut length = 0u32;
                VerQueryValueW(data.as_ptr().cast(), PCWSTR(key.as_ptr()), &mut value, &mut length)
                    .as_bool()
                    .then_some((value as *const u8, length))
                    .filter(|(v, l)| !v.is_null() && *l > 0)
            };
            let (pairs, length) = query("\\VarFileInfo\\Translation")?;
            let pairs = std::slice::from_raw_parts(pairs as *const u16, length as usize / 2);
            let mut keys: Vec<String> = pairs
                .chunks_exact(2)
                .map(|p| format!("\\StringFileInfo\\{:04x}{:04x}\\FileDescription", p[0], p[1]))
                .collect();
            keys.push("\\StringFileInfo\\040904b0\\FileDescription".into());
            keys.iter().find_map(|key| {
                let (text, length) = query(key)?;
                let text = std::slice::from_raw_parts(text as *const u16, length as usize);
                let end = text.iter().position(|&c| c == 0).unwrap_or(text.len());
                let text = String::from_utf16_lossy(&text[..end]).trim().to_owned();
                (!text.is_empty() && text.len() <= 64).then_some(text)
            })
        }
    }

    /// Programs with a sound session on any active output, once each.
    pub fn playing() -> Result<Vec<serde_json::Value>> {
        Ok(sessions(true)?
            .into_iter()
            .map(|s| serde_json::json!({"name":s.name,"label":s.label,"playing":s.playing,"game":s.game}))
            .collect())
    }

    struct Session {
        name: String,
        label: String,
        playing: bool,
        game: bool,
    }

    /// The game running now with sound (the one playing first), as a process name.
    fn current_game() -> Option<String> {
        sessions(false).ok()?.into_iter().find(|s| s.game).map(|s| s.name)
    }

    /// The tree root of the named program, or with "@game" of the running game.
    fn find(name: &str) -> Option<u32> {
        if name == super::GAME {
            return root_process(&current_game()?);
        }
        root_process(name)
    }

    fn sessions(labels: bool) -> Result<Vec<Session>> {
        let mut found: Vec<(String, String, bool, bool)> = vec![];
        unsafe {
            CoInitializeEx(None, COINIT_MULTITHREADED).ok()?;
            let devices: IMMDeviceEnumerator = CoCreateInstance(&MMDeviceEnumerator, None, CLSCTX_ALL)?;
            let outputs = devices.EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE)?;
            for i in 0..outputs.GetCount()? {
                let Ok(manager) = outputs.Item(i)?.Activate::<IAudioSessionManager2>(CLSCTX_ALL, None) else {
                    continue;
                };
                let sessions = manager.GetSessionEnumerator()?;
                for j in 0..sessions.GetCount()? {
                    let Ok(session) = sessions.GetSession(j) else { continue };
                    let Ok(session) = session.cast::<IAudioSessionControl2>() else { continue };
                    if session.IsSystemSoundsSession() == S_OK {
                        continue;
                    }
                    let Ok(pid) = session.GetProcessId() else { continue };
                    let Some(path) = exe_path(pid) else { continue };
                    let file = path.rsplit('\\').next().unwrap_or(&path);
                    let name = file.strip_suffix(".exe").or_else(|| file.strip_suffix(".EXE")).unwrap_or(file);
                    let active = session.GetState().is_ok_and(|s| s == AudioSessionStateActive);
                    match found.iter_mut().find(|(n, ..)| n.eq_ignore_ascii_case(name)) {
                        Some(entry) => entry.2 |= active,
                        None => {
                            let label = if labels { label_of(&path).unwrap_or_else(|| name.to_owned()) } else { String::new() };
                            found.push((name.to_owned(), label, active, super::is_game_path(&path)))
                        }
                    }
                }
            }
        }
        found.sort_by(|a, b| b.2.cmp(&a.2).then(a.1.to_lowercase().cmp(&b.1.to_lowercase())));
        Ok(found
            .into_iter()
            .map(|(name, label, playing, game)| Session { name, label, playing, game })
            .collect())
    }

    /// A program's own description, read once per program and remembered.
    fn label_of(path: &str) -> Option<String> {
        use std::collections::HashMap;
        use std::sync::Mutex;
        static SEEN: Mutex<Option<HashMap<String, Option<String>>>> = Mutex::new(None);
        let mut seen = SEEN.lock().ok()?;
        seen.get_or_insert_with(HashMap::new).entry(path.to_owned()).or_insert_with(|| description(path)).clone()
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
        // Never dropped: dropping a PROPVARIANT clears it, and clearing a VT_BLOB frees
        // its data, which here is `params` on this stack; that froze the worker.
        let mut variant = std::mem::ManuallyDrop::new(std::mem::zeroed::<PROPVARIANT>());
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
                Some(&*variant),
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

    /// How often to look for the program starting or closing.
    const LOOK_EVERY: Duration = Duration::from_secs(1);

    /// Wall-clock-paced sound for the whole capture, across the program
    /// starting and closing, so the track never falls behind the video.
    struct Track {
        start: Instant,
        written: u64,
    }

    impl Track {
        /// Silence up to now. False once FFmpeg's reader has closed.
        fn catch_up(&mut self, out: &mut impl Write) -> bool {
            let gap = silence_needed(self.start.elapsed().as_millis() as u64, self.written, u64::MAX);
            self.written += gap;
            send_silence(out, gap as usize * FRAME_BYTES)
        }
    }

    /// FFmpeg's end of a private pipe; a failed write means it has closed.
    struct PipeOut(HANDLE);
    impl Write for PipeOut {
        fn write(&mut self, data: &[u8]) -> std::io::Result<usize> {
            let mut written = 0;
            unsafe { WriteFile(self.0, Some(data), Some(&mut written), None) }
                .map_err(std::io::Error::other)?;
            Ok(written as usize)
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }

    /// To stdout, or with `pipe` straight to FFmpeg: "ready" once the pipe
    /// exists, and the track's clock starts when FFmpeg connects.
    pub fn capture(name: &str, include: bool, pipe: Option<&str>) -> Result<()> {
        unsafe {
            CoInitializeEx(None, COINIT_MULTITHREADED).ok()?;
        }
        let Some(pipe) = pipe else {
            return capture_to(name, include, &mut std::io::stdout().lock());
        };
        let handle = crate::screen::windows_capture::private_pipe(pipe, 65536)?;
        let result = (|| -> Result<()> {
            println!("ready");
            std::io::stdout().flush()?;
            if let Err(error) = unsafe { ConnectNamedPipe(handle, None) } {
                if error.code() != ERROR_PIPE_CONNECTED.to_hresult() {
                    return Err(error.into());
                }
            }
            capture_to(name, include, &mut PipeOut(handle))
        })();
        unsafe {
            let _ = CloseHandle(handle);
        }
        result
    }

    fn capture_to(name: &str, include: bool, out: &mut impl Write) -> Result<()> {
        let mut track = Track { start: Instant::now(), written: 0 };
        loop {
            let program = find(name);
            let target = match (program, include) {
                (Some(pid), _) => pid,
                // Everything else, while the program isn't running: exclude only this worker.
                (None, false) => std::process::id(),
                (None, true) => {
                    // Silent until it starts.
                    let looked = Instant::now();
                    while looked.elapsed() < LOOK_EVERY {
                        if !track.catch_up(out) {
                            return Ok(());
                        }
                        std::thread::sleep(Duration::from_millis(20));
                    }
                    continue;
                }
            };
            let game = name == super::GAME && program.is_some();
            match follow(target, include, program.is_none().then_some(name), game, out, &mut track) {
                Ok(Followed::ReaderClosed) => return Ok(()),
                Ok(Followed::Changed) => continue,
                // It closed while Windows was starting its capture: look again.
                Err(_) if program.is_some() && find(name) != program => continue,
                Err(error) => return Err(error),
            }
        }
    }

    enum Followed {
        ReaderClosed,
        /// The program closed, or started while everything else was captured.
        Changed,
    }

    /// Capture `target`'s tree (or all but it) until the reader closes, the
    /// target ends, or, with `waiting_for`, that program starts.
    fn follow(
        target: u32,
        include: bool,
        waiting_for: Option<&str>,
        game: bool,
        out: &mut impl Write,
        track: &mut Track,
    ) -> Result<Followed> {
        let process = if waiting_for.is_none() { watch(target) } else { None };
        let result = unsafe { follow_client(target, include, waiting_for, game, process, out, track) };
        if let Some(handle) = process {
            let _ = unsafe { CloseHandle(handle) };
        }
        result
    }

    unsafe fn follow_client(
        target: u32,
        include: bool,
        waiting_for: Option<&str>,
        game: bool,
        process: Option<HANDLE>,
        out: &mut impl Write,
        track: &mut Track,
    ) -> Result<Followed> {
        unsafe {
            let client = activate(target, include)?;
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
            let result = (|| -> Result<Followed> {
                client.SetEventHandle(event)?;
                let capture: IAudioCaptureClient = client.GetService()?;
                client.Start()?;
                let mut last_packet = Instant::now();
                let mut looked = Instant::now();
                loop {
                    let _ = WaitForSingleObject(event, 20);
                    while capture.GetNextPacketSize()? > 0 {
                        let mut data = std::ptr::null_mut();
                        let mut frames = 0;
                        let mut flags = 0;
                        capture.GetBuffer(&mut data, &mut frames, &mut flags, None, None)?;
                        let bytes = frames as usize * FRAME_BYTES;
                        let late = silence_before(
                            track.start.elapsed().as_millis() as u64,
                            track.written,
                            frames as u64,
                        );
                        track.written += late;
                        let ok = send_silence(out, late as usize * FRAME_BYTES)
                            && if flags & AUDCLNT_BUFFERFLAGS_SILENT.0 as u32 != 0 || data.is_null() {
                                send_silence(out, bytes)
                            } else {
                                send(out, std::slice::from_raw_parts(data, bytes))
                            };
                        capture.ReleaseBuffer(frames)?;
                        if !ok {
                            return Ok(Followed::ReaderClosed);
                        }
                        track.written += frames as u64;
                        last_packet = Instant::now();
                    }
                    let gap = silence_needed(
                        track.start.elapsed().as_millis() as u64,
                        track.written,
                        last_packet.elapsed().as_millis() as u64,
                    );
                    if !send_silence(out, gap as usize * FRAME_BYTES) {
                        return Ok(Followed::ReaderClosed);
                    }
                    track.written += gap;
                    if looked.elapsed() >= LOOK_EVERY {
                        looked = Instant::now();
                        if ended(process)
                            || waiting_for.is_some_and(|name| find(name).is_some())
                            // Another game is the one playing now: follow it instead.
                            || (game && find(super::GAME).is_some_and(|pid| pid != target))
                        {
                            return Ok(Followed::Changed);
                        }
                    }
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
    fn games_are_the_programs_where_stores_install_them() {
        assert!(is_game_path(r"D:\SteamLibrary\steamapps\common\SpaceRacer\RacerGame\Binaries\Win64\SpaceRacer-Win64-Shipping.exe"));
        assert!(is_game_path(r"C:\Program Files\Epic Games\Fortnite\FortniteClient-Win64-Shipping.exe"));
        assert!(is_game_path(r"C:\Program Files (x86)\GOG Galaxy\Games\Cyberpunk 2077\bin\x64\Cyberpunk2077.exe"));
        assert!(!is_game_path(r"C:\Users\me\AppData\Local\Discord\app-1.0\Discord.exe"));
        assert!(!is_game_path(r"C:\Program Files (x86)\Steam\steam.exe"));
        // Store programs beside the games: their sound must never become the game track.
        assert!(!is_game_path(r"C:\Program Files\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"));
        assert!(!is_game_path(r"C:\Program Files\Epic Games\Launcher\Engine\Binaries\Win64\EpicWebHelper.exe"));
        assert!(!is_game_path(
            r"C:\Program Files (x86)\Epic Games\Epic Online Services\managedArtifacts\0123abcd\EOSOverlayRenderer-Win64-Shipping.exe"
        ));
        assert!(!is_game_path(r"C:\Program Files (x86)\Steam\steamapps\common\SteamVR\bin\win64\vrmonitor.exe"));
        assert!(is_game_path(r"C:\Program Files\Epic Games\SomeGame\Binaries\Win64\SomeGame.exe"));
    }

    #[test]
    fn sound_after_a_quiet_spell_lands_at_its_own_time() {
        // Padded to 1.000 s, then a 10 ms packet arrives at 1.059 s: it covers
        // 1.049..1.059, so 49 ms of silence go first, not none.
        assert_eq!(silence_before(1059, 48000, 480), 2352);
        // Ordinary jitter while sound flows: a packet 20 ms late inserts nothing.
        assert_eq!(silence_before(1030, 48000, 480), 0);
        // Ahead of the clock: nothing.
        assert_eq!(silence_before(1000, 49000, 480), 0);
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
