fn main() {
    let _ = std::fs::create_dir_all("resources/sidecar");
    tauri_build::build()
}
