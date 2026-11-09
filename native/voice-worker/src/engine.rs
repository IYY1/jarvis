use crate::inference::{release_unused, trim, Arabic, Decoder, Model, Text, Transcriber};
use crate::matching::{phrase, wording, Matcher};
use crate::on_demand::{Loader, OnDemand};
use crate::{NativeConfig, Result};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::collections::{BTreeSet, HashMap, HashSet, VecDeque};
use std::sync::Arc;
use std::time::Duration;

#[derive(Default, Deserialize)]
pub struct Grammar {
    #[serde(default)]
    pub phrases: Vec<String>,
    #[serde(default)]
    pub apps: Vec<String>,
    #[serde(default)]
    pub verbs: Vec<String>,
    #[serde(default)]
    pub prefixes: Vec<String>,
    #[serde(default)]
    pub overflow: Vec<String>,
    #[serde(default)]
    pub launches: serde_json::Map<String, Value>,
    #[serde(default)]
    pub blocked_apps: HashSet<String>,
    #[serde(default = "default_close_verbs")]
    pub close_verbs: Vec<String>,
}

fn default_close_verbs() -> Vec<String> {
    vec!["close".into()]
}

#[cfg(test)]
mod early_tests {
    use super::*;
    use std::time::{Duration, Instant};

    struct Slow(Duration);
    fn slow_len(model: &Slow, audio: &[u8]) -> Result<usize> {
        std::thread::sleep(model.0);
        Ok(audio.len())
    }
    fn fails(_: &Slow, _: &[u8]) -> Result<usize> {
        Err("corrupt GTCRN".into())
    }
    fn panics(_: &Slow, _: &[u8]) -> Result<usize> {
        panic!("GTCRN kernel panicked")
    }

    #[test]
    fn joined_result_and_errors_reach_the_caller() {
        let model = Arc::new(Slow(Duration::from_millis(50)));
        assert_eq!(finish(early(model.clone(), &[0; 960], slow_len).unwrap()).unwrap(), 960);
        let error = finish(early(model.clone(), &[], fails).unwrap()).err().unwrap();
        assert_eq!(error.to_string(), "corrupt GTCRN");
        let error = finish(early(model, &[], panics).unwrap()).err().unwrap();
        assert_eq!(error.to_string(), "Denoiser stopped unexpectedly");
    }

    /// An Arabic match drops the handle while GTCRN is still running: the
    /// caller must not wait, and the model handle must go away afterwards so
    /// an idle release can free the weights.
    #[test]
    fn dropping_a_running_handle_detaches_and_later_frees_the_model() {
        let model = Arc::new(Slow(Duration::from_millis(300)));
        let start = Instant::now();
        let handle = early(model.clone(), &[0; 960], slow_len).unwrap();
        std::thread::sleep(Duration::from_millis(20));
        assert_eq!(Arc::strong_count(&model), 2, "the thread holds the model");
        drop(handle);
        assert!(start.elapsed() < Duration::from_millis(150), "the caller waited");
        let deadline = Instant::now() + Duration::from_secs(5);
        while Arc::strong_count(&model) > 1 {
            assert!(Instant::now() < deadline, "the detached thread kept the model");
            std::thread::sleep(Duration::from_millis(10));
        }
    }
}

#[cfg(test)]
mod confidence_tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn fast_path_never_uses_missing_or_partial_word_confidence() {
        let good = json!({"text":"open firefox","result":[{"word":"open","conf":0.99},{"word":"firefox","conf":1.0}]});
        assert_eq!(complete_confidence(&good), Some(0.99));
        for bad in [
            json!({"text":"open firefox","result":[]}),
            json!({"text":"open firefox"}),
            json!({"text":"open firefox","result":[{"word":"open","conf":0.99},{"word":"firefox"}]}),
            json!({"text":"open firefox","result":[{"word":"open","conf":1.01},{"word":"firefox","conf":1.0}]}),
            json!({"text":"open firefox","result":[{"word":"open","conf":1.0}]}),
            json!({"text":"open firefox","result":[{"word":"open firefox","conf":1.0}]}),
        ] {
            assert!(complete_confidence(&bad).is_none(), "{bad}");
        }
    }
    #[test]
    fn arabic_hears_the_100_ms_before_the_wake_name_ends() {
        let audio: Vec<u8> = (0..64000u32).map(|i| i as u8).collect();
        assert_eq!(arabic_lead(&audio, 32000), &audio[28800..32000]);
        assert_eq!(arabic_lead(&audio, 32001), &audio[28800..32000]); // odd cut: whole samples
        assert_eq!(arabic_lead(&audio, 1000), &audio[..1000]);
        assert!(arabic_lead(&audio, 0).is_empty());
        assert_eq!(arabic_lead(&audio, 90000), &audio[60800..]);
    }
}

#[derive(Clone, Debug, Serialize)]
pub struct Recognition {
    pub kind: String,
    pub text: String,
    pub confidence: Option<f64>,
    pub min_word_posterior: Option<f64>,
    pub heard: Option<String>,
    pub closest_match: bool,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub recognition_pass: Option<&'static str>,
}
impl Recognition {
    pub(crate) fn new(kind: &str, text: String, confidence: Option<f64>) -> Self {
        Self {
            kind: kind.into(),
            text,
            confidence,
            min_word_posterior: None,
            heard: None,
            closest_match: false,
            recognition_pass: None,
        }
    }
}

