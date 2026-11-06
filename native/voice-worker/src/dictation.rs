//! Dictation: speech is cut at pauses and transcribed on its own thread, so
//! capture keeps running while Whisper works. Text goes to the controller to
//! insert; it never reaches the command parser.
use crate::inference::Text;
use crate::input::FRAME;
use crate::Result;
use std::collections::VecDeque;
use std::sync::mpsc::{channel, Receiver, Sender, TryRecvError};
use std::sync::Arc;
use std::thread::JoinHandle;

/// 300 ms of speech before a segment counts; shorter bursts are clicks.
const MIN_SPEECH_FRAMES: usize = 10;
/// A 600 ms pause ends a segment.
const PAUSE_FRAMES: usize = 20;
/// Segments are cut at the next pause after 15 s, and at 20 s regardless
/// (the recognizer's limit, with its 10 s of padding inside Whisper's 30 s).
const SOFT_LIMIT_FRAMES: usize = 500;
const HARD_LIMIT_FRAMES: usize = 666;
const PREROLL_FRAMES: usize = 10;
/// Segments waiting for Whisper. Speech beyond this is dropped with a notice
/// instead of queueing without bound behind a slow transcriber.
const MAX_QUEUED: usize = 3;

/// Turns one segment of 16 kHz s16le speech into text.
pub type Transcribe = Arc<dyn Fn(&[u8]) -> Result<Text> + Send + Sync>;

pub struct Dictation {
    work: Option<Sender<Vec<u8>>>,
    results: Receiver<std::result::Result<Text, String>>,
    thread: Option<JoinHandle<()>>,
    preroll: VecDeque<[u8; FRAME]>,
    segment: Vec<u8>,
    speech_frames: usize,
    quiet_run: usize,
    queued: usize,
    pub dropped: usize,
    /// Frames since speech was last heard, for the idle stop.
    pub silent_frames: usize,
}

impl Dictation {
    pub fn start(transcribe: Transcribe) -> Result<Self> {
        let (work, jobs) = channel::<Vec<u8>>();
        let (done, results) = channel();
        let thread = std::thread::Builder::new()
            .name("dictation".into())
            .stack_size(8 << 20)
            .spawn(move || {
                while let Ok(pcm) = jobs.recv() {
                    let result = transcribe(&pcm).map_err(|e| e.to_string());
                    if done.send(result).is_err() {
                        break;
                    }
                }
            })?;
        Ok(Self {
            work: Some(work),
            results,
            thread: Some(thread),
            preroll: VecDeque::new(),
            segment: vec![],
            speech_frames: 0,
            quiet_run: 0,
            queued: 0,
            dropped: 0,
            silent_frames: 0,
        })
    }
    /// One 30 ms frame. `quiet` comes from the worker's adaptive gate.
    pub fn feed(&mut self, pcm: &[u8; FRAME], quiet: bool) {
        if quiet {
            self.silent_frames += 1;
        } else {
            self.silent_frames = 0;
        }
        if self.segment.is_empty() {
            if quiet {
                if self.preroll.len() == PREROLL_FRAMES {
                    self.preroll.pop_front();
                }
                self.preroll.push_back(*pcm);
                return;
            }
            for frame in self.preroll.drain(..) {
                self.segment.extend_from_slice(&frame);
            }
        }
        self.segment.extend_from_slice(pcm);
        if quiet {
            self.quiet_run += 1;
        } else {
            self.speech_frames += 1;
            self.quiet_run = 0;
        }
        let frames = self.segment.len() / FRAME;
        if self.quiet_run >= PAUSE_FRAMES
            || (frames >= SOFT_LIMIT_FRAMES && quiet)
            || frames >= HARD_LIMIT_FRAMES
        {
            self.cut();
        }
    }
    /// Sends the speech gathered so far, e.g. when the user stops dictation.
    pub fn cut(&mut self) {
        let segment = std::mem::take(&mut self.segment);
        let speech = std::mem::take(&mut self.speech_frames);
        self.quiet_run = 0;
        if speech < MIN_SPEECH_FRAMES {
            return;
        }
        if self.queued >= MAX_QUEUED {
            self.dropped += 1;
            return;
        }
        if let Some(work) = &self.work {
            if work.send(segment).is_ok() {
                self.queued += 1;
            }
        }
    }
    /// Finished transcriptions, oldest first.
    pub fn poll(&mut self) -> Vec<std::result::Result<Text, String>> {
        let mut out = vec![];
        loop {
            match self.results.try_recv() {
                Ok(result) => {
                    self.queued = self.queued.saturating_sub(1);
                    out.push(result);
                }
                Err(TryRecvError::Empty) | Err(TryRecvError::Disconnected) => return out,
            }
        }
    }
    pub fn pending(&self) -> bool {
        self.queued > 0
    }
    /// Stops taking speech and waits for queued segments, returning them.
    pub fn finish(mut self) -> Vec<std::result::Result<Text, String>> {
        drop(self.work.take());
        if let Some(thread) = self.thread.take() {
            let _ = thread.join();
        }
        self.poll()
    }
}

impl Drop for Dictation {
    fn drop(&mut self) {
        // Cancel: the thread ends after its current segment; its result is
        // discarded with the receiver.
        drop(self.work.take());
        drop(self.thread.take());
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn segmenter() -> Dictation {
        let (work, _jobs) = channel();
        let (_done, results) = channel();
        std::mem::forget(_jobs);
        std::mem::forget(_done);
        Dictation {
            work: Some(work),
            results,
            thread: None,
            preroll: VecDeque::new(),
            segment: vec![],
            speech_frames: 0,
            quiet_run: 0,
            queued: 0,
            dropped: 0,
            silent_frames: 0,
        }
    }
    #[test]
    fn pauses_cut_segments_and_clicks_are_ignored() {
        let mut d = segmenter();
        let frame = [0_u8; FRAME];
        for _ in 0..5 {
            d.feed(&frame, false);
        }
        for _ in 0..PAUSE_FRAMES {
            d.feed(&frame, true);
        }
        assert_eq!(d.queued, 0, "a 150 ms click is not speech");
        for _ in 0..30 {
            d.feed(&frame, false);
        }
        for _ in 0..PAUSE_FRAMES {
            d.feed(&frame, true);
        }
        assert_eq!(d.queued, 1);
        assert_eq!(d.silent_frames, PAUSE_FRAMES);
        for _ in 0..HARD_LIMIT_FRAMES {
            d.feed(&frame, false);
        }
        assert_eq!(d.queued, 2, "continuous speech is cut before Whisper's window");
        for _ in 0..4 * HARD_LIMIT_FRAMES {
            d.feed(&frame, false);
        }
        assert_eq!(d.queued, MAX_QUEUED);
        assert!(d.dropped > 0, "a stalled transcriber never grows the queue");
    }
}
