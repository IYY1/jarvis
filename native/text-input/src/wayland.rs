//! Wayland backend: input method first, virtual keyboard as the fallback.
use serde_json::{json, Value};
use std::io::Write;
use std::os::fd::{AsFd, AsRawFd};
use wayland_client::{
    protocol::{wl_registry, wl_seat},
    Connection, Dispatch, EventQueue, QueueHandle, WEnum,
};
use wayland_protocols::wp::text_input::zv3::client::zwp_text_input_v3::{
    ContentHint, ContentPurpose,
};
use wayland_protocols_misc::zwp_input_method_v2::client::{
    zwp_input_method_manager_v2::ZwpInputMethodManagerV2,
    zwp_input_method_v2::{self, ZwpInputMethodV2},
};
use wayland_protocols_misc::zwp_virtual_keyboard_v1::client::{
    zwp_virtual_keyboard_manager_v1::ZwpVirtualKeyboardManagerV1,
    zwp_virtual_keyboard_v1::ZwpVirtualKeyboardV1,
};

use crate::{printable, Result};

/// Double-buffered input-method state, applied on `done`.
#[derive(Clone, Copy, Default)]
struct Field {
    active: bool,
    hint: u32,
    purpose: u32,
}

#[derive(Default)]
struct State {
    seat: Option<wl_seat::WlSeat>,
    im_manager: Option<ZwpInputMethodManagerV2>,
    vk_manager: Option<ZwpVirtualKeyboardManagerV1>,
    pending: Field,
    current: Field,
    serial: u32,
    unavailable: bool,
}

impl Dispatch<wl_registry::WlRegistry, ()> for State {
    fn event(
        state: &mut Self,
        registry: &wl_registry::WlRegistry,
        event: wl_registry::Event,
        _: &(),
        _: &Connection,
        qh: &QueueHandle<Self>,
    ) {
        if let wl_registry::Event::Global {
            name,
            interface,
            version,
        } = event
        {
            match interface.as_str() {
                "wl_seat" if state.seat.is_none() => {
                    state.seat = Some(registry.bind(name, version.min(7), qh, ()))
                }
                "zwp_input_method_manager_v2" => {
                    state.im_manager = Some(registry.bind(name, 1, qh, ()))
                }
                "zwp_virtual_keyboard_manager_v1" => {
                    state.vk_manager = Some(registry.bind(name, 1, qh, ()))
                }
                _ => {}
            }
        }
    }
}

impl Dispatch<ZwpInputMethodV2, ()> for State {
    fn event(
        state: &mut Self,
        _: &ZwpInputMethodV2,
        event: zwp_input_method_v2::Event,
        _: &(),
        _: &Connection,
        _: &QueueHandle<Self>,
    ) {
        use zwp_input_method_v2::Event;
        match event {
            Event::Activate => state.pending = Field { active: true, ..Field::default() },
            Event::Deactivate => state.pending.active = false,
            Event::ContentType { hint, purpose } => {
                state.pending.hint = match hint {
                    WEnum::Value(h) => h.bits(),
                    WEnum::Unknown(h) => h,
                };
                state.pending.purpose = match purpose {
                    WEnum::Value(p) => p.into(),
                    WEnum::Unknown(p) => p,
                };
            }
            Event::Done => {
                state.serial = state.serial.wrapping_add(1);
                state.current = state.pending;
            }
            // Another input method (fcitx5, ibus) already serves this seat.
            Event::Unavailable => state.unavailable = true,
            _ => {}
        }
    }
}

macro_rules! ignore {
    ($($t:ty),*) => {$(
        impl Dispatch<$t, ()> for State {
            fn event(_: &mut Self, _: &$t, _: <$t as wayland_client::Proxy>::Event, _: &(),
                     _: &Connection, _: &QueueHandle<Self>) {}
        }
    )*};
}
ignore!(
    wl_seat::WlSeat,
    ZwpInputMethodManagerV2,
    ZwpVirtualKeyboardManagerV1,
    ZwpVirtualKeyboardV1
);

/// Fields that must never receive dictation.
fn sensitive(field: &Field) -> bool {
    let purpose = field.purpose;
    purpose == u32::from(ContentPurpose::Password)
        || purpose == u32::from(ContentPurpose::Pin)
        || field.hint & (ContentHint::SensitiveData | ContentHint::HiddenText).bits() != 0
}

pub struct Typer {
    conn: Connection,
    queue: EventQueue<State>,
    state: State,
    method: Option<ZwpInputMethodV2>,
}

