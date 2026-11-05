//! Arabic command wording, mapped onto the English commands the parser already
//! understands. Recognizing Arabic speech is the transcriber's job; this only
//! decides whether a transcript is a supported Arabic command. Mirrored by
//! src/murmur/phrases.py, which reads the same table.
use crate::{clip_phrase, normalize, number_words, MAX_CLIP_SECONDS};
use serde_json::Value;
use std::sync::LazyLock;

static TABLE: LazyLock<Value> = LazyLock::new(|| {
    serde_json::from_str(include_str!("../../../src/murmur/arabic_commands.json")).unwrap()
});

fn list(key: &str) -> Vec<&'static str> {
    let mut words: Vec<_> = TABLE[key]
        .as_array()
        .unwrap()
        .iter()
        .map(|w| w.as_str().unwrap())
        .collect();
    // Longest first, so "افتح لي" wins over "افتح".
    words.sort_by_key(|w| std::cmp::Reverse(w.chars().count()));
    words
}

fn lookup(key: &str, text: &str) -> Option<u64> {
    TABLE[key].get(text).and_then(Value::as_u64)
}

/// True when the text contains Arabic letters.
pub fn has_arabic(text: &str) -> bool {
    text.chars()
        .any(|c| ('\u{0600}'..='\u{06FF}').contains(&c) || ('\u{0750}'..='\u{077F}').contains(&c))
}

/// Arabic spelling folded the way speech transcripts vary: no diacritics,
/// tatweel or hamza seats, one alef, Western digits. Other text is unchanged.
pub(crate) fn fold_chars(text: &str) -> String {
    text.chars()
        .filter(|c| {
            !matches!(c, '\u{0610}'..='\u{061A}' | '\u{064B}'..='\u{065F}' | '\u{0670}'
                | '\u{06D6}'..='\u{06ED}' | '\u{0640}' | 'ء')
        })
        .map(|c| match c {
            'أ' | 'إ' | 'آ' | 'ٱ' => 'ا',
            'ة' => 'ه',
            'ى' | 'ئ' => 'ي',
            'ؤ' => 'و',
            '\u{0660}'..='\u{0669}' => char::from(b'0' + (c as u32 - 0x0660) as u8),
            '\u{06F0}'..='\u{06F9}' => char::from(b'0' + (c as u32 - 0x06F0) as u8),
            _ => c,
        })
        .collect()
}

/// The spelling the table uses (the ordinary command normalization).
pub fn fold(text: &str) -> String {
    normalize(text)
}

/// 1–59 in Arabic words or digits: "خمسه وعشرين", "خمسطعش", "30".
pub fn number(text: &str) -> Option<u64> {
    if !text.is_empty() && text.len() <= 4 && text.bytes().all(|c| c.is_ascii_digit()) {
        return text.parse().ok();
    }
    if let Some(n) = lookup("numbers", text) {
        return Some(n);
    }
    let words: Vec<_> = text.split(' ').collect();
    let (unit, tens) = match words[..] {
        [unit, tens] => (unit, tens.strip_prefix('و')?),
        [unit, "و", tens] => (unit, tens),
        _ => return None,
    };
    let unit = lookup("numbers", unit).filter(|n| (1..10).contains(n))?;
    let tens = lookup("numbers", tens).filter(|n| *n >= 20 && n % 10 == 0)?;
    Some(tens + unit)
}

fn strip_edges(mut text: &str) -> &str {
    loop {
        let before = text;
        text = text.strip_prefix("يا ").unwrap_or(text);
        for wake in list("wake_words").into_iter().chain(["jarvis", "hey jarvis"]) {
            if text == wake {
                return "";
            }
            if let Some(rest) = text.strip_prefix(&format!("{wake} ")) {
                text = rest;
            }
        }
        for p in list("polite_prefixes") {
            if let Some(rest) = text.strip_prefix(&format!("{p} ")) {
                text = rest;
                break;
            }
        }
        for s in list("polite_suffixes") {
            if let Some(rest) = text.strip_suffix(&format!(" {s}")) {
                text = rest;
                break;
            }
        }
        if text == before {
            return text;
        }
    }
}

