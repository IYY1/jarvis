//! Dictation sessions: speech the voice worker transcribed is typed into the
//! window that was focused when dictation started, never parsed as a command.
//!
//! Text goes through `jarvis-type` (Wayland input method, or a virtual
//! keyboard for apps without text-input support). Before each insertion the
//! focused window is compared with the starting one; if it changed, nothing
//! is typed and dictation stops. Text that could not be typed is put on the
//! clipboard (never from a password field), so Ctrl+V pastes it.
use crate::{desktop, err, policy::normalize, util, Result};
use serde_json::{json, Value};
use std::{process::Stdio, sync::Arc};
use tokio::{
    io::{AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, ChildStdout},
    sync::Mutex,
    time::{timeout, Duration},
};

/// Windows that must never receive virtual-keyboard text: the input method
/// cannot see inside them, so a password prompt there would be invisible.
const SECRET_WINDOWS: [&str; 12] = [
    "pinentry",
    "polkit",
    "gcr-prompter",
    "keepassxc",
    "bitwarden",
    "1password",
    "seahorse",
    "kwalletd",
    "password",
    "passphrase",
    "كلمة المرور",
    "كلمه المرور",
];

pub struct Typer {
    child: Child,
    input: ChildStdin,
    output: BufReader<ChildStdout>,
}

impl Typer {
    pub async fn start() -> Result<(Self, Value)> {
        let binary = helper()?;
        let mut command = util::owned_command(
            &[binary.to_string_lossy().into_owned()],
            libc::SIGTERM,
            false,
        )?;
        command
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = command.spawn()?;
        let input = child.stdin.take().ok_or("Missing text helper input")?;
        let mut output = BufReader::new(child.stdout.take().ok_or("Missing text helper output")?);
        let ready = read(&mut output).await?;
        if ready["ready"] != true {
            let _ = child.kill().await;
            return err(ready["message"]
                .as_str()
                .unwrap_or("Text insertion is unavailable in this session")
                .to_owned());
        }
        Ok((
            Self {
                child,
                input,
                output,
            },
            ready["capabilities"].clone(),
        ))
    }
    pub async fn insert(&mut self, text: &str, keyboard: bool) -> Result<Value> {
        let mut line = serde_json::to_vec(&json!({"op":"insert","text":text,"keyboard":keyboard}))?;
        line.push(b'\n');
        timeout(Duration::from_secs(2), self.input.write_all(&line)).await??;
        // Typing through the virtual keyboard takes a few ms per character.
        timeout(
            Duration::from_secs(5 + text.chars().count() as u64 / 100),
            read(&mut self.output),
        )
        .await?
    }
    pub async fn close(mut self) {
        drop(self.input);
        let _ = util::stop(&mut self.child, libc::SIGTERM, 2).await;
    }
}

async fn read(output: &mut BufReader<ChildStdout>) -> Result<Value> {
    let line = timeout(Duration::from_secs(5), util::line(output, 65536))
        .await??
        .ok_or("Text insertion helper stopped")?;
    Ok(serde_json::from_slice(&line)?)
}

fn helper() -> Result<std::path::PathBuf> {
    let mut candidates = vec![];
    if let Ok(exe) = std::env::current_exe() {
        candidates.push(exe.with_file_name("jarvis-type"));
    }
    if let Some(root) = crate::voice::project_root() {
        candidates.extend([
            root.join("src/jarvis/bin/jarvis-type"),
            root.join("native/text-input/target/release/jarvis-type"),
        ]);
    }
    candidates
        .into_iter()
        .find(|p| p.is_file())
        .ok_or_else(|| "Dictation's text helper is missing. Run scripts/build_controller.py.".into())
}

/// Jarvis's own settings window and dictation bar: never a dictation target.
const OWN_WINDOWS: [&str; 2] = ["org.jarvis.controller", "org.jarvis.dictation"];

/// The focused window: its address, whether it looks like a secret prompt,
/// and whether it is Jarvis's own window.
pub async fn focused() -> Option<(String, bool, bool)> {
    let window = desktop::query("activewindow").await.ok()?;
    let address = window["address"].as_str()?.to_owned();
    let label = format!(
        "{} {} {}",
        window["class"].as_str().unwrap_or(""),
        window["initialClass"].as_str().unwrap_or(""),
        window["title"].as_str().unwrap_or("")
    )
    .to_lowercase();
    let secret = SECRET_WINDOWS.iter().any(|s| label.contains(s));
    let class = window["class"].as_str().unwrap_or("").to_lowercase();
    Some((address, secret, OWN_WINDOWS.contains(&class.as_str())))
}

/// Dictation's language. Voice → Dictation can fix it; otherwise ("auto") it
/// follows the phrase that started it: اسمعني dictates Arabic, “listen”
/// English. The button and shortcut dictate Arabic while Arabic is on.
pub fn language(config: &Value, trigger: Option<&str>) -> &'static str {
    let arabic_on = config["recognition"]["language"] == "ar_en";
    match config["dictation"]["language"].as_str() {
        Some("ar") => "ar",
        Some("en") => "en",
        _ if !arabic_on => "en",
        _ => match trigger {
            Some(phrase) if !phrase.chars().any(|c| ('\u{0600}'..='\u{06FF}').contains(&c)) => "en",
            _ => "ar",
        },
    }
}

