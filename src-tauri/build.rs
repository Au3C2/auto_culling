fn main() {
    let _ = std::fs::create_dir_all("resources/sidecar");
    // Ensure resources/WebView2Loader.dll exists so tauri-build never fails on missing resource
    let wv2_path = std::path::Path::new("resources/WebView2Loader.dll");
    if !wv2_path.exists() {
        let _ = std::fs::write(wv2_path, b"");
    }
    tauri_build::build()
}
