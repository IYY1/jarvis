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
        "dictation",
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
    choices(
        &n["speech_model"],
        &["small", "light"],
        "Speech model must be small or light",
    )?;
    choices(
        &n["language"],
        &["en", "ar_en"],
        "Recognition language must be en (English) or ar_en (Arabic and English)",
    )?;
    let d = &c["dictation"];
    choices(
        &d["language"],
        &["auto", "ar", "en"],
        "Dictation language must be auto, ar or en",
    )?;
    if !d["idle_seconds"]
        .as_f64()
        .is_some_and(|v| (2.0..=300.0).contains(&v))
        || !d["max_minutes"]
            .as_f64()
            .is_some_and(|v| (1.0..=60.0).contains(&v))
        || !d["keyboard_fallback"].is_boolean()
    {
        return err("Dictation needs 5–300 idle seconds, 1–60 minutes and a true/false keyboard fallback");
    }
    let stops = d["stop_phrases"]
        .as_array()
        .filter(|a| a.len() <= 8)
        .ok_or("Use up to eight dictation stop phrases")?;
    for phrase in stops {
        let ok = phrase.as_str().is_some_and(|s| {
            s.chars().count() <= 40 && !normalize(s).unwrap_or_default().is_empty()
        });
        if !ok {
            return err("Dictation stop phrases must be short words");
        }
    }
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
        (&r["fps"], 1, 240, "Recording fps must be 1–240"),
        (&r["quality"], 0, 51, "Recording quality must be CRF 0–51"),
        (
            &r["bitrate_mbps"],
            1,
            500,
            "Recording bitrate must be 1–500 Mbps",
        ),
        (
            &c["replay"]["seconds"],
            10,
            1800,
            "Replay length must be 10–1800 seconds (30 minutes)",
        ),
    ] {
        if !value.as_u64().is_some_and(|n| (min..=max).contains(&n)) {
            return err(message);
        }
    }
    choices(
        &r["codec"],
        &[
            "libx264",
            "h264_vaapi",
            "h264",
            "hevc",
            "av1",
            "hevc_10bit",
            "av1_10bit",
        ],
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
        &["medium", "high", "very_high", "ultra"],
        "Choose a supported quality preset",
    )?;
    if !r["bitrate_auto"].is_boolean() {
        return err("Automatic bitrate must be true or false");
    }
    choices(
        &r["resolution"],
        &crate::capture::RESOLUTIONS.map(|(name, _)| name),
        "Choose native, 2160, 1440, 1080 or 720 recording resolution",
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
    for key in ["autostart", "game_audio", "microphone", "sound", "own_video", "bitrate_auto"] {
        if !c["replay"][key].is_boolean() {
            return err("Replay switches must be true or false");
        }
    }
    // Replay's own video settings (used when own_video is on); GPU formats only.
    let v = &c["replay"];
    choices(
        &v["codec"],
        &crate::capture::GPU_CODECS,
        "Replay records with a GPU format: choose AV1, HEVC or H.264",
    )?;
    for (value, max, message) in [
        (&v["fps"], 240, "Replay fps must be 1–240"),
        (&v["bitrate_mbps"], 500, "Replay bitrate must be 1–500 Mbps"),
    ] {
        if !value.as_u64().is_some_and(|n| (1..=max).contains(&n)) {
            return err(message);
        }
    }
    choices(&v["rate_control"], &["quality", "bitrate"], "Choose quality or bitrate control")?;
    choices(
        &v["quality_preset"],
        &["medium", "high", "very_high", "ultra"],
        "Choose a supported quality preset",
    )?;
    choices(
        &v["resolution"],
        &crate::capture::RESOLUTIONS.map(|(name, _)| name),
        "Choose native, 2160, 1440, 1080 or 720 recording resolution",
    )?;
    choices(
        &c["replay"]["audio_tracks"],
        &["mixed", "separate"],
        "Replay audio tracks must be mixed or separate",
    )?;
    let apps = c["replay"]["separate_apps"]
        .as_array()
        .filter(|a| a.len() <= 4)
        .ok_or("Choose up to four apps for their own replay tracks")?;
    for app in apps {
        if !app.as_str().is_some_and(crate::capture::valid_app_name) {
            return err("App audio names may use letters, digits, spaces, dots, dashes and underscores");
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
/// Earlier copies of the settings: before a repair, an import, or the first
/// start of another version. The newest ten are kept.
pub fn backups_dir() -> PathBuf {
    path().with_file_name("backups")
}
/// Copies the settings file to backups/config-<label>.json. Best effort.
pub fn backup(label: &str) -> Option<PathBuf> {
    let source = path();
    if !source.is_file() {
        return None;
    }
    let dir = backups_dir();
    util::private_dir(&dir).ok()?;
    let target = dir.join(format!("config-{label}.json"));
    fs::copy(&source, &target).ok()?;
    let mut copies: Vec<_> = fs::read_dir(&dir)
        .ok()?
        .filter_map(|e| e.ok())
        .filter(|e| e.file_name().to_string_lossy().starts_with("config-"))
        .filter_map(|e| Some((e.metadata().ok()?.modified().ok()?, e.path())))
        .collect();
    copies.sort();
    for (_, old) in copies.iter().rev().skip(10) {
        let _ = fs::remove_file(old);
    }
    Some(target)
}
/// On the first start of a different version, keeps the settings the
/// previous one wrote (backups/config-from-<version>.json).
pub fn note_version(version: &str) {
    let stamp = path().with_file_name(".version");
    let previous = fs::read_to_string(&stamp).unwrap_or_default();
    let previous = previous.trim();
    if previous != version {
        backup(&format!(
            "from-{}",
            if previous.is_empty() { "earlier" } else { previous }
        ));
        let _ = util::atomic_write(&stamp, version.as_bytes());
    }
}
/// Settings another Jarvis version wrote can hold a value this one rejects
/// (a changed range or a renamed choice). Keeps every value that still
/// validates and resets only the others; returns them as "section.name".
pub fn repair(data: &Value) -> (Value, Vec<String>) {
    let mut pending: Vec<(String, Option<String>, Value)> = vec![];
    if let (Some(map), Value::Object(order)) = (data.as_object(), defaults()) {
        // Defaults order puts aliases before app_prefixes, which refer to them.
        for section in order.keys() {
            match map.get(section) {
                Some(Value::Object(fields)) if !fields.is_empty() => pending.extend(
                    fields
                        .iter()
                        .map(|(name, v)| (section.clone(), Some(name.clone()), v.clone())),
                ),
                Some(value) => pending.push((section.clone(), None, value.clone())),
                None => {}
            }
        }
    }
    let mut kept = Value::Object(Default::default());
    // A value can depend on another (two audio sources need the combined
    // source), so retry the rejected ones while more are being accepted.
    for _ in 0..3 {
        let before = pending.len();
        pending.retain(|(section, name, value)| {
            let mut trial = kept.clone();
            match name {
                Some(name) => trial[section.as_str()][name.as_str()] = value.clone(),
                None => trial[section.as_str()] = value.clone(),
            }
            if validate(&trial).is_ok() {
                kept = trial;
                false
            } else {
                true
            }
        });
        if pending.len() == before {
            break;
        }
    }
    let reset = pending
        .into_iter()
        .map(|(section, name, _)| name.map_or(section.clone(), |n| format!("{section}.{n}")))
        .collect();
    (validate(&kept).unwrap_or_else(|_| defaults()), reset)
}
pub fn load() -> Result<Value> {
    let path = path();
    if !path.exists() {
        return Ok(defaults());
    }
    if fs::metadata(&path)?.len() > 65536 {
        return err("Configuration exceeds 64 KiB");
    }
    let stamp = crate::capture::timestamp();
    let mut data: Value = match serde_json::from_slice(&fs::read(&path)?) {
        Ok(data) => data,
        Err(e) => {
            let kept = backup(&format!("unreadable-{stamp}"));
            eprintln!("jarvis: settings file is not valid JSON ({e}); starting from defaults, original kept at {kept:?}");
            return save(&defaults());
        }
    };
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
    match validate(&data) {
        Ok(c) => Ok(c),
        Err(e) => {
            let (c, reset) = repair(&data);
            let kept = backup(&format!("before-repair-{stamp}"));
            eprintln!(
                "jarvis: some settings are not valid in this version ({e}); reset {} and kept the rest; original at {kept:?}",
                reset.join(", ")
            );
            save(&c)
        }
    }
}
pub fn save(data: &Value) -> Result<Value> {
    let c = validate(data)?;
    let mut bytes = serde_json::to_vec(&c)?;
    bytes.push(b'\n');
    util::atomic_write(&path(), &bytes)?;
    Ok(c)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn repair_keeps_valid_settings_and_resets_only_rejected_ones() {
        // Written by another version: fps above this version's range and a
        // removed quality preset; everything else must survive.
        let old = json!({
            "activation": {"wake_phrase": "alfred"},
            "aliases": {"streamio": "com.stremio.Stremio.desktop"},
            "app_prefixes": {"streamio": "watch"},
            "recording": {"fps": 999, "quality_preset": "insane", "codec": "av1",
                          "system_audio": true, "microphone": true,
                          "audio_device": "combined", "combined_audio_source": true},
            "replay": {"seconds": 300}
        });
        assert!(validate(&old).is_err());
        let (fixed, reset) = repair(&old);
        assert_eq!(reset, vec!["recording.fps", "recording.quality_preset"]);
        assert_eq!(fixed["activation"]["wake_phrase"], "alfred");
        assert_eq!(fixed["app_prefixes"]["streamio"], "watch");
        assert_eq!(fixed["recording"]["codec"], "av1");
        // Accepted once the combined source it depends on is in.
        assert_eq!(fixed["recording"]["microphone"], true);
        assert_eq!(fixed["recording"]["fps"], defaults()["recording"]["fps"]);
        assert_eq!(fixed["replay"]["seconds"], 300);
    }
}
