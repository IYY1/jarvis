# Dictation

Say **“Jarvis, listen”** or **“Jarvis, listen to me”** (also “start dictation”, or اسمعني), press
**Start dictation** in Overview or Voice, press **Ctrl+D** in the settings
window, or bind a key to `jarvis dictate`:

```ini
# Hyprland example; Jarvis never edits your configuration
bind = SUPER, D, exec, jarvis dictate
```

Jarvis then types what you say into the text field you are in. A small bar at
the top of the screen shows that it is listening, how much it has typed, and
**Stop** / **Cancel** buttons. The bar never takes keyboard focus.

Dictation stops when you:

- end a sentence with a stop phrase (“stop dictation”, وقف الكتابة; change them in Voice → Dictation), which is not typed;
- press Stop, the shortcut again, or run `jarvis dictate stop` (Cancel discards speech not yet typed);
- stay quiet for 20 seconds (adjustable), or reach 10 minutes;
- move focus to another window, mute the microphone, or a password field is focused.

## What it does and does not do

- Speech is cut at pauses (0.6 s) and each piece is transcribed locally:
  Arabic with the [Arabic model](arabic.md), English with Whisper. Text
  appears about a second after each pause.
- **The phrase picks the language.** With Arabic on, اسمعني dictates Arabic and
  “listen” dictates English; the button and shortcut dictate Arabic. Voice →
  Dictation → *Always Arabic* / *Always English* overrides this. With Arabic
  off, dictation is English (Jarvis can't hear اسمعني then).
- **Starting from Jarvis's own window** (the Start dictation button) types into
  the next window you click into, not into Jarvis.
- Dictated speech is never parsed as a command, and Jarvis never presses Enter,
  Tab or any other key: newlines become spaces. A terminal therefore never runs
  anything because of dictation.
- Jarvis remembers the window that was focused when dictation started. If focus
  moves to another window, dictation stops **without typing there**.
- **Text that can't be typed goes to the clipboard.** When focus moved, no text
  field is focused, the app accepts no typed text or typing fails, everything
  not typed in this session is copied (wl-copy on Linux, the Windows
  clipboard) and the status says “press Ctrl+V to paste it”. Text meant for a
  password field is never copied; it stays under **Copy text that was not
  typed**, which copies only when you press it. **Also type into apps without
  text-field support** (Voice → Dictation, on by default) is what lets Jarvis
  type into terminals, Electron apps and XWayland windows at all.
- Long continuous speech is cut at 20 seconds. If transcription falls behind,
  at most three pieces wait; later speech is dropped and the status says so.

## How text is typed

| Platform | Method | Password fields | Limits |
| --- | --- | --- | --- |
| Hyprland / Wayland | Input method (`zwp_input_method_v2`), through the `jarvis-type` helper | Refused: the app reports the field type | Only one input method at a time: if fcitx5 or ibus is running, Jarvis falls back to the virtual keyboard. Apps must support text-input-v3 (GTK, Qt, Firefox do; many terminals, Electron apps without `--enable-wayland-ime`, games and XWayland apps don't). |
| Hyprland / Wayland, fallback | Virtual keyboard (`zwp_virtual_keyboard_v1`) with a temporary keymap | Not visible to Jarvis; windows that look like password prompts (pinentry, polkit, KeePassXC, Bitwarden, titles containing “password”) are skipped | Optional: Voice → Dictation → “Also type into apps without text-field support”. Hyprland may briefly show the temporary layout until you press a key. |
| Windows 10/11 | `SendInput` with Unicode characters; UI Automation identifies the focused element | Refused when UI Automation reports a password field | Windows blocks typing into apps running as administrator (UIPI); Jarvis reports it. **Not yet tested on Windows.** |

Verified on Hyprland 0.57 (2026-11-05): Arabic, English and digits were typed
into a GTK 4 text field through the input method, a newline became a space, a
GTK password field was refused, and the virtual-keyboard fallback typed the same
text into an app without input-method support. Electron, terminal and XWayland
apps were not tested.

## Requirements

- The speech model (Voice → Accurate recognition), and the Arabic model for Arabic dictation.
- Linux: a Wayland compositor with the input-method or virtual-keyboard
  protocol (Hyprland has both). The indicator bar uses `gtk4-layer-shell`; without it a
  notification is shown instead.
- Nothing is downloaded during dictation and no audio or text is saved.
