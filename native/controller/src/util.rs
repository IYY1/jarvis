use crate::{err, Result};
use std::{
    env, fs,
    os::unix::fs::{DirBuilderExt, OpenOptionsExt, PermissionsExt},
    path::{Path, PathBuf},
    process::Stdio,
};
use tokio::{
    io::{AsyncBufRead, AsyncBufReadExt, AsyncReadExt},
    process::{Child, Command},
    time::{timeout, Duration},
};

pub fn home() -> PathBuf {
    env::var_os("HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| PathBuf::from("/"))
}
pub fn expand(text: &str) -> PathBuf {
    if text == "~" {
        home()
    } else if let Some(rest) = text.strip_prefix("~/") {
        home().join(rest)
    } else {
        PathBuf::from(text)
    }
}
pub fn which(name: &str) -> Option<PathBuf> {
    let executable = |p: &Path| {
        p.is_file() && fs::metadata(p).is_ok_and(|m| m.permissions().mode() & 0o111 != 0)
    };
    if name.contains('/') {
        return executable(Path::new(name)).then(|| PathBuf::from(name));
    }
    env::split_paths(&env::var_os("PATH").unwrap_or_default())
        .map(|p| p.join(name))
        .find(|p| executable(p))
}
pub fn private_dir(path: &Path) -> Result<()> {
    fs::DirBuilder::new()
        .recursive(true)
        .mode(0o700)
        .create(path)?;
    Ok(())
}
pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    use std::io::Write;
    let parent = path
        .parent()
        .ok_or("Configuration has no parent directory")?;
    private_dir(parent)?;
    let temporary = parent.join(format!(
        ".config-{}-{}",
        std::process::id(),
        now().to_bits()
    ));
    let result = (|| {
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temporary)?;
        file.write_all(bytes)?;
        file.sync_all()?;
        fs::rename(&temporary, path)?;
        fs::File::open(parent)?.sync_all()?;
        Ok(())
    })();
    let _ = fs::remove_file(temporary);
    result
}
pub fn now() -> f64 {
    let mut t = libc::timespec {
        tv_sec: 0,
        tv_nsec: 0,
    };
    unsafe {
        libc::clock_gettime(libc::CLOCK_MONOTONIC, &mut t);
    }
    t.tv_sec as f64 + t.tv_nsec as f64 / 1e9
}
pub async fn line<R: AsyncBufRead + Unpin>(
    reader: &mut R,
    limit: usize,
) -> Result<Option<Vec<u8>>> {
    let mut bytes = vec![];
    let n = reader
        .take((limit + 1) as u64)
        .read_until(b'\n', &mut bytes)
        .await?;
    if bytes.len() > limit {
        return err("Input exceeds its size limit");
    }
    Ok((n != 0).then_some(bytes))
}
pub fn owned_command(argv: &[String], signal: i32, session: bool) -> Result<Command> {
    let program = argv.first().ok_or("Empty process arguments")?;
    let mut cmd = Command::new(program);
    cmd.args(&argv[1..]).stdin(Stdio::null()).kill_on_drop(true);
    let parent = std::process::id() as i32;
    // The current-thread runtime always spawns owned children on its long-lived
    // main OS thread. Only async-signal-safe libc calls occur after fork.
    unsafe {
        cmd.pre_exec(move || {
            if libc::prctl(libc::PR_SET_PDEATHSIG, signal) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::getppid() != parent {
                libc::_exit(1);
            }
            if session && libc::setsid() == -1 {
                return Err(std::io::Error::last_os_error());
            }
            libc::umask(0o077);
            Ok(())
        });
    }
    Ok(cmd)
}
pub async fn query(argv: &[String], seconds: u64) -> Result<String> {
    let mut cmd = owned_command(argv, libc::SIGTERM, false)?;
    cmd.stdout(Stdio::piped()).stderr(Stdio::piped());
    let mut child = cmd.spawn()?;
    let stdout = child.stdout.take().ok_or("Missing stdout")?;
    let stderr = child.stderr.take().ok_or("Missing stderr")?;
    let result = timeout(Duration::from_secs(seconds), async {
        let read = |stream| async move {
            let mut bytes = vec![];
            tokio::io::AsyncReadExt::take(stream, 8 * 1024 * 1024 + 1)
                .read_to_end(&mut bytes)
                .await?;
            Ok::<_, std::io::Error>(bytes)
        };
        // Type-erased streams share one bounded reader implementation.
        let out: Box<dyn tokio::io::AsyncRead + Send + Unpin> = Box::new(stdout);
        let errors: Box<dyn tokio::io::AsyncRead + Send + Unpin> = Box::new(stderr);
        let (out, errors, status) = tokio::join!(read(out), read(errors), child.wait());
        Ok::<_, crate::Error>((out?, errors?, status?))
    })
    .await;
    match result {
        Ok(Ok((out, errors, status))) => {
            if out.len() > 8 * 1024 * 1024 || errors.len() > 8 * 1024 * 1024 {
                return err("Process output exceeds its size limit");
            }
            if !status.success() {
                return err(format!(
                    "{} failed: {}",
                    argv[0],
                    String::from_utf8_lossy(if errors.is_empty() { &out } else { &errors })
                        .trim()
                        .chars()
                        .take(1000)
                        .collect::<String>()
                ));
            }
            Ok(String::from_utf8(out)?)
        }
        other => {
            let _ = child.kill().await;
            match other {
                Ok(Err(e)) => Err(e),
                _ => err(format!(
                    "{} did not finish within {seconds} seconds",
                    argv[0]
                )),
            }
        }
    }
}
pub async fn stop(child: &mut Child, signal: i32, seconds: u64) -> Result<bool> {
    if let Some(pid) = child.id() {
        unsafe {
            libc::kill(pid as i32, signal);
        }
    }
    if timeout(Duration::from_secs(seconds), child.wait())
        .await
        .is_ok()
    {
        return Ok(false);
    }
    if let Some(pid) = child.id() {
        unsafe {
            libc::kill(pid as i32, libc::SIGTERM);
        }
    }
    if timeout(Duration::from_secs(2), child.wait()).await.is_err() {
        child.kill().await?;
    }
    Ok(true)
}
pub fn desktop_argv(argv: Vec<String>) -> Result<Vec<String>> {
    if env::var("JARVIS_MANAGED_SERVICE").as_deref() != Ok("1") {
        return Ok(argv);
    }
    let runner = which("systemd-run")
        .ok_or("systemd-run is required to launch desktop apps from the installed user service")?;
    let mut result = vec![
        runner.to_string_lossy().into_owned(),
        "--user".into(),
        "--scope".into(),
        "--collect".into(),
        "--quiet".into(),
        "--expand-environment=no".into(),
        "--".into(),
    ];
    result.extend(argv);
    Ok(result)
}
