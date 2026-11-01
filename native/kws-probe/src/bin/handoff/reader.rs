//! Bounded child stdout decoding with explicit cancellation and join ownership.
use super::*;
use std::process::ChildStdout;
use std::sync::atomic::AtomicUsize;

static ACTIVE: AtomicUsize = AtomicUsize::new(0);
static SPAWNED: AtomicUsize = AtomicUsize::new(0);
static JOINED: AtomicUsize = AtomicUsize::new(0);

pub(super) fn counters() -> Value {
    json!({"active":ACTIVE.load(Ordering::Relaxed),"spawned":SPAWNED.load(Ordering::Relaxed),
        "joined":JOINED.load(Ordering::Relaxed)})
}

struct Active;
impl Drop for Active {
    fn drop(&mut self) {
        ACTIVE.fetch_sub(1, Ordering::Relaxed);
    }
}

pub(super) struct Reader {
    stop: Arc<AtomicBool>,
    thread: Option<thread::JoinHandle<()>>,
    join: bool,
}
impl Reader {
    pub fn spawn(
        mut output: ChildStdout,
        send: mpsc::SyncSender<std::result::Result<Value, String>>,
        join: bool,
    ) -> Self {
        let stop = Arc::new(AtomicBool::new(false));
        let cancel = stop.clone();
        let thread = thread::spawn(move || {
            ACTIVE.fetch_add(1, Ordering::Relaxed);
            let _active = Active;
            let mut line = Vec::new();
            let mut bytes = [0u8; 8192];
            while !cancel.load(Ordering::Relaxed) {
                #[cfg(unix)]
                {
                    use std::os::fd::AsRawFd;
                    let mut fd = libc::pollfd {
                        fd: output.as_raw_fd(),
                        events: libc::POLLIN,
                        revents: 0,
                    };
                    // Read fixed chunks after readiness; never block waiting for
                    // a newline held open by an unrelated/inherited pipe writer.
                    let ready = unsafe { libc::poll(&mut fd, 1, 100) };
                    if ready == 0 {
                        continue;
                    }
                    if ready < 0 {
                        let error = std::io::Error::last_os_error();
                        if error.kind() == std::io::ErrorKind::Interrupted {
                            continue;
                        }
                        let _ = send.send(Err(error.to_string()));
                        break;
                    }
                }
                match output.read(&mut bytes) {
                    Ok(0) => {
                        if !line.is_empty() {
                            let _ =
                                send.send(serde_json::from_slice(&line).map_err(|e| e.to_string()));
                        }
                        break;
                    }
                    Ok(n) => {
                        for byte in &bytes[..n] {
                            if line.len() == 1048576 {
                                let _ = send.send(Err("Oversized child event".into()));
                                return;
                            }
                            line.push(*byte);
                            if *byte == b'\n' {
                                let result =
                                    serde_json::from_slice(&line).map_err(|e| e.to_string());
                                let failed = result.is_err();
                                if send.send(result).is_err() || failed {
                                    return;
                                }
                                line.clear();
                            }
                        }
                    }
                    Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
                    Err(error) => {
                        let _ = send.send(Err(error.to_string()));
                        break;
                    }
                }
            }
        });
        SPAWNED.fetch_add(1, Ordering::Relaxed);
        Self {
            stop,
            thread: Some(thread),
            join,
        }
    }
    // Caller MUST disconnect the bounded event receiver before invoking this:
    // a producer blocked on a full channel must be able to leave send().
    pub fn close(&mut self) -> Result<()> {
        self.stop.store(true, Ordering::Relaxed);
        if let Some(thread) = self.thread.take() {
            if self.join {
                thread.join().map_err(|_| "Child event reader panicked")?;
                JOINED.fetch_add(1, Ordering::Relaxed);
            }
        }
        Ok(())
    }
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::os::fd::{FromRawFd, OwnedFd};
    fn pipe() -> (ChildStdout, File) {
        let mut fds = [0; 2];
        assert_eq!(unsafe { libc::pipe(fds.as_mut_ptr()) }, 0);
        unsafe {
            (
                ChildStdout::from(OwnedFd::from_raw_fd(fds[0])),
                File::from(OwnedFd::from_raw_fd(fds[1])),
            )
        }
    }
    #[test]
    fn cancels_an_unterminated_line_even_with_a_live_pipe_writer() {
        let (output, mut writer) = pipe();
        let (send, events) = mpsc::sync_channel(1);
        let mut reader = Reader::spawn(output, send, true);
        writer.write_all(b"{\"ready\":true}\npartial").unwrap();
        assert_eq!(
            events
                .recv_timeout(Duration::from_secs(1))
                .unwrap()
                .unwrap()["ready"],
            true
        );
        thread::sleep(Duration::from_millis(30));
        drop(events);
        let start = Instant::now();
        reader.close().unwrap();
        assert!(start.elapsed() < Duration::from_secs(1));
        assert!(reader.thread.is_none());
        drop(writer);
    }
    #[test]
    fn disconnecting_a_full_channel_unblocks_join_and_the_pipe_writer() {
        let (output, mut writer) = pipe();
        let (send, events) = mpsc::sync_channel(1);
        let mut reader = Reader::spawn(output, send, true);
        let writer = thread::spawn(move || {
            let _ = writer.write_all(b"{}\n".repeat(100000).as_slice());
        });
        thread::sleep(Duration::from_millis(30));
        drop(events);
        let start = Instant::now();
        reader.close().unwrap();
        writer.join().unwrap();
        assert!(start.elapsed() < Duration::from_secs(1));
    }
    #[test]
    fn accepts_complete_final_json_at_eof_without_a_newline() {
        let (output, mut writer) = pipe();
        let (send, events) = mpsc::sync_channel(1);
        let mut reader = Reader::spawn(output, send, true);
        writer.write_all(b"{\"final\":true}").unwrap();
        drop(writer);
        assert_eq!(
            events
                .recv_timeout(Duration::from_secs(1))
                .unwrap()
                .unwrap()["final"],
            true
        );
        drop(events);
        reader.close().unwrap();
    }
}