pub struct Engine {
    // Decoders retain their shared model, so all handle destruction is ordered.
    wake: Decoder,
    command: Decoder,
    open: Option<Decoder>,
    whisper: Option<OnDemand<Transcriber>>,
    /// Arabic commands and dictation, loaded alongside Whisper on demand.
    arabic: Option<OnDemand<Arabic>>,
    dictation_language: &'static str,
    vosk_fast_path: bool,
    finalize_utterances: bool,
    fast_commands: u64,
    whisper_commands: u64,
    matcher: Matcher,
    sounds: HashMap<String, String>,
    wake_names: Vec<Vec<String>>,
    wake_phrase: String,
    sensitivity: f64,
    mode: Option<&'static str>,
    preroll: VecDeque<[u8; 960]>,
    hangover: usize,
    utterance_bytes: usize,
    open_text: Vec<String>,
    wake_audio: Vec<u8>,
    stream_bytes: usize,
    wake_start: usize,
    pending: Option<Recognition>,
    command_audio: Vec<u8>,
    /// What this session has heard so far without making a command: "open",
    /// when a pause came before "spotify". The next part is tried joined to it.
    held_audio: Vec<u8>,
    held_heard: String,
    /// Word sequences that are the start of a longer command, like "open steam"
    /// beside "open steam support center". These must wait for the end of speech.
    incomplete: HashSet<String>,
    /// The candidate Whisper already looked at during this utterance.
    early_tried: Option<String>,
    speech_frames: usize,
    quiet_run: usize,
    pub unsupported: Vec<String>,
    pub unmatched: Option<String>,
    pub name: String,
}

type Early<R> = std::thread::JoinHandle<std::result::Result<R, String>>;

/// Starts `work` on a short-lived jarvis-denoise thread with its own handle
/// to the model and a copy of the audio. Dropping the returned handle detaches
/// the thread: it finishes on its own and then drops the model handle, so an
/// idle release still frees the weights. None if no thread could start.
fn early<T, R>(
    model: Arc<T>,
    audio: &[u8],
    work: fn(&T, &[u8]) -> Result<R>,
) -> Option<Early<R>>
where
    T: Send + Sync + 'static,
    R: Send + 'static,
{
    let audio = audio.to_vec();
    std::thread::Builder::new()
        .name("jarvis-denoise".into())
        .spawn(move || work(&model, &audio).map_err(|e| e.to_string()))
        .ok()
}

/// The early result; a panic in the thread becomes an error, not an unwind.
fn finish<R>(handle: Early<R>) -> Result<R> {
    Ok(handle
        .join()
        .map_err(|_| "Denoiser stopped unexpectedly")??)
}

fn token(word: &str) -> bool {
    word.split(['\'', '-'])
        .all(|p| !p.is_empty() && p.bytes().all(|b| b.is_ascii_lowercase()))
}

fn supported(model: &Model, phrases: &[String]) -> Vec<String> {
    phrases
        .iter()
        .map(|s| phrase(s))
        .filter(|s| !s.is_empty() && s.split_whitespace().all(|w| token(w) && model.knows(w)))
        .collect::<BTreeSet<_>>()
        .into_iter()
        .collect()
}

/// Quiet that lets a complete command answer early: 4 frames of 30 ms.
const EARLY_QUIET_FRAMES: usize = 4;

/// Every proper word-prefix of these phrases.
fn word_prefixes<'a>(phrases: impl Iterator<Item = &'a String>) -> HashSet<String> {
    let mut prefixes = HashSet::new();
    for phrase in phrases {
        let mut end = 0;
        for word in phrase.split(' ') {
            end += word.len();
            if end < phrase.len() {
                prefixes.insert(phrase[..end].to_owned());
            }
            end += 1;
        }
    }
    prefixes
}

/// Mistakes here cost work or end the program, so these always get the full check.
fn may_answer_early(intent: &str) -> bool {
    !intent.starts_with("close_app") && intent != "quit"
}

/// The most audio kept for joining: the 10 seconds an utterance may last.
const HELD_AUDIO: usize = 320000;
/// Silence that ends speech: 20 frames of 30 ms.
const END_OF_SPEECH_FRAMES: usize = 20;
/// How much slower Whisper hears a command it couldn't follow, in turn.
const SLOWER: [f64; 2] = [1.5, 2.0];
/// Seconds of the wake name kept before a command said in the same breath.
/// Measured on spoken commands up to 2.7x normal speed: 0.08 understood the most
/// (0.16 left too much of the name in, nothing lost the command's first sound).
const TAIL_MARGIN: f64 = 0.08;

/// Older audio first, keeping only the newest `HELD_AUDIO` bytes.
fn joined_audio(held: &[u8], latest: &[u8]) -> Vec<u8> {
    let mut joined = Vec::with_capacity(held.len() + latest.len());
    joined.extend_from_slice(held);
    joined.extend_from_slice(latest);
    if joined.len() > HELD_AUDIO {
        // Whole samples only: an odd cut would turn the rest into noise.
        let cut = joined.len() - HELD_AUDIO;
        joined.drain(..cut + cut % 2);
    }
    joined
}

fn joined_text(held: &str, latest: &str) -> String {
    match (held.is_empty(), latest.is_empty()) {
        (true, _) => latest.to_owned(),
        (_, true) => held.to_owned(),
        _ => format!("{held} {latest}"),
    }
}

/// Whisper describes silence and noise in brackets, as "[ silence ]" or
/// "(wind blowing)". Neither is something the person said.
fn spoken(transcript: String) -> String {
    if transcript.starts_with(['[', '(']) {
        String::new()
    } else {
        transcript
    }
}

#[cfg(test)]
mod held_speech_tests {
    use super::*;

    #[test]
    fn parts_of_a_command_join_in_the_order_they_were_said() {
        assert_eq!(joined_audio(&[1, 2], &[3, 4]), vec![1, 2, 3, 4]);
        assert_eq!(joined_text("open", "spotify"), "open spotify");
        assert_eq!(joined_text("", "spotify"), "spotify");
        assert_eq!(joined_text("open", ""), "open");
    }

    #[test]
    fn held_audio_is_bounded_and_keeps_whole_samples() {
        let held = vec![1_u8; HELD_AUDIO - 2];
        let joined = joined_audio(&held, &[2, 2, 2, 2]);
        assert_eq!(joined.len(), HELD_AUDIO);
        assert_eq!(joined.len() % 2, 0);
        assert_eq!(joined.last(), Some(&2));
    }