fn clip(text: &str) -> Option<String> {
    let rest = list("clip_verbs")
        .into_iter()
        .find_map(|v| text.strip_prefix(&format!("{v} ")))?;
    let mut rest = rest.strip_prefix("لي ").unwrap_or(rest);
    for last in list("clip_last") {
        rest = rest.strip_prefix(&format!("{last} ")).unwrap_or(rest);
        rest = rest.strip_suffix(&format!(" {last}")).unwrap_or(rest);
    }
    for gone in [" اللي فاتت", " اللي فات", " اللي راحت"] {
        rest = rest.strip_suffix(gone).unwrap_or(rest);
    }
    let rest = rest.strip_prefix("ال").filter(|r| number(r).is_some()).unwrap_or(rest);
    let seconds = lookup("lengths", rest).or_else(|| {
        let (amount, unit) = rest.rsplit_once(' ')?;
        let scale = if list("second_words").contains(&unit) {
            1
        } else if list("minute_words").contains(&unit) {
            60
        } else {
            return None;
        };
        number(amount.strip_prefix("ال").unwrap_or(amount))?.checked_mul(scale)
    })?;
    let seconds = u32::try_from(seconds).ok()?;
    if !(1..=MAX_CLIP_SECONDS).contains(&seconds) {
        return None;
    }
    Some(clip_phrase(seconds).unwrap_or_else(|| format!("clip the last {seconds} seconds")))
}

fn workspace(text: &str) -> Option<String> {
    for (kind, english) in [("move", "move this window to workspace"), ("go", "go to workspace")] {
        let mut forms: Vec<_> = TABLE["workspace"][kind]
            .as_array()
            .unwrap()
            .iter()
            .map(|f| f.as_str().unwrap())
            .collect();
        forms.sort_by_key(|f| std::cmp::Reverse(f.chars().count()));
        for form in forms {
            if let Some(rest) = text.strip_prefix(&format!("{form} ")) {
                let rest = rest.strip_prefix("رقم ").unwrap_or(rest);
                let n = number(rest).or_else(|| lookup("ordinals", rest))?;
                return Some(if (1..=20).contains(&n) {
                    format!("{english} {}", number_words(n as u32))
                } else {
                    format!("{english} {n}")
                });
            }
        }
    }
    None
}

fn app_name(name: &str) -> String {
    TABLE["app_names"]
        .get(name)
        .and_then(Value::as_str)
        .unwrap_or(name)
        .to_owned()
}

fn application(text: &str, close_word: &str) -> Option<String> {
    for verb in list("close_verbs") {
        if let Some(name) = text.strip_prefix(&format!("{verb} ")) {
            return Some(format!("{close_word} {}", app_name(name)));
        }
    }
    for verb in list("switch_verbs") {
        let Some(rest) = text.strip_prefix(&format!("{verb} ")) else {
            continue;
        };
        let name = list("switch_prepositions")
            .into_iter()
            .find_map(|p| rest.strip_prefix(&format!("{p} ")))
            .map(str::to_owned)
            .or_else(|| rest.strip_prefix("لل").map(|r| format!("ال{r}")))
            .or_else(|| rest.strip_prefix('ل').map(str::to_owned))
            .unwrap_or_else(|| rest.to_owned());
        if !name.is_empty() {
            return Some(format!("switch to {}", app_name(&name)));
        }
    }
    for verb in list("open_verbs") {
        if let Some(name) = text.strip_prefix(&format!("{verb} ")) {
            return Some(format!("open {}", app_name(name)));
        }
    }
    None
}

/// The English command an Arabic transcript asks for, if it is a supported
/// command. `close_word` is the first configured close word. Text without
/// Arabic letters is left to the English parser.
pub fn translate(text: &str, close_word: &str) -> Option<String> {
    if !has_arabic(text) {
        return None;
    }
    let folded = fold(text);
    let p = strip_edges(&folded);
    if p.is_empty() {
        return None;
    }
    if let Some(english) = TABLE["fixed"].get(p).and_then(Value::as_str) {
        return Some(english.to_owned());
    }
    clip(p)
        .or_else(|| workspace(p))
        .or_else(|| application(p, close_word))
}

/// Every Arabic example the Commands page lists, with its English meaning.
pub fn fixed_phrases() -> Vec<(String, String)> {
    TABLE["fixed"]
        .as_object()
        .unwrap()
        .iter()
        .map(|(k, v)| (k.clone(), v.as_str().unwrap().to_owned()))
        .collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn najdi_and_msa_commands_map_to_english() {
        // Shared with tests/test_arabic.py, so both parsers agree.
        let cases: Vec<(String, Option<String>)> = serde_json::from_str(include_str!(
            "../../../tests/fixtures/arabic_command_cases.json"
        ))
        .unwrap();
        for (heard, english) in cases {
            assert_eq!(translate(&heard, "close"), english, "{heard}");
        }
        assert_eq!(translate("سكر فايرفوكس", "quit").as_deref(), Some("quit firefox"));
    }
    #[test]
    fn folding_removes_diacritics_and_unifies_letters() {
        assert_eq!(fold("أَهْلاً بِكَ يا مُحمّد ١٢"), "اهلا بك يا محمد 12");
        assert_eq!(fold("مِلْءُ الشّاشَةِ"), "مل الشاشه");
        assert_eq!(number("خمسه وعشرين"), Some(25));
        assert_eq!(number("خمسه و عشرين"), Some(25));
        assert_eq!(number("عشره وعشرين"), None);
    }
}
