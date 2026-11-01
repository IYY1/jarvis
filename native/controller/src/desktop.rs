use crate::{apps::App, err, util, Result};
use serde_json::{json, Value};
use std::{env, path::PathBuf};

pub async fn query(name: &str) -> Result<Value> {
    if ![
        "monitors",
        "clients",
        "activewindow",
        "activeworkspace",
        "version",
    ]
    .contains(&name)
    {
        return err("Unsupported desktop query");
    }
    Ok(serde_json::from_str(
        &util::query(&["hyprctl".into(), "-j".into(), name.into()], 2).await?,
    )?)
}
pub fn dispatch_args(action: &str, target: &Value) -> Result<(Vec<String>, Vec<String>)> {
    let (legacy, lua) = match action {
        "workspace" | "move_workspace" => {
            let n = target
                .as_u64()
                .filter(|n| (1..=10).contains(n))
                .ok_or("Invalid workspace")?;
            if action == "workspace" {
                (
                    vec!["workspace".into(), n.to_string()],
                    format!("hl.dsp.focus({{ workspace = \"{n}\" }})"),
                )
            } else {
                (
                    vec!["movetoworkspacesilent".into(), n.to_string()],
                    format!("hl.dsp.window.move({{ workspace = \"{n}\", follow = false }})"),
                )
            }
        }
        "fullscreen" | "unfullscreen" => {
            let state = if action == "fullscreen" {
                "set"
            } else {
                "unset"
            };
            (
                vec!["fullscreen".into(), format!("0 {state}")],
                format!(
                    "hl.dsp.window.fullscreen({{ action = \"{state}\", mode = \"fullscreen\" }})"
                ),
            )
        }
        "focus_address" | "close_address" => {
            let address = target.as_str().ok_or("Invalid window address")?;
            if !address
                .strip_prefix("0x")
                .is_some_and(|s| !s.is_empty() && s.bytes().all(|b| b.is_ascii_hexdigit()))
            {
                return err("Invalid window address");
            }
            if action == "close_address" {
                (
                    vec!["closewindow".into(), format!("address:{address}")],
                    format!("hl.dsp.window.close({{ window = \"address:{address}\" }})"),
                )
            } else {
                (
                    vec!["focuswindow".into(), format!("address:{address}")],
                    format!("hl.dsp.focus({{ window = \"address:{address}\" }})"),
                )
            }
        }
        _ => return err("Unsupported desktop action"),
    };
    Ok((legacy, vec![lua]))
}
#[derive(Default)]
pub struct Desktop {
    syntax: Option<bool>,
}
impl Desktop {
    pub async fn dispatch(&mut self, action: &str, target: &Value) -> Result<()> {
        let (legacy, lua) = dispatch_args(action, target)?;
        let root = env::var_os("XDG_CONFIG_HOME")
            .map(PathBuf::from)
            .unwrap_or_else(|| util::home().join(".config"));
        let preferred = self
            .syntax
            .unwrap_or_else(|| root.join("hypr/hyprland.lua").is_file());
        let mut detail = String::new();
        for syntax in [preferred, !preferred] {
            let mut argv = vec!["hyprctl".into(), "dispatch".into()];
            argv.extend(if syntax { lua.clone() } else { legacy.clone() });
            match util::query(&argv, 2).await {
                Ok(output)
                    if !["error", "invalid", "unknown"]
                        .iter()
                        .any(|s| output.to_lowercase().contains(s)) =>
                {
                    self.syntax = Some(syntax);
                    return Ok(());
                }
                Ok(output) => detail = output,
                Err(e) => detail = e.to_string(),
            }
        }
        err(format!(
            "Hyprland rejected the action: {}",
            detail.chars().take(200).collect::<String>()
        ))
    }
    pub async fn running(&self, app: &App) -> Result<Vec<Value>> {
        let classes = app.classes();
        let data = query("clients").await?;
        let mut windows: Vec<_> = data
            .as_array()
            .ok_or("Invalid Hyprland clients")?
            .iter()
            .filter(|c| {
                ["class", "initialClass"]
                    .iter()
                    .any(|key| classes.contains(&c[key].as_str().unwrap_or("").to_lowercase()))
            })
            .cloned()
            .collect();
        windows.sort_by_key(|c| c["focusHistoryID"].as_i64().unwrap_or(9999));
        Ok(windows)
    }
}
pub async fn volume(action: &str) -> Result<()> {
    let args = match action {
        "volume_down" => vec!["set-volume", "@DEFAULT_AUDIO_SINK@", "5%-"],
        "volume_up" => vec!["set-volume", "-l", "1.0", "@DEFAULT_AUDIO_SINK@", "5%+"],
        "volume_mute" => vec!["set-mute", "@DEFAULT_AUDIO_SINK@", "1"],
        "volume_unmute" => vec!["set-mute", "@DEFAULT_AUDIO_SINK@", "0"],
        _ => return err("Unsupported volume action"),
    };
    let mut argv = vec!["wpctl".into()];
    argv.extend(args.into_iter().map(str::to_owned));
    util::query(&argv, 2).await?;
    Ok(())
}
pub async fn monitors() -> Result<Value> {
    let value = query("monitors").await?;
    let list = value
        .as_array()
        .ok_or("Hyprland returned an invalid monitor list.")?;
    Ok(json!(list.iter().filter(|m|m["name"].is_string()&&m["disabled"]!=true).map(|m|json!({"name":m["name"],"description":m.get("description").cloned().unwrap_or(json!("")),"width":m["width"],"height":m["height"],"colorManagementPreset":m["colorManagementPreset"],"currentFormat":m["currentFormat"],"focused":m["focused"]==true})).collect::<Vec<_>>()))
}
pub async fn audio_sources() -> Result<Value> {
    let value: Value = serde_json::from_str(
        &util::query(
            &[
                "pactl".into(),
                "--format=json".into(),
                "list".into(),
                "sources".into(),
            ],
            3,
        )
        .await?,
    )?;
    Ok(json!(value.as_array().ok_or("Invalid audio source list")?.iter().filter(|d|d["name"].is_string()).map(|d|json!({"name":d["name"],"description":d.get("description").unwrap_or(&d["name"]),"monitor":!d["monitor_of_sink"].is_null()&&d["monitor_of_sink"]!=""&&d["monitor_of_sink"]!=4294967295u64})).collect::<Vec<_>>()))
}
pub async fn devices() -> Result<Value> {
    let value: Value = serde_json::from_str(&util::query(&["pw-dump".into()], 3).await?)?;
    let mut result = vec![json!({"id":"","name":"System default input"})];
    for node in value.as_array().ok_or("Invalid PipeWire device list")? {
        let p = &node["info"]["props"];
        if p["media.class"] == "Audio/Source" && p["node.name"].is_string() {
            result.push(json!({"id":p["node.name"],"name":p.get("node.description").unwrap_or(&p["node.name"])}));
        }
    }
    Ok(json!(result))
}
