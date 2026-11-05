//! Exact, configured shortcuts shared by the controller and recognizer.
pub mod arabic;
use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;

pub fn normalize(s: &str) -> String {
    arabic::fold_chars(&s.nfkc().collect::<String>())
        .chars()
        .case_fold()
        .map(|c| {
            if c.is_alphanumeric() || c == '_' || c.is_whitespace() {
                c
            } else {
                ' '
            }
        })
        .collect::<String>()
        .split_whitespace()
        .map(|w| if w == "fullscreen" { "full screen" } else { w })
        .collect::<Vec<_>>()
        .join(" ")
}
#[derive(Clone, Debug, Default)]
pub struct Rules {
    pub launches: Map<String, Value>,
}
impl Rules {
    pub fn from_config(c: &Value) -> Result<Self, String> {
        let mut rules = Self::default();
        let aliases = c["aliases"]
            .as_object()
            .ok_or("aliases must be an object")?;
        let prefixes = c["app_prefixes"]
            .as_object()
            .ok_or("app_prefixes must be an object")?;
        if prefixes.len() > 256 {
            return Err("Too many application prefixes".into());
        }
        let mut seen = BTreeSet::new();
        for (alias, prefix) in prefixes {
            let name = normalize(alias);
            let prefix = prefix
                .as_str()
                .ok_or("Application launch word must be text")?;
            if !seen.insert(name.clone())
                || prefix.chars().count() > 40
                || prefix.chars().any(char::is_control)
                || prefix.split_whitespace().count() > 4
            {
                return Err("Invalid or duplicate application prefix".into());
            }
            let _target = aliases
                .iter()
                .find(|(k, _)| normalize(k) == name)
                .map(|(_, v)| v)
                .ok_or("Launch words need an existing application alias")?;
            let p = normalize(prefix);
            let phrase = if p.is_empty() {
                name.clone()
            } else {
                format!("{p} {name}")
            };
            if rules.launches.insert(phrase, json!(name)).is_some() {
                return Err("Conflicting application shortcut".into());
            }
        }
        Ok(rules)
    }
    pub fn from_grammar(g: &Value) -> Self {
        Self {
            launches: g["launches"].as_object().cloned().unwrap_or_default(),
        }
    }
    pub fn parse(&self, text: &str) -> Option<Value> {
        self.parse_normalized(&normalize(text))
    }
    pub fn parse_normalized(&self, p: &str) -> Option<Value> {
        if let Some(target) = self.launches.get(p) {
            return Some(json!({"action":"open_app","target":target}));
        }
        for (shortcut, alias) in &self.launches {
            if let Some(alias) = alias.as_str() {
                if let Some(prefix) = shortcut.strip_suffix(alias) {
                    if p == format!("{prefix}the {alias}") {
                        return Some(json!({"action":"open_app","target":alias}));
                    }
                }
            }
        }
        None
    }
    pub fn phrases(&self) -> Vec<String> {
        let mut p: Vec<_> = self.launches.keys().cloned().collect();
        p.sort();
        p
    }
    pub fn canonical(&self, text: &str) -> Option<String> {
        let intent = self.parse(text)?;

        // One canonical name prevents synonyms competing with each other.
        if intent["action"] == "open_app" {
            return self
                .launches
                .iter()
                .find(|(_, id)| **id == intent["target"])
                .map(|(p, _)| p.clone());
        }
        None
    }
}

/// Longest clip a voice command may request: the longest replay buffer.
pub const MAX_CLIP_SECONDS: u32 = 1800;
const ONES: [&str; 20] = [
    "zero",
    "one",
    "two",
    "three",
    "four",
    "five",
    "six",
    "seven",
    "eight",
    "nine",
    "ten",
    "eleven",
    "twelve",
    "thirteen",
    "fourteen",
    "fifteen",
    "sixteen",
    "seventeen",
    "eighteen",
    "nineteen",
];
const TENS: [&str; 10] = [
    "", "", "twenty", "thirty", "forty", "fifty", "sixty", "seventy", "eighty", "ninety",
];

/// 1–99 in words ("twenty five"), the form speech grammars contain.
pub fn number_words(n: u32) -> String {
    match n {
        0..=19 => ONES[n as usize].to_owned(),
        20..=99 if n.is_multiple_of(10) => TENS[(n / 10) as usize].to_owned(),
        20..=99 => format!("{} {}", TENS[(n / 10) as usize], ONES[(n % 10) as usize]),
        _ => n.to_string(),
    }
}

