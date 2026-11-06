"""Launch the optional Rust controller without retaining a Python process."""
import os
from pathlib import Path


def selected_binary():
    from .host import is_windows
    if is_windows():
        return None
    selection = os.environ.get("JARVIS_CONTROLLER", "python")
    if selection == "python":
        return None
    if selection != "rust":
        raise ValueError("JARVIS_CONTROLLER must be python or rust")
    packaged = Path(__file__).with_name("bin") / "jarvis-controller"
    checkout = Path(__file__).resolve().parents[2] / "native/controller/target/release/jarvis-controller"
    for path in (packaged, checkout):
        if path.is_file() and os.access(path, os.X_OK):
            return path
    raise ValueError("Rust controller missing. Run scripts/build_controller.py first.")
