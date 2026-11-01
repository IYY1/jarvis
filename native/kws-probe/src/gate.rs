//! Experimental energy gate. The bounded ring preserves soft word onsets;
//! retiring a quiet stream prevents background hypotheses surviving a gap.
use crate::Result;
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;

#[derive(Clone, Copy, Debug, Deserialize, Serialize)]
#[serde(default, deny_unknown_fields)]
pub struct Config {
    pub rms: f64,
    pub preroll_ms: usize,
    pub hangover_ms: usize,
    /// Experimental: native model chunk shift, not an acoustic threshold.
    /// Omitted preserves the original gate exactly.
    pub align_chunk_ms: Option<usize>,
}
impl Default for Config {
    fn default() -> Self {
        Self {
            rms: 0.001,
            preroll_ms: 300,
            hangover_ms: 1200,
            align_chunk_ms: None,
        }
    }
}
impl Config {
    pub fn validate(&self) -> Result<()> {
        if !self.rms.is_finite()
            || !(0.0001..=0.05).contains(&self.rms)
            || !(300..=900).contains(&self.preroll_ms)
            || !self.preroll_ms.is_multiple_of(30)
            || !(600..=3000).contains(&self.hangover_ms)
            || !self.hangover_ms.is_multiple_of(30)
            || self.align_chunk_ms.is_some_and(|ms| {
                !(10..=1000).contains(&ms)
                    || !ms.is_multiple_of(10)
                    || self.preroll_ms + ms > self.hangover_ms
            })
        {
            return Err("quiet_gate: RMS 0.0001–0.05; preroll 300–900 ms and hangover 600–3000 ms in 30 ms steps".into());
        }
        Ok(())
    }
}

pub struct Frame {
    pub samples: [f32; 480],
    pub len: usize,
    pub end: u64,
}
impl Frame {
    pub fn start(&self) -> u64 {
        self.end - self.len as u64
    }
    pub fn audio(&self) -> &[f32] {
        &self.samples[..self.len]
    }
}

#[derive(Debug, PartialEq)]
pub enum Action {
    Skip,
    Open,
    Feed,
    Close,
}

pub struct Gate {
    config: Config,
    remaining_samples: usize,
    pub preroll: VecDeque<Frame>,
}
impl Gate {
    pub fn new(config: Config) -> Self {
        Self {
            config,
            remaining_samples: 0,
            preroll: VecDeque::with_capacity(
                (config.preroll_ms + config.align_chunk_ms.unwrap_or(0)).div_ceil(30),
            ),
        }
    }
    pub fn retains_active_context(&self) -> bool {
        self.config.align_chunk_ms.is_some()
    }
    pub fn origin(&self, frame: &Frame) -> u64 {
        if let Some(ms) = self.config.align_chunk_ms {
            let requested = frame
                .start()
                .saturating_sub((self.config.preroll_ms * 16) as u64);
            let alignment = (ms * 16) as u64;
            requested / alignment * alignment
        } else {
            self.preroll.front().map_or(frame.start(), Frame::start)
        }
    }
    pub fn observe(&mut self, frame: &Frame) -> Action {
        let energy = frame
            .audio()
            .iter()
            .map(|s| f64::from(*s).powi(2))
            .sum::<f64>();
        let loud = energy >= self.config.rms.powi(2) * frame.len as f64;
        if loud {
            let opening = self.remaining_samples == 0;
            self.remaining_samples = self.config.hangover_ms * 16;
            if opening {
                Action::Open
            } else {
                Action::Feed
            }
        } else if self.remaining_samples == 0 {
            Action::Skip
        } else {
            self.remaining_samples = self.remaining_samples.saturating_sub(frame.len);
            if self.remaining_samples == 0 {
                Action::Close
            } else {
                Action::Feed
            }
        }
    }
    pub fn remember(&mut self, frame: Frame) {
        let capacity =
            (self.config.preroll_ms + self.config.align_chunk_ms.unwrap_or(0)).div_ceil(30);
        if self.preroll.len() == capacity {
            self.preroll.pop_front();
        }
        self.preroll.push_back(frame);
    }
}

