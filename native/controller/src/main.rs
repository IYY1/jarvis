use jarvis_controller::{
    apps, capture, config, desktop, err, policy, service, util, voice, Error, Result,
};
use serde_json::{json, Value};
use std::{
    env, fs,
    os::{
        fd::AsRawFd,
        unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    },
    path::PathBuf,
    sync::Arc,
};
use tokio::{
    io::{AsyncReadExt, AsyncWriteExt, BufReader},
    net::{UnixListener, UnixStream},
    sync::{mpsc, oneshot, watch, Semaphore},
    time::{timeout, Duration},
};

async fn write_json<W: tokio::io::AsyncWrite + Unpin>(writer: &mut W, value: &Value) -> Result<()> {
    let mut bytes = serde_json::to_vec(value)?;
    bytes.push(b'\n');
    timeout(Duration::from_secs(2), writer.write_all(&bytes)).await??;
    Ok(())
}
async fn connection(
    stream: UnixStream,
    events: mpsc::Sender<service::Message>,
    mut status: watch::Receiver<Value>,
) -> Result<()> {
    if stream.peer_cred()?.uid() != unsafe { libc::getuid() } {
        return err("Peer user is not authorized");
    }
    let (read, mut write) = stream.into_split();
    let mut reader = BufReader::new(read);
    let result = async {
        let bytes = timeout(Duration::from_secs(5), util::line(&mut reader, 65536))
            .await??
            .ok_or("Request must be a JSON object")?;
        let payload: Value = serde_json::from_slice(&bytes)?;
        if !payload.is_object() {
            return err("Request must be a JSON object");
        }
        if payload["op"] == "watch" {
            let initial = status.borrow_and_update().clone();
            write_json(&mut write, &initial).await?;
            let mut byte = [0];
            loop {
                tokio::select! {
                    _ = reader.read(&mut byte) => break,
                    changed = status.changed() => {
                        if changed.is_err() {
                            break;
                        }
                        let value = status.borrow_and_update().clone();
                        write_json(&mut write, &value).await?;
                    }
                }
            }
            return Ok(());
        }
        let (reply, rx) = oneshot::channel();
        events
            .send(service::Message::Request(payload, reply))
            .await
            .map_err(|_| "Service is stopping")?;
        // A save that restarts capture waits for a recording to be finalized.
        let response = timeout(Duration::from_secs(120), rx).await??;
        write_json(&mut write, &response).await?;
        Ok::<_, Error>(())
    }
    .await;
    if let Err(error) = result {
        let _ = write_json(&mut write, &json!({"ok":false,"error":error.to_string()})).await;
    }
    Ok(())
}
fn response(result: Result<Value>) -> Value {
    match result {
        Ok(value) => value,
        Err(error) => json!({"ok":false,"error":error.to_string()}),
    }
}

async fn discover(op: &str) -> Result<Value> {
    match op {
        "devices" => Ok(json!({"ok":true,"devices":desktop::devices().await?})),
        "monitors" => Ok(json!({"ok":true,"monitors":desktop::monitors().await?})),
        _ => {
            let devices = desktop::audio_sources().await?;
            let devices: Vec<_> = devices
                .as_array()
                .unwrap()
                .iter()
                .map(|d| json!({"id":d["name"],"name":d["description"],"monitor":d["monitor"]}))
                .collect();
            Ok(json!({"ok":true,"devices":devices}))
        }
    }
}

