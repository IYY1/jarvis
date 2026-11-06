//! Explicit, pinned model installation. Discovery and diagnostics never download.
use jarvis_controller::{err, util, Result};
use sha2::{Digest, Sha256};
use std::{
    collections::HashSet,
    env,
    ffi::CString,
    fs::{self, File, OpenOptions},
    io::{Read, Write},
    os::unix::{
        ffi::OsStrExt,
        fs::{DirBuilderExt, OpenOptionsExt},
    },
    path::{Component, Path, PathBuf},
    time::Duration,
};

pub const VOSK_NAME: &str = "vosk-model-small-en-us-0.15";
pub const VOSK_FILES: &[&str] = &[
    "am/final.mdl",
    "conf/model.conf",
    "graph/Gr.fst",
    "graph/HCLr.fst",
];
pub const WHISPER_NAME: &str = "sherpa-onnx-whisper-small.en";
pub const WHISPER_FILES: &[&str] = &[
    "small.en-encoder.int8.onnx",
    "small.en-decoder.int8.onnx",
    "small.en-tokens.txt",
];
pub const WHISPER_TINY_NAME: &str = "sherpa-onnx-whisper-tiny.en";
pub const WHISPER_TINY_FILES: &[&str] = &[
    "tiny.en-encoder.int8.onnx",
    "tiny.en-decoder.int8.onnx",
    "tiny.en-tokens.txt",
];
pub const DENOISER_NAME: &str = "gtcrn_simple.onnx";
/// Moonshine Streaming Tiny Arabic, exported by scripts/export_moonshine_arabic.py.
pub const ARABIC_NAME: &str = "moonshine-streaming-tiny-ar";
pub const ARABIC_FILES: &[&str] = &["encoder.onnx", "cross.onnx", "decoder.onnx", "tokenizer.json"];
const ARABIC_SHA: &str = "df959ea0723c07733ac5996f0f7580a8bdf5612f09caf381332a7debd946cfd5";
/// The published archive (release models-arabic-1 on GitHub);
/// `--archive` installs a local copy with the same checksum instead.
const ARABIC_URL: Option<&str> = Some("https://github.com/abdullaho21/jarvis/releases/download/models-arabic-1/moonshine-streaming-tiny-ar.tar.bz2");
const VOSK_SHA: &str = "30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498";
const WHISPER_SHA: &str = "0cdba2b8aaab69e04847f3427cc9709574112e67913a1a84b7fec3a8729faa9a";
const WHISPER_TINY_SHA: &str = "2bd6cf965c8bb3e068ef9fa2191387ee63a9dfa2a4e37582a8109641c20005dd";

/// Folder and files of the command model chosen in settings ("small" or "light").
pub fn whisper(speech_model: &str) -> (&'static str, &'static [&'static str]) {
    if speech_model == "light" {
        (WHISPER_TINY_NAME, WHISPER_TINY_FILES)
    } else {
        (WHISPER_NAME, WHISPER_FILES)
    }
}
const DENOISER_SHA: &str = "e77603ac0c23dac3227dd2d7135b3a585cbee2679048aecfa886657d3ae1b534";
const MIB: u64 = 1024 * 1024;

pub fn root() -> PathBuf {
    env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| util::home().join(".local/share"))
        .join("jarvis/models")
}
pub fn valid(path: &Path, files: &[&str]) -> bool {
    path.is_dir() && files.iter().all(|f| path.join(f).is_file())
}
fn exists(path: &Path) -> bool {
    fs::symlink_metadata(path).is_ok()
}
fn ready(path: &Path, files: &[&str]) -> Result<bool> {
    if !exists(path) {
        return Ok(false);
    }
    if !path.is_symlink() && valid(path, files) {
        return Ok(true);
    }
    err(format!(
        "Incomplete or linked model at {}; review it before removing or replacing it",
        path.display()
    ))
}

