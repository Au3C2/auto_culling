#!/usr/bin/env python3
"""packaging/build_gui.py — automated packaging builder for AutoCulling Tauri GUI.

Produces:
- macOS: AutoCulling_v{ver}_macos_arm64.dmg (and AutoCulling.app)
- Windows: AutoCulling_v{ver}_win_x64_setup.exe (NSIS) and AutoCulling_v{ver}_win_x64_portable.zip
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC_TAURI = ROOT / "src-tauri"


def _version() -> str:
    env_ver = os.environ.get("CULL_VERSION", "").strip().lstrip("v")
    if env_ver:
        return env_ver
    toml = ROOT / "pyproject.toml"
    if toml.exists():
        for line in toml.read_text().splitlines():
            if line.strip().startswith("version"):
                m = re.search(r'"([^"]+)"', line)
                if m:
                    return m.group(1).strip()
    return "0.1"


def get_rust_target_triple() -> str:
    """Detect current rust host target triple."""
    try:
        res = subprocess.run(["rustc", "-vV"], capture_output=True, text=True, check=True)
        for line in res.stdout.splitlines():
            if line.startswith("host:"):
                return line.split(":", 1)[1].strip()
    except Exception:
        pass
    # Fallbacks
    sys_plat = sys.platform
    if sys_plat == "darwin":
        return "aarch64-apple-darwin" if platform.machine() == "arm64" else "x86_64-apple-darwin"
    elif sys_plat == "win32":
        return "x86_64-pc-windows-msvc"
    return "x86_64-unknown-linux-gnu"


def build_sidecar_binary() -> Path:
    """Compile cull_photos into a windowed onedir sidecar using PyInstaller.

    onedir (not onefile): the onefile form re-extracts the whole ~160 MB
    bundle to a fresh temp dir on EVERY launch and re-pays the macOS
    signature-verification tax (~15-25 s). The onedir form starts instantly
    and is shipped via Tauri resources.
    """
    print("=== Step 1: Building Python Sidecar (onedir) ===")
    spec_path = ROOT / "cull_sidecar.spec"
    if not spec_path.exists():
        raise FileNotFoundError("cull_sidecar.spec not found in project root")

    # Guard against the 0-byte placeholder trap: tauri build silently packs
    # whatever sits in src-tauri/binaries/, so a stale empty file produces a
    # broken DMG whose sidecar fails with Permission denied at runtime.
    triple = get_rust_target_triple()
    ext = ".exe" if sys.platform == "win32" else ""
    legacy_bin = SRC_TAURI / "binaries" / f"cull-sidecar-{triple}{ext}"
    if legacy_bin.exists() and legacy_bin.stat().st_size < 1_000_000:
        legacy_bin.unlink()

    _pyi = "pyinstaller.exe" if sys.platform == "win32" else "pyinstaller"
    pyinstaller = ROOT / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / _pyi
    if not pyinstaller.exists():
        pyinstaller = Path(shutil.which(_pyi) or _pyi)

    env = dict(os.environ)
    env["CULL_ONEDIR"] = "1"
    cmd = [str(pyinstaller), str(spec_path), "--noconfirm", "--clean"]
    print(f"Running: {' '.join(cmd)}")
    subprocess.run(cmd, cwd=str(ROOT), check=True, env=env)

    sidecar_dir = ROOT / "dist" / "cull_sidecar"
    sidecar_bin = sidecar_dir / ("cull_sidecar.exe" if sys.platform == "win32" else "cull_sidecar")
    if not sidecar_bin.exists():
        raise FileNotFoundError(f"Expected onedir sidecar binary at {sidecar_bin}")

    print(f"PASS: Compiled onedir sidecar: {sidecar_dir} ({sum(f.stat().st_size for f in sidecar_dir.rglob('*') if f.is_file()) / 1024 / 1024:.1f} MB)")

    # Ship the onedir via Tauri resources (src-tauri/resources/sidecar/)
    res_dir = SRC_TAURI / "resources" / "sidecar"
    shutil.rmtree(res_dir, ignore_errors=True)
    shutil.copytree(sidecar_dir, res_dir)
    if sys.platform != "win32":
        os.chmod(sidecar_bin, 0o755)

    print(f"PASS: Staged sidecar onedir to {res_dir}")
    return sidecar_bin


def build_tauri_gui() -> None:
    """Run tauri build to produce desktop bundle packages."""
    print("\n=== Step 2: Building Tauri Desktop GUI Packages ===")
    
    # Try npx tauri build or cargo tauri build
    tauri_cli = None
    if shutil.which("cargo-tauri"):
        tauri_cli = ["cargo", "tauri", "build"]
    elif shutil.which("npx"):
        # Use the resolved path: on Windows "npx" is npx.cmd, which
        # CreateProcess cannot spawn by bare name (WinError 2).
        tauri_cli = [shutil.which("npx"), "@tauri-apps/cli", "build"]
    else:
        tauri_cli = ["cargo", "tauri", "build"]

    # Bundle type is platform-specific: Windows Tauri only accepts msi/nsis,
    # macOS uses the .app bundle (DMG is assembled by build_macos_dmg).
    bundle_type = "nsis" if sys.platform == "win32" else "app"
    print(f"Running Tauri build: {' '.join(tauri_cli)} --bundles {bundle_type}")
    subprocess.run([*tauri_cli, "--bundles", bundle_type], cwd=str(ROOT), check=True)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def build_macos_dmg(dist_dir: Path, ver: str) -> Path | None:
    """Deterministic DMG assembly from a pre-baked Finder layout.

    Tauri's create-dmg prettify step drives Finder via AppleScript, which
    times out (AppleEvent -1712) in background shells and CI. Instead we
    stage the volume contents ourselves — .app, Applications symlink,
    background image and a committed .DS_Store (captured from the
    human-approved layout) — and let hdiutil compress it. No Finder, no
    AppleScript, byte-identical layout on every machine.
    """
    app_path = SRC_TAURI / "target/release/bundle/macos/AutoCulling.app"
    if not app_path.exists():
        print("WARNING: AutoCulling.app not found to create DMG.")
        return None

    stage = SRC_TAURI / "target/release/dmg-stage"
    shutil.rmtree(stage, ignore_errors=True)
    stage.mkdir(parents=True)

    shutil.copytree(app_path, stage / "AutoCulling.app", symlinks=True)
    (stage / "Applications").symlink_to("/Applications")

    bg_src = SRC_TAURI / "icons/dmg-background.png"
    if bg_src.exists():
        (stage / ".background").mkdir()
        shutil.copy2(bg_src, stage / ".background/dmg-background.png")

    ds_store = ROOT / "packaging/dmg-assets/.DS_Store"
    if ds_store.exists():
        shutil.copy2(ds_store, stage / ".DS_Store")
        print("Injected pre-baked Finder layout (.DS_Store)")
    else:
        print("WARNING: packaging/dmg-assets/.DS_Store missing — window layout will be Finder defaults")

    vol_icns = SRC_TAURI / "icons/icon.icns"
    if vol_icns.exists():
        shutil.copy2(vol_icns, stage / ".VolumeIcon.icns")

    dst_dmg = dist_dir / f"AutoCulling_v{ver}_macos_arm64.dmg"
    if dst_dmg.exists():
        dst_dmg.unlink()
    cmd = [
        "hdiutil", "create",
        "-volname", "AutoCulling",
        "-srcfolder", str(stage),
        "-fs", "HFS+",
        "-ov", "-format", "UDZO",
        str(dst_dmg),
    ]
    print(f"Creating DMG via hdiutil: {' '.join(cmd)}")
    subprocess.run(cmd, check=True)
    shutil.rmtree(stage, ignore_errors=True)
    print(f"Output DMG: {dst_dmg} ({dst_dmg.stat().st_size / 1024 / 1024:.1f} MB)")
    return dst_dmg


def organize_dist_artifacts() -> list[Path]:
    """Copy and standardize packaged artifacts into dist/ directory."""
    print("\n=== Step 3: Organizing Release Distribution Artifacts ===")
    dist_dir = ROOT / "dist"
    dist_dir.mkdir(parents=True, exist_ok=True)
    ver = _version()

    collected_artifacts: list[Path] = []
    bundle_dir = SRC_TAURI / "target/release/bundle"

    if sys.platform == "darwin":
        dmg = build_macos_dmg(dist_dir, ver)
        if dmg is not None:
            collected_artifacts.append(dmg)

    elif sys.platform == "win32":
        # 1. NSIS Setup Exe
        nsis_candidates = sorted(
            (bundle_dir / "nsis").glob("*.exe"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        if nsis_candidates:
            src_exe = nsis_candidates[0]
            dst_exe = dist_dir / f"AutoCulling_v{ver}_win_x64_setup.exe"
            shutil.copy2(src_exe, dst_exe)
            print(f"Output Setup EXE: {dst_exe} ({dst_exe.stat().st_size / 1024 / 1024:.1f} MB)")
            collected_artifacts.append(dst_exe)

        # 2. Portable ZIP — app exe + onedir sidecar resources so the
        #    green build resolves the sidecar exactly like the NSIS install.
        release_exe = SRC_TAURI / "target/release/AutoCulling.exe"
        sidecar_stage = SRC_TAURI / "resources/sidecar"
        if release_exe.exists() and sidecar_stage.exists():
            portable_zip = dist_dir / f"AutoCulling_v{ver}_win_x64_portable.zip"
            with zipfile.ZipFile(portable_zip, "w", zipfile.ZIP_DEFLATED) as z:
                z.write(release_exe, "AutoCulling.exe")
                # Ensure WebView2Loader.dll sits next to AutoCulling.exe
                wv2_candidates = [
                    SRC_TAURI / "target/release/WebView2Loader.dll",
                    SRC_TAURI / "resources/WebView2Loader.dll",
                ]
                for wv2 in wv2_candidates:
                    if wv2.exists():
                        z.write(wv2, "WebView2Loader.dll")
                        break
                for f in sorted(sidecar_stage.rglob("*")):
                    if f.is_file():
                        z.write(f, str(f.relative_to(SRC_TAURI)))
            print(f"Output Portable ZIP: {portable_zip} ({portable_zip.stat().st_size / 1024 / 1024:.1f} MB)")
            collected_artifacts.append(portable_zip)

    # Generate SHA256 checksum files
    for artifact in collected_artifacts:
        sha = sha256_file(artifact)
        sha_file = dist_dir / f"{artifact.name}.sha256"
        sha_file.write_text(f"{sha}  {artifact.name}\n", encoding="utf-8")
        print(f"SHA256: {sha_file.name} -> {sha}")

    return collected_artifacts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-sidecar", action="store_true", help="Skip compiling sidecar binary")
    args = parser.parse_args()

    try:
        if not args.skip_sidecar:
            build_sidecar_binary()
        build_tauri_gui()
        organize_dist_artifacts()
        print("\nAll GUI Packaging steps completed successfully!")
        return 0
    except subprocess.CalledProcessError as e:
        print(f"\nERROR: Build command failed with exit code {e.returncode}")
        return e.returncode
    except Exception as e:
        print(f"\nERROR: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
