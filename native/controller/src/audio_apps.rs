//! Which apps get their own instant-replay sound track.
//!
//! With separate tracks, each app gets one, so a clip can keep Discord and mute
//! Firefox: the apps you named, those playing sound when replay starts, and
//! those heard before (newest first), up to `MAX_APPS`. gpu-screen-recorder
//! links an `app:` source whenever that app plays, so a remembered app that
//! starts later still lands on its own track. An app never heard before lands
//! in "Everything else" and gets its own track from the next start.
//! Mirrored by replay.py for the Python controller.

use crate::{capture::valid_app_name, policy::strings, util};
use serde_json::{json, Value};
use std::{collections::HashMap, path::PathBuf};
use tokio::{
    process::Command,
    time::{timeout, Duration},
};

/// gpu-screen-recorder records ten audio tracks without trouble: eight apps,
/// everything else and the microphone.
pub const MAX_APPS: usize = 8;
const REMEMBERED: usize = 16;
/// Jarvis's own sound (the clip preview, save chirps) stays in "Everything else".
const OWN: [&str; 3] = ["jarvis-ui", "pw-play", "pw-cat"];

/// An app as gpu-screen-recorder matches it (`app:NAME`) and the name to show.
#[derive(Clone, Debug, PartialEq)]
pub struct App {
    pub name: String,
    pub label: String,
}

fn heard_path() -> PathBuf {
    env_dir("XDG_STATE_HOME")
        .unwrap_or_else(|| util::home().join(".local/state"))
        .join("jarvis/heard-apps.json")
}

fn env_dir(name: &str) -> Option<PathBuf> {
    std::env::var_os(name)
        .map(PathBuf::from)
        .filter(|p| p.is_absolute())
}

/// Apps heard before, newest first.
pub fn heard() -> Vec<App> {
    let Ok(text) = std::fs::read_to_string(heard_path()) else {
        return vec![];
    };
    let Ok(value) = serde_json::from_str::<Value>(&text) else {
        return vec![];
    };
    value["apps"]
        .as_array()
        .into_iter()
        .flatten()
        .filter_map(|app| {
            let name = app["name"].as_str()?.to_owned();
            let label = app["label"].as_str().unwrap_or(&name).to_owned();
            Some(App { name, label })
        })
        .collect()
}

fn remember(apps: &[App]) {
    if apps.is_empty() {
        return;
    }
    let mut kept = apps.to_vec();
    for app in heard() {
        if !kept.iter().any(|a| a.name.eq_ignore_ascii_case(&app.name)) {
            kept.push(app);
        }
    }
    kept.truncate(REMEMBERED);
    let list: Vec<Value> = kept
        .iter()
        .map(|a| json!({"name":a.name,"label":a.label}))
        .collect();
    let _ = util::atomic_write(
        &heard_path(),
        json!({ "apps": list }).to_string().as_bytes(),
    );
}

/// The program behind generic stream names: Discord's voice plays as
/// "WEBRTC VoiceEngine", and so do calls in other Chromium-based apps.
pub fn label(name: &str, program: Option<&str>) -> String {
    let lower = name.to_lowercase();
    match program.map(str::trim).filter(|p| !p.is_empty()) {
        Some(program) if lower.contains("webrtc") || lower.contains("voiceengine") => {
            let mut letters = program.chars();
            letters
                .next()
                .map(|first| first.to_uppercase().chain(letters).collect())
                .unwrap_or_else(|| name.to_owned())
        }
        _ => name.to_owned(),
    }
}

fn wanted(name: &str) -> bool {
    valid_app_name(name) && !OWN.contains(&name.to_lowercase().as_str())
}

