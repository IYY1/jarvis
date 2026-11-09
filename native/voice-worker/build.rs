//! Windows: name the worker in Task Manager and file properties ("Jarvis voice and
//! screen" instead of "jarvis-voice"). Needs windres (MinGW, the GNU target's
//! toolchain); without it the build goes on and the worker is only unnamed.
use std::env;
use std::path::PathBuf;
use std::process::Command;

fn main() {
    println!("cargo:rerun-if-changed=build.rs");
    if env::var("CARGO_CFG_TARGET_OS").as_deref() != Ok("windows")
        || env::var("CARGO_CFG_TARGET_ENV").as_deref() != Ok("gnu")
    {
        return;
    }
    let version = env::var("CARGO_PKG_VERSION").unwrap_or_default();
    let mut numbers: Vec<u32> = version.split('.').filter_map(|p| p.parse().ok()).collect();
    numbers.resize(4, 0);
    let numbers = numbers.iter().map(u32::to_string).collect::<Vec<_>>().join(",");
    let out = PathBuf::from(env::var("OUT_DIR").expect("OUT_DIR"));
    let script = out.join("jarvis-voice.rc");
    let object = out.join("jarvis-voice-resource.o");
    let rc = format!(
        r#"1 VERSIONINFO
FILEVERSION {numbers}
PRODUCTVERSION {numbers}
FILEOS 0x40004
FILETYPE 0x1
BEGIN
  BLOCK "StringFileInfo"
  BEGIN
    BLOCK "040904B0"
    BEGIN
      VALUE "CompanyName", "Jarvis"
      VALUE "FileDescription", "Jarvis voice and screen"
      VALUE "FileVersion", "{version}"
      VALUE "InternalName", "jarvis-voice"
      VALUE "OriginalFilename", "jarvis-voice.exe"
      VALUE "ProductName", "Jarvis"
      VALUE "ProductVersion", "{version}"
    END
  END
  BLOCK "VarFileInfo"
  BEGIN
    VALUE "Translation", 0x409, 1200
  END
END
"#
    );
    if std::fs::write(&script, rc).is_err() {
        return;
    }
    let windres = env::var("WINDRES").unwrap_or_else(|_| "windres".into());
    let made = Command::new(&windres)
        .arg(&script)
        .arg("-O")
        .arg("coff")
        .arg("-o")
        .arg(&object)
        .status()
        .is_ok_and(|status| status.success());
    if made {
        println!("cargo:rustc-link-arg-bins={}", object.display());
    } else {
        println!("cargo:warning=windres not found; jarvis-voice.exe builds without a file description");
    }
}
