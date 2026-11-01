//! Bounded local pipe protocol: one JSON header, optional raw append bytes,
//! one JSON response. Fatal errors exit; the owning capture must stop too.
use jarvis_replay_store::{Session, Store};
use serde_json::{json, Value};
use std::{
    io::{self, BufRead, Read, Write},
    path::Path,
};

fn run() -> Result<(), Box<dyn std::error::Error>> {
    let mut input = io::stdin().lock();
    let mut output = io::stdout().lock();
    let mut store: Option<Store> = None;
    loop {
        let mut line = Vec::new();
        if input.by_ref().take(8193).read_until(b'\n', &mut line)? == 0 {
            return Ok(());
        }
        if line.len() > 8192 || line.last() != Some(&b'\n') {
            return Err("Replay request is too long.".into());
        }
        let request: Value = serde_json::from_slice(&line)?;
        let result = match request["op"].as_str().unwrap_or("") {
            "open" if store.is_none() => {
                let root = request["root"].as_str().ok_or("Missing replay directory")?;
                let session = Session::create(Path::new(root))?;
                store = Some(Store::new(
                    session,
                    request["seconds"]
                        .as_u64()
                        .ok_or("Missing replay time limit")? as usize,
                    request["bytes"]
                        .as_u64()
                        .ok_or("Missing replay byte limit")?,
                )?);
                json!({"ok":true})
            }
            "close" => return Ok(()),
            op => {
                let s = store.as_mut().ok_or("Open storage first")?;
                match op {
                    "append" => {
                        let bytes = request["bytes"].as_u64().ok_or("Missing segment length")?;
                        if bytes > 64 * 1024 * 1024 {
                            return Err("Replay segment exceeds 64 MiB".into());
                        }
                        let mut payload = input.by_ref().take(bytes);
                        let appended = s.append(
                            request["sequence"]
                                .as_u64()
                                .ok_or("Missing segment sequence")?,
                            bytes,
                            &mut payload,
                        );
                        // Drain a rejected segment before answering so the
                        // sender cannot block on a full pipe. Keep existing
                        // snapshot files alive until Release/EOF on failure.
                        io::copy(&mut payload, &mut io::sink())?;
                        match appended {
                            Ok(()) => json!({"ok":true,"bytes":s.bytes()}),
                            Err(e) => {
                                json!({"ok":false,"error":format!("Replay disk storage: {e}")})
                            }
                        }
                    }
                    "snapshot" => match s.snapshot() {
                        Ok(paths) => json!({"ok":true,"paths":paths}),
                        Err(e) => json!({"ok":false,"error":e.to_string()}),
                    },
                    "release" => {
                        s.release()?;
                        json!({"ok":true})
                    }
                    _ => return Err("Unknown replay operation".into()),
                }
            }
        };
        serde_json::to_writer(&mut output, &result)?;
        output.write_all(b"\n")?;
        output.flush()?;
    }
}
fn main() {
    if std::env::args().nth(1).as_deref() == Some("--version") {
        println!("jarvis-replay-store 0.2.0 (Rust disk buffer)");
        return;
    }
    if let Err(e) = run() {
        println!(
            "{}",
            json!({"ok":false,"error":format!("Replay disk storage: {e}")})
        );
        std::process::exit(1);
    }
}
