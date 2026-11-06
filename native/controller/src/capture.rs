use crate::{desktop, err, replay_budget::Budget, util, Result};
use serde_json::{json, Value};
use std::{
    collections::VecDeque,
    fs,
    os::unix::fs::{MetadataExt, OpenOptionsExt},
    path::{Path, PathBuf},
    process::Stdio,
    sync::{
        atomic::{AtomicBool, Ordering},
        Arc,
    },
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, BufReader},
    net::UnixStream,
    sync::{mpsc, watch, RwLock},
    time::{timeout, Duration},
};

pub fn color(cfg: &Value, monitor: &Value) -> Result<&'static str> {
    let mode = cfg["color_mode"].as_str().unwrap_or("auto");
    let hdr = ["hdr", "hdredid"].contains(
        &monitor["colorManagementPreset"]
            .as_str()
            .unwrap_or("")
            .to_lowercase()
            .as_str(),
    );
    if mode == "sdr" && hdr {
        return err("This display is in HDR mode. Choose Automatic or Preserve HDR, or disable display HDR before SDR capture. Direct monitor capture does not tone map HDR to SDR.");
    }
    if !["auto", "hdr", "sdr"].contains(&mode) {
        return err("Capture color mode must be auto, hdr or sdr.");
    }
    Ok(if mode == "hdr" || (mode == "auto" && hdr) {
        "hdr"
    } else {
        "sdr"
    })
}
/// Formats GPU Screen Recorder encodes. The 10-bit ones record SDR without
/// banding; HDR capture is always 10-bit whichever family is chosen.
pub const GPU_CODECS: [&str; 5] = ["h264", "hevc", "av1", "hevc_10bit", "av1_10bit"];
const TEN_BIT: [&str; 4] = ["yuv420p10le", "p010le", "p010be", "yuv420p10be"];
/// recording.resolution: the largest output size; smaller displays are never
/// scaled up, larger ones are scaled down keeping their aspect ratio.
pub const RESOLUTIONS: [(&str, &str); 5] = [
    ("native", ""),
    ("2160", "3840x2160"),
    ("1440", "2560x1440"),
    ("1080", "1920x1080"),
    ("720", "1280x720"),
];
fn gpu_args(color: &str, r: &Value) -> Vec<String> {
    let codec = match r["codec"].as_str().unwrap_or("libx264") {
        c @ ("av1" | "av1_10bit") => {
            if color == "hdr" {
                "av1_hdr"
            } else {
                c
            }
        }
        c @ ("hevc" | "hevc_10bit") => {
            if color == "hdr" {
                "hevc_hdr"
            } else {
                c
            }
        }
        _ => {
            if color == "hdr" {
                "hevc_hdr"
            } else {
                "h264"
            }
        }
    };
    let mut args: Vec<String> = [
        "-k",
        codec,
        "-cr",
        "limited",
        "-fallback-cpu-encoding",
        "no",
    ]
    .into_iter()
    .map(str::to_owned)
    .collect();
    let size = RESOLUTIONS
        .iter()
        .find(|(name, _)| r["resolution"] == *name)
        .map_or("", |(_, size)| size);
    if !size.is_empty() {
        args.extend(["-s".into(), size.into()]);
    }
    if r["rate_control"] == "bitrate" {
        args.extend([
            "-bm".into(),
            "cbr".into(),
            "-q".into(),
            (r["bitrate_mbps"].as_u64().unwrap_or(8) * 1000).to_string(),
        ]);
    } else {
        args.extend([
            "-bm".into(),
            "qp".into(),
            "-q".into(),
            r["quality_preset"].as_str().unwrap_or("very_high").into(),
        ]);
    }
    args
}
/// Video settings replay can keep apart from recording (replay.own_video).
pub const REPLAY_VIDEO: [&str; 7] = [
    "codec",
    "fps",
    "resolution",
    "rate_control",
    "quality_preset",
    "bitrate_mbps",
    "bitrate_auto",
];
/// The fixed bitrate (Mbps) used while `bitrate_auto` is on: it follows the
/// output size (the display's, or the resolution limit) and frame rate, at
/// about 0.075 bits per pixel for AV1/HEVC and 0.11 for H.264 (mirrors
/// encoding.py). 2K at 60 fps in AV1 is about 17 Mbps.
pub fn auto_bitrate(video: &Value, display: &Value) -> u64 {
    let mut width = display["width"].as_f64().unwrap_or(1920.0);
    let mut height = display["height"].as_f64().unwrap_or(1080.0);
    let limit = RESOLUTIONS
        .iter()
        .find(|(name, _)| video["resolution"] == *name)
        .and_then(|(_, size)| size.split_once('x'))
        .and_then(|(w, h)| Some((w.parse::<f64>().ok()?, h.parse::<f64>().ok()?)));
    if let Some((w, h)) = limit {
        let scale = (w / width).min(h / height).min(1.0);
        width *= scale;
        height *= scale;
    }
    let fps = video["fps"].as_f64().unwrap_or(60.0);
    let codec = video["codec"].as_str().unwrap_or("");
    let bits_per_pixel = if ["h264", "libx264", "h264_vaapi"].contains(&codec) {
        0.11
    } else {
        0.075
    };
    ((width * height * fps * bits_per_pixel / 1e6).round() as u64).clamp(4, 500)
}
/// The recording section with replay's own video settings, when it has them.
pub fn replay_video(cfg: &Value) -> Value {
    let mut video = cfg["recording"].clone();
    if cfg["replay"]["own_video"] == true {
        for key in REPLAY_VIDEO {
            video[key] = cfg["replay"][key].clone();
        }
    }
    video
}
/// `video` is replay_video(cfg), with any automatic bitrate already set.
pub fn replay_command(
    cfg: &Value,
    video: &Value,
    ipc: &Path,
    folder: &Path,
    color: &str,
    apps: &[String],
) -> Vec<String> {
    let r = video;
    let replay = &cfg["replay"];
    let mut argv = vec![
        "gpu-screen-recorder".into(),
        "-w".into(),
        r["monitor"].as_str().unwrap().into(),
        "-f".into(),
        r["fps"].to_string(),
        "-r".into(),
        replay["seconds"].to_string(),
        "-replay-storage".into(),
        replay["storage"].as_str().unwrap_or("ram").into(),
        "-c".into(),
        "mp4".into(),
        "-o".into(),
        folder.to_string_lossy().into(),
        "-ipc".into(),
        ipc.to_string_lossy().into(),
        "-v".into(),
        "no".into(),
    ];
    argv.extend(gpu_args(color, r));
    for track in audio_tracks(replay, apps) {
        argv.extend(["-a".into(), track]);
    }
    argv
}

