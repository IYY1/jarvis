import os

# Tests must never move the real settings, models or videos of whoever runs them.
os.environ["JARVIS_NO_MIGRATION"] = "1"
