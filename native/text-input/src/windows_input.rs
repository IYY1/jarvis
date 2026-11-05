//! Windows backend: UI Automation identifies the focused element and refuses
//! password fields; SendInput types the text as Unicode characters, so the
//! keyboard layout does not matter and no Enter key is ever pressed.
//!
//! Windows blocks typing into apps running as administrator from a normal
//! process (UIPI); that shows as a short SendInput count and an error.
use crate::{printable, Result};
use serde_json::{json, Value};
use windows::Win32::System::Com::{
    CoCreateInstance, CoInitializeEx, CLSCTX_INPROC_SERVER, COINIT_MULTITHREADED,
};
use windows::Win32::UI::Accessibility::{
    CUIAutomation, IUIAutomation, UIA_ComboBoxControlTypeId, UIA_DocumentControlTypeId,
    UIA_EditControlTypeId,
};
use windows::Win32::UI::Input::KeyboardAndMouse::{
    SendInput, INPUT, INPUT_0, INPUT_KEYBOARD, KEYBDINPUT, KEYEVENTF_KEYUP, KEYEVENTF_UNICODE,
    VIRTUAL_KEY,
};

pub struct Typer {
    automation: Option<IUIAutomation>,
}

/// What UI Automation says about the focused element.
enum Focus {
    Text,
    Password,
    Other,
    Unknown,
}

impl Typer {
    pub fn connect() -> Result<Self> {
        let automation = unsafe {
            let _ = CoInitializeEx(None, COINIT_MULTITHREADED);
            CoCreateInstance::<_, IUIAutomation>(&CUIAutomation, None, CLSCTX_INPROC_SERVER).ok()
        };
        Ok(Self { automation })
    }
    pub fn check(&self) -> Value {
        json!({"input_method": false, "virtual_keyboard": true,
               "focus_details": self.automation.is_some()})
    }
    pub fn dispatch(&mut self) -> Result<()> {
        Ok(())
    }
    fn focus(&self) -> Focus {
        let Some(automation) = &self.automation else {
            return Focus::Unknown;
        };
        unsafe {
            let Ok(element) = automation.GetFocusedElement() else {
                return Focus::Unknown;
            };
            if element.CurrentIsPassword().is_ok_and(|p| p.as_bool()) {
                return Focus::Password;
            }
            match element.CurrentControlType() {
                Ok(t) if t == UIA_EditControlTypeId
                    || t == UIA_DocumentControlTypeId
                    || t == UIA_ComboBoxControlTypeId =>
                {
                    Focus::Text
                }
                Ok(_) => Focus::Other,
                Err(_) => Focus::Unknown,
            }
        }
    }
    /// `keyboard` allows typing when the focused element is not recognizably a
    /// text field (games, terminals and custom-drawn apps often look like that).
    pub fn insert(&mut self, text: &str, keyboard: bool) -> Result<Value> {
        let text = printable(text);
        if text.trim().is_empty() {
            return Ok(json!({"ok":true,"method":"none"}));
        }
        match self.focus() {
            Focus::Password => {
                return Ok(json!({"ok":false,"reason":"sensitive",
                    "message":"The focused field is a password field; dictation was not typed there."}))
            }
            Focus::Other | Focus::Unknown if !keyboard => {
                return Ok(json!({"ok":false,"reason":"no-field",
                    "message":"No text field is focused. Click into a text field and keep talking."}))
            }
            _ => {}
        }
        let mut inputs = Vec::with_capacity(text.len() * 4);
        for unit in text.encode_utf16() {
            for flags in [KEYEVENTF_UNICODE, KEYEVENTF_UNICODE | KEYEVENTF_KEYUP] {
                inputs.push(INPUT {
                    r#type: INPUT_KEYBOARD,
                    Anonymous: INPUT_0 {
                        ki: KEYBDINPUT {
                            wVk: VIRTUAL_KEY(0),
                            wScan: unit,
                            dwFlags: flags,
                            time: 0,
                            dwExtraInfo: 0,
                        },
                    },
                });
            }
        }
        let sent = unsafe { SendInput(&inputs, std::mem::size_of::<INPUT>() as i32) };
        if sent as usize != inputs.len() {
            return Ok(json!({"ok":false,"reason":"blocked",
                "message":"Windows blocked typing into this app. Apps running as administrator cannot receive dictation from Jarvis."}));
        }
        Ok(json!({"ok":true,"method":"send-input"}))
    }
}