impl Typer {
    pub fn connect() -> Result<Self> {
        let conn = Connection::connect_to_env()
            .map_err(|_| "No Wayland session. Dictation inserts text only on Wayland.")?;
        let mut queue = conn.new_event_queue();
        let qh = queue.handle();
        conn.display().get_registry(&qh, ());
        let mut state = State::default();
        queue.roundtrip(&mut state)?;
        let method = match (&state.im_manager, &state.seat) {
            (Some(manager), Some(seat)) => Some(manager.get_input_method(seat, &qh, ())),
            _ => None,
        };
        queue.roundtrip(&mut state)?;
        Ok(Self {
            conn,
            queue,
            state,
            method,
        })
    }
    pub fn check(&self) -> Value {
        json!({"input_method": self.method.is_some() && !self.state.unavailable,
               "input_method_taken": self.state.unavailable,
               "virtual_keyboard": self.state.vk_manager.is_some()})
    }
    /// Reads pending compositor events without blocking.
    pub fn dispatch(&mut self) -> Result<()> {
        self.conn.flush()?;
        if let Some(guard) = self.queue.prepare_read() {
            let fd = guard.connection_fd().as_raw_fd();
            let mut poll = libc::pollfd {
                fd,
                events: libc::POLLIN,
                revents: 0,
            };
            if unsafe { libc::poll(&mut poll, 1, 0) } > 0 {
                guard.read()?;
            }
        }
        self.queue.dispatch_pending(&mut self.state)?;
        Ok(())
    }
    pub fn insert(&mut self, text: &str, keyboard: bool) -> Result<Value> {
        self.queue.roundtrip(&mut self.state)?;
        let text = printable(text);
        if text.trim().is_empty() {
            return Ok(json!({"ok":true,"method":"none"}));
        }
        if let Some(method) = &self.method {
            if !self.state.unavailable && self.state.current.active {
                if sensitive(&self.state.current) {
                    return Ok(json!({"ok":false,"reason":"sensitive",
                        "message":"The focused field is a password or other sensitive field; dictation was not typed there."}));
                }
                method.commit_string(text);
                method.commit(self.state.serial);
                self.queue.roundtrip(&mut self.state)?;
                return Ok(json!({"ok":true,"method":"input-method"}));
            }
        }
        if !keyboard {
            return Ok(json!({"ok":false,"reason":"no-field",
                "message":"No text field is accepting dictation. Click into a text field and keep talking."}));
        }
        self.type_keys(&text)?;
        Ok(json!({"ok":true,"method":"virtual-keyboard"}))
    }
    /// One temporary keymap with a key per distinct character, as wtype does.
    fn type_keys(&mut self, text: &str) -> Result<()> {
        let (Some(manager), Some(seat)) = (&self.state.vk_manager, &self.state.seat) else {
            return Err("This compositor offers no virtual keyboard for apps without text-input support".into());
        };
        let mut chars: Vec<char> = text.chars().collect();
        chars.sort_unstable();
        chars.dedup();
        if chars.len() > 240 {
            return Err("Too many distinct characters to type at once".into());
        }
        let keymap = keymap(&chars);
        let fd = memfd(keymap.as_bytes())?;
        let qh = self.queue.handle();
        let keyboard = manager.create_virtual_keyboard(seat, &qh, ());
        keyboard.keymap(1, fd.as_fd(), keymap.len() as u32 + 1);
        self.queue.roundtrip(&mut self.state)?;
        let mut time = 0_u32;
        for c in text.chars() {
            let key = chars.binary_search(&c).unwrap() as u32 + 1;
            keyboard.key(time, key, 1);
            time += 2;
            keyboard.key(time, key, 0);
            time += 2;
            self.conn.flush()?;
            std::thread::sleep(std::time::Duration::from_millis(2));
        }
        self.queue.roundtrip(&mut self.state)?;
        keyboard.destroy();
        self.conn.flush()?;
        Ok(())
    }
}

/// An XKB keymap mapping evdev key N (keycode N+8) to the Nth character.
fn keymap(chars: &[char]) -> String {
    let mut codes = String::new();
    let mut symbols = String::new();
    for (i, c) in chars.iter().enumerate() {
        let code = i + 9;
        codes.push_str(&format!("    <K{code}> = {code};\n"));
        symbols.push_str(&format!("    key <K{code}> {{ [ U{:04X} ] }};\n", *c as u32));
    }
    format!(
        "xkb_keymap {{\n  xkb_keycodes \"jarvis\" {{\n    minimum = 8;\n    maximum = {};\n{codes}  }};\n  xkb_types \"jarvis\" {{ include \"complete\" }};\n  xkb_compatibility \"jarvis\" {{ include \"complete\" }};\n  xkb_symbols \"jarvis\" {{\n{symbols}  }};\n}};\n",
        chars.len() + 9
    )
}

fn memfd(bytes: &[u8]) -> Result<std::os::fd::OwnedFd> {
    use std::os::fd::FromRawFd;
    let fd = unsafe { libc::memfd_create(c"jarvis-keymap".as_ptr(), libc::MFD_CLOEXEC) };
    if fd < 0 {
        return Err(std::io::Error::last_os_error().into());
    }
    let owned = unsafe { std::os::fd::OwnedFd::from_raw_fd(fd) };
    let mut file = std::fs::File::from(owned.try_clone()?);
    file.write_all(bytes)?;
    file.write_all(&[0])?;
    Ok(owned)
}


#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn secrets_are_refused() {
        let password = Field {
            active: true,
            hint: 0,
            purpose: ContentPurpose::Password.into(),
        };
        assert!(sensitive(&password));
        let hidden = Field {
            active: true,
            hint: ContentHint::HiddenText.bits(),
            purpose: 0,
        };
        assert!(sensitive(&hidden));
        assert!(!sensitive(&Field {
            active: true,
            hint: ContentHint::Spellcheck.bits(),
            purpose: ContentPurpose::Normal.into(),
        }));
    }
    #[test]
    fn keymap_maps_each_distinct_character_once() {
        let map = keymap(&['a', 'ب']);
        assert!(map.contains("<K9> = 9;") && map.contains("U0061") && map.contains("U0628"));
        assert!(map.contains("maximum = 11;"));
    }
}
