//! Supervisor for the external GSR backend. GSR owns its files; never unlink
//! live buffer files or disturb a save. Stop capture on pressure or write errors.
use crate::{err, util, Result};
use jarvis_replay_store::Session;
use serde_json::Value;
use std::{env, path::PathBuf};

/// GSR 6.1.5 `replay_buffer_disk.c` writes one file until it reaches 256 MiB
/// and removes the oldest only once the next file covers the replay length.
/// A smaller budget is always reached at the first file rollover.
const GSR_FILE_MB: u64 = 256;
const DISK_MINIMUM_MB: u64 = 2 * GSR_FILE_MB;

pub struct Budget {
    pub disk: Option<Session>,
    pub cap: u64,
}
impl Budget {
    /// RAM mode has nothing to supervise but write errors: GSR has no RAM byte
    /// cap, and its RSS includes encoder and allocator overhead, so stopping
    /// at a fixed RSS would end replay instead of shortening history.
    pub fn new(cfg: &Value) -> Result<Self> {
        let mb = cfg["replay"]["max_mb"].as_u64().unwrap_or(256);
        let disk = if cfg["replay"]["storage"] == "disk" {
            if mb < DISK_MINIMUM_MB {
                return err(format!("Disk replay needs a budget of at least {DISK_MINIMUM_MB} MiB: gpu-screen-recorder writes {GSR_FILE_MB} MiB buffer files and keeps the previous one until the next covers the replay length."));
            }
            let configured = cfg["replay"]["buffer_dir"].as_str().unwrap_or("");
            let root = if configured.is_empty() {
                env::var_os("XDG_DATA_HOME")
                    .map(PathBuf::from)
                    .unwrap_or_else(|| util::home().join(".local/share"))
                    .join("murmur/replay-buffer")
            } else {
                util::expand(configured)
            };
            util::private_dir(root.parent().ok_or("Invalid replay buffer directory")?)?;
            Some(Session::create(&root)?)
        } else {
            None
        };
        Ok(Self {
            disk,
            cap: mb * 1024 * 1024,
        })
    }
    pub fn check(&self) -> Result<()> {
        if let Some(session) = &self.disk {
            if session.available()? < 16 * 1024 * 1024 {
                return err("Replay stopped safely: the buffer disk has less than 16 MiB free.");
            }
            if session.usage()? >= self.cap {
                return err("Replay stopped safely: the disk buffer reached its size budget. Increase the budget, shorten replay or lower the bitrate.");
            }
        }
        Ok(())
    }
}