async fn request(service: &mut service::Service, payload: Value, reply: service::Reply) {
    let op = payload["op"].as_str().unwrap_or("");
    if op == "set_startup" {
        let Some(enabled) = payload["enabled"].as_bool() else {
            let _ = reply.send(json!({"ok":false,"error":"Startup must be true or false"}));
            return;
        };
        let events = service.events.clone();
        tokio::spawn(async move {
            let result = util::query(
                &[
                    "systemctl".into(),
                    "--user".into(),
                    if enabled {
                        "enable".into()
                    } else {
                        "disable".into()
                    },
                    "murmur.service".into(),
                ],
                5,
            )
            .await
            .map(|_| ())
            .map_err(|e| e.to_string());
            let _ = events
                .send(service::Message::StartupDone(enabled, result, reply))
                .await;
        });
        return;
    }
    if op == "save_config" {
        if let Ok((config, restart)) = service.restarts_for(&payload["config"]) {
            if !restart.is_empty() {
                // Stopping can take seconds (a recording is finalized), so it
                // runs beside the state actor: stop, save, start again.
                let (recording, replay) = (service.recording.clone(), service.replay.clone());
                let events = service.events.clone();
                tokio::spawn(async move {
                    let capture = |kind: &str| if kind == "record" { recording.clone() } else { replay.clone() };
                    for kind in &restart {
                        if let Err(e) = capture(kind).lock().await.stop().await {
                            let _ = reply.send(json!({"ok":false,"error":format!("Could not stop to apply the settings: {e}")}));
                            return;
                        }
                    }
                    let (saved, rx) = oneshot::channel();
                    if events.send(service::Message::Request(payload, saved)).await.is_err() {
                        return;
                    }
                    let Ok(mut response) = rx.await else { return };
                    let mut errors = vec![];
                    if response["ok"] == true {
                        for kind in &restart {
                            if let Err(e) = capture(kind).lock().await.start(&config).await {
                                errors.push(e.to_string());
                            }
                        }
                    }
                    response["restarted"] = json!(restart);
                    response["restart_errors"] = json!(errors);
                    let _ = reply.send(response);
                });
                return;
            }
        }
    }
    if matches!(op, "devices" | "recording_devices" | "monitors") {
        // External servers can be slow. Discovery stays outside the state actor
        // so status, cancellation and mute can still be processed immediately.
        tokio::spawn(async move {
            let result = discover(payload["op"].as_str().unwrap()).await;
            let _ = reply.send(response(result));
        });
        return;
    }
    let phrase = match op {
        "execute" => payload["phrase"].as_str(),
        "record_start" => Some("start recording"),
        "record_stop" => Some("stop recording"),
        "replay_start" => Some("start replay"),
        "replay_stop" => Some("stop replay"),
        "replay_save" => Some("save replay"),
        _ => None,
    };
    if let Some(phrase) = phrase {
        service.execute(phrase, Some(reply), None, None).await;
    } else {
        let _ = reply.send(response(service.handle(&payload).await));
    }
}

async fn message(service: &mut service::Service, message: service::Message) {
    use service::Message;
    match message {
        Message::Request(payload, reply) => request(service, payload, reply).await,
        Message::Voice(generation, event) => {
            if let Err(error) = service.event(generation, event).await {
                service
                    .voice_end(generation, format!("Voice worker failed: {error}"))
                    .await;
            }
        }
        Message::VoiceEnd(generation, detail) => service.voice_end(generation, detail).await,
        Message::StartupDone(enabled, result, reply) => {
            let _ = reply.send(response(service.startup_done(enabled, result)));
        }
        Message::Typed {
            session,
            text,
            result,
        } => service.typed(session, text, result).await,
        Message::ActionDone {
            result,
            reply,
            generation,
            heard,
            capture,
        } => {
            service.complete(result, reply, generation, heard, capture);
            if let Some((generation, detail)) = service.completed_voice_end() {
                service.voice_end(generation, detail).await;
            }
        }
    }
}