/// Puts text on the Wayland clipboard with wl-copy, which keeps serving it
/// after it returns. False when wl-copy is missing or fails.
pub async fn copy(text: &str) -> bool {
    copy_with("wl-copy", text).await
}
async fn copy_with(program: &str, text: &str) -> bool {
    let Ok(mut child) = tokio::process::Command::new(program)
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
    else {
        return false;
    };
    if let Some(mut input) = child.stdin.take() {
        if input.write_all(text.as_bytes()).await.is_err() {
            return false;
        }
    }
    matches!(timeout(Duration::from_secs(3), child.wait()).await, Ok(Ok(s)) if s.success())
}

/// What to tell the user about text that was not typed: a refusal's message,
/// plus how to paste it when it reached the clipboard.
pub fn held_message(message: &str, copied: bool) -> String {
    if copied {
        format!("{message} The text is on the clipboard: press Ctrl+V to paste it.")
    } else {
        message.to_owned()
    }
}

/// Splits a configured stop phrase off the end of dictated text. Returns the
/// text to type and whether dictation should stop.
pub fn split_stop(text: &str, stops: &[String]) -> (String, bool) {
    let words: Vec<&str> = text.split_whitespace().collect();
    for stop in stops {
        let stop = normalize(stop).unwrap_or_default();
        let size = stop.split(' ').count();
        if stop.is_empty() || words.len() < size {
            continue;
        }
        let tail = words[words.len() - size..]
            .iter()
            .filter_map(|w| normalize(w).ok())
            .filter(|w| !w.is_empty())
            .collect::<Vec<_>>()
            .join(" ");
        if tail == stop {
            return (words[..words.len() - size].join(" "), true);
        }
    }
    (text.to_owned(), false)
}

/// The small always-on-top bar with Stop/Cancel (src/jarvis/indicator.py).
/// gtk4-layer-shell must load before libwayland-client, hence the preload.
pub fn indicator() -> Option<Child> {
    let python = crate::voice::python().ok()?;
    let mut command = tokio::process::Command::new(python);
    command
        .args(["-m", "jarvis.indicator"])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    if let Some(root) = crate::voice::project_root() {
        command.env("PYTHONPATH", root.join("src"));
    }
    if let Some(layer) = ["/usr/lib/libgtk4-layer-shell.so.0", "/usr/lib64/libgtk4-layer-shell.so.0"]
        .into_iter()
        .find(|p| std::path::Path::new(p).is_file())
    {
        command.env("LD_PRELOAD", layer);
    }
    command.spawn().ok()
}

/// One dictation session's visible state.
#[derive(Default)]
pub struct Session {
    pub active: bool,
    pub id: u64,
    pub typer: Option<Arc<Mutex<Typer>>>,
    pub window: Option<String>,
    pub inserted: usize,
    pub method: String,
    pub held: String,
    pub message: String,
    pub capabilities: Value,
    pub typed_any: bool,
    pub language: String,
    pub indicator: Option<Child>,
}

impl Session {
    pub fn status(&self) -> Value {
        json!({"active":self.active,"inserted":self.inserted,"method":self.method,
               "held":self.held,"message":self.message,"capabilities":self.capabilities,
               "language":self.language})
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test(flavor = "current_thread")]
    async fn copy_sends_the_text_and_reports_failure() {
        let dir = std::env::temp_dir().join(format!("jarvis-copy-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let fake = dir.join("wl-copy");
        let saved = dir.join("clipboard");
        std::fs::write(&fake, format!("#!/bin/sh\ncat > '{}'\n", saved.display())).unwrap();
        std::fs::set_permissions(&fake, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        assert!(copy_with(fake.to_str().unwrap(), "مرحبا hello").await);
        assert_eq!(std::fs::read_to_string(&saved).unwrap(), "مرحبا hello");
        assert!(!copy_with("/nonexistent/wl-copy", "x").await);
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn dictation_language_follows_the_phrase_unless_fixed() {
        let config = |recognition: &str, dictation: &str| {
            json!({"recognition":{"language":recognition},"dictation":{"language":dictation}})
        };
        let both = config("ar_en", "auto");
        assert_eq!(language(&both, Some("جارفس اسمعني")), "ar");
        assert_eq!(language(&both, Some("listen to me")), "en");
        assert_eq!(language(&both, None), "ar");
        assert_eq!(language(&config("en", "auto"), Some("اسمعني")), "en");
        assert_eq!(language(&config("ar_en", "en"), Some("اسمعني")), "en");
        assert_eq!(language(&config("ar_en", "ar"), Some("listen")), "ar");
    }

    #[test]
    fn held_text_says_how_to_paste_only_when_copied() {
        let refusal = "No text field is focused.";
        assert!(held_message(refusal, true).ends_with("press Ctrl+V to paste it."));
        assert_eq!(held_message(refusal, false), refusal);
    }
    #[test]
    fn stop_phrases_end_dictation_without_being_typed() {
        let stops = vec!["stop dictation".to_owned(), "وقف الكتابة".to_owned()];
        assert_eq!(
            split_stop("send the report today. Stop dictation.", &stops),
            ("send the report today.".into(), true)
        );
        assert_eq!(
            split_stop("ارسل التقرير اليوم وقّف الكتابة", &stops),
            ("ارسل التقرير اليوم".into(), true)
        );
        assert_eq!(split_stop("Stop dictation", &stops), ("".into(), true));
        assert_eq!(
            split_stop("we should stop dictation software", &stops),
            ("we should stop dictation software".into(), false)
        );
    }
}
