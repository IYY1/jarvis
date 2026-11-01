use crate::models;
use jarvis_controller::{config, util, voice};
use serde_json::{json, Map, Value};
use std::env;

pub async fn report() -> Value {
    let memory = match tokio::time::timeout(
        std::time::Duration::from_secs(2),
        jarvis_controller::ipc::request(&json!({"op":"status"})),
    )
    .await
    {
        Ok(Ok(response)) if response["memory"].is_object() => response["memory"].clone(),
        _ => {
            json!({"complete":false,"error":"Service unavailable or needs restarting to provide process memory"})
        }
    };
    let mut tools = Map::new();
    for (name, arg) in [
        ("hyprctl", "version"),
        ("pw-cli", "--version"),
        ("pw-record", "--version"),
        ("pw-play", "--version"),
        ("pw-dump", "--version"),
        ("pactl", "--version"),
        ("wf-recorder", "--version"),
        ("gpu-screen-recorder", "--version"),
        ("wpctl", "status"),
        ("ffprobe", "-version"),
        ("gio", "version"),
        ("setpriv", "--version"),
        ("systemd-run", "--version"),
    ] {
        let path = util::which(name);
        let result = if let Some(path) = &path {
            match util::query(&[path.to_string_lossy().into_owned(), arg.into()], 3).await {
                Ok(output) => {
                    json!({"path":path,"ok":true,"version":output.lines().take(1).collect::<Vec<_>>()})
                }
                Err(error) => json!({"path":path,"ok":false,"error":error.to_string()}),
            }
        } else {
            json!({"path":null,"ok":false,"version":[]})
        };
        tools.insert(name.into(), result);
    }
    let settings = match config::load() {
        Ok(value) => value,
        Err(error) => {
            return json!({"runtime":"Rust","error":error.to_string(),"tools":tools,"memory":memory})
        }
    };
    let model = settings["recognition"]["model_path"]
        .as_str()
        .filter(|p| !p.is_empty())
        .map(util::expand)
        .unwrap_or_else(|| models::root().join(models::VOSK_NAME));
    let ui = voice::ui_argv("dashboard").ok();
    json!({
        "runtime":"Rust", "version":env!("CARGO_PKG_VERSION"), "interpreter":null,
        "hyprland_session":env::var_os("HYPRLAND_INSTANCE_SIGNATURE").is_some_and(|s|!s.is_empty()),
        "wayland_session":env::var_os("WAYLAND_DISPLAY").is_some_and(|s|!s.is_empty()),
        "voice_engine":{"backend":"vosk", "configured_backend":settings["recognition"]["backend"],
            "worker":voice::binary().ok(),"vosk_library":voice::vosk_library().ok(),
            "model_path":model,"model_ready":models::valid(&model,models::VOSK_FILES),
            "accurate_model_ready":models::valid(&models::root().join(models::WHISPER_NAME),models::WHISPER_FILES),
            "accurate_model_on":settings["recognition"]["accurate"],
            "noise_suppression_on":settings["recognition"]["noise_suppression"],
            "denoiser_model_ready":models::root().join(models::DENOISER_NAME).is_file()},
        "settings_ui":{"runtime":"Python/GTK", "launcher":ui,"imports_checked":false},
        "tools":tools,"memory":memory,"note":"Dependency presence is not microphone, capture permission, or recognition verification. The Rust controller requires auto/vosk; PocketSphinx remains a Python fallback."
    })
}
