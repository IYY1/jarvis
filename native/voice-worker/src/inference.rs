//! Owned native handles. Vosk's shared library outlives every model/decoder;
//! Whisper and GTCRN execute through the Rust recognizer/denoiser crates.
use crate::{NativeConfig, Result, WhisperFiles};
use jarvis_gtcrn::Denoiser;
use jarvis_whisper::Recognizer;
use libloading::Library;
use serde_json::Value;
use std::ffi::{c_char, c_int, c_void, CStr, CString};
use std::rc::Rc;
use std::sync::Mutex;

static LOAD_LOCK: Mutex<()> = Mutex::new(());

type Handle = *mut c_void;
struct Api {
    model_new: unsafe extern "C" fn(*const c_char) -> Handle,
    model_free: unsafe extern "C" fn(Handle),
    find_word: unsafe extern "C" fn(Handle, *const c_char) -> c_int,
    recognizer_new: unsafe extern "C" fn(Handle, f32) -> Handle,
    recognizer_new_grm: unsafe extern "C" fn(Handle, f32, *const c_char) -> Handle,
    recognizer_free: unsafe extern "C" fn(Handle),
    set_words: unsafe extern "C" fn(Handle, c_int),
    accept: unsafe extern "C" fn(Handle, *const c_char, c_int) -> c_int,
    result: unsafe extern "C" fn(Handle) -> *const c_char,
    partial_result: unsafe extern "C" fn(Handle) -> *const c_char,
    final_result: unsafe extern "C" fn(Handle) -> *const c_char,
    reset: unsafe extern "C" fn(Handle),
    _library: Library,
}

pub struct Model {
    ptr: Handle,
    api: Api,
}
impl Model {
    pub fn load(library: &str, path: &str) -> Result<Rc<Self>> {
        // SAFETY: symbols use Vosk's public C signatures (vosk_api.h).
        // The Library is retained until the last Rc<Model> is dropped.
        unsafe {
            let lib = load(library)?;
            let set_log: unsafe extern "C" fn(c_int) = *lib.get(b"vosk_set_log_level\0")?;
            set_log(-1);
            let api = Api {
                model_new: *lib.get(b"vosk_model_new\0")?,
                model_free: *lib.get(b"vosk_model_free\0")?,
                find_word: *lib.get(b"vosk_model_find_word\0")?,
                recognizer_new: *lib.get(b"vosk_recognizer_new\0")?,
                recognizer_new_grm: *lib.get(b"vosk_recognizer_new_grm\0")?,
                recognizer_free: *lib.get(b"vosk_recognizer_free\0")?,
                set_words: *lib.get(b"vosk_recognizer_set_words\0")?,
                accept: *lib.get(b"vosk_recognizer_accept_waveform\0")?,
                result: *lib.get(b"vosk_recognizer_result\0")?,
                partial_result: *lib.get(b"vosk_recognizer_partial_result\0")?,
                final_result: *lib.get(b"vosk_recognizer_final_result\0")?,
                reset: *lib.get(b"vosk_recognizer_reset\0")?,
                _library: lib,
            };
            let path = CString::new(path)?;
            let ptr = (api.model_new)(path.as_ptr());
            if ptr.is_null() {
                return Err("Cannot load Vosk model".into());
            }
            Ok(Rc::new(Self { ptr, api }))
        }
    }
    pub fn knows(&self, word: &str) -> bool {
        CString::new(word).is_ok_and(|s| unsafe { (self.api.find_word)(self.ptr, s.as_ptr()) >= 0 })
    }
}
impl Drop for Model {
    fn drop(&mut self) {
        unsafe { (self.api.model_free)(self.ptr) };
    }
}

pub struct Decoder {
    ptr: Handle,
    model: Rc<Model>,
}
impl Decoder {
    pub fn new(model: Rc<Model>, grammar: Option<Vec<String>>, words: bool) -> Result<Self> {
        let ptr = unsafe {
            if let Some(mut phrases) = grammar {
                phrases.push("[unk]".into());
                let json = CString::new(serde_json::to_string(&phrases)?)?;
                (model.api.recognizer_new_grm)(model.ptr, 16000.0, json.as_ptr())
            } else {
                (model.api.recognizer_new)(model.ptr, 16000.0)
            }
        };
        if ptr.is_null() {
            return Err("Cannot create Vosk decoder".into());
        }
        if words {
            unsafe { (model.api.set_words)(ptr, 1) };
        }
        Ok(Self { ptr, model })
    }
    pub fn accept(&self, pcm: &[u8]) -> Result<bool> {
        let size = c_int::try_from(pcm.len())?;
        let result = unsafe { (self.model.api.accept)(self.ptr, pcm.as_ptr().cast(), size) };
        if result < 0 {
            return Err("Vosk could not decode audio".into());
        }
        Ok(result != 0)
    }
    pub fn result(&self, final_result: bool) -> Result<Value> {
        self.read(if final_result {
            self.model.api.final_result
        } else {
            self.model.api.result
        })
    }
    /// The in-progress hypothesis, `{"partial": "..."}`, without ending the utterance.
    pub fn partial(&self) -> Result<Value> {
        self.read(self.model.api.partial_result)
    }
    fn read(&self, call: unsafe extern "C" fn(Handle) -> *const c_char) -> Result<Value> {
        let ptr = unsafe { call(self.ptr) };
        if ptr.is_null() {
            return Err("Vosk returned a null result".into());
        }
        // Copy before the next native decoder call invalidates this pointer.
        Ok(serde_json::from_slice(
            unsafe { CStr::from_ptr(ptr) }.to_bytes(),
        )?)
    }
    pub fn reset(&self) {
        unsafe { (self.model.api.reset)(self.ptr) };
    }
    pub fn clear(&self, finalize: bool) -> Result<()> {
        if finalize {
            // Discard this hypothesis. Quiet/timeout/reset boundaries must
            // never turn background speech into a dispatched command.
            self.result(true)?;
        }
        self.reset();
        Ok(())
    }
}
impl Drop for Decoder {
    fn drop(&mut self) {
        unsafe { (self.model.api.recognizer_free)(self.ptr) };
    }
}