    #[test]
    fn a_command_that_can_continue_is_marked_incomplete() {
        let phrases = ["open steam".to_string(), "open steam support center".to_string()];
        let prefixes = word_prefixes(phrases.iter());
        assert!(prefixes.contains("open"));
        assert!(prefixes.contains("open steam"));
        assert!(prefixes.contains("open steam support"));
        assert!(!prefixes.contains("open steam support center"));
    }

    #[test]
    fn closing_and_quitting_never_answer_early() {
        assert!(may_answer_early("open_app:spotify"));
        assert!(may_answer_early("volume_up"));
        assert!(!may_answer_early("close_app:spotify"));
        assert!(!may_answer_early("quit"));
    }

    #[test]
    fn descriptions_of_silence_are_not_speech() {
        assert_eq!(spoken("[ silence ]".into()), "");
        assert_eq!(spoken("(wind blowing)".into()), "");
        assert_eq!(spoken("open spotify".into()), "open spotify");
    }
}

fn text(result: &Value) -> String {
    result["text"]
        .as_str()
        .unwrap_or("")
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
}
fn confidence(result: &Value) -> Option<f64> {
    result["result"].as_array().and_then(|words| {
        words
            .iter()
            .filter_map(|w| w["conf"].as_f64())
            .min_by(f64::total_cmp)
    })
}

/// The fast path requires a posterior for every recognized word. Missing or
/// malformed word data must never turn a partial confidence into approval.
fn complete_confidence(result: &Value) -> Option<f64> {
    let words = result["result"].as_array().filter(|w| !w.is_empty())?;
    let mut spoken = Vec::with_capacity(words.len());
    let mut minimum: f64 = 1.0;
    for word in words {
        let name = word["word"]
            .as_str()
            .filter(|s| !s.is_empty() && !s.contains(char::is_whitespace))?;
        let confidence = word["conf"]
            .as_f64()
            .filter(|c| c.is_finite() && (0.0..=1.0).contains(c))?;
        spoken.push(name);
        minimum = minimum.min(confidence);
    }
    (spoken.join(" ") == text(result)).then_some(minimum)
}

/// The 100 ms before `cut` (where Vosk heard the wake name end), for the
/// Arabic pass only: English Vosk often stretches “jarvis” over the first
/// syllable of an Arabic word said in the same breath, and without it the
/// Arabic model hears اسمعني as “معني” or “ما عليه”.
fn arabic_lead(audio: &[u8], cut: usize) -> &[u8] {
    let end = (cut - cut % 2).min(audio.len());
    &audio[end.saturating_sub(3200)..end]
}

