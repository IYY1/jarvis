use crate::{err, Result};
use serde_json::{json, Value};
use std::{collections::BTreeSet, sync::LazyLock};

pub static TABLES: LazyLock<Value> =
    LazyLock::new(|| serde_json::from_str(include_str!("../data/commands.json")).unwrap());
pub fn strings(value: &Value) -> Vec<String> {
    value
        .as_array()
        .map(|a| {
            a.iter()
                .filter_map(Value::as_str)
                .map(str::to_owned)
                .collect()
        })
        .unwrap_or_default()
}
pub fn normalize(text: &str) -> Result<String> {
    if text.chars().count() > 256 {
        return err("Phrase must be at most 256 characters");
    }
    Ok(jarvis_phrases::normalize(text))
}
fn strip_edges(text: &str) -> String {
    let mut result = text;
    for key in ["POLITE_PREFIXES", "POLITE_SUFFIXES"] {
        let mut forms = strings(&TABLES[key]);
        forms.sort_by_key(|s| std::cmp::Reverse(s.len()));
        for form in forms {
            let stripped = if key == "POLITE_PREFIXES" {
                result.strip_prefix(&format!("{form} "))
            } else {
                result.strip_suffix(&format!(" {form}"))
            };
            if let Some(s) = stripped {
                result = s;
                break;
            }
        }
    }
    result.to_owned()
}
pub fn spoken_numbers(text: &str) -> String {
    static DIGITS: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"\b([0-9]{1,2})\b").unwrap());
    DIGITS
        .replace_all(text, |c: &regex::Captures| {
            let n: usize = c[1].parse().unwrap();
            TABLES["NUMBER_WORDS"][n]
                .as_str()
                .unwrap_or(&c[1])
                .to_owned()
        })
        .into_owned()
}
#[derive(Clone, Debug)]
pub struct Parser {
    pub custom: Value,
    pub rules: jarvis_phrases::Rules,
    pub blocked_apps: BTreeSet<String>,
}
impl Parser {
    pub fn new(custom: &Value) -> Result<Self> {
        let mut output = serde_json::Map::new();
        for (k, v) in custom.as_object().ok_or("commands must be an object")? {
            let phrase = normalize(k)?;
            let target = normalize(v.as_str().ok_or("Command target must be text")?)?;
            if TABLES["FIXED"].get(&phrase).is_some() || TABLES["VARIANTS"].get(&phrase).is_some() {
                return err("Built-in phrases cannot be overridden; choose a new phrase");
            }
            if TABLES["FIXED"].get(&target).is_none() {
                return err(format!(
                    "Custom command target is not a built-in phrase: {target}"
                ));
            }
            output.insert(phrase, json!(target));
        }
        Ok(Self {
            custom: Value::Object(output),
            rules: jarvis_phrases::Rules::default(),
            blocked_apps: BTreeSet::new(),
        })
    }
    pub fn from_config(c: &Value) -> Result<Self> {
        let mut parser = Self::new(&c["commands"])?;
        let rules = jarvis_phrases::Rules::from_config(c).map_err(std::io::Error::other)?;
        for phrase in parser.custom.as_object().unwrap().keys() {
            if rules.parse_normalized(phrase).is_some() {
                return err("Command shortcut conflicts with a configured voice control");
            }
        }
        for phrase in rules.launches.keys() {
            if let Ok(original) = parser.parse(phrase) {
                if !["open_app", "focus_app"].contains(&original["action"].as_str().unwrap_or(""))
                    || parser.custom.get(phrase).is_some()
                {
                    return err("Application shortcut conflicts with an existing command");
                }
            }
        }
        parser.rules = rules;
        parser.blocked_apps = c["app_prefixes"]
            .as_object()
            .unwrap()
            .keys()
            .map(|k| normalize(k))
            .collect::<Result<_>>()?;
        Ok(parser)
    }
    pub fn parse(&self, text: &str) -> Result<Value> {
        static SIGNED: LazyLock<regex::Regex> =
            LazyLock::new(|| regex::Regex::new(r"(?i)workspace\s+[+-]\s*\w").unwrap());
        if SIGNED.is_match(text) {
            return err("Use an unsigned workspace number one through ten");
        }
        let mut phrase = normalize(text)?;
        if let Some(intent) = self.rules.parse_normalized(&phrase) {
            return Ok(intent);
        }
        if self.custom.get(&phrase).is_none() {
            phrase = strip_edges(&phrase);
        }
        phrase = self
            .custom
            .get(&phrase)
            .and_then(Value::as_str)
            .unwrap_or(&phrase)
            .to_owned();
        phrase = TABLES["VARIANTS"]
            .get(&phrase)
            .and_then(Value::as_str)
            .unwrap_or(&phrase)
            .to_owned();
        if let Some(intent) = TABLES["FIXED"].get(&phrase) {
            return Ok(intent.clone());
        }
        if let Some(intent) = self.rules.parse_normalized(&phrase) {
            return Ok(intent);
        }
        for (form, action) in TABLES["WORKSPACE_FORMS"].as_object().unwrap() {
            if let Some(number) = phrase.strip_prefix(&format!("{form} ")) {
                if number.contains(' ') {
                    continue;
                }
                let n = TABLES["NUMBERS"][number]
                    .as_u64()
                    .or_else(|| {
                        if number.bytes().all(|c| c.is_ascii_digit()) {
                            number.parse().ok()
                        } else {
                            None
                        }
                    })
                    .unwrap_or(0);
                if !(1..=10).contains(&n) {
                    return err("Choose workspace one through ten");
                }
                return Ok(json!({"action":action,"target":n}));
            }
        }
        let mut verbs: Vec<_> = TABLES["APP_VERBS"].as_object().unwrap().iter().collect();
        verbs.sort_by_key(|(verb, _)| std::cmp::Reverse(verb.len()));
        for (verb, action) in verbs {
            if let Some(target) = phrase.strip_prefix(&format!("{verb} ")) {
                let target = target.strip_prefix("the ").unwrap_or(target);
                if action == "open_app" && self.blocked_apps.contains(target) {
                    return err("Use the launch word configured for this application name");
                }
                if !target.is_empty() {
                    return Ok(json!({"action":action,"target":target}));
                }
            }
        }
        err("Phrase not recognized. Open Commands to see supported phrases.")
    }
    pub fn grammar(&self, names: impl IntoIterator<Item = String>) -> Value {
        let mut core = BTreeSet::new();
        for key in ["FIXED", "VARIANTS"] {
            core.extend(TABLES[key].as_object().unwrap().keys().cloned());
        }
        core.extend(self.custom.as_object().unwrap().keys().cloned());
        core.extend(self.rules.phrases());
        for form in TABLES["WORKSPACE_FORMS"].as_object().unwrap().keys() {
            for number in TABLES["NUMBERS"].as_object().unwrap().keys() {
                core.insert(format!("{form} {number}"));
            }
        }
        let apps: Vec<_> = names
            .into_iter()
            .filter(|s| !s.is_empty())
            .collect::<BTreeSet<_>>()
            .into_iter()
            .collect();
        let mut verbs: Vec<_> = TABLES["APP_VERBS"]
            .as_object()
            .unwrap()
            .keys()
            .filter(|s| !strings(&TABLES["TYPED_ONLY_VERBS"]).contains(s))
            .cloned()
            .collect();
        verbs.sort();
        json!({"launches":self.rules.launches,"blocked_apps":self.blocked_apps,"phrases":core,"apps":apps.iter().take(2048).collect::<Vec<_>>(),"custom":self.custom,"verbs":verbs,"prefixes":TABLES["POLITE_PREFIXES"],"suffixes":[],"overflow":apps.iter().skip(2048).map(|s|format!("open {s}")).collect::<Vec<_>>()})
    }
    pub fn candidates(&self, grammar: &Value) -> Result<Value> {
        let mut phrases = strings(&grammar["phrases"]);
        for verb in strings(&grammar["verbs"]) {
            for app in strings(&grammar["apps"]) {
                phrases.push(format!("{verb} {app}"));
            }
        }
        let mut result = vec![];
        for phrase in phrases {
            if let Ok(intent) = self.parse(&phrase) {
                let action = intent["action"].as_str().unwrap();
                let target = if ["open_app", "focus_app", "close_app"].contains(&action) {
                    grammar["app_ids"]
                        .get(intent["target"].as_str().unwrap_or(""))
                        .unwrap_or(&intent["target"])
                } else {
                    &intent["target"]
                };
                let target = target.as_str().map(str::to_owned).unwrap_or_else(|| {
                    if target.is_null() {
                        "None".into()
                    } else {
                        target.to_string()
                    }
                });
                result.push(json!([phrase, format!("{action}:{target}")]));
            }
        }
        Ok(json!(result))
    }
}
pub fn wake_forms(text: &str) -> Result<Vec<String>> {
    let normalized = normalize(text)?;
    let words: Vec<_> = normalized.split_whitespace().collect();
    let greetings = strings(&TABLES["GREETINGS"]);
    if words.len() >= 2 && greetings.iter().any(|g| g == words[0]) {
        let name = words[1..].join(" ");
        let mut forms: BTreeSet<_> = greetings
            .into_iter()
            .filter(|g| g != "ok")
            .map(|g| format!("{g} {name}"))
            .collect();
        forms.insert(name);
        Ok(forms.into_iter().collect())
    } else {
        Ok(vec![normalized])
    }
}

