"""Arabic model and dictation controls for native Windows settings."""
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, ttk

from .. import models


class VoiceControls:
    def __init__(self, window, language_page, dictation_page):
        self.window = window
        language = window.group(language_page, "Language")
        window.choice(language, "Understands", "recognition", "language", {"English": "en", "Arabic and English": "ar_en"},
                      "Arabic uses an optional local model. English keeps working.")
        window.buttons(language, [("Download Arabic model (118 MB)", self.download), ("Install model from file…", self.install_file)])
        self.model_state = tk.StringVar(value="Arabic model installed." if models.arabic_model_ready() else "Arabic model not installed.")
        window.line(language, ttk.Label(language, textvariable=self.model_state, style="Caption.TLabel", wraplength=520))
        self.downloading = False
        d = window.group(dictation_page, "Dictation", "Types into the focused text field; password fields are refused. Untyped text can be copied.")
        buttons = window.buttons(d, [("Start dictation", self.toggle, "Primary.TButton"),
                                    ("Cancel dictation", lambda: window.call("dictation", action="cancel"))])
        self.button = buttons.winfo_children()[0]
        window.choice(d, "Language", "dictation", "language", {"Match the phrase": "auto", "Always English": "en", "Always Arabic": "ar"})
        window.number(d, "Stop after quiet (seconds)", "dictation", "idle_seconds", 2, 300, 1)
        window.number(d, "Maximum session (minutes)", "dictation", "max_minutes", 1, 60, 1)
        self.stop_phrases = tk.StringVar(value=", ".join(window.config['dictation']['stop_phrases']))
        window.text(d, "Stop phrases", variable=self.stop_phrases, detail="Separate phrases with commas.")
        self.message = tk.StringVar()
        window.line(d, ttk.Label(d, textvariable=self.message, style="Caption.TLabel", wraplength=520))
        window.buttons(d, [("Copy untyped text", self.copy)])
        window.root.bind('<Control-d>', lambda _event: (self.toggle(), 'break')[-1])
        window.variable('recognition', 'language').trace_add('write', lambda *_: window.arabic_enabled.set(window.variable('recognition', 'language').get() == 'ar_en'))

    def toggle(self):
        if not self.window.config['privacy']['onboarded']:
            self.window.show('dashboard')
            self.window.say('Allow the microphone on Home first.', error=True)
            return
        self.window.call('dictation', action='toggle')

    def toggle_arabic(self):
        enabled = self.window.arabic_enabled.get()
        if enabled and not models.arabic_model_ready():
            self.window.arabic_enabled.set(False)
            self.window.show('voice')
            self.window.say('Download or install the Arabic model first.', error=True)
            return
        self.window.variable('recognition', 'language').set('ar_en' if enabled else 'en')
        self.window.changed(0)

    def install_file(self):
        path = filedialog.askopenfilename(parent=self.window.root, title='Install Arabic model archive',
                                         filetypes=[('Model archive', '*.tar.bz2'), ('All files', '*.*')])
        if path:
            self.download(Path(path))

    def download(self, archive=None):
        if self.downloading:
            return
        self.downloading = True
        self.model_state.set('Installing and checking the Arabic model…')
        def run():
            try:
                models.install_arabic(archive)
                return None
            except Exception as error:
                return str(error)
        def done(error):
            self.downloading = False
            self.model_state.set(error or 'Arabic model installed.')
            if error:
                self.window.say(error, error=True)
            else:
                self.window.variable('recognition', 'language').set('ar_en')
                self.window.changed(0)
        self.window.submit(run, done)

    def status(self, status):
        d = status.get('dictation') or {}
        label = 'Stop dictation' if d.get('active') else 'Start dictation'
        self.button.configure(text=label)
        self.window.dictate_button.configure(text=label)
        self.message.set(str(d.get('message') or '') + ('\n' + str(d['held'])[:2000] if d.get('held') else ''))

    def copy(self):
        text = (self.window.status.get('dictation') or {}).get('held', '')
        if text:
            self.window.root.clipboard_clear()
            self.window.root.clipboard_append(text)
            self.window.say('Copied untyped dictation text.')