pub struct Transcriber {
    recognizer: Recognizer,
    denoiser: Option<Denoiser>,
}
/// A transcript and the language of the model that produced it.
pub struct Text {
    pub text: String,
    pub language: &'static str,
}
fn samples(pcm: &[u8]) -> Vec<f32> {
    // No Python float objects or intermediate lists. PCM values are exact
    // multiples of 2^-15, so f32 conversion matches the Python binding.
    pcm.as_chunks::<2>()
        .0
        .iter()
        .map(|s| i16::from_le_bytes([s[0], s[1]]) as f32 / 32768.0)
        .collect()
}
impl Transcriber {
    pub fn new(config: &NativeConfig) -> Result<Self> {
        let files = config
            .whisper
            .as_ref()
            .ok_or("Whisper model is not configured")?;
        Self::load(files, config.denoiser.as_deref(), config.threads as usize)
    }
    pub fn load(files: &WhisperFiles, denoiser: Option<&str>, threads: usize) -> Result<Self> {
        let _guard = LOAD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        // GTCRN compiles on its own thread while Whisper loads. Errors keep
        // their order: Whisper's first, then the model check, then GTCRN's.
        let (recognizer, denoiser) = std::thread::scope(|scope| {
            let gtcrn = denoiser.map(|path| {
                std::thread::Builder::new()
                    .name("whisper-load".into())
                    .stack_size(8 << 20)
                    .spawn_scoped(scope, move || Denoiser::load(path).map_err(|e| e.to_string()))
            });
            let recognizer =
                Recognizer::load(&files.encoder, &files.decoder, &files.tokens, threads);
            let denoiser = match gtcrn {
                // No thread could start: load here, as before.
                Some(Err(_)) => Some(Denoiser::load(denoiser.unwrap()).map_err(|e| e.to_string())),
                Some(Ok(handle)) => Some(
                    handle
                        .join()
                        .unwrap_or_else(|_| Err("GTCRN loader stopped unexpectedly".into())),
                ),
                None => None,
            };
            (recognizer, denoiser)
        });
        let recognizer = recognizer?;
        if recognizer.multilingual() {
            return Err("Commands use an English Whisper model; Arabic uses the Arabic model".into());
        }
        let denoiser = denoiser.transpose()?;
        trim();
        Ok(Self {
            recognizer,
            denoiser,
        })
    }
    pub fn transcribe(&self, pcm: &[u8]) -> Result<String> {
        let input = self.command_input(pcm)?;
        self.transcribe_input(input)
    }
    /// Command audio as Whisper hears it: scaled and, with GTCRN on,
    /// denoised; None when it is too short to transcribe. Split from
    /// `transcribe` so a command can be denoised while Moonshine listens.
    pub fn command_input(&self, pcm: &[u8]) -> Result<Option<Vec<f32>>> {
        if pcm.len() > 320000 {
            return Err("Command audio exceeds ten seconds".into());
        }
        self.input(pcm)
    }
    /// Dictated speech, up to 20 seconds per segment.
    pub fn dictate(&self, pcm: &[u8]) -> Result<Text> {
        if pcm.len() > 640000 {
            return Err("Dictation segment exceeds twenty seconds".into());
        }
        Ok(Text {
            text: self.transcribe_input(self.input(pcm)?)?,
            language: "en",
        })
    }
    fn input(&self, pcm: &[u8]) -> Result<Option<Vec<f32>>> {
        if pcm.len() < 6400 {
            return Ok(None);
        }
        let mut samples = samples(pcm);
        if let Some(denoiser) = &self.denoiser {
            samples = denoiser.process(&samples)?;
        }
        Ok(Some(samples))
    }
    /// Whisper's transcript of `command_input`'s result.
    pub fn transcribe_input(&self, input: Option<Vec<f32>>) -> Result<String> {
        let Some(samples) = input else {
            return Ok(String::new());
        };
        let text = self.recognizer.transcribe(&samples)?;
        drop(samples);
        trim();
        Ok(text)
    }
}

