//! Shared Jarvis policy, process ownership and local integration code.
//!
//! Both the controller and command-line client use these implementations;
//! importing this library does not start a service or open audio devices.
pub mod apps;
pub mod capture;
pub mod config;
pub mod desktop;
pub mod dictation;
pub mod ipc;
pub mod memory;
pub mod policy;
mod replay_budget;
mod replay_clip;
pub mod service;
pub mod util;
pub mod voice;
pub type Error = Box<dyn std::error::Error + Send + Sync>;
pub type Result<T> = std::result::Result<T, Error>;
pub fn err<T>(message: impl Into<String>) -> Result<T> {
    Err(message.into().into())
}