/// Apps playing sound now, as gpu-screen-recorder lists them. Best effort:
/// an error only means no app gets an automatic track this time.
pub async fn playing() -> Vec<App> {
    let listing = Command::new("gpu-screen-recorder")
        .arg("--list-application-audio")
        .kill_on_drop(true)
        .output();
    let Ok(Ok(output)) = timeout(Duration::from_secs(5), listing).await else {
        return vec![];
    };
    let names: Vec<String> = String::from_utf8_lossy(&output.stdout)
        .lines()
        .map(str::trim)
        .filter(|name| wanted(name))
        .map(str::to_owned)
        .collect();
    if names.is_empty() {
        return vec![];
    }
    let programs = programs().await;
    names
        .into_iter()
        .map(|name| App {
            label: label(&name, programs.get(&name).map(String::as_str)),
            name,
        })
        .collect()
}

/// Application name → program, from PipeWire's playback streams.
async fn programs() -> HashMap<String, String> {
    let dump = Command::new("pw-dump").kill_on_drop(true).output();
    let Ok(Ok(output)) = timeout(Duration::from_secs(5), dump).await else {
        return HashMap::new();
    };
    let Ok(Value::Array(objects)) = serde_json::from_slice::<Value>(&output.stdout) else {
        return HashMap::new();
    };
    objects
        .iter()
        .map(|o| &o["info"]["props"])
        .filter(|p| p["media.class"] == "Stream/Output/Audio")
        .filter_map(|p| {
            Some((
                p["application.name"].as_str()?.to_owned(),
                p["application.process.binary"].as_str()?.to_owned(),
            ))
        })
        .collect()
}

/// The apps with their own track, in track order: named first, then playing,
/// then heard before; at most `MAX_APPS`, each once.
pub fn choose(replay: &Value, playing: &[App], heard: &[App]) -> Vec<App> {
    let named = strings(&replay["separate_apps"]).into_iter().map(|name| {
        let label = playing
            .iter()
            .chain(heard)
            .find(|a| a.name.eq_ignore_ascii_case(&name))
            .map_or_else(|| name.clone(), |a| a.label.clone());
        App { name, label }
    });
    let mut apps: Vec<App> = vec![];
    for app in named
        .chain(playing.iter().cloned())
        .chain(heard.iter().cloned())
    {
        if apps.len() == MAX_APPS {
            break;
        }
        if wanted(&app.name) && !apps.iter().any(|a| a.name.eq_ignore_ascii_case(&app.name)) {
            apps.push(app);
        }
    }
    apps
}

/// The apps for a replay starting now. Apps playing are remembered, so they
/// keep their own track at later starts even when quiet then.
pub async fn for_replay(replay: &Value) -> Vec<App> {
    if replay["audio_tracks"] != "separate" {
        return vec![];
    }
    if replay["game_audio"] != true {
        // No desktop sound: only the apps you named are recorded.
        return choose(replay, &[], &[]);
    }
    let playing = playing().await;
    remember(&playing);
    choose(replay, &playing, &heard())
}

/// After a clip is saved: apps playing now get their own track next time.
pub async fn note_playing() {
    remember(&playing().await);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn app(name: &str) -> App {
        App {
            name: name.into(),
            label: name.into(),
        }
    }

    #[test]
    fn named_then_playing_then_heard_each_once_and_never_jarvis() {
        let replay =
            json!({"audio_tracks":"separate","game_audio":true,"separate_apps":["Spotify"]});
        let playing = [app("Firefox"), app("jarvis-ui"), app("WEBRTC VoiceEngine")];
        let heard = [app("firefox"), app("Deadlock"), app("bad|name")];
        let names: Vec<String> = choose(&replay, &playing, &heard)
            .into_iter()
            .map(|a| a.name)
            .collect();
        assert_eq!(
            names,
            ["Spotify", "Firefox", "WEBRTC VoiceEngine", "Deadlock"]
        );
        let many: Vec<App> = (0..12).map(|i| app(&format!("App {i}"))).collect();
        assert_eq!(choose(&replay, &many, &[]).len(), MAX_APPS);
    }

    #[test]
    fn generic_stream_names_show_their_program() {
        assert_eq!(label("WEBRTC VoiceEngine", Some("Discord")), "Discord");
        assert_eq!(label("WEBRTC VoiceEngine", None), "WEBRTC VoiceEngine");
        assert_eq!(label("Firefox", Some("firefox")), "Firefox");
    }
}
