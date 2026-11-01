use crate::{
    err,
    policy::{normalize, spoken_numbers},
    util, Result,
};
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet, HashSet},
    env, fs,
    path::{Path, PathBuf},
    sync::LazyLock,
};
use unicode_casefold::UnicodeCaseFold;

fn fold(s: &str) -> String {
    s.case_fold().collect()
}
fn stem(id: &str) -> String {
    static DNS: LazyLock<regex::Regex> =
        LazyLock::new(|| regex::Regex::new(r"^[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+){2,}$").unwrap());
    let s = id.strip_suffix(".desktop").unwrap_or(id);
    if DNS.is_match(s) {
        s.rsplit('.').next().unwrap().into()
    } else {
        s.into()
    }
}
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct App {
    pub id: String,
    pub name: String,
    pub path: String,
    pub exec: String,
    pub icon: String,
    pub wm_class: String,
}
impl App {
    pub fn classes(&self) -> HashSet<String> {
        const WRAPPERS:&str="env sh bash dash zsh fish nice ionice taskset chrt setsid flatpak snap steam lutris heroic bottles-cli itch legendary wine wine64 proton umu-run gamemoderun game-performance prime-run mangohud gamescope xdg-open gio gtk-launch kioclient kioclient5 kde-open kde-open5 dbus-launch dbus-run-session systemd-run uwsm uwsm-app app2unit pkexec sudo java python python3 electron";
        let mut names = vec![
            self.wm_class.clone(),
            self.id.trim_end_matches(".desktop").into(),
            stem(&self.id),
        ];
        if let Some(argv) = shlex::split(&self.exec) {
            if let Some(first) = argv.first() {
                let program = Path::new(first)
                    .file_name()
                    .unwrap_or_default()
                    .to_string_lossy();
                if !WRAPPERS.split_whitespace().any(|w| w == program)
                    && !argv
                        .iter()
                        .skip(1)
                        .any(|a| a.starts_with("--app-id=") || a.starts_with("--app="))
                {
                    names.push(program.into_owned());
                }
                for arg in argv.iter().skip(1) {
                    if let Some(rest) = arg.strip_prefix("steam://rungameid/") {
                        let digits: String =
                            rest.chars().take_while(char::is_ascii_digit).collect();
                        if !digits.is_empty() {
                            names.push(format!("steam_app_{digits}"));
                        }
                    }
                }
            }
        }
        names
            .into_iter()
            .filter(|s| !s.is_empty())
            .map(|s| fold(&s))
            .collect()
    }
    fn display(&self) -> String {
        normalize(&self.name)
            .map(|s| spoken_numbers(&s))
            .unwrap_or_default()
    }
    fn flatpak(&self) -> bool {
        self.path.contains("/flatpak/")
            || self
                .exec
                .split(' ')
                .next()
                .unwrap_or("")
                .ends_with("flatpak")
    }
}
#[derive(Clone)]
pub struct Registry {
    pub apps: BTreeMap<String, App>,
    pub index: BTreeMap<String, Vec<String>>,
    pub aliases: Value,
    fallbacks: BTreeMap<String, String>,
}
pub fn data_dirs() -> Vec<PathBuf> {
    let mut roots = vec![env::var_os("XDG_DATA_HOME")
        .map(PathBuf::from)
        .unwrap_or_else(|| util::home().join(".local/share"))];
    roots.extend(
        env::split_paths(
            &env::var_os("XDG_DATA_DIRS").unwrap_or_else(|| "/usr/local/share:/usr/share".into()),
        )
        .filter(|p| !p.as_os_str().is_empty()),
    );
    roots.extend([
        util::home().join(".local/share/flatpak/exports/share"),
        PathBuf::from("/var/lib/flatpak/exports/share"),
    ]);
    let mut seen = HashSet::new();
    roots
        .into_iter()
        .filter(|p| seen.insert(p.clone()))
        .map(|p| p.join("applications"))
        .collect()
}
fn desktop_files(root: &Path, paths: &mut Vec<PathBuf>, depth: usize) {
    if depth > 16 || paths.len() > 16384 {
        return;
    }
    if let Ok(entries) = fs::read_dir(root) {
        for entry in entries.flatten() {
            let path = entry.path();
            if entry.file_type().is_ok_and(|t| t.is_dir()) {
                desktop_files(&path, paths, depth + 1);
            } else if path.extension().is_some_and(|s| s == "desktop") {
                paths.push(path);
            }
        }
    }
}
fn entry(path: &Path) -> Result<BTreeMap<String, String>> {
    if fs::metadata(path)?.len() > 1024 * 1024 {
        return err("Desktop entry too large");
    }
    let text = fs::read_to_string(path)?;
    let mut active = false;
    let mut values = BTreeMap::new();
    for line in text.lines() {
        let trimmed = line.trim();
        if trimmed.starts_with('[') {
            active = trimmed == "[Desktop Entry]";
            continue;
        }
        if !active || trimmed.starts_with(['#', ';']) {
            continue;
        }
        if let Some((key, value)) = trimmed.split_once('=') {
            values.insert(key.trim().to_lowercase(), value.trim().into());
        }
    }
    Ok(values)
}
fn boolean(map: &BTreeMap<String, String>, key: &str) -> Result<bool> {
    match map.get(key).map(|s| s.to_lowercase()).as_deref() {
        None | Some("false" | "no" | "off" | "0") => Ok(false),
        Some("true" | "yes" | "on" | "1") => Ok(true),
        _ => err("Invalid desktop boolean"),
    }
}
impl Registry {
    pub fn scan(aliases: &Value, roots: Option<Vec<PathBuf>>) -> Self {
        let mut registry = Self {
            apps: BTreeMap::new(),
            index: BTreeMap::new(),
            aliases: aliases.clone(),
            fallbacks: BTreeMap::new(),
        };
        let mut seen = HashSet::new();
        let desktops: HashSet<_> = env::var("XDG_CURRENT_DESKTOP")
            .unwrap_or_default()
            .split(':')
            .filter(|s| !s.is_empty())
            .map(fold)
            .collect();
        for root in roots.unwrap_or_else(data_dirs) {
            let mut paths = vec![];
            desktop_files(&root, &mut paths, 0);
            paths.sort();
            for path in paths {
                let id = path
                    .strip_prefix(&root)
                    .unwrap()
                    .to_string_lossy()
                    .replace('/', "-");
                if !seen.insert(id.clone()) {
                    continue;
                }
                let parsed = (|| -> Result<App> {
                    let e = entry(&path)?;
                    let get = |key: &str| e.get(key).cloned().unwrap_or_default();
                    if get("type") != "Application"
                        || boolean(&e, "hidden")?
                        || boolean(&e, "nodisplay")?
                    {
                        return err("Hidden entry");
                    }
                    let only: HashSet<_> = get("onlyshowin")
                        .split(';')
                        .filter(|s| !s.is_empty())
                        .map(fold)
                        .collect();
                    let never: HashSet<_> = get("notshowin")
                        .split(';')
                        .filter(|s| !s.is_empty())
                        .map(fold)
                        .collect();
                    if (!only.is_empty() && only.is_disjoint(&desktops))
                        || !never.is_disjoint(&desktops)
                    {
                        return err("Different desktop");
                    }
                    if !get("tryexec").is_empty() && util::which(&get("tryexec")).is_none() {
                        return err("Unavailable program");
                    }
                    if get("name").is_empty()
                        || (get("exec").is_empty() && !boolean(&e, "dbusactivatable")?)
                    {
                        return err("Incomplete entry");
                    }
                    Ok(App {
                        id: id.clone(),
                        name: get("name"),
                        path: fs::canonicalize(&path)?.to_string_lossy().into(),
                        exec: get("exec"),
                        icon: get("icon"),
                        wm_class: get("startupwmclass"),
                    })
                })();
                if let Ok(app) = parsed {
                    let mut indexed = BTreeSet::new();
                    for name in [app.name.clone(), stem(&id)] {
                        if let Ok(n) = normalize(&name) {
                            indexed.insert(spoken_numbers(&n));
                            indexed.insert(n.strip_prefix("the ").unwrap_or(&n).to_owned());
                            indexed.insert(spoken_numbers(n.strip_prefix("the ").unwrap_or(&n)));
                            indexed.insert(n);
                        }
                    }
                    for name in indexed {
                        if !name.is_empty() {
                            registry.index.entry(name).or_default().push(id.clone());
                        }
                    }
                    registry.apps.insert(id, app);
                }
            }
        }
        if let Some(aliases) = aliases.as_object() {
            for (name, target) in aliases {
                if let Some(id) = target.as_str() {
                    if registry.apps.contains_key(id) {
                        if let Ok(name) = normalize(name) {
                            registry.index.insert(name, vec![id.into()]);
                        }
                    }
                }
            }
        }
        let mut used: BTreeSet<_> = registry.index.keys().cloned().collect();
        if let Some(aliases) = aliases.as_object() {
            used.extend(aliases.keys().filter_map(|name| normalize(name).ok()));
        }
        let digits = [
            "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
        ];
        let mut counter = 1;
        for id in registry.apps.keys() {
            let name = loop {
                let name = format!(
                    "application {}",
                    counter
                        .to_string()
                        .bytes()
                        .map(|d| digits[(d - b'0') as usize])
                        .collect::<Vec<_>>()
                        .join(" ")
                );
                counter += 1;
                if used.insert(name.clone()) {
                    break name;
                }
            };
            registry.fallbacks.insert(name, id.clone());
        }
        registry
    }
    pub fn resolve(&self, phrase: &str) -> Result<&App> {
        let name = normalize(phrase)?;
        if let Some(id) = self.fallbacks.get(&name) {
            return self
                .apps
                .get(id)
                .ok_or_else(|| "Application no longer installed".into());
        }
        for (k, v) in self.aliases.as_object().unwrap() {
            if normalize(k)? == name && !self.apps.contains_key(v.as_str().unwrap_or("")) {
                return err(format!(
                    "Alias points to a missing application: {}",
                    v.as_str().unwrap_or("")
                ));
            }
        }
        let Some(ids) = self.index.get(&name) else {
            return err("Application not found. Add an alias in Applications.");
        };
        let mut matches: Vec<_> = ids.iter().filter_map(|id| self.apps.get(id)).collect();
        if matches.len() > 1 {
            let exact: Vec<_> = matches
                .iter()
                .copied()
                .filter(|a| a.display() == name)
                .collect();
            if !exact.is_empty() {
                matches = exact;
            }
            if matches
                .iter()
                .map(|a| a.display())
                .collect::<HashSet<_>>()
                .len()
                == 1
            {
                let classes = matches[0].classes();
                if matches
                    .iter()
                    .skip(1)
                    .all(|a| !classes.is_disjoint(&a.classes()))
                {
                    matches.sort_by_key(|a| {
                        (
                            a.flatpak(),
                            !a.path
                                .starts_with(&util::home().to_string_lossy().to_string()),
                            a.id.len(),
                            a.id.clone(),
                        )
                    });
                    matches.truncate(1);
                }
            }
        }
        if matches.len() > 1 {
            return err(format!(
                "Ambiguous application; add an alias for: {}",
                matches
                    .iter()
                    .map(|a| a.id.as_str())
                    .collect::<Vec<_>>()
                    .join(", ")
            ));
        }
        matches
            .first()
            .copied()
            .ok_or_else(|| "Application not found. Add an alias in Applications.".into())
    }
    pub fn list(&self) -> Vec<Value> {
        let mut apps: Vec<_> = self.apps.values().collect();
        apps.sort_by_key(|a| fold(&a.name));
        apps.into_iter()
            .map(|app| {
                let mut value = json!(app);
                value["aliases"] = json!(self
                    .aliases
                    .as_object()
                    .unwrap()
                    .iter()
                    .filter(|(_, v)| v.as_str() == Some(&app.id))
                    .map(|(k, _)| k)
                    .collect::<Vec<_>>());
                value["speakable"] = Value::Null;
                value
            })
            .collect()
    }
    pub fn voice_commands(&self, parser: &crate::policy::Parser) -> Result<Vec<Value>> {
        let mut apps: Vec<_> = self.apps.values().collect();
        apps.sort_by_key(|app| (fold(&app.name), app.id.clone()));
        let mut result = Vec::with_capacity(apps.len());
        for app in apps {
            let mut names = vec![app.display()];
            names.extend(
                self.index
                    .iter()
                    .filter(|(_, ids)| ids.contains(&app.id))
                    .map(|(name, _)| name.clone()),
            );
            names.extend(
                self.fallbacks
                    .iter()
                    .filter(|(_, id)| **id == app.id)
                    .map(|(name, _)| name.clone()),
            );
            let name = names
                .into_iter()
                .find(|name| {
                    if name.is_empty() {
                        return false;
                    }
                    [("open", "open_app"), ("close", "close_app")]
                        .iter()
                        .all(|(verb, action)| {
                            parser.parse(&format!("{verb} {name}")).is_ok_and(|intent| {
                                intent["action"] == *action
                                    && intent["target"].as_str().is_some_and(|target| {
                                        self.resolve(target)
                                            .is_ok_and(|resolved| resolved.id == app.id)
                                    })
                            })
                        })
                })
                .ok_or_else(|| format!("Cannot assign open/close commands to {}", app.name))?;
            result.push(
                json!({"category":"Applications", "title":app.name, "app_id":app.id,
                "voice_name":name, "examples":[format!("open {name}"),format!("close {name}")]}),
            );
        }
        Ok(result)
    }
    pub fn grammar(&self, parser: &crate::policy::Parser) -> Result<Value> {
        let mut names: BTreeSet<_> = self.index.keys().cloned().collect();
        names.extend(
            self.voice_commands(parser)?
                .iter()
                .filter_map(|row| row["voice_name"].as_str().map(str::to_owned)),
        );
        let mut grammar = parser.grammar(names.iter().cloned());
        let mut ids = serde_json::Map::new();
        for name in &names {
            ids.insert(
                name.clone(),
                json!(self.resolve(name).map(|a| a.id.as_str()).unwrap_or(name)),
            );
        }
        grammar["app_ids"] = Value::Object(ids);
        Ok(grammar)
    }
}