/// Arabic speech (Moonshine Streaming Tiny Arabic). Najdi, Hijazi and other
/// dialects are transcribed as spoken; English words inside Arabic speech
/// usually come out in Arabic script.
pub struct Arabic {
    recognizer: jarvis_moonshine::Recognizer,
}
impl Arabic {
    /// `threads` splits Moonshine's matrix products like Whisper's; each
    /// output tile still comes from one thread, so transcripts are unchanged.
    pub fn load(directory: &str, threads: usize) -> Result<Self> {
        let _guard = LOAD_LOCK.lock().unwrap_or_else(|e| e.into_inner());
        let recognizer = jarvis_moonshine::Recognizer::load_with_threads(directory, threads)?;
        trim();
        Ok(Self { recognizer })
    }
    pub fn transcribe(&self, pcm: &[u8]) -> Result<String> {
        if pcm.len() > 640000 {
            return Err("Arabic audio exceeds twenty seconds".into());
        }
        let samples = samples(pcm);
        let text = self.recognizer.transcribe(&samples)?;
        drop(samples);
        trim();
        Ok(text)
    }
}

pub fn trim() {
    // Like the Python implementation, only free scratch pages, never live weights.
    #[cfg(all(target_os = "linux", target_env = "gnu"))]
    unsafe {
        libc::malloc_trim(0);
    }
    // Large blocks are already returned on free; this decommits coalesced
    // free ranges of the process heap.
    #[cfg(windows)]
    unsafe {
        use windows::Win32::System::Memory::{GetProcessHeap, HeapCompact, HEAP_FLAGS};
        if let Ok(heap) = GetProcessHeap() {
            HeapCompact(heap, HEAP_FLAGS(0));
        }
    }
}

/// libvosk.dll's MinGW runtime DLLs sit beside it in the vosk package.
#[cfg(windows)]
unsafe fn load(path: &str) -> std::result::Result<Library, libloading::Error> {
    use libloading::os::windows::{Library, LOAD_WITH_ALTERED_SEARCH_PATH};
    Library::load_with_flags(path, LOAD_WITH_ALTERED_SEARCH_PATH).map(Into::into)
}
#[cfg(not(windows))]
unsafe fn load(path: &str) -> std::result::Result<Library, libloading::Error> {
    Library::new(path)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn whisper_load_errors_come_before_denoiser_errors() {
        let missing = WhisperFiles {
            encoder: "/nonexistent/tiny.en-encoder.int8.onnx".into(),
            decoder: "/nonexistent/tiny.en-decoder.int8.onnx".into(),
            tokens: "/nonexistent/tiny.en-tokens.txt".into(),
        };
        let both = Transcriber::load(&missing, Some("/nonexistent/gtcrn.onnx"), 2)
            .err()
            .unwrap()
            .to_string();
        let whisper_only = Transcriber::load(&missing, None, 2).err().unwrap().to_string();
        assert_eq!(both, whisper_only, "the denoiser error must not win");
    }
    /// The early-denoise path (command_input, then transcribe_input) must give
    /// exactly what transcribe gives. Runs with JARVIS_WHISPER_MODEL (a
    /// sherpa-onnx Whisper folder), JARVIS_GTCRN_MODEL and
    /// JARVIS_SPEECH_TEST_RAW; skipped otherwise.
    #[test]
    fn split_whisper_path_matches_transcribe() {
        let (Ok(folder), Ok(gtcrn), Ok(raw)) = (
            std::env::var("JARVIS_WHISPER_MODEL"),
            std::env::var("JARVIS_GTCRN_MODEL"),
            std::env::var("JARVIS_SPEECH_TEST_RAW"),
        ) else {
            eprintln!("skipped: set JARVIS_WHISPER_MODEL, JARVIS_GTCRN_MODEL, JARVIS_SPEECH_TEST_RAW");
            return;
        };
        let file = |suffix: &str| {
            std::fs::read_dir(&folder)
                .unwrap()
                .map(|e| e.unwrap().path())
                .find(|p| p.to_string_lossy().ends_with(suffix))
                .unwrap()
                .to_string_lossy()
                .into_owned()
        };
        let files = WhisperFiles {
            encoder: file("-encoder.int8.onnx"),
            decoder: file("-decoder.int8.onnx"),
            tokens: file("-tokens.txt"),
        };
        let pcm = std::fs::read(raw).unwrap();
        let pcm = &pcm[..pcm.len().min(320000) / 2 * 2];
        for denoiser in [Some(gtcrn.as_str()), None] {
            let model = Transcriber::load(&files, denoiser, 4).unwrap();
            let whole = model.transcribe(pcm).unwrap();
            assert!(!whole.is_empty());
            let split = model
                .transcribe_input(model.command_input(pcm).unwrap())
                .unwrap();
            assert_eq!(split, whole, "denoiser {denoiser:?}");
            // Too short and too long behave the same on both paths.
            assert!(model.command_input(&pcm[..6398]).unwrap().is_none());
            assert_eq!(model.transcribe(&pcm[..6398]).unwrap(), "");
            assert!(model.command_input(&vec![0; 320002]).is_err());
            assert!(model.transcribe(&vec![0; 320002]).is_err());
        }
    }
}
