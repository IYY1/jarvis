//! Disk-only replay storage. The caller opts in before creating a session.
//! One root is exclusively locked; only marked, private stale sessions are removed.
use std::{
    collections::{BTreeMap, VecDeque},
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    path::{Path, PathBuf},
    time::{SystemTime, UNIX_EPOCH},
};
#[cfg(windows)]
mod windows;

fn error(message: impl Into<String>) -> io::Error {
    io::Error::other(message.into())
}

#[cfg(unix)]
fn private(path: &Path) -> io::Result<()> {
    use std::os::unix::fs::MetadataExt;
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.uid() != unsafe { libc::getuid() } || m.mode() & 0o077 != 0 {
        return Err(error(
            "Replay storage must be a user-owned private directory (0700).",
        ));
    }
    Ok(())
}

#[cfg(windows)]
fn private(path: &Path) -> io::Result<()> {
    use std::os::windows::fs::MetadataExt;
    // The Windows adapter applies a protected, user-only inheritable DACL
    // before launching this helper. Refuse junctions/reparse points here too.
    let m = fs::symlink_metadata(path)?;
    if !m.is_dir() || m.file_attributes() & 0x400 != 0 {
        return Err(error(
            "Replay storage cannot be a junction or reparse point.",
        ));
    }
    Ok(())
}

#[cfg(unix)]
fn new_private_dir(path: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    #[cfg(unix)]
    {
        use std::os::unix::fs::DirBuilderExt;
        builder.mode(0o700);
    }
    builder.create(path)
}

#[cfg(windows)]
fn new_private_dir(path: &Path) -> io::Result<()> {
    windows::directory(path)
}

#[cfg(windows)]
fn new_file(path: &Path) -> io::Result<File> {
    windows::file(path)
}

#[cfg(unix)]
fn new_file(path: &Path) -> io::Result<File> {
    let mut o = OpenOptions::new();
    o.read(true).write(true).create_new(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        o.mode(0o600)
            .custom_flags(libc::O_NOFOLLOW | libc::O_CLOEXEC);
    }
    o.open(path)
}

/// Fail closed on RAM filesystems. Windows accepts local NTFS/ReFS volumes;
/// third-party RAM disks that masquerade as fixed NTFS disks cannot be identified.
pub fn disk_space(path: &Path) -> io::Result<u64> {
    #[cfg(unix)]
    {
        use std::{ffi::CString, mem::MaybeUninit, os::unix::ffi::OsStrExt};
        let name = CString::new(path.as_os_str().as_bytes())?;
        let mut stat = MaybeUninit::<libc::statfs>::uninit();
        if unsafe { libc::statfs(name.as_ptr(), stat.as_mut_ptr()) } != 0 {
            return Err(io::Error::last_os_error());
        }
        let stat = unsafe { stat.assume_init() };
        if [0x0102_1994, 0x8584_58f6].contains(&(stat.f_type as u64)) {
            return Err(error(
                "Replay disk storage refuses tmpfs/ramfs. Choose a directory on a real disk.",
            ));
        }
        Ok(stat.f_bavail.saturating_mul(stat.f_bsize as u64))
    }
    #[cfg(windows)]
    {
        use std::{os::windows::ffi::OsStrExt, ptr};
        #[link(name = "kernel32")]
        extern "system" {
            fn GetVolumePathNameW(path: *const u16, volume: *mut u16, size: u32) -> i32;
            fn GetDriveTypeW(root: *const u16) -> u32;
            fn GetVolumeInformationW(
                root: *const u16,
                name: *mut u16,
                n: u32,
                serial: *mut u32,
                max: *mut u32,
                flags: *mut u32,
                fs: *mut u16,
                fs_n: u32,
            ) -> i32;
            fn GetDiskFreeSpaceExW(
                path: *const u16,
                available: *mut u64,
                total: *mut u64,
                free: *mut u64,
            ) -> i32;
        }
        let name: Vec<u16> = path.as_os_str().encode_wide().chain(Some(0)).collect();
        let mut root = vec![0; 32768];
        let mut format = [0; 64];
        let mut available = 0;
        unsafe {
            if GetVolumePathNameW(name.as_ptr(), root.as_mut_ptr(), root.len() as u32) == 0 {
                return Err(io::Error::last_os_error());
            }
            if ![2, 3].contains(&GetDriveTypeW(root.as_ptr()))
                || GetVolumeInformationW(
                    root.as_ptr(),
                    ptr::null_mut(),
                    0,
                    ptr::null_mut(),
                    ptr::null_mut(),
                    ptr::null_mut(),
                    format.as_mut_ptr(),
                    format.len() as u32,
                ) == 0
            {
                return Err(error("Replay disk storage needs a local NTFS/ReFS disk."));
            }
            let end = format.iter().position(|c| *c == 0).unwrap_or(format.len());
            if !["NTFS", "ReFS"].contains(&String::from_utf16_lossy(&format[..end]).as_str()) {
                return Err(error("Replay disk storage needs a local NTFS/ReFS disk."));
            }
            if GetDiskFreeSpaceExW(
                name.as_ptr(),
                &mut available,
                ptr::null_mut(),
                ptr::null_mut(),
            ) == 0
            {
                return Err(io::Error::last_os_error());
            }
        }
        Ok(available)
    }
}

