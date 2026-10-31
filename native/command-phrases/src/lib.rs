//! Exact, configured shortcuts shared by the controller and recognizer.
use serde_json::{json, Map, Value};
use std::collections::BTreeSet;
use unicode_casefold::UnicodeCaseFold;
use unicode_normalization::UnicodeNormalization;

pub fn normalize(s: &str) -> String {
    s.nfkc()
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
