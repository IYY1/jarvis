//! Shared, bounded Unix-socket transport. No service or microphone is started.
use crate::{err, util, Result};
use serde_json::Value;
use std::{env, path::PathBuf};
use tokio::{
    io::{AsyncWriteExt, BufReader},
    net::UnixStream,
    time::{timeout, Duration},
};

pub fn socket_path() -> PathBuf {
    env::var_os("MURMUR_SOCKET")
        .map(PathBuf::from)
        .unwrap_or_else(|| {
            env::var_os("XDG_RUNTIME_DIR")
                .map(PathBuf::from)
                .map(|p| p.join("murmur"))
                .unwrap_or_else(|| {
                    PathBuf::from(format!("/tmp/murmur-{}", unsafe { libc::getuid() }))
                })
                .join("control.sock")
        })
}
pub async fn connect() -> Result<UnixStream> {
    let stream = UnixStream::connect(socket_path()).await?;
    if stream.peer_cred()?.uid() != unsafe { libc::getuid() } {
        return err("Service belongs to another user");
    }
    Ok(stream)
}
pub fn seconds(op: &str) -> u64 {
    match op {
        "record_stop" | "execute" | "replay_save" => 45,
        "record_start" | "replay_start" | "apps" => 20,
        "save_config" => 30,
        "set_startup" => 15,
        _ => 12,
    }
}
pub async fn request(payload: &Value) -> Result<Value> {
    let seconds = seconds(payload["op"].as_str().unwrap_or(""));
    timeout(Duration::from_secs(seconds), async {
        let mut bytes = serde_json::to_vec(payload)?;
        bytes.push(b'\n');
        if bytes.len() > 65536 {
            return err("Request exceeds 64 KiB");
        }
        let mut stream = connect().await?;
        stream.write_all(&bytes).await?;
        let bytes = util::line(&mut BufReader::new(stream), 1048576)
            .await?
            .ok_or("Invalid response from Jarvis service")?;
        let response: Value = serde_json::from_slice(&bytes)?;
        if !response.is_object() {
            return err("Invalid response from Jarvis service");
        }
        Ok(response)
    })
    .await
    .map_err(|_| format!("Jarvis service did not respond within {seconds} seconds"))?
}
