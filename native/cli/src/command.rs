use jarvis_controller::{err, ipc, policy::normalize, Result};
use serde_json::{json, Value};
use std::{collections::BTreeSet, path::PathBuf};
use unicode_casefold::UnicodeCaseFold;

pub const HELP: &str = "Usage: jarvis [COMMAND]\n\nLocal voice control for Hyprland (native Rust client)\n\n  ui [--page PAGE]           Open the GTK settings window (default)\n  daemon                     Run the Rust controller\n  status | config            Read service state or configuration\n  listen | cancel            Start or cancel one bounded voice command\n  dictate [start|stop|cancel|toggle]\n                             Type speech into the focused text field\n  mute | resume              Pause or resume wake listening\n  execute PHRASE             Execute an allowlisted phrase\n  record start|stop          Control screen recording\n  replay start|stop|save      Control instant replay\n  alias [NAME APP]           List or add voice names for installed apps\n  alias NAME --remove        Remove a voice name\n  waybar                     Stream status as Waybar JSON lines\n  doctor                     Read-only dependency and session checks\n  install-model [--basic] [--archive ZIP]\n                             Download and verify voice models\n  install-model --arabic [--archive TAR.BZ2]\n                             Install the optional Arabic model\n  install-user [--service] [--replace] [--dry-run]\n                             Install user launcher and optional service\n  --version | --help\n";

#[derive(Debug)]
pub enum Command {
    Help,
    Version,
    Daemon,
    Ui(String),
    Doctor,
    Waybar,
    Alias {
        name: Option<String>,
        app: Option<String>,
        remove: bool,
    },
    Install {
        archive: Option<PathBuf>,
        basic: bool,
        arabic: bool,
    },
    Setup {
        service: bool,
        replace: bool,
        dry_run: bool,
    },
    Request(Value),
}

pub fn parse(args: &[String]) -> Result<Command> {
    use Command::*;
    if args.is_empty() {
        return Ok(Ui("dashboard".into()));
    }
    if args.iter().any(|a| a == "--help" || a == "-h") {
        return Ok(Help);
    }
    let rest = &args[1..];
    let no_args = || {
        if rest.is_empty() {
            Ok(())
        } else {
            err("Unexpected arguments")
        }
    };
    match args[0].as_str() {
        "--version" => {
            no_args()?;
            Ok(Version)
        }
        "daemon" => {
            no_args()?;
            Ok(Daemon)
        }
        "doctor" => {
            no_args()?;
            Ok(Doctor)
        }
        "waybar" => {
            no_args()?;
            Ok(Waybar)
        }
        "ui" => match rest {
            [] => Ok(Ui("dashboard".into())),
            [flag, page] if flag == "--page" => Ok(Ui(page.clone())),
            _ => err("Use ui [--page PAGE]"),
        },
        "status" | "config" | "listen" | "cancel" => {
            no_args()?;
            Ok(Request(json!({"op": args[0]})))
        }
        "mute" | "resume" => {
            no_args()?;
            Ok(Request(json!({"op":"mute", "muted":args[0]=="mute"})))
        }
        "dictate" => match rest {
            [] => Ok(Request(json!({"op":"dictation","action":"toggle"}))),
            [action] if ["start", "stop", "cancel", "toggle"].contains(&action.as_str()) => {
                Ok(Request(json!({"op":"dictation","action":action})))
            }
            _ => err("Use dictate [start|stop|cancel|toggle]"),
        },
        "execute" if rest.len() == 1 => Ok(Request(json!({"op":"execute","phrase":rest[0]}))),
        "record" | "replay"
            if rest.len() == 1
                && (rest[0] == "start"
                    || rest[0] == "stop"
                    || (args[0] == "replay" && rest[0] == "save")) =>
        {
            Ok(Request(json!({"op":format!("{}_{}", args[0], rest[0])})))
        }
        "alias" => {
            let mut positional = vec![];
            let mut remove = false;
            let mut literal = false;
            for arg in rest {
                match arg.as_str() {
                    "--" if !literal => literal = true,
                    "--remove" if !literal && !remove => remove = true,
                    _ if !literal && arg.starts_with('-') => {
                        return err(format!("Unknown alias option: {arg}"))
                    }
                    _ => positional.push(arg.clone()),
                }
            }
            if positional.len() > 2 {
                return err("Use alias [NAME APP] or alias NAME --remove");
            }
            Ok(Alias {
                name: positional.first().cloned(),
                app: positional.get(1).cloned(),
                remove,
            })
        }
        "install-model" => {
            let mut archive = None;
            let mut basic = false;
            let mut arabic = false;
            let mut i = 0;
            while i < rest.len() {
                match rest[i].as_str() {
                    "--basic" if !basic => basic = true,
                    "--arabic" if !arabic => arabic = true,
                    "--archive" if archive.is_none() && i + 1 < rest.len() => {
                        i += 1;
                        archive = Some(jarvis_controller::util::expand(&rest[i]));
                    }
                    _ => return err("Use install-model [--basic] [--archive ZIP] or install-model --arabic [--archive TAR.BZ2]"),
                }
                i += 1;
            }
            if arabic && basic {
                return err("Use install-model --arabic on its own");
            }
            Ok(Install {
                archive,
                basic,
                arabic,
            })
        }
        "install-user" => {
            let mut service = false;
            let mut replace = false;
            let mut dry_run = false;
            for arg in rest {
                match arg.as_str() {
                    "--service" if !service => service = true,
                    "--replace" if !replace => replace = true,
                    "--dry-run" if !dry_run => dry_run = true,
                    _ => return err("Use install-user [--service] [--replace] [--dry-run]"),
                }
            }
            Ok(Setup {
                service,
                replace,
                dry_run,
            })
        }
        _ => err(format!("Invalid command or arguments: {}", args[0])),
    }
}