struct Stage(PathBuf);
impl Stage {
    fn new(parent: &Path) -> Result<Self> {
        util::private_dir(parent)?;
        for attempt in 0..10 {
            let path = parent.join(format!(
                ".model-{}-{}-{attempt}",
                std::process::id(),
                util::now().to_bits()
            ));
            match fs::DirBuilder::new().mode(0o700).create(&path) {
                Ok(()) => return Ok(Self(path)),
                Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => continue,
                Err(e) => return Err(e.into()),
            }
        }
        err("Cannot create private model staging directory")
    }
}
impl Drop for Stage {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}
fn create(path: &Path) -> Result<File> {
    util::private_dir(path.parent().ok_or("Missing file parent")?)?;
    Ok(OpenOptions::new()
        .write(true)
        .create_new(true)
        .mode(0o600)
        .open(path)?)
}
fn copy_verified(mut input: impl Read, target: &Path, sha: &str, limit: u64) -> Result<()> {
    let mut output = create(target)?;
    let mut digest = Sha256::new();
    let mut total = 0u64;
    let mut buffer = [0u8; 65536];
    loop {
        let n = input.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        total += n as u64;
        if total > limit {
            return err("Model download/archive exceeds its size limit");
        }
        digest.update(&buffer[..n]);
        output.write_all(&buffer[..n])?;
    }
    if format!("{:x}", digest.finalize()) != sha {
        return err("Model checksum mismatch; nothing was installed");
    }
    output.sync_all()?;
    Ok(())
}
fn download(url: &str, target: &Path, sha: &str, limit: u64) -> Result<()> {
    let agent: ureq::Agent = ureq::Agent::config_builder()
        .https_only(true)
        .timeout_connect(Some(Duration::from_secs(30)))
        .timeout_per_read(Some(Duration::from_secs(60)))
        .timeout_global(Some(Duration::from_secs(3600)))
        .max_redirects(10)
        .user_agent("Jarvis-native-model-setup/0.1")
        .build()
        .into();
    let mut response = agent.get(url).call()?;
    if response.body().content_length().is_some_and(|n| n > limit) {
        return err("Model download exceeds its size limit");
    }
    copy_verified(response.body_mut().as_reader(), target, sha, limit)
}
fn safe_path(name: &str, prefix: &str) -> Result<PathBuf> {
    let path = Path::new(name);
    if name.contains(['\0', '\\'])
        || path
            .components()
            .any(|part| !matches!(part, Component::Normal(_)))
        || path.components().next() != Some(Component::Normal(prefix.as_ref()))
    {
        return err("Invalid model archive path");
    }
    Ok(path.to_path_buf())
}
fn bounded_file(input: &mut impl Read, target: &Path, remaining: &mut u64) -> Result<()> {
    let mut output = create(target)?;
    let mut buffer = [0u8; 65536];
    loop {
        let n = input.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        if n as u64 > *remaining {
            return err("Expanded model exceeds its size limit");
        }
        *remaining -= n as u64;
        output.write_all(&buffer[..n])?;
    }
    output.sync_all()?;
    Ok(())
}
fn unpack_zip(archive: &Path, stage: &Path) -> Result<PathBuf> {
    let mut zip = zip::ZipArchive::new(File::open(archive)?)?;
    if zip.len() > 128 {
        return err("Too many model archive entries");
    }
    let mut names = HashSet::new();
    let mut remaining = 256 * MIB;
    for i in 0..zip.len() {
        let mut entry = zip.by_index(i)?;
        let name = entry.name()?.into_owned();
        let path = safe_path(&name, VOSK_NAME)?;
        let special = entry
            .unix_mode()
            .is_some_and(|mode| !matches!(mode & 0o170000, 0 | 0o100000 | 0o040000));
        if special
            || !names.insert(path.clone())
            || entry.is_symlink()
            || (!entry.is_dir() && !entry.is_file())
        {
            return err("Model archive contains duplicate paths or links");
        }
        if entry.size() > remaining {
            return err("Expanded model exceeds its size limit");
        }
        let target = stage.join(path);
        if entry.is_dir() {
            util::private_dir(&target)?;
        } else {
            bounded_file(&mut entry, &target, &mut remaining)?;
        }
    }
    let model = stage.join(VOSK_NAME);
    if !valid(&model, VOSK_FILES) {
        return err("Model archive is missing required files");
    }
    Ok(model)
}
fn unpack_tar(archive: &Path, stage: &Path, model_name: &str, files: &[&str]) -> Result<PathBuf> {
    let decoder = bzip2_rs::DecoderReader::new(File::open(archive)?);
    let mut archive = tar::Archive::new(decoder);
    let mut names = HashSet::new();
    let mut total = 0u64;
    let mut remaining = 1024 * MIB;
    for (index, entry) in archive.entries()?.enumerate() {
        if index >= 4096 {
            return err("Too many model archive entries");
        }
        let mut entry = entry?;
        let path = safe_path(
            entry
                .path()?
                .to_str()
                .ok_or("Non-UTF-8 model archive path")?,
            model_name,
        )?;
        if !names.insert(path.clone()) {
            return err("Duplicate model archive path");
        }
        let kind = entry.header().entry_type();
        if !kind.is_file() && !kind.is_dir() {
            return err("Model archive contains a link or special file");
        }
        total = total
            .checked_add(entry.size())
            .ok_or("Expanded archive overflow")?;
        if total > 4096 * MIB {
            return err("Expanded model archive exceeds its size limit");
        }
        if files
            .iter()
            .any(|name| path == Path::new(model_name).join(name))
        {
            if !kind.is_file() || entry.size() > remaining {
                return err("Invalid accurate model file");
            }
            bounded_file(&mut entry, &stage.join(&path), &mut remaining)?;
        }
    }
    let model = stage.join(model_name);
    if !valid(&model, files) {
        return err("Accurate model archive is missing required int8 files");
    }
    Ok(model)
}
// Atomic publication must not replace another installation or any existing path.
fn publish(source: &Path, target: &Path) -> Result<()> {
    let source = CString::new(source.as_os_str().as_bytes())?;
    let target_c = CString::new(target.as_os_str().as_bytes())?;
    if unsafe {
        libc::renameat2(
            libc::AT_FDCWD,
            source.as_ptr(),
            libc::AT_FDCWD,
            target_c.as_ptr(),
            libc::RENAME_NOREPLACE,
        )
    } != 0
    {
        return Err(std::io::Error::last_os_error().into());
    }
    File::open(target.parent().ok_or("Missing model parent")?)?.sync_all()?;
    Ok(())
}

