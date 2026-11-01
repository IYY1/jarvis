//! A value loaded on a background thread when it is first wanted and dropped
//! after going unused, so waiting for the wake phrase holds no Whisper weights.
use crate::Result;
use std::sync::Arc;
use std::thread::JoinHandle;
use std::time::{Duration, Instant};

pub type Loader<T> = Arc<dyn Fn() -> std::result::Result<T, String> + Send + Sync>;

/// How often to trim again while released. Allocations made after a release
/// (Vosk decoding speech) re-dirty pages the release freed, and glibc returns
/// them only on a trim, which costs well under a millisecond.
const TRIM_EVERY: Duration = Duration::from_secs(10);

enum Slot<T> {
    Empty,
    Loading(JoinHandle<std::result::Result<T, String>>),
    Ready(T, Instant),
}

pub struct OnDemand<T> {
    load: Loader<T>,
    idle: Option<Duration>,
    slot: Slot<T>,
    trim_every: Duration,
    trimmed: Instant,
    loads: u64,
}

impl<T: Send + 'static> OnDemand<T> {
    /// Without an idle limit the value loads now and is never released.
    pub fn new(load: Loader<T>, idle: Option<Duration>) -> Result<Self> {
        let mut value = Self {
            load,
            idle,
            slot: Slot::Empty,
            trim_every: TRIM_EVERY,
            trimmed: Instant::now(),
            loads: 0,
        };
        if idle.is_none() {
            value.get()?;
        }
        Ok(value)
    }
    /// Starts a background load, or restarts the idle clock of a loaded value.
    pub fn prepare(&mut self) {
        match &mut self.slot {
            Slot::Empty => {
                let load = self.load.clone();
                // Same stack as the main thread, which loaded models before.
                // If no thread can start, get() loads on the caller instead.
                if let Ok(handle) = std::thread::Builder::new()
                    .name("whisper-load".into())
                    .stack_size(8 << 20)
                    .spawn(move || load())
                {
                    self.slot = Slot::Loading(handle);
                    self.loads += 1;
                }
            }
            Slot::Loading(_) => {}
            Slot::Ready(_, used) => *used = Instant::now(),
        }
    }
    /// The loaded value, waiting for a background load or loading it here.
    pub fn get(&mut self) -> Result<&T> {
        self.collect(true)?;
        if matches!(self.slot, Slot::Empty) {
            self.loads += 1;
            self.slot = Slot::Ready((self.load)()?, Instant::now());
        }
        if let Slot::Ready(value, used) = &mut self.slot {
            *used = Instant::now();
            return Ok(value);
        }
        Err("Whisper did not load".into())
    }
    pub fn load_count(&self) -> u64 {
        self.loads
    }
    /// Collects a finished background load (reporting its error) and releases
    /// a value unused for the idle limit unless `busy`. True when the caller
    /// should trim the heap: on release, then every `TRIM_EVERY` while an
    /// on-demand value stays unloaded and the caller is not busy.
    pub fn maintain(&mut self, busy: bool) -> Result<bool> {
        self.collect(false)?;
        let expired = match (&self.slot, self.idle) {
            (Slot::Ready(_, used), Some(idle)) => !busy && used.elapsed() >= idle,
            _ => false,
        };
        if expired {
            self.slot = Slot::Empty;
        }
        // A resident value is never empty, so only on-demand values trim.
        let trim = expired
            || (!busy
                && matches!(self.slot, Slot::Empty)
                && self.trimmed.elapsed() >= self.trim_every);
        if trim {
            self.trimmed = Instant::now();
        }
        Ok(trim)
    }
    /// Joins a background load: always when `wait`, otherwise once it finished.
    fn collect(&mut self, wait: bool) -> Result<()> {
        if !matches!(&self.slot, Slot::Loading(h) if wait || h.is_finished()) {
            return Ok(());
        }
        if let Slot::Loading(handle) = std::mem::replace(&mut self.slot, Slot::Empty) {
            let value = handle
                .join()
                .map_err(|_| "Whisper loader stopped unexpectedly")??;
            self.slot = Slot::Ready(value, Instant::now());
        }
        Ok(())
    }
    #[cfg(test)]
    fn loaded(&self) -> bool {
        matches!(self.slot, Slot::Ready(..))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::thread::sleep;

    fn counting(delay: Duration) -> (Loader<usize>, Arc<AtomicUsize>) {
        let loads = Arc::new(AtomicUsize::new(0));
        let counter = loads.clone();
        let load: Loader<usize> = Arc::new(move || {
            sleep(delay);
            Ok(counter.fetch_add(1, Ordering::SeqCst) + 1)
        });
        (load, loads)
    }

    #[test]
    fn resident_value_loads_immediately_and_is_kept() {
        let (load, loads) = counting(Duration::ZERO);
        let mut value = OnDemand::new(load, None).unwrap();
        assert_eq!(loads.load(Ordering::SeqCst), 1);
        assert_eq!(value.load_count(), 1);
        assert!(!value.maintain(false).unwrap());
        assert_eq!(*value.get().unwrap(), 1);
        assert_eq!(loads.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn prepare_loads_off_thread_and_idle_release_reloads_on_demand() {
        let (load, loads) = counting(Duration::from_millis(300));
        let mut value = OnDemand::new(load, Some(Duration::ZERO)).unwrap();
        assert_eq!(loads.load(Ordering::SeqCst), 0);
        let start = Instant::now();
        value.prepare();
        value.prepare();
        assert!(
            start.elapsed() < Duration::from_millis(150),
            "prepare blocked"
        );
        assert!(!value.loaded());
        assert_eq!(*value.get().unwrap(), 1);
        // A command in progress keeps the value; the next idle check releases it.
        assert!(!value.maintain(true).unwrap());
        assert!(value.maintain(false).unwrap());
        assert!(!value.loaded());
        assert_eq!(*value.get().unwrap(), 2);
        assert_eq!(value.load_count(), 2);
    }

    #[test]
    fn maintain_collects_a_finished_load_without_waiting() {
        let (load, _) = counting(Duration::from_millis(100));
        let mut value = OnDemand::new(load, Some(Duration::from_secs(60))).unwrap();
        value.prepare();
        assert!(!value.maintain(false).unwrap());
        assert!(!value.loaded());
        sleep(Duration::from_millis(400));
        assert!(!value.maintain(false).unwrap());
        assert!(value.loaded());
    }

    #[test]
    fn use_and_prepare_restart_the_idle_clock() {
        let (load, loads) = counting(Duration::ZERO);
        let mut value = OnDemand::new(load, Some(Duration::from_secs(1))).unwrap();
        value.get().unwrap();
        sleep(Duration::from_millis(600));
        value.prepare();
        sleep(Duration::from_millis(600));
        assert!(!value.maintain(false).unwrap());
        sleep(Duration::from_millis(600));
        assert!(value.maintain(false).unwrap());
        assert_eq!(loads.load(Ordering::SeqCst), 1);
    }

    #[test]
    fn released_value_requests_periodic_trims_only_while_idle_and_unloaded() {
        let (load, _) = counting(Duration::ZERO);
        let mut value = OnDemand::new(load, Some(Duration::ZERO)).unwrap();
        value.trim_every = Duration::from_millis(200);
        value.get().unwrap();
        assert!(value.maintain(false).unwrap(), "release trims");
        assert!(
            !value.maintain(false).unwrap(),
            "not again within the period"
        );
        sleep(Duration::from_millis(250));
        assert!(!value.maintain(true).unwrap(), "never while busy");
        assert!(value.maintain(false).unwrap(), "periodic trim once idle");
        assert!(!value.maintain(false).unwrap());
        // Loaded or loading values hold their pages, so nothing to trim.
        value.idle = Some(Duration::from_secs(60));
        value.get().unwrap();
        sleep(Duration::from_millis(250));
        assert!(!value.maintain(false).unwrap());
        let (slow, _) = counting(Duration::from_millis(300));
        let mut loading = OnDemand::new(slow, Some(Duration::from_secs(60))).unwrap();
        loading.trim_every = Duration::ZERO;
        loading.prepare();
        assert!(!loading.maintain(false).unwrap());
    }

    #[test]
    fn load_errors_reach_the_caller_from_either_path() {
        let load: Loader<usize> = Arc::new(|| Err("corrupt model".into()));
        let error = OnDemand::new(load.clone(), None).err().unwrap();
        assert_eq!(error.to_string(), "corrupt model");
        let mut value = OnDemand::new(load, Some(Duration::from_secs(60))).unwrap();
        assert_eq!(value.get().err().unwrap().to_string(), "corrupt model");
        value.prepare();
        sleep(Duration::from_millis(200));
        assert_eq!(
            value.maintain(false).err().unwrap().to_string(),
            "corrupt model"
        );
    }
}