/// Count unique original-input samples separately from overlapped context.
/// A restarted stream may replay quiet frames already accepted by its predecessor.
#[derive(Default)]
pub struct Accounting {
    pub decoded: u64,
    pub replayed: u64,
    last_end: u64,
}
impl Accounting {
    pub fn accept(&mut self, start: u64, end: u64) {
        let length = end - start;
        let fresh = end.saturating_sub(self.last_end).min(length);
        self.decoded += fresh;
        self.replayed += length - fresh;
        self.last_end = self.last_end.max(end);
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    fn frame(value: f32, end: u64) -> Frame {
        Frame {
            samples: [value; 480],
            len: 480,
            end,
        }
    }
    #[test]
    fn bounded_onset_ring_pause_and_recovery() {
        let mut gate = Gate::new(Config::default());
        for i in 1..=100 {
            let f = frame(0.0005, i * 480);
            assert_eq!(gate.observe(&f), Action::Skip);
            gate.remember(f);
        }
        assert_eq!(gate.preroll.len(), 10);
        assert_eq!(gate.preroll.front().unwrap().start(), 90 * 480);
        assert_eq!(gate.observe(&frame(0.002, 101 * 480)), Action::Open);
        gate.preroll.clear();
        for _ in 0..10 {
            assert_eq!(gate.observe(&frame(0.0, 480)), Action::Feed);
        }
        assert_eq!(
            gate.observe(&frame(0.002, 480)),
            Action::Feed,
            "pause within name stays open"
        );
        for _ in 0..39 {
            assert_eq!(gate.observe(&frame(0.0, 480)), Action::Feed);
        }
        assert_eq!(gate.observe(&frame(0.0, 480)), Action::Close);
        assert_eq!(gate.observe(&frame(0.0, 480)), Action::Skip);
        assert_eq!(
            gate.observe(&frame(0.002, 480)),
            Action::Open,
            "new stream after background"
        );
    }
    #[test]
    fn rejects_unbounded_and_nonfinite_settings() {
        for config in [
            Config {
                rms: f64::NAN,
                ..Config::default()
            },
            Config {
                preroll_ms: 0,
                ..Config::default()
            },
            Config {
                hangover_ms: 3010,
                ..Config::default()
            },
            Config {
                preroll_ms: 301,
                ..Config::default()
            },
        ] {
            assert!(config.validate().is_err());
        }
        assert!(Config::default().validate().is_ok());
    }
    #[test]
    fn aligned_context_covers_all_input_chunk_phases_without_inventing_samples() {
        let config = Config {
            align_chunk_ms: Some(320),
            ..Config::default()
        };
        config.validate().unwrap();
        let mut gate = Gate::new(config);
        for i in 1..=100 {
            let f = frame(0.0, i * 480);
            let origin = gate.origin(&f);
            assert_eq!(origin % 5120, 0);
            if let Some(first) = gate.preroll.front() {
                assert!(origin >= first.start());
            }
            assert!(origin <= f.start());
            gate.remember(f);
            assert!(gate.preroll.len() <= 21);
        }
        assert!(gate.retains_active_context());
    }
    #[test]
    fn overlapped_context_does_not_hide_skipped_input_or_underflow_counters() {
        let mut count = Accounting::default();
        count.accept(0, 100);
        count.accept(50, 150);
        count.accept(250, 300);
        assert_eq!(count.decoded, 200);
        assert_eq!(count.replayed, 50);
        assert_eq!(300 - count.decoded, 100);
    }
    #[test]
    fn alignment_is_bounded_by_quiet_history_and_feature_frame_steps() {
        for ms in [0, 15, 1001, 1200] {
            assert!(Config {
                align_chunk_ms: Some(ms),
                ..Config::default()
            }
            .validate()
            .is_err());
        }
        assert!(Config {
            align_chunk_ms: Some(640),
            hangover_ms: 600,
            ..Config::default()
        }
        .validate()
        .is_err());
        assert!(!Gate::new(Config::default()).retains_active_context());
    }
}
