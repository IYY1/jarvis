//! Explicit-request Linux process-tree accounting. Never polled by the service.
use serde_json::{json, Value};
use std::{
    collections::{BTreeMap, BTreeSet},
    fs,
    path::Path,
};

fn stat(raw: &str) -> Option<(String, u32)> {
    let (_, tail) = raw.split_once('(')?;
    let (name, fields) = tail.rsplit_once(") ")?;
    let mut fields = fields.split_whitespace();
    if fields.next()? == "Z" {
        return None;
    }
    Some((name.into(), fields.next()?.parse().ok()?))
}

fn counters(raw: &str) -> Value {
    let mut result = json!({"rss_bytes":null,"pss_bytes":null});
    for line in raw.lines() {
        let Some((name, rest)) = line.split_once(':') else {
            continue;
        };
        let key = match name {
            "Rss" => "rss_bytes",
            "Pss" => "pss_bytes",
            _ => continue,
        };
        if let Some(bytes) = rest
            .split_whitespace()
            .next()
            .and_then(|v| v.parse::<u64>().ok())
            .and_then(|v| v.checked_mul(1024))
        {
            result[key] = json!(bytes);
        }
    }
    result
}

pub fn snapshot(root: u32) -> Value {
    snapshot_at(root, Path::new("/proc"))
}

fn snapshot_at(root: u32, proc: &Path) -> Value {
    let mut result = json!({"schema_version":1,"platform":"linux","roots":[root],
        "scope":"explicit roots and descendants; add separately launched settings as a root",
        "processes":[],"totals":{"rss_bytes":null,"pss_bytes":null},"complete":false});
    let entries = match fs::read_dir(proc) {
        Ok(entries) => entries,
        Err(e) => {
            result["error"] = json!(e.to_string());
            return result;
        }
    };
    let mut processes = BTreeMap::new();
    let mut children: BTreeMap<u32, Vec<u32>> = BTreeMap::new();
    for entry in entries.flatten() {
        let Some(pid) = entry
            .file_name()
            .to_str()
            .and_then(|s| s.parse::<u32>().ok())
        else {
            continue;
        };
        if let Ok(raw) = fs::read_to_string(entry.path().join("stat")) {
            if let Some((name, ppid)) = stat(&raw) {
                processes.insert(pid, (name, ppid));
                children.entry(ppid).or_default().push(pid);
            }
        }
    }
    let mut included = BTreeSet::new();
    let mut pending = vec![root];
    while let Some(pid) = pending.pop() {
        if included.insert(pid) {
            pending.extend(children.get(&pid).into_iter().flatten());
        }
    }
    let rows: Vec<Value> = included
        .into_iter()
        .map(|pid| {
            let mut row =
                json!({"pid":pid,"ppid":null,"name":null,"rss_bytes":null,"pss_bytes":null});
            if let Some((name, ppid)) = processes.get(&pid) {
                row["name"] = json!(name);
                row["ppid"] = json!(ppid);
            }
            match fs::read_to_string(proc.join(pid.to_string()).join("smaps_rollup")) {
                Ok(raw) => {
                    let memory = counters(&raw);
                    row["rss_bytes"] = memory["rss_bytes"].clone();
                    row["pss_bytes"] = memory["pss_bytes"].clone();
                }
                Err(e) => {
                    row["error"] = json!(e.to_string());
                }
            }
            row
        })
        .collect();
    for key in ["rss_bytes", "pss_bytes"] {
        let total = rows
            .iter()
            .try_fold(0u64, |sum, row| sum.checked_add(row[key].as_u64()?));
        result["totals"][key] = json!(total);
    }
    result["complete"] = json!(
        !rows.is_empty()
            && result["totals"]
                .as_object()
                .unwrap()
                .values()
                .all(|v| v.is_u64())
    );
    result["processes"] = json!(rows);
    result
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn parses_names_with_parentheses_and_missing_counters() {
        assert_eq!(
            stat("7 (capture (test)) S 3 0"),
            Some(("capture (test)".into(), 3))
        );
        assert_eq!(stat("7 (dead) Z 3 0"), None);
        assert_eq!(counters("Rss: 2048 kB\nPss: 1024 kB")["pss_bytes"], 1048576);
        assert!(counters("Rss: 2048 kB")["pss_bytes"].is_null());
    }
    #[test]
    fn live_self_and_missing_root() {
        let own = snapshot(std::process::id());
        assert_eq!(own["complete"], true);
        assert!(own["totals"]["rss_bytes"].as_u64().unwrap() > 0);
        let absent = snapshot(u32::MAX);
        assert_eq!(absent["complete"], false);
        assert!(absent["totals"]["pss_bytes"].is_null());
    }
}
