use crate::{
    apps::Registry,
    capture::Capture,
    config,
    desktop::{self, Desktop},
    dictation, err,
    policy::{self, Activation, Parser},
    util, voice, Result,
};
use serde_json::{json, Value};
use std::{
    env,
    process::Stdio,
    sync::{
        atomic::{AtomicU64, Ordering},
        Arc,
    },
};
use tokio::{
    io::{AsyncWriteExt, BufReader},
    process::{Child, ChildStdin, Command},
    sync::{mpsc, oneshot, watch, Mutex, Semaphore},
    time::{timeout, Duration},
};

pub type Reply = oneshot::Sender<Value>;
/// Replay settings a running replay can't change; with any recording
/// setting they make a save restart the replay (mirrors client.py).
pub const REPLAY_CAPTURE_KEYS: [&str; 16] = [
    "seconds",
    "game_audio",
    "microphone",
    "storage",
    "max_mb",
    "buffer_dir",
    "audio_tracks",
    "separate_apps",
    "own_video",
    "codec",
    "fps",
    "resolution",
    "rate_control",
    "quality_preset",
    "bitrate_mbps",
    "bitrate_auto",
];
pub enum Message {
    Request(Value, Reply),
    Voice(u64, Value),
    VoiceEnd(u64, String),
    StartupDone(bool, std::result::Result<(), String>, Reply),
    /// A dictated segment was typed (or refused) by the text helper.
    Typed {
        session: u64,
        text: String,
        result: std::result::Result<Value, String>,
    },
    ActionDone {
        result: std::result::Result<String, String>,
        reply: Option<Reply>,
        generation: Option<u64>,
        heard: Option<(String, String)>,
        capture: bool,
    },
}
struct Worker {
    child: Child,
    input: Option<ChildStdin>,
    reader: tokio::task::JoinHandle<()>,
    mode: String,
}
pub struct Service {
    pub config: Value,
    parser: Parser,
    apps: Arc<Registry>,
    activation: Activation,
    muted: bool,
    message: String,
    worker: Option<Worker>,
    generation: u64,
    unanswered: Option<usize>,
    level: Value,
    gate: Value,
    engine: Value,
    unsupported: Value,
    pub updates: watch::Sender<Value>,
    pub events: mpsc::Sender<Message>,
    pub recording: Arc<Mutex<Capture>>,
    pub replay: Arc<Mutex<Capture>>,
    pub recording_updates: watch::Receiver<Value>,
    pub replay_updates: watch::Receiver<Value>,
    action_lock: Arc<Mutex<Desktop>>,
    action_slots: Arc<Semaphore>,
    epoch: Arc<AtomicU64>,
    voice_pending: bool,
    deferred_voice_end: Option<(u64, String)>,
    voice_available: bool,
    pending_captures: usize,
    last_recording: String,
    last_replay: Value,
    dictation: dictation::Session,
    /// Set by “close yourself” or a quit request: the service ends after replying.
    pub quit: bool,
}
impl Service {
    pub fn new(
        config: Value,
        events: mpsc::Sender<Message>,
        socket: &std::path::Path,
    ) -> Result<Self> {
        let muted = config["privacy"]["onboarded"] != true;
        let recording = Capture::new(false, socket.with_file_name("recording.sock"));
        recording.inspect_previous(&config);
        let replay = Capture::new(true, socket.with_file_name("replay.sock"));
        let recording_updates = recording.status.subscribe();
        let replay_updates = replay.status.subscribe();
        let mut result = Self {
            parser: Parser::from_config(&config)?,
            apps: Arc::new(Registry::scan(&config["aliases"], None)),
            activation: Activation::new(&config),
            config,
            muted,
            message: if muted {
                "Complete first-run microphone consent in Settings".into()
            } else {
                "Starting voice control".into()
            },
            worker: None,
            generation: 0,
            unanswered: None,
            level: json!(0),
            gate: Value::Null,
            engine: Value::Null,
            unsupported: json!([]),
            updates: watch::channel(json!({})).0,
            events,
            recording: Arc::new(Mutex::new(recording)),
            replay: Arc::new(Mutex::new(replay)),
            recording_updates,
            replay_updates,
            action_lock: Arc::new(Mutex::new(Desktop::default())),
            action_slots: Arc::new(Semaphore::new(16)),
            epoch: Arc::new(AtomicU64::new(0)),
            voice_pending: false,
            deferred_voice_end: None,
            voice_available: voice::available(),
            pending_captures: 0,
            last_recording: "idle".into(),
            last_replay: Value::Null,
            dictation: Default::default(),
            quit: false,
        };
        result.publish();
        Ok(result)
    }
    pub fn status(&self) -> Value {
        json!({"state":self.activation.state,"message":self.message,"muted":self.muted,"recording":*self.recording_updates.borrow(),"replay":*self.replay_updates.borrow(),"level":self.level,"voice_available":self.voice_available,"unsupported_phrases":self.unsupported,"engine":self.engine,"gate":self.gate,"dry_run":env::var("MURMUR_DRY_RUN").as_deref()==Ok("1"),"microphone_open":self.worker.is_some(),"mic_test":self.worker.as_ref().is_some_and(|w|w.mode=="test"),"dictation":self.dictation.status(),"controller":"rust"})
    }
    pub fn publish(&mut self) {
        self.updates
            .send_replace(json!({"ok":true,"status":self.status()}));
    }
    pub async fn stop_worker(&mut self) {
        self.generation += 1;
        self.epoch.fetch_add(1, Ordering::SeqCst);
        self.voice_pending = false;
        self.deferred_voice_end = None;
        self.unanswered = None;
        self.level = json!(0);
        self.engine = Value::Null;
        if let Some(mut worker) = self.worker.take() {
            worker.reader.abort();
            worker.input.take();
            let _ = util::stop(&mut worker.child, libc::SIGTERM, 3).await;
            let _ = worker.reader.await;
        }
    }
    pub async fn start_worker(&mut self, mode: &str) -> Result<()> {
        if self.muted || self.config["privacy"]["onboarded"] != true {
            return err("Microphone is muted. Complete consent and unmute in Settings.");
        }
        if mode == "wake_word" && self.config["activation"]["mode"] != "wake_word" {
            return Ok(());
        }
        self.stop_worker().await;
        self.message = "Opening microphone…".into();
        if mode == "once" {
            self.activation.set("idle");
        }
        let grammar = self.apps.grammar(&self.parser)?;
        let mut config = self.config.clone();
        if mode == "dictation" {
            // The session's language (from the phrase) wins over the setting.
            config["dictation"]["language"] = json!(self.dictation.language);
        }
        let (binary, payload) = voice::prepare(&config, mode, &self.parser, grammar)?;
        let mut encoded = serde_json::to_vec(&payload)?;
        encoded.push(b'\n');
        if encoded.len() > 1048576 {
            return err("Voice grammar exceeds the 1 MiB worker limit");
        }
        let mut command = util::owned_command(
            &[binary.to_string_lossy().into_owned()],
            libc::SIGTERM,
            false,
        )?;
        command
            .env("MURMUR_OWNER_PID", std::process::id().to_string())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null());
        let mut child = command.spawn()?;
        let mut input = child.stdin.take().ok_or("Missing voice control input")?;
        let stdout = child.stdout.take().ok_or("Missing voice events")?;
        if !matches!(
            timeout(Duration::from_secs(3), input.write_all(&encoded)).await,
            Ok(Ok(()))
        ) {
            let _ = child.kill().await;
            return err("Voice worker did not accept configuration");
        }
        let input = if mode == "wake_word" || mode == "dictation" {
            Some(input)
        } else {
            drop(input);
            None
        };
        let generation = self.generation;
        let tx = self.events.clone();
        let reader = tokio::spawn(async move {
            let mut reader = BufReader::new(stdout);
            let result = async {
                while let Some(bytes) = util::line(&mut reader, 65536).await? {
                    let event: Value = serde_json::from_slice(&bytes)?;
                    tx.send(Message::Voice(generation, event))
                        .await
                        .map_err(|_| "Controller is stopping")?;
                }
                Ok::<_, crate::Error>(())
            }
            .await;
            let detail = result
                .err()
                .map(|e| format!("Voice worker failed: {e}"))
                .unwrap_or_else(|| {
                    "Voice worker stopped unexpectedly. Check the microphone and choose Resume."
                        .into()
                });
            let _ = tx.send(Message::VoiceEnd(generation, detail)).await;
        });
        self.worker = Some(Worker {
            child,
            input,
            reader,
            mode: mode.into(),
        });
        self.publish();
        Ok(())
    }
    pub async fn set_mute(&mut self, muted: bool) -> Result<()> {
        if !muted && self.config["privacy"]["onboarded"] != true {
            return err("Complete first-run microphone consent in Settings");
        }
        self.muted = muted;
        if muted {
            self.end_dictation("Dictation stopped: microphone muted").await;
            self.activation.set("paused");
            self.stop_worker().await;
            self.message = "Microphone muted · audio capture closed".into();
        } else if self.config["activation"]["mode"] == "wake_word" {
            self.start_worker("wake_word").await?;
        } else {
            self.stop_worker().await;
            self.activation.set("paused");
            self.message = "Push-to-talk ready · microphone closed".into();
        }
        self.publish();
        Ok(())
    }
    async fn steer(&mut self, line: &[u8]) -> bool {
        if self.muted || self.unanswered.is_none() || self.activation.state == "error" {
            return false;
        }
        let Some(worker) = &mut self.worker else {
            return false;
        };
        if worker.mode != "wake_word" {
            return false;
        }
        let Some(input) = &mut worker.input else {
            return false;
        };
        self.epoch.fetch_add(1, Ordering::SeqCst);
        self.voice_pending = false;
        self.activation.set("idle");
        self.unanswered = Some(self.unanswered.unwrap_or(0) + 1);
        matches!(
            timeout(Duration::from_secs(2), input.write_all(line)).await,
            Ok(Ok(()))
        )
    }
    pub async fn event(&mut self, generation: u64, event: Value) -> Result<()> {
        if generation != self.generation || self.worker.is_none() {
            return Ok(());
        }
        let kind = event["event"]
            .as_str()
            .ok_or("Voice event has no event type")?;
        if self.unanswered.unwrap_or(0) > 0
            && ["wake", "command", "heard", "timeout"].contains(&kind)
        {
            return Ok(());
        }
        match kind {
            "loading" => {
                self.message = event["message"]
                    .as_str()
                    .ok_or("Invalid loading event")?
                    .into()
            }
            "ready" => {
                self.unanswered = Some(self.unanswered.unwrap_or(1).saturating_sub(1));
                self.unsupported = event.get("unsupported").cloned().unwrap_or(json!([]));
                self.engine = event["engine"].clone();
                self.activation.set("idle");
                if event["listening"] == true {
                    self.activation.listen(event["deadline"].as_f64())?;
                }
                self.message = if event["listening"] == true {
                    "Say a command".into()
                } else if self.worker.as_ref().is_some_and(|w| w.mode == "test") {
                    "Microphone test · ends in 15 seconds".into()
                } else {
                    format!(
                        "Waiting for “{}”",
                        self.config["activation"]["wake_phrase"].as_str().unwrap()
                    )
                };
            }
            "wake" => {
                self.activation.listen(event["deadline"].as_f64())?;
                self.message = "Listening · say one command".into();
            }
            "command" => {
                if self.activation.accept(&event) {
                    self.message = "Processing command…".into();
                    self.publish();
                    let text = event["text"].as_str().ok_or("Invalid command event")?;
                    let heard = if event["closest_match"] == true {
                        event["heard"]
                            .as_str()
                            .map(|s| (s.to_owned(), text.to_owned()))
                    } else {
                        None
                    };
                    self.voice_pending = true;
                    self.execute(text, None, Some(self.epoch.load(Ordering::SeqCst)), heard)
                        .await;
                } else {
                    self.message = "No confident match. Try again or adjust sensitivity.".into();
                    if !self.voice_pending {
                        self.finish_voice(self.epoch.load(Ordering::SeqCst));
                    }
                }
            }
            "heard" => {
                self.message = format!(
                    "Heard “{}”, which isn't a command I know. Try again.",
                    event["text"].as_str().unwrap_or("")
                )
            }
            "timeout" => {
                self.message = "No command heard. Try again.".into();
                if !self.voice_pending {
                    self.finish_voice(self.epoch.load(Ordering::SeqCst));
                }
            }
            "dictation" => {
                if event["state"] == "started" {
                    self.dictation.active = true;
                    self.activation.set("dictating");
                    self.message = "Dictation · speak, then say “stop dictation” or press Stop".into();
                    self.dictation.message = self.message.clone();
                } else {
                    let reason = match event["reason"].as_str().unwrap_or("") {
                        "idle" => "Dictation stopped after a quiet stretch",
                        "limit" => "Dictation stopped at its time limit",
                        "cancel" => "Dictation cancelled",
                        "error" => "Dictation could not start",
                        _ => "Dictation stopped",
                    };
                    let dropped = event["dropped"].as_u64().unwrap_or(0);
                    let mut message = reason.to_owned();
                    if dropped > 0 {
                        message.push_str(&format!(
                            " · {dropped} part(s) were skipped because transcription fell behind"
                        ));
                    }
                    // A refusal already ended the session with its own reason.
                    if self.dictation.active {
                        self.end_dictation(&message).await;
                    }
                    if self.activation.state == "dictating" {
                        self.activation.set(
                            if self.worker.as_ref().is_some_and(|w| w.mode == "wake_word") {
                                "idle"
                            } else {
                                "paused"
                            },
                        );
                    }
                }
            }
            "dictation_text" => {
                let text = event["text"].as_str().unwrap_or("").to_owned();
                self.dictated(text).await;
            }
            "dictation_error" => {
                self.dictation.message = format!(
                    "Dictation: {}",
                    event["message"].as_str().unwrap_or("transcription failed")
                );
                self.message = self.dictation.message.clone();
            }
            "level" => {
                self.level = event["level"].clone();
                self.gate = event["gate"].clone();
            }
            "error" => {
                self.activation.set("error");
                self.message = event["message"]
                    .as_str()
                    .unwrap_or("Voice worker failed")
                    .into();
            }
            _ => return err("Unrecognized voice event"),
        }
        self.publish();
        Ok(())
    }
    fn finish_voice(&mut self, generation: u64) {
        if !self.muted && self.epoch.load(Ordering::SeqCst) == generation {
            self.voice_pending = false;
            self.activation.set(
                if self.worker.as_ref().is_some_and(|w| w.mode == "wake_word") {
                    "idle"
                } else {
                    "paused"
                },
            );
        }
    }
    pub async fn voice_end(&mut self, generation: u64, message: String) {
        if generation != self.generation {
            return;
        }
        // A listen-once worker exits after emitting its result. Its valid,
        // accepted action must finish before replacing that worker; ordinary
        // EOF must not invalidate an action still waiting for the dispatcher.
        if self.voice_pending
            && self
                .worker
                .as_ref()
                .is_some_and(|w| w.mode == "once" || w.mode == "test")
        {
            self.deferred_voice_end = Some((generation, message));
            return;
        }
        let mode = self
            .worker
            .as_ref()
            .map(|w| w.mode.clone())
            .unwrap_or_default();
        self.stop_worker().await;
        if ["test", "once"].contains(&mode.as_str())
            && !self.muted
            && self.activation.state != "error"
            && self.config["activation"]["mode"] == "wake_word"
        {
            if let Err(e) = self.start_worker("wake_word").await {
                self.activation.set("error");
                self.message = e.to_string();
            }
        } else if self.activation.state != "error" {
            if mode == "wake_word" && !self.muted {
                self.activation.set("error");
                self.message = message;
            } else {
                self.activation.set("paused");
            }
        }
        self.publish();
    }
    pub fn completed_voice_end(&mut self) -> Option<(u64, String)> {
        if self.voice_pending {
            None
        } else {
            self.deferred_voice_end.take()
        }
    }
    pub async fn execute(
        &mut self,
        phrase: &str,
        reply: Option<Reply>,
        generation: Option<u64>,
        heard: Option<(String, String)>,
    ) {
        let parsed = self.parser.parse(phrase);
        let intent = match parsed {
            Ok(i) => i,
            Err(e) => {
                self.message = e.to_string();
                if let Some(g) = generation {
                    self.finish_voice(g);
                }
                self.publish();
                if let Some(reply) = reply {
                    let _ = reply.send(json!({"ok":false,"error":self.message}));
                }
                return;
            }
        };
        let action = intent["action"].as_str().unwrap().to_owned();
        let immediate = if env::var("MURMUR_DRY_RUN").as_deref() == Ok("1") {
            Some(Ok(format!("Dry run: {action}")))
        } else if action == "pause" {
            Some(
                self.set_mute(true)
                    .await
                    .map(|_| "Microphone muted. Resume in Murmur or from your shortcut.".into()),
            )
        } else if action == "cancel" {
            Some(Ok("Cancelled".into()))
        } else if action == "quit" {
            self.quit = true;
            Some(Ok("Closing Jarvis. Open it again from your app menu or with: jarvis".into()))
        } else if action == "dictation_start" {
            Some(
                self.start_dictation(Some(phrase))
                    .await
                    .map(|_| "Dictation starting · speak into the focused text field".into()),
            )
        } else if action == "dictation_stop" {
            self.stop_dictation(false).await;
            Some(Ok("Dictation stopped".into()))
        } else {
            None
        };
        if let Some(result) = immediate {
            self.complete(
                result.map_err(|e| e.to_string()),
                reply,
                generation,
                heard,
                false,
            );
            return;
        }
        let capture = action.starts_with("record_") || action.starts_with("replay_");
        if capture {
            self.pending_captures += 1;
        }
        let permit = match self.action_slots.clone().try_acquire_owned() {
            Ok(permit) => permit,
            Err(_) => {
                self.complete(
                    Err("Too many pending actions; try again shortly".into()),
                    reply,
                    generation,
                    heard,
                    capture,
                );
                return;
            }
        };
        let epoch = self.epoch.clone();
        let config = self.config.clone();
        let apps = self.apps.clone();
        let recording = self.recording.clone();
        let replay = self.replay.clone();
        let desktop = self.action_lock.clone();
        let tx = self.events.clone();
        tokio::spawn(async move {
            let _permit = permit;
            let mut desktop = desktop.lock().await;
            let cancelled = generation.is_some_and(|g| g != epoch.load(Ordering::SeqCst))
                || reply.as_ref().is_some_and(oneshot::Sender::is_closed);
            let result = if cancelled {
                Err("Cancelled".into())
            } else {
                dispatch(&intent, &config, &apps, &mut desktop, &recording, &replay)
                    .await
                    .map_err(|e| e.to_string())
            };
            let _ = tx
                .send(Message::ActionDone {
                    result,
                    reply,
                    generation,
                    heard,
                    capture,
                })
                .await;
        });
    }
    pub fn complete(
        &mut self,
        result: std::result::Result<String, String>,
        reply: Option<Reply>,
        generation: Option<u64>,
        heard: Option<(String, String)>,
        capture: bool,
    ) {
        if capture {
            self.pending_captures = self.pending_captures.saturating_sub(1);
        }
        // A command queued before mute, cancel or worker replacement must not
        // alter the newer session's state or feedback when it finishes.
        if generation.is_some_and(|g| g != self.epoch.load(Ordering::SeqCst)) {
            return;
        }
        let response = match result {
            Ok(message) => {
                self.message = message;
                json!({"ok":true,"message":self.message})
            }
            Err(error) => {
                self.message = error.clone();
                json!({"ok":false,"error":error})
            }
        };
        if let Some((heard, text)) = heard {
            self.message = format!("Heard “{heard}” → {text}. {}", self.message);
        }
        if let Some(g) = generation {
            self.finish_voice(g);
        }
        self.publish();
        if let Some(reply) = reply {
            let _ = reply.send(response);
        }
    }
    /// The running captures a save of `config` would change: "record" while
    /// recording, "replay" while replay is on. The caller stops them, saves
    /// and starts them again, so settings apply without stopping by hand.
    pub fn restarts_for(&self, config: &Value) -> Result<(Value, Vec<&'static str>)> {
        let c = config::validate(config)?;
        let recording_changed = c["recording"] != self.config["recording"];
        let mut restart = vec![];
        if self.recording_updates.borrow()["state"] == "recording" && recording_changed {
            restart.push("record");
        }
        if self.replay_updates.borrow()["state"] == "on"
            && (recording_changed
                || REPLAY_CAPTURE_KEYS
                    .iter()
                    .any(|key| c["replay"][*key] != self.config["replay"][*key]))
        {
            restart.push("replay");
        }
        Ok((c, restart))
    }
    pub async fn handle(&mut self, payload: &Value) -> Result<Value> {
        let op = payload["op"].as_str().ok_or("Unsupported operation")?;
        match op {
            "status" => {
                return Ok(
                    json!({"ok":true,"status":self.status(),"pid":std::process::id(),"memory":crate::memory::snapshot(std::process::id())}),
                )
            }
            "config" => return Ok(json!({"ok":true,"config":self.config})),
            "quit" => {
                self.quit = true;
                return Ok(json!({"ok":true,"message":"Closing Jarvis"}));
            }
            "commands" => {
                return Ok(
                    json!({"ok":true,"commands":policy::TABLES["REFERENCE"],"applications":self.apps.voice_commands(&self.parser)?}),
                )
            }
            "apps" => {
                let apps = Registry::scan(&self.config["aliases"], None);
                let changed = apps.index.keys().ne(self.apps.index.keys())
                    || apps.voice_commands(&self.parser)?
                        != self.apps.voice_commands(&self.parser)?;
                self.apps = Arc::new(apps);
                if changed
                    && self.worker.is_some()
                    && !self.muted
                    && self.config["activation"]["mode"] == "wake_word"
                {
                    self.start_worker("wake_word").await?;
                }
                let mut list = self.apps.list();
                if self.worker.is_some() {
                    let excluded = policy::strings(&self.unsupported);
                    for app in &mut list {
                        let mut names = vec![app["name"].as_str().unwrap().into()];
                        names.extend(policy::strings(&app["aliases"]));
                        app["speakable"] = json!(names
                            .iter()
                            .filter_map(|n| policy::normalize(n).ok())
                            .any(|n| !excluded.contains(&format!("open {n}"))));
                    }
                }
                return Ok(json!({"ok":true,"apps":list}));
            }
            "mute" => {
                self.set_mute(
                    payload["muted"]
                        .as_bool()
                        .ok_or("Mute must be true or false")?,
                )
                .await?
            }
            "listen" => {
                if !self.steer(b"listen\n").await {
                    self.start_worker("once").await?;
                }
            }
            "cancel" => {
                if !self.steer(b"cancel\n").await {
                    self.stop_worker().await;
                    self.activation.set("paused");
                    if !self.muted && self.config["activation"]["mode"] == "wake_word" {
                        self.start_worker("wake_word").await?;
                    } else {
                        self.message = "Cancelled · microphone closed".into();
                    }
                }
            }
            "dictation" => match payload["action"].as_str().unwrap_or("") {
                "start" => self.start_dictation(None).await?,
                "stop" => self.stop_dictation(false).await,
                "cancel" => self.stop_dictation(true).await,
                "toggle" if self.dictation.active => self.stop_dictation(false).await,
                "toggle" => self.start_dictation(None).await?,
                "copied" => self.dictation.held.clear(),
                _ => return err("Dictation action must be start, stop, cancel or toggle"),
            },
            "mic_test" => {
                if payload["enabled"] == true {
                    self.start_worker("test").await?;
                } else if self.worker.as_ref().is_some_and(|w| w.mode == "test") {
                    self.stop_worker().await;
                    if !self.muted && self.config["activation"]["mode"] == "wake_word" {
                        self.start_worker("wake_word").await?;
                    } else {
                        self.activation.set("paused");
                    }
                }
            }
            "save_config" => {
                let c = config::validate(&payload["config"])?;
                let recording_active = ["recording", "stopping"].contains(
                    &self.recording_updates.borrow()["state"]
                        .as_str()
                        .unwrap_or(""),
                );
                let replay_active = ["on", "stopping"]
                    .contains(&self.replay_updates.borrow()["state"].as_str().unwrap_or(""));
                if (recording_active || self.pending_captures > 0)
                    && c["recording"] != self.config["recording"]
                {
                    return err("Stop recording before changing capture settings");
                }
                if (replay_active || self.pending_captures > 0)
                    && (c["recording"] != self.config["recording"]
                        || REPLAY_CAPTURE_KEYS
                            .iter()
                            .any(|key| c["replay"][*key] != self.config["replay"][*key]))
                {
                    return err("Stop instant replay before changing capture settings");
                }
                let was_onboarded = self.config["privacy"]["onboarded"] == true;
                self.config = config::save(&c)?;
                self.parser = Parser::from_config(&c)?;
                self.apps = Arc::new(Registry::scan(&c["aliases"], None));
                self.activation.timeout = c["activation"]["command_timeout"].as_f64().unwrap();
                self.activation.confidence = c["activation"]["confidence"].as_f64().unwrap();
                self.set_mute(if c["privacy"]["onboarded"] != true {
                    true
                } else if was_onboarded {
                    self.muted
                } else {
                    false
                })
                .await?;
                return Ok(json!({"ok":true,"config":self.config}));
            }
            "open_ui" => {
                open_ui(if payload["page"] == "recording" {
                    "recording"
                } else {
                    "dashboard"
                })?;
            }
            _ => return err("Unsupported operation"),
        }
        self.publish();
        Ok(json!({"ok":true,"status":self.status()}))
    }
    /// Starts dictation in the running hands-free worker, or a dictation-only
    /// worker in push-to-talk mode. Text goes to the window focused now, or,
    /// when that is Jarvis's own window, to the next one focused. `trigger` is
    /// the spoken phrase, which can choose the language.
    pub async fn start_dictation(&mut self, trigger: Option<&str>) -> Result<()> {
        if self.muted || self.config["privacy"]["onboarded"] != true {
            return err("Microphone is muted. Complete consent and unmute in Settings.");
        }
        if self.dictation.active {
            return Ok(());
        }
        if self.config["recognition"]["accurate"] != true {
            return err("Dictation needs the speech model. Turn on Accurate recognition in Voice settings.");
        }
        let (typer, capabilities) = dictation::Typer::start().await?;
        if capabilities["input_method"] != true && capabilities["virtual_keyboard"] != true {
            typer.close().await;
            return err("This desktop offers no way to type into other apps (no Wayland input method or virtual keyboard)");
        }
        self.dictation = dictation::Session {
            active: true,
            id: self.dictation.id + 1,
            typer: Some(Arc::new(Mutex::new(typer))),
            window: dictation::focused()
                .await
                .filter(|(_, _, own)| !own)
                .map(|(address, _, _)| address),
            capabilities,
            message: "Starting dictation…".into(),
            language: dictation::language(&self.config, trigger).into(),
            indicator: dictation::indicator(),
            ..Default::default()
        };
        let steered = match &mut self.worker {
            Some(Worker {
                mode,
                input: Some(input),
                ..
            }) if mode == "wake_word" => matches!(
                timeout(
                    Duration::from_secs(2),
                    input.write_all(format!("dictate {}\n", self.dictation.language).as_bytes())
                )
                .await,
                Ok(Ok(()))
            ),
            _ => false,
        };
        if !steered {
            if let Err(e) = self.start_worker("dictation").await {
                self.end_dictation(&e.to_string()).await;
                return Err(e);
            }
        }
        self.publish();
        Ok(())
    }
    /// Ends dictation. Without `cancel`, speech already captured is still typed.
    pub async fn stop_dictation(&mut self, cancel: bool) {
        if !self.dictation.active {
            return;
        }
        let line: &[u8] = if cancel { b"cancel\n" } else { b"dictate_stop\n" };
        let sent = match &mut self.worker {
            Some(Worker {
                input: Some(input), ..
            }) => matches!(
                timeout(Duration::from_secs(2), input.write_all(line)).await,
                Ok(Ok(()))
            ),
            _ => false,
        };
        if !sent {
            self.end_dictation("Dictation stopped").await;
        }
        self.publish();
    }
    async fn end_dictation(&mut self, message: &str) {
        if let Some(typer) = self.dictation.typer.take() {
            // Queued insertions finish first; they hold the lock.
            tokio::spawn(async move {
                let typer = typer.lock().await;
                drop(typer);
            });
        }
        if self.dictation.active || !message.is_empty() {
            self.dictation.message = message.to_owned();
            self.message = message.to_owned();
        }
        self.dictation.active = false;
        self.dictation.window = None;
        // The bar also closes itself when it sees dictation end.
        if let Some(mut bar) = self.dictation.indicator.take() {
            tokio::spawn(async move {
                if timeout(Duration::from_secs(2), bar.wait()).await.is_err() {
                    let _ = bar.kill().await;
                }
            });
        }
    }
    async fn dictated(&mut self, text: String) {
        if !self.dictation.active {
            return;
        }
        let stops = policy::strings(&self.config["dictation"]["stop_phrases"]);
        let (text, stop) = dictation::split_stop(text.trim(), &stops);
        if !text.trim().is_empty() && self.dictation.window.is_none() {
            // Started from Jarvis's own window: type into the first other one.
            self.dictation.window = dictation::focused()
                .await
                .filter(|(_, _, own)| !own)
                .map(|(address, _, _)| address);
        }
        if !text.trim().is_empty() {
            if let Some(typer) = self.dictation.typer.clone() {
                // Separate segments with a space; never press Enter or Tab.
                let text = if self.dictation.typed_any {
                    format!(" {}", text.trim())
                } else {
                    text.trim().to_owned()
                };
                self.dictation.typed_any = true;
                let session = self.dictation.id;
                let window = self.dictation.window.clone();
                let keyboard = self.config["dictation"]["keyboard_fallback"] == true;
                let tx = self.events.clone();
                tokio::spawn(async move {
                    let mut typer = typer.lock().await;
                    let result = async {
                        let focused = dictation::focused().await;
                        if focused.as_ref().is_some_and(|f| f.2) {
                            return Ok(json!({"ok":false,"reason":"no-field","message":"Click into the app you want to type in; Jarvis doesn't type into its own window."}));
                        }
                        if window.is_some() && focused.as_ref().map(|f| &f.0) != window.as_ref() {
                            return Ok(json!({"ok":false,"reason":"focus","message":"Focus moved to another window, so dictation stopped without typing there."}));
                        }
                        let secret = focused.is_some_and(|f| f.1);
                        typer.insert(&text, keyboard && !secret).await
                    }
                    .await
                    .map_err(|e| e.to_string());
                    let _ = tx.send(Message::Typed { session, text, result }).await;
                });
            }
        }
        if stop {
            self.stop_dictation(false).await;
        }
    }
    /// Keeps text that was not typed and, unless it was meant for a password
    /// field, puts everything held this session on the clipboard.
    async fn hold(&mut self, text: &str, reason: &str, message: &str) -> String {
        self.dictation.held.push_str(text);
        let copied = reason != "sensitive" && dictation::copy(self.dictation.held.trim_start()).await;
        dictation::held_message(message, copied)
    }
    /// Outcome of one insertion. Refusals keep the text on the clipboard.
    pub async fn typed(&mut self, session: u64, text: String, result: std::result::Result<Value, String>) {
        if session != self.dictation.id {
            return;
        }
        match result {
            Ok(r) if r["ok"] == true => {
                self.dictation.inserted += text.chars().count();
                self.dictation.method = r["method"].as_str().unwrap_or("").to_owned();
            }
            Ok(r) => {
                let reason = r["reason"].as_str().unwrap_or("");
                let message = r["message"].as_str().unwrap_or("Dictation could not type the text");
                let message = self.hold(&text, reason, message).await;
                if ["focus", "sensitive"].contains(&reason) {
                    self.stop_dictation(true).await;
                    self.end_dictation(&message).await;
                } else {
                    self.dictation.message = message.clone();
                    self.message = message;
                }
            }
            Err(e) => {
                let message = format!("Dictation could not type the text: {e}");
                self.dictation.message = self.hold(&text, "error", &message).await;
                self.message = self.dictation.message.clone();
            }
        }
        self.publish();
    }
    pub fn capture_changed(&mut self) {
        let recording = self.recording_updates.borrow().clone();
        let replay = self.replay_updates.borrow().clone();
        let state = recording["state"].as_str().unwrap_or("");
        if state != self.last_recording
            && ["recording", "idle", "error"].contains(&state)
            && self.config["recording"]["sound_feedback"] == true
        {
            feedback(state);
        }
        self.last_recording = state.into();
        if !replay["file"].is_null()
            && replay["file"] != self.last_replay
            && self.config["replay"]["sound"] == true
        {
            feedback("clip");
        }
        self.last_replay = replay["file"].clone();
        self.publish();
    }
    pub fn voice_failed(&mut self, message: String) {
        self.activation.set("error");
        self.message = message;
        self.publish();
    }
    pub fn startup_done(
        &mut self,
        enabled: bool,
        result: std::result::Result<(), String>,
    ) -> Result<Value> {
        result.map_err(|e| -> crate::Error { e.into() })?;
        let mut updated = self.config.clone();
        updated["startup"] = json!(enabled);
        self.config = config::save(&updated)?;
        self.publish();
        Ok(json!({"ok":true,"status":self.status()}))
    }
    pub async fn shutdown(&mut self) {
        let _ = self.set_mute(true).await;
        let rec = self.recording.clone();
        let replay = self.replay.clone();
        let _ = timeout(Duration::from_secs(21), async {
            tokio::join!(
                async {
                    let _ = rec.lock().await.stop().await;
                },
                async {
                    let _ = replay.lock().await.stop().await;
                }
            );
        })
        .await;
    }
}
fn open_ui(page: &str) -> Result<()> {
    let argv = voice::ui_argv(page)?;
    let mut child = Command::new(&argv[0])
        .args(&argv[1..])
        .stdin(Stdio::null())
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()?;
    tokio::spawn(async move {
        let _ = child.wait().await;
    });
    Ok(())
}
async fn dispatch(
    intent: &Value,
    cfg: &Value,
    apps: &Registry,
    desktop: &mut Desktop,
    recording: &Arc<Mutex<Capture>>,
    replay: &Arc<Mutex<Capture>>,
) -> Result<String> {
    let action = intent["action"].as_str().ok_or("Invalid action")?;
    match action {
        "close_app" => {
            let app = apps.resolve(intent["target"].as_str().unwrap_or(""))?;
            let windows = desktop.running(app).await?;
            if windows.is_empty() {
                return err(format!("{} is not running.", app.name));
            }
            let mut addresses = std::collections::BTreeSet::new();
            for window in &windows {
                let address = window["address"].as_str().ok_or("Invalid window address")?;
                desktop::dispatch_args("close_address", &json!(address))?;
                addresses.insert(address);
            }
            for address in addresses {
                desktop.dispatch("close_address", &json!(address)).await?;
            }
            Ok(format!("Close requested for {}", app.name))
        }
        "open_app" | "focus_app" => {
            let app = apps.resolve(intent["target"].as_str().unwrap_or(""))?;
            let windows = match desktop.running(app).await {
                Ok(w) => w,
                Err(e) if action == "focus_app" => return Err(e),
                Err(_) => vec![],
            };
            if let Some(window) = windows.first() {
                desktop
                    .dispatch("focus_address", &window["address"])
                    .await?;
                return Ok(format!("Focused {}", app.name));
            }
            if action == "focus_app" {
                return err(format!(
                    "{} is not running. Say open {}.",
                    app.name, app.name
                ));
            }
            let launcher =
                util::which("gio").ok_or("GIO desktop launcher is missing (install glib2)")?;
            util::query(
                &util::desktop_argv(vec![
                    launcher.to_string_lossy().into(),
                    "launch".into(),
                    app.path.clone(),
                ])?,
                8,
            )
            .await?;
            let display = policy::spoken_numbers(&policy::normalize(&app.name).unwrap_or_default());
            let others: Vec<_> = apps
                .apps
                .values()
                .filter(|a| {
                    a.id != app.id
                        && policy::normalize(&a.name)
                            .is_ok_and(|n| policy::spoken_numbers(&n) == display)
                })
                .map(|a| a.id.as_str())
                .collect();
            Ok(if others.is_empty() {
                format!("Opened {}", app.name)
            } else {
                format!(
                    "Opened {} ({}). Also installed as {}; add an alias to choose another.",
                    app.name,
                    app.id,
                    others.join(", ")
                )
            })
        }
        "workspace" | "move_workspace" | "fullscreen" | "unfullscreen" => {
            desktop.dispatch(action, &intent["target"]).await?;
            Ok("Desktop updated".into())
        }
        "volume_down" | "volume_up" | "volume_mute" | "volume_unmute" => {
            desktop::volume(action).await?;
            Ok("Volume updated".into())
        }
        "record_start" | "record_stop" => {
            let mut capture = recording.lock().await;
            let status = if action == "record_start" {
                capture.start(cfg).await?
            } else {
                capture.stop().await?
            };
            Ok(status["message"].as_str().unwrap_or("").into())
        }
        "replay_start" | "replay_stop" | "replay_save" => {
            let mut capture = replay.lock().await;
            let status = match action {
                "replay_start" => capture.start(cfg).await?,
                "replay_stop" => capture.stop().await?,
                _ => capture.save(intent["target"].as_u64()).await?,
            };
            Ok(status["message"].as_str().unwrap_or("").into())
        }
        "open_recorder" => {
            open_ui("recording")?;
            Ok("Opened recording settings".into())
        }
        _ => err("Unsupported action"),
    }
}
fn feedback(state: &str) {
    let tones: &[f64] = match state {
        "recording" => &[880.],
        "idle" => &[660.],
        "clip" => &[880., 1320.],
        "error" => &[220.],
        _ => &[440.],
    };
    let Some(player) = util::which("pw-play") else {
        return;
    };
    let mut pcm = vec![];
    for &frequency in tones {
        let n = 2400;
        for i in 0..n {
            let envelope = (i as f64 / 120.).min((n - i) as f64 / 120.).min(1.);
            let sample = (1800.
                * envelope
                * (2. * std::f64::consts::PI * frequency * i as f64 / 24000.).sin())
                as i16;
            pcm.extend(sample.to_le_bytes());
        }
    }
    tokio::spawn(async move {
        let argv = vec![
            player.to_string_lossy().into(),
            "--raw".into(),
            "--rate".into(),
            "24000".into(),
            "--channels".into(),
            "1".into(),
            "--format".into(),
            "s16".into(),
            "-".into(),
        ];
        if let Ok(mut command) = util::owned_command(&argv, libc::SIGTERM, false) {
            command
                .stdin(Stdio::piped())
                .stdout(Stdio::null())
                .stderr(Stdio::null());
            if let Ok(mut child) = command.spawn() {
                if let Some(mut input) = child.stdin.take() {
                    let _ = timeout(Duration::from_secs(2), input.write_all(&pcm)).await;
                }
                if timeout(Duration::from_secs(3), child.wait()).await.is_err() {
                    let _ = child.kill().await;
                }
            }
        }
    });
}