pub fn find_app<'a>(apps: &'a [Value], query: &str) -> Result<&'a Value> {
    let wanted = normalize(query)?;
    for stage in 0..3 {
        let found: Vec<_> = apps
            .iter()
            .filter(|app| {
                let id = app["id"].as_str().unwrap_or("");
                let name = app["name"].as_str().unwrap_or("");
                match stage {
                    0 => id == query || id == format!("{query}.desktop"),
                    1 => normalize(name).is_ok_and(|name| name == wanted),
                    _ => {
                        normalize(name).is_ok_and(|name| name.contains(&wanted))
                            || id.case_fold().collect::<String>().contains(&wanted)
                    }
                }
            })
            .collect();
        let unique: BTreeSet<_> = found.iter().filter_map(|a| a["id"].as_str()).collect();
        if unique.len() == 1 {
            return Ok(found[0]);
        }
        if unique.len() > 1 {
            return err(format!(
                "“{query}” matches several apps: {}. Use the desktop ID.",
                unique.into_iter().collect::<Vec<_>>().join(", ")
            ));
        }
    }
    err(format!(
        "No installed app matches “{query}”. Check the name in your app launcher."
    ))
}

fn checked(response: Value) -> Result<Value> {
    if response["ok"] != true {
        return err(response["error"]
            .as_str()
            .unwrap_or("Jarvis service is not running"));
    }
    Ok(response)
}
pub async fn alias(name: Option<String>, app: Option<String>, remove: bool) -> Result<i32> {
    let response = checked(ipc::request(&json!({"op":"config"})).await?)?;
    let mut config = response["config"].clone();
    if !config["aliases"].is_object() {
        config["aliases"] = json!({});
    }
    let Some(name) = name.filter(|n| !n.is_empty()) else {
        let aliases = config["aliases"].as_object().ok_or("Invalid aliases")?;
        let mut names: Vec<_> = aliases.keys().collect();
        names.sort();
        for name in names {
            println!("{name}  ->  {}", aliases[name].as_str().unwrap_or(""));
        }
        if aliases.is_empty() {
            println!("No voice names yet. Example: jarvis alias streamio stremio");
        }
        return Ok(0);
    };
    let name = name
        .to_lowercase()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    let message = if remove {
        if config["aliases"]
            .as_object_mut()
            .ok_or("Invalid aliases")?
            .remove(&name)
            .is_none()
        {
            return err(format!("No voice name “{name}”"));
        }
        if let Some(prefixes) = config["app_prefixes"].as_object_mut() {
            prefixes.remove(&name);
        }
        format!("Removed “{name}”.")
    } else {
        let query = app.ok_or("Say which app, e.g.: jarvis alias streamio stremio")?;
        let response = checked(ipc::request(&json!({"op":"apps"})).await?)?;
        let app = find_app(
            response["apps"].as_array().ok_or("Invalid app list")?,
            &query,
        )?;
        config["aliases"][&name] = app["id"].clone();
        format!(
            "Saved: say “{}, open {name}” to open {} ({}).",
            config["activation"]["wake_phrase"]
                .as_str()
                .unwrap_or("jarvis"),
            app["name"].as_str().unwrap_or(""),
            app["id"].as_str().unwrap_or("")
        )
    };
    checked(ipc::request(&json!({"op":"save_config","config":config})).await?)?;
    println!("{message}");
    Ok(0)
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn commands_are_validated_before_connecting() {
        for args in [
            vec!["record", "save"],
            vec!["replay", "oops"],
            vec!["execute"],
            vec!["mute", "extra"],
            vec!["alias", "--unknown"],
        ] {
            assert!(parse(&args.into_iter().map(String::from).collect::<Vec<_>>()).is_err());
        }
        let Command::Request(value) = parse(&["resume".into()]).unwrap() else {
            panic!()
        };
        assert_eq!(value, json!({"op":"mute","muted":false}));
    }
    #[test]
    fn application_precedence_and_ambiguity() {
        let apps = vec![
            json!({"id":"foo.desktop","name":"Straße"}),
            json!({"id":"foo-extra.desktop","name":"Other"}),
        ];
        assert_eq!(find_app(&apps, "foo").unwrap()["id"], "foo.desktop");
        assert_eq!(find_app(&apps, "STRASSE").unwrap()["id"], "foo.desktop");
        assert!(find_app(&apps, "desktop")
            .unwrap_err()
            .to_string()
            .contains("foo-extra.desktop, foo.desktop"));
    }
}