fn spoken_number(text: &str) -> Option<u32> {
    if text == "a" || text == "an" {
        return Some(1);
    }
    if !text.is_empty() && text.len() <= 4 && text.bytes().all(|c| c.is_ascii_digit()) {
        return text.parse().ok();
    }
    let words: Vec<_> = text.split(' ').collect();
    let unit = |w: &str| ONES.iter().position(|o| *o == w).map(|n| n as u32);
    match words[..] {
        [w] => unit(w).or_else(|| {
            TENS.iter()
                .position(|t| !t.is_empty() && *t == w)
                .map(|n| n as u32 * 10)
        }),
        [tens, ones] => {
            let tens = TENS.iter().position(|t| !t.is_empty() && *t == tens)? as u32 * 10;
            unit(ones).filter(|n| (1..10).contains(n)).map(|n| tens + n)
        }
        _ => None,
    }
}

/// Seconds requested by a normalized clip command such as "clip the last
/// twenty five seconds", "save the last 2 minutes" or "clip the last minute".
/// "clip that" (the whole buffer) is a fixed phrase and returns None here.
pub fn clip_seconds(p: &str) -> Option<u32> {
    let rest = p
        .strip_prefix("clip ")
        .or_else(|| p.strip_prefix("save "))?;
    let rest = ["that ", "it "]
        .iter()
        .find_map(|w| rest.strip_prefix(w))
        .unwrap_or(rest);
    let rest = rest.strip_prefix("the ").unwrap_or(rest);
    let (last, rest) = match ["last ", "past ", "previous "]
        .iter()
        .find_map(|w| rest.strip_prefix(w))
    {
        Some(r) => (true, r),
        None => (false, rest),
    };
    if last && rest == "minute" {
        return Some(60);
    }
    let (amount, unit) = rest.rsplit_once(' ')?;
    let scale = match unit {
        "second" | "seconds" | "sec" | "secs" => 1,
        "minute" | "minutes" | "min" | "mins" => 60,
        _ => return None,
    };
    let total = spoken_number(amount)?.checked_mul(scale)?;
    (1..=MAX_CLIP_SECONDS).contains(&total).then_some(total)
}

/// The one wording the recognizer is given for a clip length, if it has one.
pub fn clip_phrase(seconds: u32) -> Option<String> {
    match seconds {
        1 => Some("clip the last one second".into()),
        2..=59 => Some(format!("clip the last {} seconds", number_words(seconds))),
        60 => Some("clip the last minute".into()),
        _ if seconds.is_multiple_of(60) && seconds <= 30 * 60 => Some(format!(
            "clip the last {} minutes",
            number_words(seconds / 60)
        )),
        _ => None,
    }
}

/// Every spoken clip length: 1–59 seconds and 1–30 minutes.
pub fn clip_phrases() -> Vec<String> {
    (1..60)
        .chain((1..=30).map(|m| m * 60))
        .filter_map(clip_phrase)
        .collect()
}

/// Maps any accepted clip wording ("clip last 25 seconds") to its grammar form.
pub fn clip_canonical(p: &str) -> Option<String> {
    clip_phrase(clip_seconds(p)?)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn clip_lengths_accept_spoken_and_written_numbers() {
        for (text, seconds) in [
            ("clip the last twenty five seconds", Some(25)),
            ("clip last 25 seconds", Some(25)),
            ("clip that last 25 seconds", Some(25)),
            ("save the last 2 minutes", Some(120)),
            ("clip the past three minutes", Some(180)),
            ("clip the last minute", Some(60)),
            ("clip a minute", Some(60)),
            ("clip 90 seconds", Some(90)),
            ("clip the last thirty minutes", Some(1800)),
            ("clip the last thirty one minutes", None),
            ("clip the last zero seconds", None),
            ("clip that", None),
            ("clip the last twenty ten seconds", None),
            ("clip firefox", None),
            ("save the replay", None),
        ] {
            assert_eq!(clip_seconds(text), seconds, "{text}");
        }
        assert_eq!(
            clip_canonical("clip last 25 seconds").unwrap(),
            "clip the last twenty five seconds"
        );
        assert_eq!(
            clip_canonical("save 1 minute").unwrap(),
            "clip the last minute"
        );
        assert_eq!(clip_canonical("clip 90 seconds"), None);
        let phrases = clip_phrases();
        assert_eq!(phrases.len(), 89);
        for phrase in &phrases {
            assert_eq!(clip_canonical(phrase).as_ref(), Some(phrase));
        }
    }
}