async fn serve() -> Result<()> {
    config::note_version(env!("CARGO_PKG_VERSION"));
    let config = config::load()?;
    let path = jarvis_controller::ipc::socket_path();
    let parent = path.parent().ok_or("Invalid IPC socket path")?;
    util::private_dir(parent)?;
    let metadata = fs::metadata(parent)?;
    if metadata.uid() != unsafe { libc::getuid() } || metadata.mode() & 0o077 != 0 {
        return err("IPC directory must be owned by you with mode 0700");
    }
    let lock = fs::OpenOptions::new()
        .create(true)
        .append(true)
        .mode(0o600)
        .open(parent.join("service.lock"))?;
    if unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } != 0 {
        return err("Murmur service is already running");
    }
    if path.exists() {
        fs::remove_file(&path)?;
    }
    let listener = UnixListener::bind(&path)?;
    fs::set_permissions(&path, fs::Permissions::from_mode(0o600))?;
    let (events, mut messages) = mpsc::channel(64);
    let mut service = service::Service::new(config, events.clone(), &path)?;
    let mut terminate = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
    let mut interrupt = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    if service.config["replay"]["autostart"] == true {
        let replay = service.replay.clone();
        let cfg = service.config.clone();
        tokio::spawn(async move {
            if let Err(e) = replay.lock().await.start(&cfg).await {
                eprintln!("jarvis: instant replay did not start: {e}");
            }
        });
    }
    if service.config["privacy"]["onboarded"] == true {
        if let Err(e) = service.set_mute(false).await {
            eprintln!("jarvis: voice control could not start: {e}");
            service.voice_failed(format!("Voice control could not start: {e}"));
        }
    }
    let limits = Arc::new(Semaphore::new(64));
    loop {
        tokio::select! { biased;
            _ = terminate.recv() => break,
            _ = interrupt.recv() => break,
            Some(event) = messages.recv() => {
                message(&mut service, event).await;
                if service.quit {
                    break;  // “close yourself”: shutdown below finishes captures first
                }
            }
            Ok(()) = service.recording_updates.changed() => service.capture_changed(),
            Ok(()) = service.replay_updates.changed() => service.capture_changed(),
            accepted = listener.accept() => {
                let (stream, _) = accepted?;
                if let Ok(permit) = limits.clone().try_acquire_owned() {
                    let events = events.clone();
                    let status = service.updates.subscribe();
                    tokio::spawn(async move {
                        let _permit = permit;
                        let _ = connection(stream, events, status).await;
                    });
                }
            }
        }
    }
    service.shutdown().await;
    drop(listener);
    fs::remove_file(path)?;
    drop(lock);
    Ok(())
}
fn diagnostic(request: &Value) -> Result<Value> {
    match request["op"].as_str().unwrap_or("") {
        "normalize" => Ok(
            json!({"normalized":policy::normalize(request["text"].as_str().ok_or("Phrase must be text")?)?}),
        ),
        "validate" => Ok(json!({"config":config::validate(&request["config"])?})),
        "parse" => Ok(
            json!({"intent":(if let Some(c) = request.get("config") { policy::Parser::from_config(&config::validate(c)?)? } else { policy::Parser::new(request.get("custom").unwrap_or(&json!({})))? }).parse(request["text"].as_str().ok_or("Phrase must be text")?)?}),
        ),
        "grammar" => Ok(
            json!({"grammar":(if let Some(c) = request.get("config") { policy::Parser::from_config(&config::validate(c)?)? } else { policy::Parser::new(request.get("custom").unwrap_or(&json!({})))? }).grammar(policy::strings(&request["apps"]))}),
        ),
        "apps" => {
            let roots = request["roots"].as_array().map(|r| {
                r.iter()
                    .filter_map(Value::as_str)
                    .map(PathBuf::from)
                    .collect()
            });
            let apps = apps::Registry::scan(request.get("aliases").unwrap_or(&json!({})), roots);
            let resolved = request["resolve"]
                .as_str()
                .map(|name| apps.resolve(name).map(|app| json!(app)))
                .transpose()?;
            let parser = if let Some(config) = request.get("config") {
                policy::Parser::from_config(&config::validate(config)?)?
            } else {
                policy::Parser::new(&json!({}))?
            };
            Ok(
                json!({"apps":apps.list(),"resolved":resolved,"applications":apps.voice_commands(&parser)?,"grammar":apps.grammar(&parser)?}),
            )
        }
        "classes" => {
            let app: apps::App = serde_json::from_value(request["app"].clone())?;
            Ok(json!({"classes":app.classes()}))
        }
        "capture_color" => {
            Ok(json!({"color":capture::color(&request["config"],&request["monitor"])?}))
        }
        "dispatch" => {
            let (legacy, lua) = desktop::dispatch_args(
                request["action"].as_str().unwrap_or(""),
                &request["target"],
            )?;
            Ok(json!({"legacy":legacy,"lua":lua}))
        }
        _ => err("Unsupported diagnostic operation"),
    }
}
fn run() -> Result<()> {
    let args: Vec<_> = env::args().skip(1).collect();
    if args == ["--version"] {
        println!(
            "jarvis-controller {}; Rust service, native JSON IPC; no Python runtime",
            env!("CARGO_PKG_VERSION")
        );
        return Ok(());
    }
    if args == ["--policy"] {
        use std::io::{BufRead, Write};
        let input = std::io::stdin();
        let mut reader = input.lock();
        loop {
            let mut bytes = vec![];
            let n = std::io::Read::take(&mut reader, 1048577).read_until(b'\n', &mut bytes)?;
            if n == 0 {
                break;
            }
            if bytes.len() > 1048576 {
                return err("Diagnostic input exceeds its size limit");
            }
            let response = serde_json::from_slice(&bytes)
                .map_err(Error::from)
                .and_then(|v| diagnostic(&v));
            let value = match response {
                Ok(v) => json!({"ok":true,"result":v}),
                Err(e) => json!({"ok":false,"error":e.to_string()}),
            };
            let mut out = std::io::stdout().lock();
            serde_json::to_writer(&mut out, &value)?;
            writeln!(out)?;
        }
        return Ok(());
    }
    if args == ["--check"] {
        let config = config::load()?;
        let apps = apps::Registry::scan(&config["aliases"], None);
        let parser = policy::Parser::from_config(&config)?;
        let (worker, payload) =
            voice::prepare(&config, "wake_word", &parser, apps.grammar(&parser)?)?;
        println!(
            "{}",
            json!({"ok":true,"controller":"rust","worker":worker,"denoiser":payload["native"]["denoiser"],"whisper":payload["native"]["whisper"]})
        );
        return Ok(());
    }
    if !args.is_empty() && args != ["daemon"] {
        return err("Use daemon, --check, --policy, or --version");
    }
    // A single long-lived OS thread owns all process creation, preserving Linux
    // PR_SET_PDEATHSIG semantics. Idle work sleeps on sockets, channels or signals.
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(serve())
}
fn main() {
    if let Err(error) = run() {
        eprintln!("jarvis-controller: {error}");
        std::process::exit(1);
    }
}
