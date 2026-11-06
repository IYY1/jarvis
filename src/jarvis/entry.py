"""Explicit internal process modes, including frozen Windows worker dispatch."""


def internal(mode, args=()):
    if mode == "voice":
        from .voice import main

        return main()
    if mode == "daemon":
        from .daemon import main

        return main()
    if mode == "ui":
        from .host import is_windows

        if is_windows():
            from .windows.ui import main
        else:
            from .ui import main
        return main(list(args))
    if mode == "audio":
        from .windows.audio import main

        return main(list(args))
    raise ValueError("Unknown internal Jarvis process mode")


def main():
    import multiprocessing

    multiprocessing.freeze_support()
    from .cli import main as cli_main

    return cli_main()


def gui_main():
    """Windowed bootloader has no stdout/stderr; retain actionable crash details."""
    try:
        return main()
    except Exception:
        import traceback
        from .host import data_dir

        folder = data_dir() / "logs"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / "interface-error.log"
        path.write_text(traceback.format_exc(), encoding="utf-8")
        import ctypes

        ctypes.windll.user32.MessageBoxW(
            None, f"Jarvis could not start. Details: {path}", "Jarvis", 0x10
        )
        return 1