/// An application name gpu-screen-recorder can match (`app:NAME`). The
/// recorder's source syntax uses `|` and `:`, so neither may appear.
pub fn valid_app_name(name: &str) -> bool {
    !name.trim().is_empty()
        && name.chars().count() <= 64
        && name
            .chars()
            .all(|c| c.is_alphanumeric() || " ._-".contains(c))
}

/// Human names for `audio_tracks`, in the same order.
pub fn track_names(replay: &Value, apps: &[crate::audio_apps::App]) -> Vec<String> {
    let game = replay["game_audio"] == true;
    let mic = replay["microphone"] == true;
    if replay["audio_tracks"] != "separate" {
        return match (game, mic) {
            (true, true) => vec!["Game sound and microphone".into()],
            (true, false) => vec!["Game sound".into()],
            (false, true) => vec!["Microphone".into()],
            _ => vec![],
        };
    }
    let mut names = vec![];
    if game {
        names.push(if apps.is_empty() {
            "Game and desktop sound".into()
        } else {
            "Everything else".into()
        });
    }
    names.extend(apps.iter().map(|a| a.label.clone()));
    if mic {
        names.push("Microphone".into());
    }
    names
}

/// MP4 replays lose gpu-screen-recorder's track titles, so the names go in a
/// small hidden file beside the clip (`.NAME.tracks.json`). Best effort: a
/// missing file only means the editor shows "Audio 1", "Audio 2"….
fn write_track_names(clip: &Path, names: &[String]) {
    if names.len() < 2 {
        return;
    }
    if let (Some(dir), Some(name)) = (clip.parent(), clip.file_name()) {
        let sidecar = dir.join(format!(".{}.tracks.json", name.to_string_lossy()));
        let _ = std::fs::write(sidecar, json!({"tracks":names}).to_string());
    }
}