/// The optional Arabic model (about 118 MB), from a local archive or its
/// published download, checked by SHA-256 like every other model.
pub fn install_arabic(local: Option<&Path>) -> Result<()> {
    let root = root();
    let arabic = root.join(ARABIC_NAME);
    if !ready(&arabic, ARABIC_FILES)? {
        let stage = Stage::new(&root)?;
        let archive = stage.0.join("model.tar.bz2");
        match (local, ARABIC_URL) {
            (Some(local), _) => copy_verified(File::open(local)?, &archive, ARABIC_SHA, 160 * MIB)?,
            (None, Some(url)) => {
                println!("Downloading the Arabic model (about 118 MB), checked by SHA-256…");
                download(url, &archive, ARABIC_SHA, 160 * MIB)?;
            }
            (None, None) => {
                return err("The Arabic model is not published for download yet. Install it from a file: jarvis install-model --arabic --archive moonshine-streaming-tiny-ar.tar.bz2")
            }
        }
        let model = unpack_tar(&archive, &stage.0, ARABIC_NAME, ARABIC_FILES)?;
        publish(&model, &arabic)?;
    }
    println!("Arabic model: ready at {}", arabic.display());
    println!("Choose Arabic and English in Voice settings, then restart Jarvis.");
    Ok(())
}