pub struct Session {
    pub path: PathBuf,
    root: PathBuf,
    _lock: File,
}

impl Session {
    #[cfg(unix)]
    pub fn lock_fd(&self) -> std::os::fd::RawFd {
        use std::os::fd::AsRawFd;
        self._lock.as_raw_fd()
    }
    /// Parent must exist. An existing root must already be private; never chmod
    /// an arbitrary user folder. The dedicated root is not a saved-clip folder.
    pub fn create(root: &Path) -> io::Result<Self> {
        match new_private_dir(root) {
            Ok(()) => (),
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => (),
            Err(e) => return Err(e),
        }
        private(root)?;
        let root = fs::canonicalize(root)?;
        disk_space(&root)?;
        let lock_path = root.join(".lock");
        let lock = match new_file(&lock_path) {
            Ok(f) => f,
            Err(e) if e.kind() == io::ErrorKind::AlreadyExists => {
                let m = fs::symlink_metadata(&lock_path)?;
                if !m.is_file() || m.file_type().is_symlink() {
                    return Err(error("Invalid replay storage lock."));
                }
                #[cfg(unix)]
                {
                    use std::os::unix::fs::MetadataExt;
                    if m.uid() != unsafe { libc::getuid() } || m.mode() & 0o077 != 0 {
                        return Err(error("Replay storage lock is not private."));
                    }
                }
                OpenOptions::new().read(true).write(true).open(lock_path)?
            }
            Err(e) => return Err(e),
        };
        lock.try_lock().map_err(|_| {
            error("Replay storage is already in use; choose another buffer directory.")
        })?;
        for e in fs::read_dir(&root)? {
            let e = e?;
            let name = e.file_name();
            let name = name.to_string_lossy();
            if !name.starts_with("session-")
                || !name[8..].chars().all(|c| c.is_ascii_hexdigit() || c == '-')
            {
                continue;
            }
            let path = e.path();
            // Never follow links during crash cleanup or delete unmarked data.
            if private(&path).is_ok()
                && fs::symlink_metadata(path.join(".jarvis-replay-v1"))
                    .is_ok_and(|m| m.is_file() && !m.file_type().is_symlink())
            {
                fs::remove_dir_all(path)?;
            }
        }
        let tag = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_err(|e| error(e.to_string()))?
            .as_nanos();
        let path = root.join(format!("session-{:x}-{tag:x}", std::process::id()));
        new_private_dir(&path)?;
        new_file(&path.join(".jarvis-replay-v1"))?;
        Ok(Self {
            path,
            root,
            _lock: lock,
        })
    }

    pub fn usage(&self) -> io::Result<u64> {
        fn total(path: &Path, depth: usize) -> io::Result<u64> {
            if depth > 4 {
                return Err(error("Unexpected replay buffer directory structure."));
            }
            let mut bytes: u64 = 0;
            for e in fs::read_dir(path)? {
                let e = e?;
                let m = match fs::symlink_metadata(e.path()) {
                    Ok(m) => m,
                    Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                    Err(e) => return Err(e),
                };
                if m.file_type().is_symlink() {
                    return Err(error("Unexpected link in replay storage."));
                }
                let size = if m.is_dir() {
                    match total(&e.path(), depth + 1) {
                        Ok(n) => n,
                        Err(e) if e.kind() == io::ErrorKind::NotFound => continue,
                        Err(e) => return Err(e),
                    }
                } else {
                    m.len()
                };
                bytes = bytes
                    .checked_add(size)
                    .ok_or_else(|| error("Replay size overflow."))?;
            }
            Ok(bytes)
        }
        total(&self.path, 0)
    }

    pub fn available(&self) -> io::Result<u64> {
        disk_space(&self.root)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.path);
    }
}

struct Segment {
    path: PathBuf,
    bytes: u64,
    pins: usize,
    retained: bool,
}

pub struct Store {
    pub session: Session,
    seconds: usize,
    cap: u64,
    size: u64,
    next: u64,
    segments: BTreeMap<u64, Segment>,
    pending: BTreeMap<u64, ()>,
    history: VecDeque<u64>,
    snapshot: Option<Vec<u64>>,
}

fn write_segment(input: &mut impl Read, output: &mut impl Write, bytes: u64) -> io::Result<()> {
    let n = io::copy(&mut input.take(bytes), output)?;
    if n != bytes {
        return Err(error("Incomplete replay segment."));
    }
    output.flush()
}

