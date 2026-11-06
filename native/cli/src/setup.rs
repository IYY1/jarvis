use jarvis_controller::{err, util, voice, Result};
use std::{env, fs, os::unix::fs::PermissionsExt, path::PathBuf};

fn quote(value: &str, desktop: bool) -> Result<String> {
    if value.contains(['\n', '\r', '\0']) {
        return err("Launcher paths cannot contain control characters");
    }
    let value = if desktop {
        let mut escaped = String::new();
        for c in value.chars() {
            match c {
                '%' => escaped.push_str("%%"),
                '\\' | '"' | '`' | '$' => {
                    escaped.push('\\');
                    escaped.push(c);
                }
                _ => escaped.push(c),
            }
        }
        escaped.replace('\\', "\\\\")
    } else {
        value
            .replace('\\', "\\\\")
            .replace('"', "\\\"")
            .replace('%', "%%")
            .replace('$', "$$")
    };
    Ok(format!("\"{value}\""))
}
pub async fn install(service: bool, replace: bool, dry_run: bool) -> Result<()> {
    let cli = quote(&env::current_exe()?.to_string_lossy(), true)?;
    let data = env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| util::home().join(".local/share"));
    let config = env::var_os("XDG_CONFIG_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| util::home().join(".config"));
    let mut files=vec![(data.join("applications/org.jarvis.Controller.desktop"),format!("[Desktop Entry]\nType=Application\nName=Jarvis\nGenericName=Local voice controller\nComment=Control Hyprland by voice and configure screen recording\nExec={cli} ui\nIcon=audio-input-microphone\nTerminal=false\nCategories=Utility;Accessibility;\nKeywords=voice;Hyprland;microphone;recording;jarvis;assistant;\nStartupNotify=true\nActions=Recorder;\n\n[Desktop Action Recorder]\nName=Open recorder\nExec={cli} ui --page recording\n"))];
    if service {
        let controller = quote(&crate::controller()?.to_string_lossy(), false)?;
        files.push((config.join("systemd/user/jarvis.service"),format!("[Unit]\nDescription=Jarvis local voice controller\nAfter=graphical-session.target pipewire.service\nPartOf=graphical-session.target\n\n[Service]\nType=simple\nEnvironment=JARVIS_MANAGED_SERVICE=1\nExecStart={controller}\nRestart=on-failure\nRestartSec=3\nTimeoutStopSec=25\nKillMode=mixed\nUMask=0077\n\n[Install]\nWantedBy=graphical-session.target\n")));
    }
    for (path, content) in &files {
        if fs::symlink_metadata(path).is_ok() {
            if path.is_symlink() || !path.is_file() {
                return err(format!(
                    "Refusing non-regular destination {}",
                    path.display()
                ));
            }
            if fs::read_to_string(path)? != *content && !replace {
                return err(format!(
                    "{} already exists; review it or pass --replace",
                    path.display()
                ));
            }
        }
    }
    for (path, content) in files {
        println!("{}", path.display());
        if dry_run {
            println!("{content}");
        } else {
            util::atomic_write(&path, content.as_bytes())?;
            fs::set_permissions(path, fs::Permissions::from_mode(0o644))?;
        }
    }
    if service && !dry_run {
        util::query(
            &["systemctl".into(), "--user".into(), "daemon-reload".into()],
            10,
        )
        .await?;
    }
    println!(
        "{}",
        if dry_run {
            "Preview only."
        } else {
            "Installed for your user. Service enablement is unchanged; configure startup in Jarvis."
        }
    );
    if voice::project_root().is_none() {
        eprintln!("Keep the native binaries at their installed paths. The settings window requires JARVIS_UI_PYTHON or Python/GTK on PATH.");
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn launcher_quoting() {
        assert_eq!(quote("/a b/$x%z", false).unwrap(), "\"/a b/$$x%%z\"");
        assert_eq!(quote("/a b/$x%z", true).unwrap(), "\"/a b/\\\\$x%%z\"");
        assert!(quote("/a\nb", false).is_err());
    }
}
