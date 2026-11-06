"""Keep launched desktop apps out of the controller service's kill group."""
import os
import shutil

from .commands import CommandError


def desktop_argv(argv: list[str]) -> list[str]:
    if os.environ.get("JARVIS_MANAGED_SERVICE") != "1":
        return argv
    runner = shutil.which("systemd-run")
    if not runner:
        raise CommandError("systemd-run is required to launch desktop apps from the installed user service")
    # Scope inherits the actual graphical environment, but has an independent
    # cgroup. Disable manager-side argument expansion; paths may contain '$'.
    return [runner, "--user", "--scope", "--collect", "--quiet",
            "--expand-environment=no", "--", *argv]
