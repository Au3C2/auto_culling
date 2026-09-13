fn main() {
    let _ = std::fs::create_dir_all("resources/engine");
    // The WebView2 loader is vendored and committed — if it is missing the
    // checkout is broken. Fail loudly instead of writing a 0-byte placeholder
    // that would get packaged into a GUI that cannot start.
    let wv2_path = std::path::Path::new("resources/WebView2Loader.dll");
    let wv2_ok = std::fs::metadata(wv2_path)
        .map(|m| m.is_file() && m.len() > 0)
        .unwrap_or(false);
    if !wv2_ok {
        panic!(
            "resources/WebView2Loader.dll is missing — restore it from git \
             (git checkout -- src-tauri/resources/WebView2Loader.dll); \
             shipping an empty placeholder would break the packaged app."
        );
    }
    tauri_build::build()
}