impl Store {
    pub fn new(session: Session, seconds: usize, cap: u64) -> io::Result<Self> {
        if seconds == 0 || seconds > 600 || cap == 0 {
            return Err(error("Invalid replay storage limits."));
        }
        Ok(Self {
            session,
            seconds,
            cap,
            size: 0,
            next: 0,
            segments: BTreeMap::new(),
            pending: BTreeMap::new(),
            history: VecDeque::new(),
            snapshot: None,
        })
    }
    fn evict(&mut self) -> io::Result<bool> {
        let Some(id) = self.history.pop_front() else {
            return Ok(false);
        };
        self.segments.get_mut(&id).unwrap().retained = false;
        self.collect(id)?;
        Ok(true)
    }
    fn collect(&mut self, id: u64) -> io::Result<()> {
        let s = &self.segments[&id];
        if !s.retained && s.pins == 0 {
            fs::remove_file(&s.path)?;
            self.size -= s.bytes;
            self.segments.remove(&id);
        }
        Ok(())
    }
    /// Read straight to disk with 64 KiB scratch; never hold an encoded segment
    /// in the Rust heap. Pending arrivals and files pinned by a save share cap.
    pub fn append(&mut self, id: u64, bytes: u64, input: &mut impl Read) -> io::Result<()> {
        if id < self.next || id > self.next + 2 || self.segments.contains_key(&id) {
            return Err(error("Replay segment sequence is invalid."));
        }
        if bytes == 0 || bytes > self.cap || bytes > 64 * 1024 * 1024 {
            return Err(error(
                "Replay segment exceeds the storage budget; lower the bitrate.",
            ));
        }
        while self.size + bytes > self.cap {
            if !self.evict()? {
                return Err(error("Replay storage budget is occupied by a save or pending segments. Replay stopped safely; increase the limit or lower the bitrate."));
            }
        }
        if self.session.available()? < bytes.saturating_add(1024 * 1024) {
            return Err(error("Replay disk is full or has less than 1 MiB free after this segment. Replay stopped safely."));
        }
        let path = self.session.path.join(format!("segment-{id:020}.mkv"));
        let result = (|| {
            let mut file = new_file(&path)?;
            write_segment(input, &mut file, bytes)?;
            // Surface delayed-allocation/writeback errors before publishing a
            // completed segment, rather than discovering them during a save.
            file.sync_data()
        })();
        if let Err(e) = result {
            let _ = fs::remove_file(path);
            return Err(e);
        }
        self.size += bytes;
        self.segments.insert(
            id,
            Segment {
                path,
                bytes,
                pins: 0,
                retained: true,
            },
        );
        self.pending.insert(id, ());
        while self.pending.remove(&self.next).is_some() {
            self.history.push_back(self.next);
            self.next += 1;
            while self.history.len() > self.seconds {
                self.evict()?;
            }
        }
        Ok(())
    }
    pub fn snapshot(&mut self) -> io::Result<Vec<PathBuf>> {
        if self.snapshot.is_some() {
            return Err(error("A replay is already being saved."));
        }
        if self.history.is_empty() {
            return Err(error("Replay is warming up; wait for one full second."));
        }
        let ids: Vec<_> = self.history.iter().copied().collect();
        let mut paths = vec![];
        for id in &ids {
            let s = self.segments.get_mut(id).unwrap();
            s.pins += 1;
            paths.push(s.path.clone());
        }
        self.snapshot = Some(ids);
        Ok(paths)
    }
    pub fn release(&mut self) -> io::Result<()> {
        if let Some(ids) = self.snapshot.take() {
            for id in ids {
                self.segments.get_mut(&id).unwrap().pins -= 1;
                self.collect(id)?;
            }
        }
        Ok(())
    }
    pub fn bytes(&self) -> u64 {
        self.size
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn disk_full_and_flush_errors_are_propagated() {
        struct Full {
            flush_only: bool,
        }
        impl Write for Full {
            fn write(&mut self, bytes: &[u8]) -> io::Result<usize> {
                if self.flush_only {
                    Ok(bytes.len())
                } else {
                    Err(io::Error::from(io::ErrorKind::StorageFull))
                }
            }
            fn flush(&mut self) -> io::Result<()> {
                Err(io::Error::from(io::ErrorKind::StorageFull))
            }
        }
        for flush_only in [false, true] {
            let error = write_segment(&mut &b"video"[..], &mut Full { flush_only }, 5).unwrap_err();
            assert_eq!(error.kind(), io::ErrorKind::StorageFull);
        }
    }

    #[test]
    fn truncated_segment_is_never_accepted() {
        assert!(write_segment(&mut &b"abc"[..], &mut Vec::new(), 4).is_err());
    }

    #[test]
    fn copy_does_not_consume_the_next_protocol_message() {
        let mut input = &b"abcNEXT"[..];
        let mut output = Vec::new();
        write_segment(&mut input, &mut output, 3).unwrap();
        assert_eq!(output, b"abc");
        assert_eq!(input, b"NEXT");
    }
}
