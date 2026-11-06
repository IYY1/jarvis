mod command;
mod doctor;
mod models;
mod setup;
mod watch;

use jarvis_controller::{ipc, voice, Result};
use std::{env, io::Write, os::unix::process::CommandExt, path::PathBuf, process::Command};

pub fn controller() -> Result<PathBuf> {
    if let Some(path) = env::var_os("JARVIS_CONTROLLER_BINARY") {
        return Ok(PathBuf::from(path));
    }
    let mut candidates = vec![env::current_exe()?.with_file_name("jarvis-controller")];
    if let Some(root) = voice::project_root() {
        candidates.push(root.join("src/jarvis/bin/jarvis-controller"));
        candidates.push(root.join("native/controller/target/release/jarvis-controller"));
    }
    candidates
        .into_iter()
        .find(|p| p.is_file())
        .ok_or_else(|| "Build the Rust controller with scripts/build_controller.py first".into())
}

fn exec(argv: &[String]) -> Result<i32> {
    let program = argv.first().ok_or("Empty launcher arguments")?;
    Err(Command::new(program).args(&argv[1..]).exec().into())
}

async fn dispatch(command: command::Command) -> Result<i32> {
    use command::Command::*;
    match command {
        Help => {
            print!("{}", command::HELP);
            Ok(0)
        }
        Version => {
            println!("jarvis {} (Rust)", env!("CARGO_PKG_VERSION"));
            Ok(0)
        }
        Daemon => exec(&[controller()?.to_string_lossy().into_owned()]),
        Ui(page) => exec(&voice::ui_argv(&page)?),
        Doctor => {
            println!("{}", serde_json::to_string_pretty(&doctor::report().await)?);
            Ok(0)
        }
        Alias { name, app, remove } => command::alias(name, app, remove).await,
        Waybar => watch::run().await,
        Install {
            archive,
            basic,
            arabic,
        } => {
            if arabic {
                models::install_arabic(archive.as_deref())?;
            } else {
                models::install(archive.as_deref(), basic)?;
            }
            Ok(0)
        }
        Setup {
            service,
            replace,
            dry_run,
        } => {
            setup::install(service, replace, dry_run).await?;
            Ok(0)
        }
        Request(payload) => {
            let response = ipc::request(&payload).await?;
            println!("{}", serde_json::to_string_pretty(&response)?);
            Ok(if response["ok"] == true { 0 } else { 1 })
        }
    }
}

fn run() -> Result<i32> {
    let args: Vec<String> = env::args().skip(1).collect();
    let command = match command::parse(&args) {
        Ok(command) => command,
        Err(error) => {
            eprintln!("jarvis: {error}\nRun jarvis --help for usage.");
            return Ok(2);
        }
    };
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()?
        .block_on(dispatch(command))
}

fn main() {
    let code = match run() {
        Ok(code) => code,
        Err(error)
            if error
                .downcast_ref::<std::io::Error>()
                .is_some_and(|e| e.kind() == std::io::ErrorKind::BrokenPipe) =>
        {
            0
        }
        Err(error) => {
            eprintln!("Jarvis: {error}");
            1
        }
    };
    let _ = std::io::stdout().flush();
    std::process::exit(code);
}
