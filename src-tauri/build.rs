fn main() {
    let _ = std::fs::create_dir_all("resources/engine");
    // The WebView2 loader is vendored and committed — if it is missing the
    // checkout is broken. Fail loudly instead of writing a 0-byte placeholder
    // that would get packaged into a GUI that cannot start.
    let wv2_path = std::path::Path::new("resources/WebView2Loader.dll");
    if !wv2_path.exists() {
        panic!(
            "resources/WebView2Loader.dll is missing — restore it from git \
             (git checkout -- src-tauri/resources/WebView2Loader.dll); \
             shipping an empty placeholder would break the packaged app."
        );
    }
    tauri_build::build()
}
