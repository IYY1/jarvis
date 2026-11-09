"""Installed Start-menu/Store applications, launched through Windows Shell."""

from pathlib import PureWindowsPath
from ..apps import App, AppRegistry as BaseRegistry, utility
from ..commands import CommandError, normalize, spoken_numbers
from .system import com_context


class WindowsApp(App):
    def classes(self):
        # AUMIDs distinguish packaged apps and browser app shortcuts. A verified
        # executable path is an additional identity only for a plain app entry.
        result = {self.id.casefold()}
        if "!" in self.id:   # a packaged app: its windows may only show their package's folder
            result.add(self.id.split("!", 1)[0].casefold())
        if self.exec and not self.wm_class:
            result.add(str(PureWindowsPath(self.exec)).casefold())
        return result


class AppRegistry(BaseRegistry):
    def _scan(self):
        try:
            self._scan_shell()
            self.scan_error = None
        except Exception as exc:
            self.scan_error = f"Windows installed-app discovery failed: {exc}"
            # Keep existing names during a temporary Shell outage. Settings,
            # mute and diagnostics must remain available even without Explorer.

    def _scan_shell(self):
        import win32com.client

        apps, index = {}, {}
        with com_context():
            shell = win32com.client.Dispatch("Shell.Application")
            folder = shell.NameSpace("shell:AppsFolder")
            if folder is None:
                raise CommandError("Windows application discovery is unavailable")
            for item in folder.Items():
                try:
                    identity, name = str(item.Path), str(item.Name)
                    if (
                        not identity
                        or not name
                        or len(identity) > 4096
                        or len(name) > 256
                        or utility(name)   # "Uninstall X", setup and updater shortcuts
                    ):
                        continue
                    target = (
                        item.ExtendedProperty("System.Link.TargetParsingPath") or ""
                    )
                    arguments = item.ExtendedProperty("System.Link.Arguments") or ""
                    app = WindowsApp(
                        identity, name, identity, str(target), wm_class=str(arguments)
                    )
                    apps[identity] = app
                    for key in {normalize(name), spoken_numbers(normalize(name))}:
                        if key:
                            index.setdefault(key, []).append(app)
                except Exception:
                    continue
        for alias, identity in self.aliases.items():
            if identity in apps:
                index[normalize(alias)] = [apps[identity]]
        self.apps, self.index = apps, index

    def launch(self, app):
        if app.id not in self.apps or self.apps[app.id] is not app:
            raise CommandError("Application is not in the installed-app registry")
        # The shell handles AUMID activation, including Store applications. No
        # user phrase is ever interpreted by cmd.exe or PowerShell.
        import win32api
        import win32con

        result = win32api.ShellExecute(
            0,
            "open",
            "explorer.exe",
            '"shell:AppsFolder\\' + app.id + '"',
            None,
            win32con.SW_SHOWNORMAL,
        )
        if result <= 32:
            raise CommandError("Windows could not open this application")