impl Engine {
    /// Only the explicit attribution probe bypasses normal gating/matching.
    pub(crate) fn history_decoder(&self, name: &str) -> Result<&Decoder> {
        match name {
            "wake" => Ok(&self.wake),
            "command" => Ok(&self.command),
            "free" => self
                .open
                .as_ref()
                .ok_or_else(|| "Free decoder is disabled".into()),
            _ => Err("Decoder must be wake, command or free".into()),
        }
    }
    pub fn new(native: &NativeConfig, grammar: &Grammar, config: &Value) -> Result<Self> {
        // Without an idle limit, compile Whisper before loading resident Vosk
        // state, so its one-time model-import scratch does not overlap Vosk's
        // steady allocation. With one, Whisper loads on a background thread
        // once a wake name is heard and is released after going unused.
        let whisper = if let Some(files) = &native.whisper {
            let (files, denoiser) = (files.clone(), native.denoiser.clone());
            let threads = native.threads as usize;
            let load: Loader<Transcriber> = Arc::new(move || {
                Transcriber::load(&files, denoiser.as_deref(), threads).map_err(|e| e.to_string())
            });
            let idle = native.whisper_idle_seconds.map(Duration::from_secs_f64);
            Some(OnDemand::new(load, idle)?)
        } else {
            None
        };
        let arabic = if let Some(directory) = &native.arabic {
            let directory = directory.clone();
            let threads = native.threads as usize;
            let load: Loader<Arabic> =
                Arc::new(move || Arabic::load(&directory, threads).map_err(|e| e.to_string()));
            let idle = native.whisper_idle_seconds.map(Duration::from_secs_f64);
            Some(OnDemand::new(load, idle)?)
        } else {
            None
        };
        let dictation_language = match native.dictation_language.as_deref() {
            Some("ar") if arabic.is_some() => "ar",
            Some("ar") => return Err("Arabic dictation needs the Arabic model".into()),
            _ => "en",
        };
        let model = Model::load(&native.vosk_library, &native.model_path)?;
        let known = supported(&model, &grammar.phrases);
        let apps = supported(&model, &grammar.apps);
        let mut verbs = supported(&model, &grammar.verbs);
        let prefixes = supported(&model, &grammar.prefixes);
        if known.is_empty() && apps.is_empty() {
            return Err("No command phrases use words the Vosk model knows".into());
        }
        let mut unsupported = grammar.overflow.clone();
        for p in grammar
            .phrases
            .iter()
            .map(|s| phrase(s))
            .collect::<BTreeSet<_>>()
        {
            if known.binary_search(&p).is_err() {
                unsupported.push(p);
            }
        }
        for a in grammar
            .apps
            .iter()
            .map(|s| phrase(s))
            .collect::<BTreeSet<_>>()
        {
            if apps.binary_search(&a).is_err() {
                unsupported.push(format!("open {a}"));
            }
        }
        let mut sounds = HashMap::new();
        for name in grammar
            .apps
            .iter()
            .map(|s| phrase(s))
            .collect::<BTreeSet<_>>()
        {
            if apps.binary_search(&name).is_ok() || !name.split_whitespace().all(token) {
                continue;
            }
            let mut parts = Vec::new();
            for word in name.split_whitespace() {
                if model.knows(word) {
                    parts.push(word.to_owned());
                    continue;
                }
                let split = (3..=word.len().saturating_sub(2)).rev().find(|&cut| {
                    word.len() - cut >= if cut >= 5 { 2 } else { 3 }
                        && model.knows(&word[..cut])
                        && model.knows(&word[cut..])
                });
                if let Some(cut) = split {
                    parts.push(format!("{} {}", &word[..cut], &word[cut..]));
                } else {
                    parts.clear();
                    break;
                }
            }
            if !parts.is_empty() {
                let spoken = parts.join(" ");
                if spoken != name && apps.binary_search(&spoken).is_err() {
                    sounds.entry(spoken).or_insert(name);
                }
            }
        }
        unsupported.retain(|p| {
            !sounds
                .values()
                .any(|name| p.strip_prefix("open ").unwrap_or(p) == name)
        });
        if !native.candidates.is_empty() {
            let reachable: HashSet<_> = grammar
                .apps
                .iter()
                .map(|a| format!("open {}", phrase(a)))
                .collect();
            unsupported.retain(|p| !reachable.contains(p));
        }
        let mut spoken_apps = apps;
        spoken_apps.extend(sounds.keys().cloned());
        let mut full: BTreeSet<String> = known.iter().cloned().collect();
        for v in &verbs {
            for a in &spoken_apps {
                let original = sounds.get(a).unwrap_or(a);
                if grammar.blocked_apps.contains(original)
                    && !["focus", "switch to"].contains(&v.as_str())
                    && !grammar.close_verbs.contains(v)
                {
                    continue;
                }
                full.insert(format!("{v} {a}"));
                full.insert(format!("{v} the {a}"));
            }
        }
        for (p, alias) in &grammar.launches {
            if let Some(alias) = alias.as_str() {
                if let Some(prefix) = p.strip_suffix(alias) {
                    let article = format!("{prefix}the {alias}");
                    if article
                        .split_whitespace()
                        .all(|w| token(w) && model.knows(w))
                    {
                        full.insert(article);
                    }
                }
                for (spoken, original) in &sounds {
                    if original == alias && p.ends_with(alias) {
                        full.insert(format!("{}{spoken}", &p[..p.len() - alias.len()]));
                        full.insert(format!("{}the {spoken}", &p[..p.len() - alias.len()]));
                    }
                }
            }
        }
        for p in &prefixes {
            for k in &known {
                full.insert(format!("{p} {k}"));
            }
        }
        let full: Vec<_> = full.into_iter().collect();
        let incomplete = word_prefixes(full.iter());
        let wake_forms = supported(&model, &native.wake_forms);
        if wake_forms.is_empty() {
            return Err(
                "Wake phrase uses words the Vosk model doesn't know; choose another".into(),
            );
        }
        let wake_phrase = phrase(
            config["activation"]["wake_phrase"]
                .as_str()
                .ok_or("Missing wake phrase")?,
        );
        let mut wake_names: Vec<Vec<String>> = wake_forms
            .iter()
            .filter_map(|f| {
                let mut words: Vec<String> = f.split_whitespace().map(str::to_owned).collect();
                if words.len() > 1 && native.greetings.contains(&words[0]) {
                    words.remove(0);
                }
                (!words.is_empty()).then_some(words)
            })
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        wake_names.sort_by_key(|n| std::cmp::Reverse(n.len()));
        let mut wake_grammar = wake_forms.clone();
        for name in &wake_names {
            wake_grammar.extend(full.iter().map(|c| format!("{} {c}", name.join(" "))));
        }
        let wake = Decoder::new(model.clone(), Some(wake_grammar), true)?;
        let command = Decoder::new(model.clone(), Some(full), true)?;
        let open = if config["recognition"]["closest_match"].as_bool() == Some(true) {
            Some(Decoder::new(model, None, false)?)
        } else {
            None
        };
        let mut matcher_apps: HashSet<_> = spoken_apps
            .into_iter()
            .chain(sounds.values().cloned())
            .collect();
        if verbs.is_empty() {
            matcher_apps.clear();
        }
        verbs.sort_by_key(|v| std::cmp::Reverse(v.len()));
        let matcher = Matcher {
            phrases: known.into_iter().collect(),
            apps: matcher_apps,
            verbs,
            prefixes: native.prefixes.clone(),
            suffixes: native.suffixes.clone(),
            candidates: native.candidates.clone(),
            exact_only: config["recognition"]["closest_match"].as_bool() == Some(false),
            rules: jarvis_phrases::Rules {
                launches: grammar.launches.clone(),
            },
            blocked_apps: grammar.blocked_apps.clone(),
            close_verbs: grammar.close_verbs.clone(),
        };
        Ok(Self {
            wake,
            command,
            open,
            whisper,
            arabic,
            dictation_language,
            vosk_fast_path: native.vosk_fast_path,
            finalize_utterances: native.vosk_finalize_utterances,
            fast_commands: 0,
            whisper_commands: 0,
            matcher,
            sounds,
            wake_names,
            wake_phrase,
            sensitivity: config["activation"]["sensitivity"]
                .as_f64()
                .ok_or("Missing sensitivity")?,
            mode: None,
            preroll: VecDeque::new(),
            hangover: 0,
            utterance_bytes: 0,
            open_text: vec![],
            wake_audio: vec![],
            stream_bytes: 0,
            wake_start: 0,
            pending: None,
            command_audio: vec![],
            held_audio: vec![],
            held_heard: String::new(),
            incomplete,
            early_tried: None,
            speech_frames: 0,
            quiet_run: 0,
            unsupported,
            unmatched: None,
            name: match &native.whisper {
                // tiny.en-encoder.int8.onnx -> vosk+whisper-tiny(+moonshine-ar)
                Some(files) => format!(
                    "vosk+whisper-{}{}",
                    std::path::Path::new(&files.encoder)
                        .file_name()
                        .and_then(|n| n.to_str())
                        .and_then(|n| n.split_once(".en-"))
                        .map_or("small", |(size, _)| size),
                    if native.arabic.is_some() { "+moonshine-ar" } else { "" }
                ),
                None => "vosk".into(),
            },
        })
    }
    fn canonical(&self, text: &str) -> String {
        if let Some(p) = self.matcher.rules.canonical(text) {
            return p;
        }
        for (p, alias) in &self.matcher.rules.launches {
            if let Some(alias) = alias.as_str() {
                for (spoken, original) in &self.sounds {
                    if original == alias
                        && p.ends_with(alias)
                        && [
                            format!("{}{spoken}", &p[..p.len() - alias.len()]),
                            format!("{}the {spoken}", &p[..p.len() - alias.len()]),
                        ]
                        .contains(&text.to_string())
                    {
                        return p.clone();
                    }
                }
            }
        }
        for verb in &self.matcher.verbs {
            if let Some(name) = text.strip_prefix(&format!("{verb} ")) {
                let name = name.strip_prefix("the ").unwrap_or(name);
                return format!(
                    "{verb} {}",
                    self.sounds.get(name).map_or(name, String::as_str)
                );
            }
        }
        text.to_owned()
    }
    fn reset(&mut self) -> Result<()> {
        self.wake.clear(self.finalize_utterances)?;
        self.command.clear(self.finalize_utterances)?;
        if let Some(open) = &self.open {
            open.clear(self.finalize_utterances)?;
        }
        self.preroll.clear();
        self.hangover = 0;
        self.utterance_bytes = 0;
        self.open_text.clear();
        self.wake_audio.clear();
        self.wake_audio.shrink_to(32000);
        self.wake_start = self.stream_bytes;
        self.command_audio.clear();
        self.command_audio.shrink_to(32000);
        self.held_audio.clear();
        self.held_heard.clear();
        self.early_tried = None;
        self.speech_frames = 0;
        self.quiet_run = 0;
        Ok(())
    }
    /// Starts listening for the command after a wake name, keeping any part of
    /// it already spoken in the same breath as the name.
    pub fn begin_after_wake(&mut self) -> Result<()> {
        let (audio, heard) = (std::mem::take(&mut self.held_audio), std::mem::take(&mut self.held_heard));
        self.begin(true)?;
        self.held_audio = audio;
        self.held_heard = heard;
        Ok(())
    }
    pub fn begin(&mut self, command: bool) -> Result<()> {
        self.reset()?;
        self.pending = None;
        self.unmatched = None;
        self.mode = Some(if command { "command" } else { "wake" });
        if command {
            self.prepare_whisper();
        }
        Ok(())
    }
    /// A transcriber for dictation, which runs on its own thread: the Arabic
    /// model for Arabic dictation, otherwise Whisper. `language` ("ar"/"en")
    /// comes from the phrase that started it; None uses the configured one.
    /// Wake and command decoding stay idle until `begin` is called again.
    pub fn dictation_model(&mut self, language: Option<&str>) -> Result<crate::dictation::Transcribe> {
        let missing = "Dictation needs the speech model. Turn on Accurate recognition in Voice settings.";
        let model: crate::dictation::Transcribe = if language.unwrap_or(self.dictation_language) == "ar" {
            let arabic = self
                .arabic
                .as_mut()
                .ok_or("Arabic dictation needs Arabic turned on (Voice → Language)")?
                .shared()?;
            Arc::new(move |pcm: &[u8]| {
                Ok(Text {
                    text: arabic.transcribe(pcm)?,
                    language: "ar",
                })
            })
        } else {
            let whisper = self.whisper.as_mut().ok_or(missing)?.shared()?;
            Arc::new(move |pcm: &[u8]| whisper.dictate(pcm))
        };
        self.reset()?;
        self.pending = None;
        self.mode = Some("dictation");
        Ok(model)
    }
    pub fn after_wake(&mut self) -> Option<Recognition> {
        self.pending.take()
    }
    fn prepare_whisper(&mut self) {
        // Prefetching would load weights even when a later exact Vosk result
        // avoids transcription. Experimental fallbacks load at decision time.
        if self.vosk_fast_path {
            return;
        }
        if let Some(arabic) = &mut self.arabic {
            arabic.prepare();
        }
        if let Some(whisper) = &mut self.whisper {
            whisper.prepare();
        }
    }
    pub fn recognition_stats(&self) -> Value {
        serde_json::json!({"vosk_fast_path":self.vosk_fast_path,"vosk_finalize_utterances":self.finalize_utterances,"fast_commands":self.fast_commands,
            "whisper_transcriptions":self.whisper_commands,
            "whisper_loads":self.whisper.as_ref().map_or(0, OnDemand::load_count)})
    }
    /// Collects a background Whisper load, releases Whisper once it has gone
    /// unused while waiting for the wake phrase, and keeps trimming the heap
    /// while it stays released.
    pub fn maintain(&mut self) -> Result<()> {
        let waiting = self.mode == Some("wake") && self.pending.is_none();
        let mut released = false;
        if let Some(whisper) = &mut self.whisper {
            released |= whisper.maintain(!waiting)?;
        }
        if let Some(arabic) = &mut self.arabic {
            released |= arabic.maintain(!waiting)?;
        }
        if released {
            trim();
            release_unused();
        }
        Ok(())
    }
    /// Position and length of a wake name within the first three words.
    fn wake_at(&self, spoken: &[&str]) -> Option<(usize, usize)> {
        (0..3).find_map(|i| {
            self.wake_names.iter().find_map(|name| {
                spoken
                    .get(i..i + name.len())
                    .filter(|s| s.iter().copied().eq(name.iter().map(String::as_str)))
                    .map(|_| (i, name.len()))
            })
        })
    }
    pub fn feed(&mut self, pcm: &[u8], quiet: bool) -> Result<Option<Recognition>> {
        if pcm.len() != 960 {
            return Err("Expected exactly 960 PCM bytes".into());
        }
        if self.mode.is_none() {
            return Ok(None);
        }
        if quiet && self.hangover == 0 {
            if self.preroll.len() == 10 {
                self.preroll.pop_front();
            }
            self.preroll
                .push_back(pcm.try_into().expect("frame size checked"));
            return Ok(None);
        }
        if !quiet {
            let preroll = self.hangover == 0;
            self.hangover = 40;
            if preroll {
                while let Some(frame) = self.preroll.pop_front() {
                    if let Some(result) = self.decode(&frame)? {
                        return Ok(Some(result));
                    }
                }
            }
        } else {
            self.hangover -= 1;
        }
        if let Some(result) = self.decode(pcm)? {
            return Ok(Some(result));
        }
        if self.mode == Some("wake") {
            if quiet {
                self.quiet_run += 1;
            } else {
                self.quiet_run = 0;
            }
            if self.time_to_try_early() {
                if let Some(result) = self.early_command()? {
                    return Ok(Some(result));
                }
            }
            if self.quiet_run == END_OF_SPEECH_FRAMES {
                if let Some(result) = self.finish_wake()? {
                    return Ok(Some(result));
                }
            }
        }
        if quiet && self.hangover == 0 && self.mode == Some("wake") {
            // The energy gate is about to stop forwarding quiet frames. Vosk
            // can still have an unfinished [unk] utterance here; retaining it
            // would attach the next wake name to that background speech.
            // Discard it instead of forcing a final hypothesis into a wake.
            self.reset()?;
        }
        if self.mode == Some("command") && self.whisper.is_some() {
            if quiet {
                self.quiet_run += 1;
            } else {
                self.speech_frames += 1;
                self.quiet_run = 0;
            }
            if self.speech_frames >= 8 && self.time_to_try_early() {
                if let Some(result) = self.early_command()? {
                    return Ok(Some(result));
                }
            }
            if self.speech_frames >= 8 && self.quiet_run >= END_OF_SPEECH_FRAMES {
                return self.flush();
            }
        }
        Ok(None)
    }
    fn time_to_try_early(&self) -> bool {
        self.quiet_run >= EARLY_QUIET_FRAMES
            && self.quiet_run < END_OF_SPEECH_FRAMES
            && (self.quiet_run - EARLY_QUIET_FRAMES).is_multiple_of(2)
    }
    /// The complete command Vosk hears so far, with its intent.
    fn early_candidate(&self) -> Result<Option<(String, String)>> {
        let waking = self.mode == Some("wake");
        let partial = if waking { self.wake.partial()? } else { self.command.partial()? };
        let words: Vec<_> = partial["partial"].as_str().unwrap_or("").split_whitespace().collect();
        let rest = if waking {
            let Some((position, size)) = self.wake_at(&words) else {
                return Ok(None);
            };
            &words[position + size..]
        } else {
            &words[..]
        };
        if rest.is_empty() || rest.contains(&"[unk]") {
            return Ok(None);
        }
        let raw = rest.join(" ");
        if self.incomplete.contains(&raw) {
            return Ok(None);
        }
        let text = self.canonical(&raw);
        if !self.matcher.accepts(&text) {
            return Ok(None);
        }
        let plain = wording(&text, &self.matcher.prefixes, &self.matcher.suffixes);
        let Some(intent) = self.matcher.intent(&plain).filter(|i| may_answer_early(i)) else {
            return Ok(None);
        };
        let intent = intent.to_owned();
        Ok(Some((text, intent)))
    }
    /// Speech has just paused. When Vosk's grammar and Whisper's free hearing
    /// independently name the same complete command, there is nothing to wait
    /// for. Anything less falls through to the usual end of speech.
    fn early_command(&mut self) -> Result<Option<Recognition>> {
        if self.whisper.is_none() {
            return Ok(None);
        }
        let Some((text, intent)) = self.early_candidate()? else {
            return Ok(None);
        };
        if self.early_tried.as_ref() == Some(&text) {
            return Ok(None);
        }
        self.early_tried = Some(text.clone());
        let waking = self.mode == Some("wake");
        let audio = if waking {
            self.wake_audio.clone()
        } else {
            joined_audio(&self.held_audio, &self.command_audio)
        };
        let whisper = self.whisper.as_mut().expect("checked above");
        let heard = spoken(phrase(&whisper.get()?.transcribe(&audio)?));
        let found = self.matcher.closest(&heard);
        let agreed = found.as_ref().is_some_and(|(p, _)| self.matcher.intent(p) == Some(intent.as_str()));
        crate::trace(serde_json::json!({"step":"early check","vosk":text,"whisper":heard,"agreed":agreed,"quiet_frames":self.quiet_run}));
        if !agreed {
            return Ok(None);
        }
        self.whisper_commands += 1;
        let mut command = Recognition::new("command", text, found.map(|(_, score)| score));
        command.recognition_pass = Some("early");
        self.mode = None;
        if waking {
            self.reset()?;
            self.pending = Some(command);
            self.mode = None;
            return Ok(Some(Recognition::new("wake", self.wake_phrase.clone(), None)));
        }
        Ok(Some(command))
    }
    /// The speaker stopped after a wake name. Vosk would wait up to a second to
    /// be sure; this is the same pause that ends a command, so finish now.
    fn finish_wake(&mut self) -> Result<Option<Recognition>> {
        let partial = self.wake.partial()?;
        let words: Vec<_> = partial["partial"]
            .as_str()
            .unwrap_or("")
            .split_whitespace()
            .collect();
        if self.wake_at(&words).is_none() {
            return Ok(None);
        }
        self.utterance_bytes = 0;
        let audio = std::mem::take(&mut self.wake_audio);
        let start = self.wake_start;
        self.wake_start = self.stream_bytes;
        let result = self.wake.result(true)?;
        let recognized = self.wake_result(result, &audio, start)?;
        if recognized.is_none() {
            // Too unsure to be a wake name: its words must not start the next one.
            self.reset()?;
        }
        Ok(recognized)
    }
    pub fn flush(&mut self) -> Result<Option<Recognition>> {
        if self.mode != Some("command") || self.command_audio.is_empty() {
            return Ok(None);
        }
        let result = self.command.result(true)?;
        self.command_result(result, &[])
    }
    fn decode(&mut self, pcm: &[u8]) -> Result<Option<Recognition>> {
        self.utterance_bytes += pcm.len();
        if self.utterance_bytes > 320000 {
            self.reset()?;
            return Ok(None);
        }
        if self.mode == Some("wake") {
            self.wake_audio.extend_from_slice(pcm);
            self.stream_bytes += pcm.len();
            if self.wake.accept(pcm)? {
                self.utterance_bytes = 0;
                let audio = std::mem::take(&mut self.wake_audio);
                let start = self.wake_start;
                self.wake_start = self.stream_bytes;
                let result = self.wake.result(self.finalize_utterances)?;
                return self.wake_result(result, &audio, start);
            }
            // Every fourth speech frame, start loading Whisper once the
            // hypothesis so far has a wake name, so it is ready (or nearly)
            // when a command spoken in the same breath ends.
            if self.whisper.is_some() && self.utterance_bytes.is_multiple_of(3840) {
                let partial = self.wake.partial()?;
                let words: Vec<_> = partial["partial"]
                    .as_str()
                    .unwrap_or("")
                    .split_whitespace()
                    .collect();
                if self.wake_at(&words).is_some() {
                    self.prepare_whisper();
                }
            }
            return Ok(None);
        }
        self.command_audio.extend_from_slice(pcm);
        if let Some(open) = &self.open {
            if open.accept(pcm)? {
                self.open_text
                    .push(text(&open.result(self.finalize_utterances)?));
            }
        }
        if self.command.accept(pcm)? {
            self.utterance_bytes = 0;
            return self.command_result(self.command.result(self.finalize_utterances)?, &[]);
        }
        Ok(None)
    }
    fn wake_result(
        &mut self,
        result: Value,
        audio: &[u8],
        start: usize,
    ) -> Result<Option<Recognition>> {
        let words: Vec<_> = result["result"]
            .as_array()
            .map(|w| {
                w.iter()
                    .filter(|w| w["word"].as_str().is_some_and(|s| !s.is_empty()))
                    .collect()
            })
            .unwrap_or_default();
        let spoken: Vec<_> = words.iter().map(|w| w["word"].as_str().unwrap()).collect();
        crate::trace(serde_json::json!({"step":"wake decoder","result":&result,"audio_seconds":audio.len() as f64 / 32000.0}));
        let Some((position, size)) = self.wake_at(&spoken) else {
            crate::trace(serde_json::json!({"step":"no wake name","spoken":spoken}));
            return Ok(None);
        };
        let name = &words[position..position + size];
        if name
            .iter()
            .filter_map(|w| w["conf"].as_f64())
            .any(|c| c < 0.8 * (1.0 - self.sensitivity))
        {
            crate::trace(serde_json::json!({"step":"wake name too unsure","floor":0.8 * (1.0 - self.sensitivity)}));
            return Ok(None);
        }
        // The command follows in this utterance or after a pause.
        self.prepare_whisper();
        let rest = &words[position + size..];
        self.pending = None;
        crate::trace(serde_json::json!({"step":"woke","words_after_name":rest.len()}));
        if !rest.is_empty() {
            let text = self.canonical(
                &rest
                    .iter()
                    .map(|w| w["word"].as_str().unwrap())
                    .collect::<Vec<_>>()
                    .join(" "),
            );
            let conf = rest
                .iter()
                .filter_map(|w| w["conf"].as_f64())
                .min_by(f64::total_cmp);
            // Vosk's end of the name runs late on fast speech and took the start of
            // the command with it ("clip that" reached Whisper as "with that").
            let end = (name.last().unwrap()["end"].as_f64().unwrap_or(0.0) - TAIL_MARGIN).max(0.0);
            let cut = ((end * 16000.0) as usize * 2).saturating_sub(start);
            let tail = &audio[(cut - cut % 2).min(audio.len())..];
            let lead = arabic_lead(audio, cut);
            crate::trace(serde_json::json!({"step":"command in the same breath","text":text,"confidence":conf,
                "tail_seconds":tail.len() as f64 / 32000.0,"accepted":!text.contains("[unk]") && self.matcher.accepts(&text)}));
            if !text.contains("[unk]")
                && self.matcher.accepts(&text)
                && conf.is_none_or(|c| c >= 0.6)
            {
                self.pending = self.command_from_audio(
                    tail,
                    Some(serde_json::json!({"text":text,"result":rest})),
                    lead,
                )?;
            } else if !audio.is_empty() {
                self.pending = self.command_from_audio(tail, None, lead)?;
            }
        }
        // Part of a command said in this breath is finished after the pause.
        let (held_audio, held_heard) = (std::mem::take(&mut self.held_audio), std::mem::take(&mut self.held_heard));
        self.reset()?;
        self.held_audio = held_audio;
        self.held_heard = held_heard;
        self.mode = None;
        Ok(Some(Recognition::new(
            "wake",
            self.wake_phrase.clone(),
            None,
        )))
    }
    /// `lead` is audio from just before `audio` that only the Arabic pass hears.
    fn command_from_audio(
        &mut self,
        audio: &[u8],
        strict: Option<Value>,
        lead: &[u8],
    ) -> Result<Option<Recognition>> {
        if strict.is_none() && audio.len() < 6400 {
            crate::trace(serde_json::json!({"step":"too little audio after the name","seconds":audio.len() as f64 / 32000.0}));
            return Ok(None);
        }
        self.command.clear(self.finalize_utterances)?;
        self.open_text.clear();
        self.command_audio = audio.to_vec();
        if let Some(open) = &self.open {
            open.clear(self.finalize_utterances)?;
            open.accept(audio)?;
        }
        let strict = if let Some(s) = strict {
            s
        } else {
            self.command.accept(audio)?;
            self.command.result(true)?
        };
        self.command_result(strict, lead)
    }
    fn command_result(&mut self, result: Value, lead: &[u8]) -> Result<Option<Recognition>> {
        let canonical = self.canonical(&text(&result));
        if let Some(open) = &self.open {
            self.open_text.push(text(&open.result(true)?));
            open.reset();
        }
        let mut heard = self
            .open_text
            .iter()
            .filter(|t| !t.is_empty())
            .cloned()
            .collect::<Vec<_>>()
            .join(" ");
        self.open_text.clear();
        let latest = std::mem::take(&mut self.command_audio);
        let latest_heard = std::mem::take(&mut heard);
        let earlier = !self.held_heard.is_empty() || !self.held_audio.is_empty();
        let audio = joined_audio(&self.held_audio, &latest);
        heard = joined_text(&self.held_heard, &latest_heard);
        self.speech_frames = 0;
        self.quiet_run = 0;
        let mut heard_match = self.matcher.closest(&heard);
        if heard_match.is_none() && earlier {
            // Something unrelated, then a whole command: the command still counts.
            heard_match = self.matcher.closest(&latest_heard);
        }
        if self.vosk_fast_path
            && self.whisper.is_some()
            && self
                .matcher
                .confident_exact(&canonical, complete_confidence(&result), &heard)
        {
            // Still use the existing chooser: no free-decoder disagreement,
            // acceptance or command-validation rule is bypassed here.
            if let Some(mut recognition) =
                self.matcher
                    .choose(&canonical, confidence(&result), &heard, heard_match.clone())
            {
                recognition.recognition_pass = Some("vosk_fast");
                self.fast_commands += 1;
                self.mode = None;
                return Ok(Some(recognition));
            }
        }
        // While Moonshine listens, denoise Whisper's input on another thread
        // when Whisper is already loaded: GTCRN takes about as long as the
        // Arabic pass, and English commands need both. An Arabic match leaves
        // the thread to finish on its own; its result is then unused.
        let early = match (&self.whisper, &self.arabic) {
            (Some(whisper), Some(_)) if !audio.is_empty() => whisper
                .ready()
                .and_then(|model| early(model, &audio, |m, a| m.command_input(a))),
            _ => None,
        };
        // Arabic first: its transcript is accepted only when it is one of the
        // supported Arabic commands, so English speech (which this model
        // writes as Arabic-script sounds) falls through to Whisper unchanged.
        if let Some(arabic) = &mut self.arabic {
            if !audio.is_empty() {
                let heard_ar = phrase(&arabic.get()?.transcribe(&[lead, &audio[..]].concat())?);
                if jarvis_phrases::arabic::has_arabic(&heard_ar)
                    && jarvis_phrases::arabic::translate(
                        &heard_ar,
                        self.matcher.close_verbs.first().map_or("close", String::as_str),
                    )
                    .is_some()
                {
                    if let Some((matched, score)) = self.matcher.closest(&heard_ar) {
                        self.mode = None;
                        let mut result = Recognition::new("command", matched, Some(score));
                        result.heard = Some(heard_ar);
                        result.closest_match = true;
                        result.recognition_pass = Some("arabic");
                        return Ok(Some(result));
                    }
                }
            }
        }
        if let Some(whisper) = &mut self.whisper {
            if !audio.is_empty() {
                self.whisper_commands += 1;
                let model = whisper.get()?;
                let mut accurate = spoken(phrase(&match early {
                    Some(denoising) => model.transcribe_input(finish(denoising)?)?,
                    None => model.transcribe(&audio)?,
                }));
                let mut accurate_match = self.matcher.closest(&accurate);
                if accurate_match.is_none() && earlier && !latest.is_empty() {
                    let alone = spoken(phrase(&model.transcribe(&latest)?));
                    if let Some(found) = self.matcher.closest(&alone) {
                        accurate = alone;
                        accurate_match = Some(found);
                    }
                }
                // Fast talkers: Whisper tiny mishears speech much quicker than it
                // learned from. Only after it failed, so ordinary speech costs nothing.
                if accurate_match.is_none() && audio.len() >= 9600 {
                    for factor in SLOWER {
                        // Whisper takes at most ten seconds; a long stretch of talk is
                        // not a quick command anyway. A failed retry never stops listening.
                        if audio.len() as f64 * factor > 320000.0 {
                            break;
                        }
                        let Ok(text) = model.transcribe(&crate::stretch::slower(&audio, factor)) else {
                            break;
                        };
                        let slowed = spoken(phrase(&text));
                        let found = self.matcher.closest(&slowed);
                        crate::trace(serde_json::json!({"step":"whisper slowed","factor":factor,"text":slowed,"match":found}));
                        if let Some(found) = found {
                            accurate = slowed;
                            accurate_match = Some(found);
                            break;
                        }
                    }
                }
                crate::trace(serde_json::json!({"step":"whisper","text":accurate,"match":accurate_match,
                    "seconds":audio.len() as f64 / 32000.0,"strict":canonical,"free":heard}));
                if accurate_match.is_some() || heard_match.is_none() {
                    if !accurate.is_empty() {
                        heard = accurate;
                    }
                    heard_match = accurate_match;
                }
            }
        }
        if let Some(mut recognition) =
            self.matcher
                .choose(&canonical, confidence(&result), &heard, heard_match)
        {
            if self.vosk_fast_path {
                recognition.recognition_pass =
                    Some(if self.whisper.is_some() && !audio.is_empty() {
                        "whisper"
                    } else {
                        "vosk"
                    });
            }
            crate::trace(serde_json::json!({"step":"command","text":recognition.text,"confidence":recognition.confidence}));
            self.mode = None;
            return Ok(Some(recognition));
        }
        crate::trace(serde_json::json!({"step":"not a command yet","strict":canonical,"heard":heard}));
        let heard = if heard.is_empty() {
            canonical.replace("[unk]", "…").trim().to_owned()
        } else {
            heard
        };
        // Not a command yet. The rest may still be coming, so keep what was said.
        self.held_audio = audio;
        self.held_heard = heard.clone();
        self.unmatched = if heard.is_empty() { None } else { Some(heard) };
        Ok(None)
    }
}
