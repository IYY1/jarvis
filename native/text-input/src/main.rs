//! Inserts dictated text into the focused text field (Wayland or Windows).
//!
//! On Wayland the input-method protocol (zwp_input_method_v2) is preferred: the
//! compositor tells it whether a text field is focused and what kind it is,
//! so password and other sensitive fields are refused. Apps without
//! text-input support (many terminals, Electron without IME flags, XWayland)
//! never activate the input method; for those the controller may allow a
//! virtual keyboard (zwp_virtual_keyboard_v1), which cannot see the field.
//!
//! Protocol: one JSON request per stdin line, one JSON reply per line.
//!   {"op":"insert","text":"…","keyboard":true}
//!     -> {"ok":true,"method":"input-method"|"virtual-keyboard"}
//!     -> {"ok":false,"reason":"sensitive"|"no-field"|"…","message":"…"}
//! `--check` prints which methods this session offers and exits.
//! On Windows see windows_input.rs: UI Automation refuses password fields and
//! SendInput types Unicode text.
//! Text is inserted exactly; no key other than its characters is pressed, so
//! a terminal never receives Enter.
#[cfg(unix)]
mod wayland;
#[cfg(windows)]
mod windows_input;
#[cfg(unix)]
use wayland::Typer;
#[cfg(windows)]
use windows_input::Typer;

use serde_json::{json, Value};
use std::io::{BufRead, Write};

type Result<T> = std::result::Result<T, Box<dyn std::error::Error>>;

/// Characters a dictated string may contain: printable text and spaces.
/// Newlines, tabs and other controls become spaces, so nothing is submitted.
fn printable(text: &str) -> String {
    text.chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect()
}

fn reply(value: Value) -> Result<()> {
    let mut out = std::io::stdout().lock();
    serde_json::to_writer(&mut out, &value)?;
    out.write_all(b"\n")?;
    out.flush()?;
    Ok(())
}

fn run() -> Result<()> {
    // Dictation helper lives only as long as its controller.
    #[cfg(target_os = "linux")]
    unsafe {
        libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGTERM);
    }
    let args: Vec<String> = std::env::args().skip(1).collect();
    if args == ["--version"] {
        println!(concat!("jarvis-type ", env!("CARGO_PKG_VERSION")));
        return Ok(());
    }
    let mut typer = Typer::connect()?;
    if args == ["--check"] {
        return reply(typer.check());
    }
    if !args.is_empty() {
        return Err("Use --check, --version, or no arguments".into());
    }
    reply(json!({"ready":true,"capabilities":typer.check()}))?;
    let stdin = std::io::stdin();
    for line in stdin.lock().lines() {
        let line = line?;
        if line.len() > 65536 {
            return Err("Request exceeds 64 KiB".into());
        }
        typer.dispatch()?;
        let request: Value = serde_json::from_str(&line)?;
        let result = match request["op"].as_str() {
            Some("insert") => typer.insert(
                request["text"].as_str().unwrap_or(""),
                request["keyboard"].as_bool().unwrap_or(false),
            ),
            Some("check") => Ok(typer.check()),
            _ => Err("Unknown request".into()),
        };
        reply(result.unwrap_or_else(
            |e| json!({"ok":false,"reason":"error","message":e.to_string()}),
        ))?;
    }
    Ok(())
}

fn main() {
    if let Err(error) = run() {
        let _ = reply(json!({"ok":false,"reason":"error","message":error.to_string()}));
        std::process::exit(1);
    }
}


#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn controls_never_reach_the_field() {
        assert_eq!(printable("سطر\nجديد\r\tok"), "سطر جديد  ok");
    }
}
