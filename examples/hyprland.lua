-- Optional Jarvis shortcuts for a Lua Hyprland config (0.55+). Review and add
-- them to ~/.config/hypr/hyprland.lua yourself; Jarvis never edits it.
local jarvis = "/absolute/path/to/checkout/run-jarvis"

-- Listen for one command without saying the wake word.
hl.bind("SUPER + V", hl.dsp.exec_cmd('"' .. jarvis .. '" listen'))
-- Mute / unmute the voice microphone.
hl.bind("SUPER + SHIFT + V", hl.dsp.exec_cmd('"' .. jarvis .. '" mute'))
