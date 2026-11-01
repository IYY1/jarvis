fn main() {
    println!("cargo:rerun-if-changed=shim.c");
    println!("cargo:rerun-if-changed=vendor/c-api.h");
    cc::Build::new()
        .file("shim.c")
        .warnings(true)
        .compile("kws_config");
}