/// Replay audio sources, one `-a` value per track.
///
/// Mixed (the default): game sound and microphone share one track. Separate:
/// each app in `apps` (see audio_apps) has its own track, "everything else"
/// has one, and the microphone has its own. The everything-else track uses
/// `app-inverse:` rather than `default_output`, which would also contain the
/// apps and make muting one in a clip impossible.
pub fn audio_tracks(replay: &Value, apps: &[String]) -> Vec<String> {
    let game = replay["game_audio"] == true;
    let mic = replay["microphone"] == true;
    if replay["audio_tracks"] != "separate" {
        let mixed: Vec<&str> = [(game, "default_output"), (mic, "default_input")]
            .into_iter()
            .filter_map(|(on, source)| on.then_some(source))
            .collect();
        return if mixed.is_empty() {
            vec![]
        } else {
            vec![mixed.join("|")]
        };
    }
    let mut tracks = vec![];
    if game {
        tracks.push(if apps.is_empty() {
            "default_output".into()
        } else {
            apps.iter()
                .map(|a| format!("app-inverse:{a}"))
                .collect::<Vec<_>>()
                .join("|")
        });
    }
    tracks.extend(apps.iter().map(|a| format!("app:{a}")));
    if mic {
        tracks.push("default_input".into());
    }
    tracks
}
pub fn orphaned() -> Vec<u32> {
    let mut result = vec![];
    let Ok(entries) = fs::read_dir("/proc") else {
        return result;
    };
    for entry in entries.flatten() {
        let Ok(pid) = entry.file_name().to_string_lossy().parse() else {
            continue;
        };
        if !entry
            .metadata()
            .is_ok_and(|m| m.uid() == unsafe { libc::getuid() })
        {
            continue;
        }
        let Ok(bytes) = fs::read(entry.path().join("cmdline")) else {
            continue;
        };
        let argv: Vec<_> = bytes
            .split(|c| *c == 0)
            .map(|s| String::from_utf8_lossy(s).into_owned())
            .collect();
        if argv.iter().take(6).any(|s| {
            Path::new(s)
                .file_name()
                .is_some_and(|n| n == "wf-recorder" || n == "gpu-screen-recorder")
        }) && argv.iter().any(|s| {
            Path::new(s)
                .file_name()
                .is_some_and(|n| n.to_string_lossy().starts_with(".Jarvis_"))
        }) {
            result.push(pid);
        }
    }
    result
}
pub async fn verify(path: &Path, color: &str, codec: Option<&str>) -> Result<()> {
    if !fs::metadata(path).is_ok_and(|m| m.len() > 0) {
        return err("The recorder produced no video data.");
    }
    let data:Value=serde_json::from_str(&util::query(&["ffprobe".into(),"-v".into(),"error".into(),"-show_entries".into(),"stream=codec_type,codec_name,width,height,pix_fmt,color_space,color_transfer,color_primaries,color_range".into(),"-of".into(),"json".into(),path.to_string_lossy().into()],10).await?)?;
    let video: Vec<_> = data["streams"]
        .as_array()
        .ok_or("The recorded file could not be validated.")?
        .iter()
        .filter(|s| {
            s["codec_type"] == "video"
                && s["width"].as_u64().unwrap_or(0) > 0
                && s["height"].as_u64().unwrap_or(0) > 0
        })
        .collect();
    if video.is_empty() {
        return err("The output contains no valid video stream.");
    }
    if let Some(codec) = codec {
        let name = codec.trim_end_matches("_10bit");
        if !video.iter().any(|s| s["codec_name"] == name) {
            return err(format!("Video format verification failed: expected {name}. Check GPU and driver support for the selected format."));
        }
        if codec.ends_with("_10bit")
            && !video
                .iter()
                .any(|s| TEN_BIT.contains(&s["pix_fmt"].as_str().unwrap_or("")))
        {
            return err(format!("Video format verification failed: expected 10-bit {name}. Check GPU and driver support for the selected format."));
        }
    }
    if color == "hdr"
        && !video.iter().any(|s| {
            TEN_BIT.contains(&s["pix_fmt"].as_str().unwrap_or(""))
                && s["color_transfer"] == "smpte2084"
                && s["color_primaries"] == "bt2020"
                && s["color_space"] == "bt2020nc"
                && s["color_range"] == "tv"
        })
    {
        return err("HDR verification failed: expected 10-bit PQ / BT.2020 limited-range video. The file is retained; check GPU Screen Recorder, driver and display HDR support.");
    }
    Ok(())
}
pub struct Capture {
    pub status: watch::Sender<Value>,
    control: Option<mpsc::Sender<()>>,
    task: Option<tokio::task::JoinHandle<()>>,
    pub replay: bool,
    color: String,
    codec: Option<String>,
    ipc: PathBuf,
    replay_output: Option<PathBuf>,
    replay_session: Option<PathBuf>,
    replay_seconds: u64,
    replay_cleanup: Arc<RwLock<()>>,
    /// Names of the replay's audio tracks, in file order, for the clip editor.
    track_names: Vec<String>,
    /// recording.game_folders: clips of a focused game go in a folder named after it.
    game_folders: bool,
}
impl Capture {
    pub fn new(replay: bool, ipc: PathBuf) -> Self {
        let status = if replay {
            json!({"state":"off","message":"Instant replay is off.","file":null})
        } else {
            json!({"state":"idle","message":"Recorder ready.","file":null})
        };
        Self {
            status: watch::channel(status).0,
            control: None,
            task: None,
            replay,
            color: "sdr".into(),
            codec: None,
            ipc,
            replay_output: None,
            replay_session: None,
            replay_seconds: 120,
            track_names: vec![],
            game_folders: false,
            replay_cleanup: Arc::new(RwLock::new(())),
        }
    }
    pub fn snapshot(&self) -> Value {
        self.status.borrow().clone()
    }
    pub fn active(&self) -> bool {
        self.task.as_ref().is_some_and(|t| !t.is_finished())
    }
    fn set(&self, state: &str, message: impl AsRef<str>, file: Option<&Path>) {
        self.status
            .send_replace(json!({"state":state,"message":message.as_ref(),"file":file}));
    }
    pub fn inspect_previous(&self, cfg: &Value) {
        let orphans = orphaned();
        let folder = util::expand(cfg["recording"]["output_dir"].as_str().unwrap());
        let mut partials: Vec<_> = fs::read_dir(&folder)
            .into_iter()
            .flatten()
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.file_name().is_some_and(|s| {
                    let s = s.to_string_lossy();
                    s.starts_with(".Jarvis_") && s.ends_with(".part.mkv")
                })
            })
            .collect();
        partials.sort();
        if let Some(pid) = orphans.first() {
            self.set("error",format!("A recorder from an earlier Jarvis session is still running (PID {pid}). Stop it with: kill -INT {pid}. Its partial file stays in the output folder."),None);
        } else if !partials.is_empty() {
            self.set("idle",format!("Found {} unfinished recording(s) from an earlier session in {} (.Jarvis_*.part.mkv). See docs/recording.md for recovery.",partials.len(),folder.display()),partials.first().map(PathBuf::as_path));
        }
    }
    async fn start_inner(&mut self, cfg: &Value) -> Result<Value> {
        if self.active() {
            if self.snapshot()["state"] == "stopping" {
                return err("The previous capture is still being finalized.");
            }
            return Ok(self.snapshot());
        }
        let mut video = if self.replay {
            replay_video(cfg)
        } else {
            cfg["recording"].clone()
        };
        if !self.replay && video["configured"] != true {
            return err("Open Recording settings and finish source, audio, folder, and permission setup first.");
        }
        let monitor = video["monitor"].as_str().unwrap().trim().to_owned();
        if monitor.is_empty() {
            return err("Choose a display in Recording settings first.");
        }
        let monitors = desktop::monitors().await?;
        let display = monitors
            .as_array()
            .unwrap()
            .iter()
            .find(|m| m["name"] == monitor.as_str())
            .ok_or("Capture source is unavailable. Choose a connected monitor.")?;
        if video["bitrate_auto"] == true {
            video["bitrate_mbps"] = json!(auto_bitrate(&video, display));
        }
        let r = &video;
        let monitor = monitor.as_str();
        self.color = color(r, display)?.into();
        self.codec = r["codec"]
            .as_str()
            .filter(|c| GPU_CODECS.contains(c))
            .map(|c| {
                if c == "h264" && self.color == "hdr" {
                    "hevc"
                } else {
                    c
                }
                .to_owned()
            });
        let gpu = self.replay
            || self.color == "hdr"
            || GPU_CODECS.contains(&r["codec"].as_str().unwrap_or(""));
        if !gpu && r["resolution"].as_str().is_some_and(|s| s != "native") {
            return err("The legacy CPU/VA-API recorder records the display's own resolution only. Choose a GPU format or the display's resolution.");
        }
        let backend = if gpu {
            "gpu-screen-recorder"
        } else {
            "wf-recorder"
        };
        if util::which(backend).is_none() {
            return err(format!(
                "{backend} is missing. Install it using your distribution's package manager."
            ));
        }
        if util::which("ffprobe").is_none() {
            return err("ffprobe is missing. Install FFmpeg to validate saved recordings.");
        }
        let mut folder = util::expand(r["output_dir"].as_str().unwrap());
        self.game_folders = r["game_folders"] == true;
        if self.game_folders && !self.replay {
            // A recording goes in the folder of the game focused when it starts.
            if let Some(game) = desktop::focused_game().await {
                folder = folder.join(game);
            }
        }
        util::private_dir(&folder)?;
        let folder = fs::canonicalize(folder)?;
        let mut staging = None;
        let mut final_path = None;
        let budget = if self.replay {
            Some(Budget::new(cfg)?)
        } else {
            None
        };
        if budget.as_ref().is_some_and(|b| b.disk.is_some()) {
            // GSR saves replay clips beside -o too. The controller publishes
            // completed clips outside this disposable directory before verify.
            let version = util::query(&[backend.into(), "--version".into()], 3).await?;
            let numbers: Vec<u32> = version
                .trim()
                .split('.')
                .filter_map(|v| v.parse().ok())
                .collect();
            if numbers.as_slice() < [6, 1, 5].as_slice() {
                return err("Disk replay needs gpu-screen-recorder 6.1.5 or newer.");
            }
        }
        let apps = if self.replay {
            crate::audio_apps::for_replay(&cfg["replay"]).await
        } else {
            vec![]
        };
        let mut argv = if self.replay {
            util::private_dir(self.ipc.parent().ok_or("Invalid replay socket")?)?;
            let names: Vec<String> = apps.iter().map(|a| a.name.clone()).collect();
            let buffer = budget.as_ref().and_then(|b| b.disk.as_ref());
            replay_command(
                cfg,
                r,
                &self.ipc,
                buffer.map(|s| s.path.as_path()).unwrap_or(&folder),
                &self.color,
                &names,
            )
        } else {
            if let Some(pid) = orphaned().first() {
                return err(format!("A recorder from an earlier Jarvis session is still running (PID {pid}). Stop it with: kill -INT {pid}"));
            }
            let mut args = if gpu {
                let mut a = vec![
                    backend.into(),
                    "-w".into(),
                    monitor.into(),
                    "-f".into(),
                    r["fps"].to_string(),
                    "-c".into(),
                    "mkv".into(),
                    "-v".into(),
                    "no".into(),
                ];
                a.extend(gpu_args(&self.color, r));
                a
            } else {
                vec![
                    backend.into(),
                    "--output".into(),
                    monitor.into(),
                    "--muxer".into(),
                    "matroska".into(),
                    "--codec".into(),
                    r["codec"].as_str().unwrap().into(),
                    "--framerate".into(),
                    r["fps"].to_string(),
                ]
            };
            if !gpu {
                if r["codec"] == "libx264" {
                    args.extend([
                        "--pixel-format".into(),
                        "yuv420p".into(),
                        "--codec-param".into(),
                        "preset=ultrafast".into(),
                        "--codec-param".into(),
                        "threads=2".into(),
                    ]);
                } else {
                    let device = r["vaapi_device"].as_str().unwrap();
                    let name = std::ffi::CString::new(device)?;
                    if unsafe { libc::access(name.as_ptr(), libc::R_OK | libc::W_OK) } != 0 {
                        return err(
                            "The selected VA-API render device is unavailable or not accessible.",
                        );
                    }
                    args.extend(["--device".into(), device.into()]);
                }
            }
            if !gpu {
                let params = if r["rate_control"] == "bitrate" {
                    let bitrate = r["bitrate_mbps"].as_u64().unwrap_or(8) * 1_000_000;
                    vec![
                        format!("b={bitrate}"),
                        format!("maxrate={bitrate}"),
                        format!("bufsize={}", bitrate * 2),
                    ]
                } else {
                    vec![format!(
                        "{}={}",
                        if r["codec"] == "libx264" { "crf" } else { "qp" },
                        r["quality"]
                    )]
                };
                for param in params {
                    args.extend(["--codec-param".into(), param]);
                }
            }
            let system = r["system_audio"] == true;
            let mic = r["microphone"] == true;
            if system || mic {
                let device = r["audio_device"].as_str().unwrap().trim();
                if device.is_empty() {
                    return err("Choose an explicit recording audio source in Recording settings.");
                }
                if system && mic && r["combined_audio_source"] != true {
                    return err("Configure a combined audio source explicitly.");
                }
                let sources = desktop::audio_sources().await?;
                let chosen=sources.as_array().unwrap().iter().find(|s|s["name"]==device).ok_or("The chosen recording audio source is unavailable. Check Recording settings.")?;
                if !(system && mic) {
                    if system && chosen["monitor"] != true {
                        return err("System audio requires a sink monitor source; choose its .monitor source.");
                    }
                    if mic && chosen["monitor"] == true {
                        return err(
                            "Microphone recording requires an input source, not a sink monitor.",
                        );
                    }
                }
                if gpu {
                    args.extend(["-a".into(), device.into(), "-ac".into(), "aac".into()]);
                } else {
                    args.extend([
                        "--audio-backend".into(),
                        "pulse".into(),
                        format!("--audio={device}"),
                        "--audio-codec".into(),
                        "aac".into(),
                    ]);
                }
            }
            let tag = format!(
                "{}_{}_{:x}",
                timestamp(),
                std::process::id(),
                util::now().to_bits()
            );
            let output = folder.join(format!("Jarvis_{tag}.mkv"));
            let partial = folder.join(format!(".Jarvis_{tag}.part.mkv"));
            fs::OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&partial)?;
            args.extend(if gpu {
                vec!["-o".into(), partial.to_string_lossy().into()]
            } else {
                vec![
                    "--overwrite".into(),
                    "--file".into(),
                    partial.to_string_lossy().into(),
                ]
            });
            staging = Some(partial);
            final_path = Some(output);
            args
        };
        argv[0] = util::which(&argv[0]).unwrap().to_string_lossy().into();
        let mut command = util::owned_command(&argv, libc::SIGINT, true)?;
        if let Some(session) = budget.as_ref().and_then(|b| b.disk.as_ref()) {
            let fd = session.lock_fd();
            // Keep the root locked even in the brief interval between a
            // controller crash and the owned recorder handling SIGINT.
            unsafe {
                command.pre_exec(move || {
                    if libc::fcntl(fd, libc::F_SETFD, 0) < 0 {
                        return Err(std::io::Error::last_os_error());
                    }
                    Ok(())
                });
            }
        }
        command.stdout(Stdio::null()).stderr(Stdio::piped());
        let mut child = match command.spawn() {
            Ok(c) => c,
            Err(e) => {
                if let Some(p) = &staging {
                    let _ = fs::remove_file(p);
                }
                return Err(e.into());
            }
        };
        let mut errors = child.stderr.take().ok_or("Missing capture stderr")?;
        let write_failed = Arc::new(AtomicBool::new(false));
        let failed = write_failed.clone();
        let mut diagnostics = tokio::spawn(async move {
            let mut tail = VecDeque::with_capacity(4);
            let mut chunk = [0; 4096];
            let mut previous = Vec::new();
            while let Ok(n) = errors.read(&mut chunk).await {
                if n == 0 {
                    break;
                }
                if tail.len() == 4 {
                    tail.pop_front();
                }
                previous.extend_from_slice(&chunk[..n]);
                let message = String::from_utf8_lossy(&previous);
                if message.contains("failed to add replay buffer data")
                    || message.contains("failed to create replay file")
                    || message.contains("failed to create replay directory")
                    || message.contains("No space left on device")
                {
                    failed.store(true, Ordering::Relaxed);
                }
                if previous.len() > 256 {
                    previous.drain(..previous.len() - 256);
                }
                tail.push_back(chunk[..n].to_vec());
            }
            tail.into_iter().flatten().collect::<Vec<u8>>()
        });
        let (tx, mut rx) = mpsc::channel(1);
        self.control = Some(tx);
        let replay = self.replay;
        let status = self.status.clone();
        let color = self.color.clone();
        let codec = self.codec.clone();
        self.set(
            if replay { "on" } else { "recording" },
            if replay && cfg["replay"]["storage"] == "disk" {
                format!(
                    "Instant replay on · {} · up to {} · disk buffer · {} MiB budget.",
                    color.to_uppercase(),
                    duration(cfg["replay"]["seconds"].as_u64().unwrap_or(120)),
                    cfg["replay"]["max_mb"],
                )
            } else if replay {
                format!(
                    "Instant replay on · {} · keeping the last {} in memory.",
                    color.to_uppercase(),
                    duration(cfg["replay"]["seconds"].as_u64().unwrap_or(120))
                )
            } else {
                format!(
                    "Recorder running · {}. Stop to finalize and verify the file.",
                    color.to_uppercase()
                )
            },
            staging.as_deref(),
        );
        self.replay_output = self.replay.then(|| folder.clone());
        self.replay_seconds = cfg["replay"]["seconds"].as_u64().unwrap_or(120);
        self.track_names = track_names(&cfg["replay"], &apps);
        self.replay_session = budget
            .as_ref()
            .and_then(|b| b.disk.as_ref())
            .map(|s| s.path.clone());
        let replay_cleanup = self.replay_cleanup.clone();
        self.task = Some(tokio::spawn(async move {
            let pressure = async {
                let Some(budget) = budget.as_ref() else {
                    return std::future::pending::<String>().await;
                };
                let mut tick = tokio::time::interval(Duration::from_millis(250));
                loop {
                    tick.tick().await;
                    if write_failed.load(Ordering::Relaxed) {
                        return "Replay stopped safely: the recorder could not write replay buffer data. Check disk space and permissions.".into();
                    }
                    if let Err(e) = budget.check() {
                        return e.to_string();
                    }
                }
            };
            let mut failure = None;
            let (requested, forced, code) = tokio::select! {
                result=child.wait()=>(false,false,result),
                reason=pressure=>{
                    failure = Some(reason);
                    let forced=util::stop(&mut child,libc::SIGINT,5).await.unwrap_or(true);
                    (false,forced,child.wait().await)
                },
                _=rx.recv()=>{
                    let previous=status.borrow().clone();
                    status.send_replace(json!({"state":"stopping","message":if replay{"Stopping instant replay…"}else{"Finishing recording…"},"file":previous["file"]}));
                    let forced=util::stop(&mut child,libc::SIGINT,if replay{5}else{12}).await.unwrap_or(true);
                    (true,forced,child.wait().await)
                }
            };
            let tail = match timeout(Duration::from_secs(2), &mut diagnostics).await {
                Ok(result) => result.unwrap_or_default(),
                Err(_) => {
                    diagnostics.abort();
                    let _ = diagnostics.await;
                    vec![]
                }
            };
            if replay {
                let mut update = if requested {
                    json!({"state":"off","message":"Instant replay is off.","file":null})
                } else {
                    json!({"state":"error","message":failure.unwrap_or_else(|| format!("Instant replay stopped (status {:?}). {}",code,String::from_utf8_lossy(&tail).trim().chars().take(800).collect::<String>())),"file":status.borrow()["file"]})
                };
                // Expose failure immediately, but let an in-flight save finish
                // publication/verification before removing its source session.
                if !requested {
                    status.send_replace(update.clone());
                }
                let _cleanup = replay_cleanup.write().await;
                drop(budget);
                if !requested {
                    update["file"] = status.borrow()["file"].clone();
                }
                status.send_replace(update);
                return;
            }
            let partial = staging.unwrap();
            let final_path = final_path.unwrap();
            let finished = async {
                if !requested || forced || !code.as_ref().is_ok_and(|c| c.success()) {
                    return err(format!(
                        "Recorder exited with status {:?}. {}",
                        code,
                        String::from_utf8_lossy(&tail)
                            .trim()
                            .chars()
                            .take(1600)
                            .collect::<String>()
                    ));
                }
                verify(&partial, &color, codec.as_deref()).await?;
                fs::File::open(&partial)?.sync_all()?;
                fs::hard_link(&partial, &final_path)?;
                fs::remove_file(&partial)?;
                fs::File::open(final_path.parent().unwrap())?.sync_all()?;
                Ok(())
            }
            .await;
            match finished {
                Ok(()) => {
                    status.send_replace(json!({"state":"idle","message":format!("Saved recording: {}",final_path.file_name().unwrap().to_string_lossy()),"file":final_path}));
                }
                Err(e) => {
                    if fs::metadata(&partial).is_ok_and(|m| m.len() == 0) {
                        let _ = fs::remove_file(&partial);
                    }
                    let retained = if partial.exists() {
                        Some(partial)
                    } else if final_path.exists() {
                        Some(final_path)
                    } else {
                        None
                    };
                    let detail=retained.as_ref().map(|p|format!(" Partial video retained at {}. See docs/recording.md for recovery.",p.display())).unwrap_or_default();
                    status.send_replace(
                        json!({"state":"error","message":format!("{e}{detail}"),"file":retained}),
                    );
                }
            }
        }));
        Ok(self.snapshot())
    }
    pub async fn start(&mut self, cfg: &Value) -> Result<Value> {
        match self.start_inner(cfg).await {
            Ok(v) => Ok(v),
            Err(e) => {
                self.set("error", e.to_string(), None);
                Err(e)
            }
        }
    }
    pub async fn stop(&mut self) -> Result<Value> {
        if self.active() {
            if let Some(tx) = &self.control {
                let _ = tx.send(()).await;
            }
            if let Some(task) = self.task.take() {
                let _ = task.await;
            }
        }
        let status = self.snapshot();
        if status["state"] == "error" && !self.replay {
            return err(status["message"].as_str().unwrap_or("Recording failed"));
        }
        Ok(status)
    }
    /// Save the whole buffer, or only its last `seconds` when given.
    pub async fn save(&self, seconds: Option<u64>) -> Result<Value> {
        if !self.replay || !self.active() {
            return err("Instant replay is off. Say “start replay” first.");
        }
        let save_guard = Arc::new(self.replay_cleanup.clone().read_owned().await);
        if !self.active() {
            return err("Instant replay stopped before saving.");
        }
        let previous = self.snapshot();
        self.set(
            "on",
            "Saving the replay…",
            previous["file"].as_str().map(Path::new),
        );
        // GSR saves what it has when asked for more than the buffer holds.
        let seconds = seconds.map_or(self.replay_seconds, |s| s.min(self.replay_seconds));
        let mut path = None;
        let saved = timeout(Duration::from_secs(40), async {
            let mut stream = UnixStream::connect(&self.ipc).await.map_err(|e| {
                format!("Instant replay is still starting; try again in a moment. {e}")
            })?;
            let mut command = serde_json::to_vec(&json!({
                "id":1,"name":"save-replay","data":{"seconds":seconds}
            }))?;
            command.push(b'\n');
            stream.write_all(&command).await?;
            let bytes = util::line(&mut BufReader::new(stream), 65536)
                .await?
                .ok_or("Instant replay did not answer")?;
            let reply: Value = serde_json::from_slice(&bytes)?;
            if reply["result"] != "ok" || !reply["data"].is_string() {
                return err(format!("Replay not saved: {}", reply["data"]));
            }
            let mut output = PathBuf::from(reply["data"].as_str().unwrap());
            if let Some(session) = &self.replay_session {
                let session = session.clone();
                let folder = self
                    .replay_output
                    .clone()
                    .ok_or("Missing replay clip destination")?;
                let publication_guard = save_guard.clone();
                let retained = tokio::task::spawn_blocking(move || {
                    let _guard = publication_guard;
                    crate::replay_clip::retain(&output, &folder, &session)
                })
                .await??;
                output = retained.path;
                path = Some(output.clone());
                if let Some(error) = retained.error {
                    return err(error);
                }
            }
            path = Some(output.clone());
            verify(&output, &self.color, self.codec.as_deref()).await?;
            if self.game_folders {
                // A clip goes in the folder of the game focused when it is saved.
                if let (Some(game), Some(parent), Some(name)) =
                    (desktop::focused_game().await, output.parent(), output.file_name())
                {
                    let folder = parent.join(game);
                    if util::private_dir(&folder).is_ok() && fs::rename(&output, folder.join(name)).is_ok() {
                        output = folder.join(name);
                        path = Some(output.clone());
                    }
                }
            }
            write_track_names(&output, &self.track_names);
            if self.track_names.len() > 2 {
                tokio::spawn(crate::audio_apps::note_playing()); // a track of their own next time
            }
            Ok::<_, crate::Error>(output)
        })
        .await;
        match saved {
            Ok(Ok(path)) => {
                let current = self.snapshot();
                let state = if self.active() && current["state"] != "error" {
                    "on"
                } else {
                    "error"
                };
                self.set(
                    state,
                    format!(
                        "Saved replay{}: {}{}",
                        if seconds < self.replay_seconds {
                            format!(" (last {})", duration(seconds))
                        } else {
                            String::new()
                        },
                        path.file_name().unwrap_or_default().to_string_lossy(),
                        if state == "error" {
                            format!(
                                ". {}",
                                current["message"].as_str().unwrap_or("Replay stopped.")
                            )
                        } else {
                            String::new()
                        },
                    ),
                    Some(&path),
                );
                Ok(self.snapshot())
            }
            other => {
                let message = match other {
                    Ok(Err(e)) => e.to_string(),
                    _ => "Instant replay did not answer within its deadline".into(),
                };
                let message = if let Some(path) = &path {
                    format!("{message} File retained at {}.", path.display())
                } else {
                    message
                };
                let current = self.snapshot();
                if self.active() {
                    self.set(
                        if current["state"] == "error" {
                            "error"
                        } else {
                            "on"
                        },
                        &message,
                        path.as_deref()
                            .or_else(|| previous["file"].as_str().map(Path::new)),
                    );
                }
                err(message)
            }
        }
    }
}
/// "25 seconds", "2 minutes", "1 min 30 s".
pub fn duration(seconds: u64) -> String {
    match (seconds / 60, seconds % 60) {
        (0, 1) => "1 second".into(),
        (0, s) => format!("{s} seconds"),
        (1, 0) => "1 minute".into(),
        (m, 0) => format!("{m} minutes"),
        (m, s) => format!("{m} min {s} s"),
    }
}
pub(crate) fn timestamp() -> String {
    let time = unsafe { libc::time(std::ptr::null_mut()) };
    let mut tm = std::mem::MaybeUninit::<libc::tm>::uninit();
    if unsafe { libc::localtime_r(&time, tm.as_mut_ptr()) }.is_null() {
        return time.to_string();
    }
    let tm = unsafe { tm.assume_init() };
    format!(
        "{:04}-{:02}-{:02}_{:02}-{:02}-{:02}",
        tm.tm_year + 1900,
        tm.tm_mon + 1,
        tm.tm_mday,
        tm.tm_hour,
        tm.tm_min,
        tm.tm_sec
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn automatic_bitrate_follows_output_size_frame_rate_and_format() {
        let display = json!({"width":2560,"height":1440});
        let video = |codec: &str, fps: u64, resolution: &str| {
            json!({"codec":codec,"fps":fps,"resolution":resolution})
        };
        assert_eq!(auto_bitrate(&video("av1", 60, "native"), &display), 17);
        assert_eq!(auto_bitrate(&video("av1", 120, "native"), &display), 33);
        assert_eq!(auto_bitrate(&video("av1", 60, "1080"), &display), 9);
        assert_eq!(auto_bitrate(&video("av1", 60, "2160"), &display), 17); // never scaled up
        assert_eq!(auto_bitrate(&video("h264", 60, "native"), &display), 24);
        assert_eq!(auto_bitrate(&video("av1", 30, "720"), &display), 4); // floor
        assert_eq!(auto_bitrate(&video("av1", 60, "native"), &json!({"width":3840,"height":2160})), 37);
    }
    #[test]
    fn replay_uses_recording_video_unless_it_has_its_own() {
        let mut cfg = json!({
            "recording": {"monitor":"DP-3","fps":60,"codec":"av1_10bit","resolution":"native",
                          "rate_control":"quality","quality_preset":"ultra","bitrate_mbps":8},
            "replay": {"own_video":false,"codec":"hevc","fps":120,"resolution":"1080",
                       "rate_control":"bitrate","quality_preset":"very_high","bitrate_mbps":40,
                       "seconds":60,"storage":"ram","game_audio":false,"microphone":false}
        });
        let arg = |argv: &[String], flag: &str| {
            argv.iter().position(|a| a == flag).map(|i| argv[i + 1].clone())
        };
        let same = replay_command(&cfg, &replay_video(&cfg), Path::new("/ipc"), Path::new("/out"), "sdr", &[]);
        assert_eq!(arg(&same, "-k").as_deref(), Some("av1_10bit"));
        assert_eq!(arg(&same, "-f").as_deref(), Some("60"));
        assert_eq!(arg(&same, "-s"), None);
        cfg["replay"]["own_video"] = json!(true);
        let own = replay_command(&cfg, &replay_video(&cfg), Path::new("/ipc"), Path::new("/out"), "sdr", &[]);
        assert_eq!(arg(&own, "-k").as_deref(), Some("hevc"));
        assert_eq!(arg(&own, "-f").as_deref(), Some("120"));
        assert_eq!(arg(&own, "-s").as_deref(), Some("1920x1080"));
        assert_eq!(arg(&own, "-q").as_deref(), Some("40000"));
        assert_eq!(arg(&own, "-w").as_deref(), Some("DP-3"));
    }

    #[test]
    fn separate_tracks_never_overlap_and_mixed_is_unchanged() {
        let app = |name: &str, label: &str| crate::audio_apps::App {
            name: name.into(),
            label: label.into(),
        };
        let apps = [app("Firefox", "Firefox"), app("WEBRTC VoiceEngine", "Discord")];
        let names = ["Firefox".to_string(), "WEBRTC VoiceEngine".to_string()];
        let mixed = json!({"game_audio":true,"microphone":true,"audio_tracks":"mixed","separate_apps":["Firefox"]});
        assert_eq!(audio_tracks(&mixed, &[]), ["default_output|default_input"]);
        let split = json!({"game_audio":true,"microphone":true,"audio_tracks":"separate","separate_apps":[]});
        assert_eq!(
            audio_tracks(&split, &names),
            [
                "app-inverse:Firefox|app-inverse:WEBRTC VoiceEngine",
                "app:Firefox",
                "app:WEBRTC VoiceEngine",
                "default_input"
            ]
        );
        assert_eq!(audio_tracks(&split, &[]), ["default_output", "default_input"]);
        let quiet = json!({"game_audio":false,"microphone":false,"audio_tracks":"separate","separate_apps":[]});
        assert!(audio_tracks(&quiet, &[]).is_empty());
        assert!(!valid_app_name("a|b") && !valid_app_name("app:x") && valid_app_name("Firefox Nightly"));
        assert_eq!(track_names(&split, &apps), ["Everything else", "Firefox", "Discord", "Microphone"]);
        assert_eq!(track_names(&split, &[]), ["Game and desktop sound", "Microphone"]);
        assert_eq!(track_names(&mixed, &[]), ["Game sound and microphone"]);
    }
}
