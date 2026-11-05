use crate::inference::{trim, Arabic, Decoder, Model, Text, Transcriber};
use crate::matching::{phrase, Matcher};
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
    speech_frames: usize,
    quiet_run: usize,
    pub unsupported: Vec<String>,
    pub unmatched: Option<String>,
    pub name: String,
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
            let load: Loader<Arabic> =
                Arc::new(move || Arabic::load(&directory).map_err(|e| e.to_string()));
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
        self.wake_start = self.stream_bytes;
        self.command_audio.clear();
        self.speech_frames = 0;
        self.quiet_run = 0;
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
    /// model for Arabic dictation, otherwise Whisper. Wake and command
    /// decoding stay idle until `begin` is called again.
    pub fn dictation_model(&mut self) -> Result<crate::dictation::Transcribe> {
        let missing = "Dictation needs the speech model. Turn on Accurate recognition in Voice settings.";
        let model: crate::dictation::Transcribe = if self.dictation_language == "ar" {
            let arabic = self.arabic.as_mut().ok_or(missing)?.shared()?;
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
        if let Some(whisper) = &mut self.whisper {
            whisper.prepare();
        }
        if let Some(arabic) = &mut self.arabic {
            arabic.prepare();
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
            if self.speech_frames >= 8 && self.quiet_run >= 20 {
                return self.flush();
            }
        }
        Ok(None)
    }
    pub fn flush(&mut self) -> Result<Option<Recognition>> {
        if self.mode != Some("command") || self.command_audio.is_empty() {
            return Ok(None);
        }
        let result = self.command.result(true)?;
        self.command_result(result)
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
            return self.command_result(self.command.result(self.finalize_utterances)?);
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
        let Some((position, size)) = self.wake_at(&spoken) else {
            return Ok(None);
        };
        let name = &words[position..position + size];
        if name
            .iter()
            .filter_map(|w| w["conf"].as_f64())
            .any(|c| c < 0.8 * (1.0 - self.sensitivity))
        {
            return Ok(None);
        }
        // The command follows in this utterance or after a pause.
        self.prepare_whisper();
        let rest = &words[position + size..];
        self.pending = None;
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
            let cut = ((name.last().unwrap()["end"].as_f64().unwrap_or(0.0) * 16000.0) as usize
                * 2)
            .saturating_sub(start);
            let tail = &audio[(cut - cut % 2).min(audio.len())..];
            if !text.contains("[unk]")
                && self.matcher.accepts(&text)
                && conf.is_none_or(|c| c >= 0.6)
            {
                self.pending = self.command_from_audio(
                    tail,
                    Some(serde_json::json!({"text":text,"result":rest})),
                )?;
            } else if !audio.is_empty() {
                self.pending = self.command_from_audio(tail, None)?;
            }
        }
        self.reset()?;
        self.mode = None;
        Ok(Some(Recognition::new(
            "wake",
            self.wake_phrase.clone(),
            None,
        )))
    }
    fn command_from_audio(
        &mut self,
        audio: &[u8],
        strict: Option<Value>,
    ) -> Result<Option<Recognition>> {
        if strict.is_none() && audio.len() < 6400 {
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
        self.command_result(strict)
    }
    fn command_result(&mut self, result: Value) -> Result<Option<Recognition>> {
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
        let audio = std::mem::take(&mut self.command_audio);
        self.speech_frames = 0;
        self.quiet_run = 0;
        let mut heard_match = self.matcher.closest(&heard);
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
        // Arabic first: its transcript is accepted only when it is one of the
        // supported Arabic commands, so English speech (which this model
        // writes as Arabic-script sounds) falls through to Whisper unchanged.
        if let Some(arabic) = &mut self.arabic {
            if !audio.is_empty() {
                let heard_ar = phrase(&arabic.get()?.transcribe(&audio)?);
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
                let accurate = phrase(&whisper.get()?.transcribe(&audio)?);
                let accurate_match = self.matcher.closest(&accurate);
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
            self.mode = None;
            return Ok(Some(recognition));
        }
        let heard = if heard.is_empty() {
            canonical.replace("[unk]", "…").trim().to_owned()
        } else {
            heard
        };
        self.unmatched = if heard.is_empty() { None } else { Some(heard) };
        Ok(None)
    }
}
