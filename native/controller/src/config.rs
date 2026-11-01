use crate::{
    err,
    policy::{normalize, Parser},
    util, Result,
};
use serde_json::Value;
use std::{collections::HashSet, env, fs, path::PathBuf};

pub fn defaults() -> Value {
    serde_json::from_str(include_str!("../data/defaults.json")).unwrap()
}
pub fn path() -> PathBuf {
    env::var_os("MURMUR_CONFIG")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            env::var_os("XDG_CONFIG_HOME")
                .map(PathBuf::from)
                .unwrap_or_else(|| util::home().join(".config"))
                .join("murmur/config.json")
        })
}
fn merge(base: &mut Value, patch: &Value) {
    if let (Some(a), Some(b)) = (base.as_object_mut(), patch.as_object()) {
        for (k, v) in b {
            if let Some(old) = a.get_mut(k) {
                if old.is_object() && v.is_object() {
                    merge(old, v);
                } else {
                    *old = v.clone();
                }
            } else {
                a.insert(k.clone(), v.clone());
            }
        }
    } else {
        *base = patch.clone();
    }
}
pub fn unknown(data: &Value) -> Vec<String> {
    unknown_settings(data, &defaults())
}
fn unknown_settings(data: &Value, d: &Value) -> Vec<String> {
    let mut result = vec![];
    if let Some(object) = data.as_object() {
        for (k, v) in object {
            match d.get(k) {
                None => result.push(k.clone()),
                Some(allowed) => {
                    if !["commands", "aliases", "app_prefixes"].contains(&k.as_str()) {
                        if let (Some(v), Some(allowed)) = (v.as_object(), allowed.as_object()) {
                            for key in v.keys() {
                                if !allowed.contains_key(key) {
                                    result.push(format!("{k}.{key}"));
                                }
                            }
                        }
                    }
                }
            }
        }
    }
    result.sort();
    result
}
fn text(value: &Value, max: usize, label: &str) -> Result<()> {
    if !value
        .as_str()
        .is_some_and(|s| s.chars().count() <= max && !s.contains('\0'))
    {
        return err(format!("Invalid {label}"));
    }
    Ok(())
}
fn choices(value: &Value, allowed: &[&str], label: &str) -> Result<()> {
    if !value.as_str().is_some_and(|s| allowed.contains(&s)) {
        return err(label);
    }
    Ok(())
}
pub fn validate(data: &Value) -> Result<Value> {
    if !data.is_object() {
        return err("Configuration must be a JSON object");
    }
    let mut c = defaults();
    let keys = unknown_settings(data, &c);
    if !keys.is_empty() {
        return err(format!(
            "Unknown setting(s): {}",
            keys.into_iter().take(5).collect::<Vec<_>>().join(", ")
        ));
    }
    merge(&mut c, data);
    for section in [
        "activation",
        "audio",
        "recognition",
        "privacy",
        "recording",
        "replay",
        "aliases",
        "commands",
    ] {
        if !c[section].is_object() {
            return err(format!("{section} must be an object"));
        }
    }
    let a = &c["activation"];
    let r = &c["recording"];
    let n = &c["recognition"];
    choices(
        &n["backend"],
        &["auto", "pocketsphinx", "vosk"],
        "Recognition backend must be auto, pocketsphinx or vosk",
    )?;
    text(&n["model_path"], 4096, "voice model path")?;
    for value in [
        &n["closest_match"],
        &n["accurate"],
        &n["noise_suppression"],
        &c["audio"]["adaptive_gate"],
    ] {
        if !value.is_boolean() {
            return err("Recognition and audio switches must be true or false");
        }
    }
    choices(
        &a["mode"],
        &["wake_word", "push_to_talk"],
        "Activation must be wake_word or push_to_talk",
    )?;
    if !a["wake_phrase"]
        .as_str()
        .is_some_and(|s| s.chars().count() <= 80 && normalize(s).is_ok_and(|s| !s.is_empty()))
    {
        return err("Choose a short wake phrase");
    }
    let names = a["wake_aliases"]
        .as_array()
        .ok_or("Wake aliases must be a list")?;
    if names.len() > 7 {
        return err("Use up to eight wake names including the primary name");
    }
    let mut seen = HashSet::new();
    for value in std::iter::once(&a["wake_phrase"]).chain(names.iter()) {
        text(value, 80, "wake name")?;
        let n = normalize(value.as_str().unwrap())?;
        let parts: Vec<_> = n.split_whitespace().collect();
        let name = if parts.len() > 1
            && crate::policy::strings(&crate::policy::TABLES["GREETINGS"])
                .iter()
                .any(|g| g == parts[0])
        {
            parts[1..].join(" ")
        } else {
            n
        };
        if name.is_empty() || !seen.insert(name) {
            return err("Wake names must be distinct and nonempty");
        }
    }
    for (value, min, max, name) in [
        (&a["sensitivity"], 0., 1., "wake sensitivity"),
        (&a["confidence"], 0., 1., "confidence"),
        (&a["command_timeout"], 2., 15., "command timeout"),
        (&c["audio"]["vad_rms"], 0.0001, 0.5, "VAD level"),
    ] {
        if !value
            .as_f64()
            .is_some_and(|v| v.is_finite() && (min..=max).contains(&v))
        {
            return err(format!("Invalid {name}: expected {min}–{max}"));
        }
    }
    text(&c["audio"]["device"], 512, "audio device")?;
    for section in ["aliases", "commands"] {
        let map = c[section].as_object().unwrap();
        let mut names = HashSet::new();
        if map.len() > 256 {
            return err(format!(
                "Invalid {section}: use short phrase to target mappings"
            ));
        }
        for (k, v) in map {
            if k.chars().count() > 120 || !v.as_str().is_some_and(|s| s.chars().count() <= 256) {
                return err(format!(
                    "Invalid {section}: use short phrase to target mappings"
                ));
            }
            let name = normalize(k)?;
            if name.is_empty() {
                return err(format!(
                    "Invalid {section}: use short phrase to target mappings"
                ));
            }
            if !names.insert(name) {
                return err(format!("Duplicate normalized {section}"));
            }
        }
    }
    Parser::from_config(&c)?;
    for value in [
        &c["privacy"]["onboarded"],
        &c["startup"],
        &r["configured"],
        &r["system_audio"],
        &r["microphone"],
        &r["combined_audio_source"],
        &r["sound_feedback"],
    ] {
        if !value.is_boolean() {
            return err("Consent, startup and recording flags must be booleans");
        }
    }
    choices(
        &r["color_mode"],
        &["auto", "hdr", "sdr"],
        "Capture color mode must be auto, hdr or sdr",
    )?;
    for (value, min, max, message) in [
        (&r["fps"], 1, 60, "Recording fps must be 1–60"),
        (&r["quality"], 0, 51, "Recording quality must be CRF 0–51"),
        (
            &r["bitrate_mbps"],
            1,
            100,
            "Recording bitrate must be 1–100 Mbps",
        ),
        (
            &c["replay"]["seconds"],
            10,
            600,
            "Replay length must be 10–600 seconds",
        ),
    ] {
        if !value.as_u64().is_some_and(|n| (min..=max).contains(&n)) {
            return err(message);
        }
    }
    choices(
        &r["codec"],
        &["libx264", "h264_vaapi", "h264", "hevc", "av1"],
        "Choose H.264, HEVC or AV1 recording encoding",
    )?;
    choices(
        &c["replay"]["storage"],
        &["ram", "disk"],
        "Replay storage must be ram or disk",
    )?;
    if !c["replay"]["max_mb"]
        .as_u64()
        .is_some_and(|n| (64..=16384).contains(&n))
    {
        return err("Replay budget must be 64–16384 MiB");
    }
    text(&c["replay"]["buffer_dir"], 4096, "replay buffer directory")?;
    choices(
        &r["rate_control"],
        &["quality", "bitrate"],
        "Choose quality or bitrate control",
    )?;
    choices(
        &r["quality_preset"],
        &["medium", "high", "very_high"],
        "Choose a supported quality preset",
    )?;
    choices(
        &r["encoder"],
        &["auto", "nvenc", "amf", "qsv", "cpu"],
        "Choose a supported encoder",
    )?;
    if !r["vaapi_device"]
        .as_str()
        .is_some_and(|s| s.starts_with("/dev/dri/renderD") && s.chars().count() <= 64)
    {
        return err("VA-API device must be a /dev/dri/renderD* path");
    }
    for key in ["output_dir", "monitor", "audio_device"] {
        text(&r[key], 4096, &format!("recording {key}"))?;
    }
    for key in ["autostart", "game_audio", "microphone", "sound"] {
        if !c["replay"][key].is_boolean() {
            return err("Replay switches must be true or false");
        }
    }
    if r["system_audio"] == true
        && r["microphone"] == true
        && !(r["combined_audio_source"] == true
            && !r["audio_device"].as_str().unwrap().trim().is_empty())
    {
        return err(
            "wf-recorder accepts one audio source. Set up a combined PipeWire source first.",
        );
    }
    let serialized = serde_json::to_string(&c)?;
    // Python's validator counts its default ensure_ascii JSON plus separator
    // spaces; retain that limit so both controllers accept the same settings.
    let mut bytes = 0;
    let mut quoted = false;
    let mut escaped = false;
    for ch in serialized.chars() {
        bytes += if ch.is_ascii() { 1 } else { 6 * ch.len_utf16() };
        if !quoted && (ch == ',' || ch == ':') {
            bytes += 1;
        }
        if ch == '"' && !escaped {
            quoted = !quoted;
        }
        escaped = ch == '\\' && !escaped;
    }
    if bytes > 60000 {
        return err("Configuration is too large; keep serialized settings below 60 KiB");
    }
    Ok(c)
}
pub fn load() -> Result<Value> {
    let path = path();
    if !path.exists() {
        return Ok(defaults());
    }
    if fs::metadata(&path)?.len() > 65536 {
        return err("Configuration exceeds 64 KiB");
    }
    let mut data: Value = serde_json::from_slice(&fs::read(path)?)?;
    let keys = unknown(&data);
    if !keys.is_empty() {
        eprintln!("jarvis: ignoring unknown settings: {}", keys.join(", "));
    }
    for key in keys {
        if let Some((section, name)) = key.split_once('.') {
            if let Some(map) = data[section].as_object_mut() {
                map.remove(name);
            }
        } else if let Some(map) = data.as_object_mut() {
            map.remove(&key);
        }
    }
    validate(&data)
}
pub fn save(data: &Value) -> Result<Value> {
    let c = validate(data)?;
    let mut bytes = serde_json::to_vec(&c)?;
    bytes.push(b'\n');
    util::atomic_write(&path(), &bytes)?;
    Ok(c)
}