pub fn all_wake_forms(config: &Value) -> Result<Vec<String>> {
    let mut forms = BTreeSet::new();
    forms.extend(wake_forms(
        config["activation"]["wake_phrase"].as_str().unwrap(),
    )?);
    for name in strings(&config["activation"]["wake_aliases"]) {
        forms.extend(wake_forms(&name)?);
    }
    Ok(forms.into_iter().collect())
}

#[derive(Clone)]
pub struct Activation {
    pub state: &'static str,
    pub timeout: f64,
    pub confidence: f64,
    deadline: Option<f64>,
    started: f64,
    last: String,
    last_at: f64,
}
impl Activation {
    pub fn new(config: &Value) -> Self {
        Self {
            state: "paused",
            timeout: config["activation"]["command_timeout"].as_f64().unwrap(),
            confidence: config["activation"]["confidence"].as_f64().unwrap(),
            deadline: None,
            started: 0.,
            last: String::new(),
            last_at: -100.,
        }
    }
    pub fn set(&mut self, state: &'static str) {
        self.state = state;
        self.deadline = None;
    }
    pub fn listen(&mut self, deadline: Option<f64>) -> Result<()> {
        if !["idle", "paused", "error"].contains(&self.state) {
            return Ok(());
        }
        let end = deadline.unwrap_or_else(|| crate::util::now() + self.timeout);
        if !end.is_finite() {
            return err("Invalid capture deadline");
        }
        self.state = "listening";
        self.deadline = Some(end);
        self.started = end - self.timeout;
        Ok(())
    }
    pub fn accept(&mut self, event: &Value) -> bool {
        if self.state != "listening" {
            return false;
        }
        let now = crate::util::now();
        let end = self.deadline.unwrap_or(0.);
        if let Some(at) = event.get("captured_at").filter(|v| !v.is_null()) {
            let Some(at) = at.as_f64() else {
                return false;
            };
            if !at.is_finite()
                || at < self.started
                || at > end
                || !(0. ..=15.).contains(&(now - at))
            {
                return false;
            }
        } else if now >= end {
            return false;
        }
        let Some(confidence) = event["confidence"].as_f64() else {
            return false;
        };
        if !(self.confidence..=1.).contains(&confidence) {
            return false;
        }
        let Ok(phrase) = normalize(event["text"].as_str().unwrap_or("")) else {
            return false;
        };
        if phrase.is_empty() || (phrase == self.last && now - self.last_at < 1.5) {
            return false;
        }
        self.last = phrase;
        self.last_at = now;
        self.set("processing");
        true
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn completed_audio_may_finish_after_capture_deadline_but_stale_audio_cannot() {
        let now = crate::util::now();
        let mut state = Activation::new(&crate::config::defaults());
        state.listen(Some(now - 2.)).unwrap();
        assert!(!state.accept(&json!({"text":"cancel","confidence":1.0})));
        assert!(!state.accept(&json!({"text":"cancel","confidence":1.0,"captured_at":now-20.})));
        assert!(state.accept(&json!({"text":"cancel","confidence":1.0,"captured_at":now-3.})));
        assert_eq!(state.state, "processing");
    }

    #[test]
    fn future_results_low_confidence_and_duplicates_never_dispatch() {
        let now = crate::util::now();
        let mut state = Activation::new(&crate::config::defaults());
        state.listen(Some(now + 8.)).unwrap();
        assert!(!state.accept(&json!({"text":"cancel","confidence":1.0,"captured_at":now+1.})));
        assert!(!state.accept(&json!({"text":"cancel","confidence":0.1})));
        assert!(state.accept(&json!({"text":"cancel","confidence":1.0})));
        state.set("idle");
        state.listen(None).unwrap();
        assert!(!state.accept(&json!({"text":"CANCEL!","confidence":1.0})));
        state.set("paused");
        assert!(!state.accept(&json!({"text":"open firefox","confidence":1.0})));
    }
}
