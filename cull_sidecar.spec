# -*- mode: python ; coding: utf-8 -*-
"""
cull_sidecar.spec — standalone PyInstaller spec for AutoCulling sidecar engine.

Produces a single-file executable `cull_sidecar` (or `cull_sidecar.exe` on Windows)
with `console=False` so no terminal/console window flashes when spawned by Tauri.
The executable communicates with the Tauri Rust backend via Stdio JSON Lines.
"""

import os
import shutil
import sys
from pathlib import Path

is_win = sys.platform == "win32"
# CULL_ONEDIR=1 produces the directory form. The onefile form re-extracts the
# whole 160 MB bundle to a fresh temp dir on EVERY launch (15-25 s macOS
# signature-verification tax; inode-keyed cache never hits across runs). The
# GUI ships the onedir form via Tauri resources — instant start.
onedir = os.environ.get("CULL_ONEDIR") == "1"

_MODEL_STAGE = Path("build") / "_bundle_models"


def _stage_models() -> list[tuple[str, str]]:
    if is_win:
        return [
            ("models/f1_yolov8n.onnx", "models"),
            ("models/yolov8n.onnx", "models"),
            ("models/p4_car_model.onnx", "models"),
        ]
    _MODEL_STAGE.mkdir(parents=True, exist_ok=True)
    out = []
    for src_name, dst_name in (
        ("f1_yolov8n_static.onnx", "f1_yolov8n.onnx"),
        ("yolov8n_static.onnx", "yolov8n.onnx"),
        ("p4_car_model_static_ane.onnx", "p4_car_model.onnx"),
    ):
        src = Path("models") / src_name
        if not src.exists():
            src = Path("models") / dst_name
        dst = _MODEL_STAGE / dst_name
        if not dst.exists() or dst.stat().st_size != src.stat().st_size:
            shutil.copy2(src, dst)
        out.append((str(dst), "models"))
    return out


def _exiftool_datas() -> list[tuple[str, str]]:
    if is_win:
        base = "external/exiftool"
        files = [
            "perl.exe", "perl532.dll", "exiftool.pl",
            "libgcc_s_seh-1.dll", "liblzma-5__.dll",
            "libstdc++-6.dll", "libwinpthread-1.dll"
        ]
        return [(f"{base}/{f}", base) for f in files] \
            + [(f"{base}/lib", f"{base}/lib"),
               (f"{base}/lib-win32", f"{base}/lib-win32")]
    return [
        ("external/exiftool/exiftool", "external/exiftool"),
        ("external/exiftool/lib", "external/exiftool/lib"),
    ]


datas = _stage_models() + _exiftool_datas()
binaries = []

a = Analysis(
    ["cull_photos.py"],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=["onnxruntime"],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "torch", "torchvision", "ultralytics", "matplotlib", "pandas",
        "polars", "IPython", "tkinter", "PySide6", "PyQt5", "coremltools",
        "onnx", "sympy", "networkx", "requests", "coloredlogs", "humanfriendly",
        "PIL._imagingtk", "PIL._tkinter_finder",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=None,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

if onedir:
    # Two entry points sharing ONE _internal tree (zero duplicated deps):
    #   - cull_sidecar     : windowed bootloader — spawned by Tauri (no console flash)
    #   - auto_culling_cli : console bootloader — user-facing CLI (stdout to terminal)
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="cull_sidecar",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    cli_exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="auto_culling_cli",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=True,  # Console subsystem so CLI stdout attaches to the terminal
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        cli_exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        upx=False,
        upx_exclude=[],
        name="cull_sidecar",
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="cull_sidecar",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,  # Windowed: no console popup when spawned by Tauri
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