pub fn install(local: Option<&Path>, basic: bool) -> Result<()> {
    let root = root();
    let vosk = root.join(VOSK_NAME);
    if !ready(&vosk, VOSK_FILES)? {
        let stage = Stage::new(&root)?;
        let archive = stage.0.join("model.zip");
        if let Some(local) = local {
            copy_verified(File::open(local)?, &archive, VOSK_SHA, 64 * MIB)?;
        } else {
            println!("Downloading Vosk model (about 40 MB), checked by SHA-256…");
            download(
                &format!("https://alphacephei.com/vosk/models/{VOSK_NAME}.zip"),
                &archive,
                VOSK_SHA,
                64 * MIB,
            )?;
        }
        let model = unpack_zip(&archive, &stage.0)?;
        publish(&model, &vosk)?;
    }
    println!("Vosk model: ready at {}", vosk.display());
    if !basic {
        // Both command models, so Settings can switch between them offline.
        for (name, files, sha, size) in [
            (WHISPER_NAME, WHISPER_FILES, WHISPER_SHA, 636),
            (WHISPER_TINY_NAME, WHISPER_TINY_FILES, WHISPER_TINY_SHA, 113),
        ] {
            let whisper = root.join(name);
            if !ready(&whisper, files)? {
                println!("Downloading command model {name} (about {size} MB), checked by SHA-256…");
                let stage = Stage::new(&root)?;
                let archive = stage.0.join("model.tar.bz2");
                download(&format!("https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/{name}.tar.bz2"),&archive,sha,(size + size / 10) * MIB)?;
                let model = unpack_tar(&archive, &stage.0, name, files)?;
                publish(&model, &whisper)?;
            }
            println!("Command model: ready at {}", whisper.display());
        }
        let denoiser = root.join(DENOISER_NAME);
        if exists(&denoiser) {
            let mut digest = Sha256::new();
            let mut file = File::open(&denoiser)?;
            if denoiser.is_symlink() || file.metadata()?.len() > 4 * MIB {
                return err("Invalid existing denoiser model; review it before replacing it");
            }
            std::io::copy(&mut file, &mut digest)?;
            if format!("{:x}", digest.finalize()) != DENOISER_SHA {
                return err("Existing denoiser checksum mismatch; review it before replacing it");
            }
        } else {
            let stage = Stage::new(&root)?;
            let file = stage.0.join(DENOISER_NAME);
            println!("Downloading GTCRN noise suppression model, checked by SHA-256…");
            download(&format!("https://github.com/k2-fsa/sherpa-onnx/releases/download/speech-enhancement-models/{DENOISER_NAME}"),&file,DENOISER_SHA,4*MIB)?;
            publish(&file, &denoiser)?;
        }
        println!("Noise suppression model: ready at {}", denoiser.display());
    }
    println!("Done. Restart Jarvis to use newly installed models.");
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    fn stage() -> Stage {
        Stage::new(&env::temp_dir()).unwrap()
    }
    #[test]
    fn tar_int8_selection_and_malicious_archives() {
        let cases: &[(&str, &[u8], bool)] = &[
            (
                "valid",
                include_bytes!("../testdata/valid-whisper.tar.bz2"),
                true,
            ),
            (
                "missing",
                include_bytes!("../testdata/missing-whisper.tar.bz2"),
                false,
            ),
            (
                "link",
                include_bytes!("../testdata/link-whisper.tar.bz2"),
                false,
            ),
            (
                "traversal",
                include_bytes!("../testdata/traversal-whisper.tar.bz2"),
                false,
            ),
            (
                "duplicate",
                include_bytes!("../testdata/duplicate-whisper.tar.bz2"),
                false,
            ),
        ];
        for (name, bytes, expected) in cases {
            let stage = stage();
            let archive = stage.0.join("archive.tar.bz2");
            fs::write(&archive, bytes).unwrap();
            let result = unpack_tar(
                &archive,
                &stage.0.join("output"),
                WHISPER_NAME,
                WHISPER_FILES,
            );
            assert_eq!(result.is_ok(), *expected, "{name}: {result:?}");
            if let Ok(model) = result {
                assert!(valid(&model, WHISPER_FILES));
                assert!(!model.join("ignored-fp32.onnx").exists());
            }
        }
    }
    #[test]
    fn complete_zip_extracts_only_inside_stage() {
        let stage = stage();
        let archive = stage.0.join("model.zip");
        {
            let mut zip = zip::ZipWriter::new(create(&archive).unwrap());
            for file in VOSK_FILES {
                zip.start_file(
                    format!("{VOSK_NAME}/{file}"),
                    zip::write::SimpleFileOptions::default(),
                )
                .unwrap();
                zip.write_all(b"model").unwrap();
            }
            zip.finish().unwrap();
        }
        assert!(valid(
            &unpack_zip(&archive, &stage.0.join("output")).unwrap(),
            VOSK_FILES
        ));
    }
    #[test]
    fn unsafe_paths_rejected() {
        for name in [
            "/tmp/evil",
            "vosk-model-small-en-us-0.15/../evil",
            "other/file",
            "vosk-model-small-en-us-0.15/\\evil",
            "vosk-model-small-en-us-0.15/a\0b",
        ] {
            assert!(safe_path(name, VOSK_NAME).is_err(), "{name:?}");
        }
    }
    #[test]
    fn checksum_size_and_publication() {
        let stage = stage();
        let sha = format!("{:x}", Sha256::digest(b"hello"));
        let source = stage.0.join("source");
        copy_verified(&b"hello"[..], &source, &sha, 5).unwrap();
        assert!(copy_verified(&b"hello"[..], &stage.0.join("too-big"), &sha, 4).is_err());
        assert!(copy_verified(&b"wrong"[..], &stage.0.join("wrong"), &sha, 5).is_err());
        let target = stage.0.join("target");
        fs::write(&target, "original").unwrap();
        assert!(publish(&source, &target).is_err());
        assert_eq!(fs::read_to_string(&target).unwrap(), "original");
        let published = stage.0.join("published");
        publish(&source, &published).unwrap();
        assert_eq!(fs::read(&published).unwrap(), b"hello");
    }
    #[test]
    fn zip_missing_files_and_links_rejected() {
        let stage = stage();
        let archive = stage.0.join("input.zip");
        {
            let mut zip = zip::ZipWriter::new(create(&archive).unwrap());
            zip.start_file(
                format!("{VOSK_NAME}/am/final.mdl"),
                zip::write::SimpleFileOptions::default(),
            )
            .unwrap();
            zip.write_all(b"model").unwrap();
            zip.finish().unwrap();
        }
        assert!(unpack_zip(&archive, &stage.0.join("output")).is_err());
        let link = stage.0.join("link.zip");
        {
            let mut zip = zip::ZipWriter::new(create(&link).unwrap());
            zip.add_symlink(
                format!("{VOSK_NAME}/evil"),
                "/tmp/evil",
                zip::write::SimpleFileOptions::default(),
            )
            .unwrap();
            zip.finish().unwrap();
        }
        assert!(unpack_zip(&link, &stage.0.join("links")).is_err());
    }
}
