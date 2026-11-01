//! Publish a completed GSR replay outside its disposable buffer before verify.
//! Never replace an existing clip. Cross-device copies are staged privately.
use crate::{err, Result};
use std::{
    fs::{self, OpenOptions},
    io,
    os::unix::fs::OpenOptionsExt,
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};

pub struct Retained {
    pub path: PathBuf,
    pub error: Option<String>,
}

fn link_unique(source: &Path, folder: &Path, filename: &str) -> io::Result<PathBuf> {
    let name = Path::new(filename);
    for index in 0..1000 {
        let target = if index == 0 {
            folder.join(name)
        } else {
            let stem = name.file_stem().unwrap_or_default().to_string_lossy();
            let extension = name.extension().unwrap_or_default().to_string_lossy();
            folder.join(format!("{stem}_{index}.{extension}"))
        };
        match fs::hard_link(source, &target) {
            Ok(()) => return Ok(target),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => continue,
            Err(e) => return Err(e),
        }
    }
    Err(io::Error::new(
        io::ErrorKind::AlreadyExists,
        "No unused replay filename after 1000 attempts",
    ))
}

fn publish(source: &Path, folder: &Path, filename: &str) -> io::Result<PathBuf> {
    let target = match link_unique(source, folder, filename) {
        Ok(path) => path,
        Err(e) if e.raw_os_error() == Some(libc::EXDEV) => {
            let stamp = SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .map_err(io::Error::other)?
                .as_nanos();
            let temporary = folder.join(format!(
                ".Replay-transfer-{}-{stamp}.part",
                std::process::id()
            ));
            let result = (|| {
                let mut output = OpenOptions::new()
                    .write(true)
                    .create_new(true)
                    .mode(0o600)
                    .open(&temporary)?;
                let mut input = OpenOptions::new()
                    .read(true)
                    .custom_flags(libc::O_NOFOLLOW)
                    .open(source)?;
                io::copy(&mut input, &mut output)?;
                output.sync_all()?;
                link_unique(&temporary, folder, filename)
            })();
            let _ = fs::remove_file(&temporary);
            result?
        }
        Err(e) => return Err(e),
    };
    fs::File::open(&target)?.sync_all()?;
    fs::File::open(folder)?.sync_all()?;
    // The destination is durable. A failed source unlink must never discard it;
    // normal session cleanup will retry removal of any remaining buffer copy.
    let _ = fs::remove_file(source);
    Ok(target)
}

pub fn retain(source: &Path, folder: &Path, session: &Path) -> Result<Retained> {
    if source.parent() != Some(session)
        || !fs::symlink_metadata(source).is_ok_and(|m| m.is_file() && !m.file_type().is_symlink())
    {
        return err("Recorder returned a replay file outside its owned buffer session");
    }
    let filename = source
        .file_name()
        .ok_or("Recorder returned an invalid replay filename")?
        .to_string_lossy();
    match publish(source, folder, &filename) {
        Ok(path) => Ok(Retained { path, error: None }),
        Err(error) => {
            // A full/unavailable clip destination should not leave a completed
            // save in a session that Stop or pressure cleanup will delete.
            let recovery = session.parent().ok_or("Invalid replay session parent")?;
            let tag = session.file_name().unwrap_or_default().to_string_lossy();
            let name = format!("Recovered-{tag}-{filename}");
            match publish(source, recovery, &name) {
                Ok(path) => Ok(Retained {
                    error: Some(format!(
                        "Could not publish replay in Clips: {error}. File retained at {}.",
                        path.display()
                    )),
                    path,
                }),
                Err(recovery_error) => err(format!(
                    "Could not retain replay: {error}; recovery failed: {recovery_error}. Temporary source: {}",
                    source.display()
                )),
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn failed_clip_destination_retains_save_outside_session_cleanup() {
        let stamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_nanos();
        let root = std::env::temp_dir().join(format!("jarvis-clip-{}-{stamp}", std::process::id()));
        let session = root.join("session-test");
        fs::create_dir_all(&session).unwrap();
        let source = session.join("Replay_test.mp4");
        fs::write(&source, b"completed replay").unwrap();
        let saved = retain(&source, &root.join("missing-clips"), &session).unwrap();
        assert!(saved.error.is_some());
        assert_eq!(saved.path.parent(), Some(root.as_path()));
        fs::remove_dir_all(session).unwrap();
        assert_eq!(fs::read(saved.path).unwrap(), b"completed replay");
        fs::remove_dir_all(root).unwrap();
    }
}
