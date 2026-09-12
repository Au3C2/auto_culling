<p align="center">
  <img src="docs/assets/logo.png" width="128" height="128" alt="Auto-Culling Logo">
</p>

# Auto Culling

**English** | [中文版](README_zh.md)

Automated culling for F1 & motorsport photography. Point it at a card straight off the
camera: it groups burst sequences, scores every frame with a multi-stage AI pipeline,
keeps the best shots per burst, and writes Lightroom-compatible star ratings, reject
flags and auto-crops — no manual triage required.

![Auto-Culling Desktop GUI Interface](docs/assets/gui_demo.png)

- **Input**: a folder straight off the camera — Sony ARW, Nikon NEF, Canon CR2/CR3,
  Fuji RAF, Olympus ORF, Panasonic RW2, HEIF (`.hif/.heif/.heic`), JPEG, PNG, TIFF
- **Output**: `.xmp` sidecars (RAW/HEIF) or in-file XMP (JPEG) with star ratings,
  reject flags and crop parameters
- **Runtime**: ONNX Runtime only, no PyTorch. GPU acceleration is automatic
  (CoreML on Apple Silicon, DirectML/CUDA on Windows) with CPU fallback everywhere.

## Quick Start

### Desktop GUI (recommended)

Download a package from [GitHub Releases](https://github.com/Au3C2/AutoCullingF1/releases):

| Platform | Package | Install |
| :--- | :--- | :--- |
| Windows | `AutoCulling_v*_win_x64_setup.exe` | Double-click — per-user install to `%LOCALAPPDATA%\AutoCulling`, no admin rights needed |
| Windows | `AutoCulling_v*_win_x64_portable.zip` | No install — unzip anywhere and run `auto_culling.exe` |
| macOS (Apple Silicon) | `AutoCulling_v*_macos_arm64.dmg` | Open the DMG and drag `AutoCulling.app` to Applications |

Every package ships with a `.sha256` sidecar — verify with
`Get-FileHash -Algorithm SHA256` (Windows) or `shasum -a 256` (macOS).

Launch `auto_culling.exe` (Windows) or `AutoCulling.app` (macOS), pick the folder
straight off the camera, and run — ratings, reject flags and crop parameters are
written as the scan progresses. The app bundles the full AI engine, so the install
folder is **flat and self-contained**: `auto_culling.exe` (GUI), `auto_culling_cli.exe`
(CLI), `auto_culling_engine.exe` (internal engine) and `lib/` (models, exiftool,
runtime) sit side by side — don't move or delete any of them individually.

First-launch notes: Windows SmartScreen may ask for confirmation on the unsigned
installer; on macOS the app is ad-hoc signed, so the first launch may require
right-click → Open.

### Bundled CLI

Every package also carries a console CLI wired to the same engine — batch mode for
scripting and server use:

```powershell
# Windows — default install dir (portable: the unzipped folder)
& "$env:LOCALAPPDATA\AutoCulling\auto_culling_cli.exe" --input-dir C:\Photos\F1 --recursive --force
```

```bash
# macOS
/Applications/AutoCulling.app/Contents/Resources/auto_culling_cli \
  --input-dir /path/to/photos --recursive --force
```

Options are identical to the Python CLI (table below). Omit `--input-dir` to open a
folder picker. Files that already carry ratings are skipped unless `--force`.

### From source (run)

Prerequisites: Python 3.10+ with [uv](https://github.com/astral-sh/uv), and ffmpeg on
PATH (`brew install ffmpeg`; Windows: vendored under `external/ffmpeg/`).

```bash
uv sync
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python cull_photos.py --input-dir /path/to/photos --recursive --force
```

Omitting `--input-dir` opens a small GUI (customtkinter) instead.

### Building the desktop packages from source

Prerequisites: Python 3.10+ with uv, the [Rust toolchain](https://rustup.rs), and
Node.js 18+ (the Tauri CLI runs through `npx`). exiftool and ffmpeg are vendored on
Windows; on macOS install them once with `brew install exiftool ffmpeg`.

```bash
uv sync
source .venv/bin/activate        # Windows: .venv\Scripts\activate
python packaging/build_gui.py    # add --skip-engine to skip PyInstaller while iterating on the GUI
```

The script compiles the Python engine (PyInstaller onedir), builds the Tauri shell
and collects the artifacts into `dist/`:

- **Windows**: `AutoCulling_v*_win_x64_setup.exe` (NSIS) + `AutoCulling_v*_win_x64_portable.zip`
- **macOS**: `AutoCulling_v*_macos_arm64.dmg` + `AutoCulling.app`

Each artifact gets a `.sha256` automatically. CI (`.github/workflows/guards-gui.yml`)
builds the same packages on every push and runs the install/launch tests against them.

### Useful options

| Option | Meaning |
| :--- | :--- |
| `--workers N` | Decode-pool size (default 8; ratings are worker-invariant) |
| `--top-n 11` | Max keepers per burst group |
| `--scale-width 1280` | Decode resolution for the scoring chain |
| `--p4-policy` | `always` (default) / `never` / `auto` (F1/GP folders only) |
| `--crop-off` | Disable auto-crop writing |
| `--dry-run` | Score and report without writing any metadata |
| `--dump-scores FILE` | Export per-image CSV (sharp/comp/raw/rating) |
| `--force` | Re-analyze files that already carry ratings |
| `--deterministic` | Bit-identical cross-platform CPU path (slower) |

## How it works

1. **Burst grouping** — frames are grouped by EXIF capture time, with a time-gap
   fallback when EXIF is unavailable.
2. **Per-frame scoring** —
   - **Sharpness**: FFT-based high-frequency energy with subject-ROI weighting;
     out-of-focus frames are rejected outright.
   - **Composition**: an F1-specific YOLO model (COCO `yolov8n` cascade fallback)
     scores subject size, placement and lead room.
   - **Orientation & integrity**: a compact classifier rejects rear-view shots and
     penalizes cut-off / occluded subjects.
3. **Top-N selection** — the best *N* frames per burst (default 11) keep their stars;
   the rest of the group is downgraded.
4. **Auto-crop** — a Lightroom crop around the detected subject (3:2 / 2:3) is written
   next to the rating.

A frame is auto-rejected (−1) when no subject is detected, the frame is out of focus,
the car is seen from the rear, or its score falls below the keep floor. The exact
weights and thresholds live in `cull/scorer.py` and are locked against the committed
deterministic truth in `tests/baselines/`.

## Performance

Gate protocol (~500 real camera files per format, steady state):

**macOS — Apple M4, workers = 4**

| JPEG | HEIF | Sony ARW | Nikon NEF |
| ---: | ---: | ---: | ---: |
| 83.5 img/s | 65.5 img/s | 49.9 img/s | 70.0 img/s |

**Windows** — Ryzen 7 5700X + RTX 4070 Ti, default workers = 8: 35–46 img/s across
formats.

Benchmark methodology, per-platform baselines and the optimization history live in
[`results/performance_baseline.md`](results/performance_baseline.md).

## For developers

```text
cull/           core engine: decode, burst grouping, detection, scoring, XMP write, GUI
models/         ONNX weights
train/          training pipelines (YOLO fine-tune, P4 multi-task, fence classifier)
packaging/      PyInstaller build + unified regression suite (guards.py)
benchmarks/     per-format steady-state perf gate
tests/          precision gates, deterministic truth, CI harness
scripts/ eval/ docs/ results/    tooling, labeling guides, baseline records
external/       vendored exiftool (+ ffmpeg on Windows)
```

Regression gates:

```bash
pytest tests/ -m deterministic                        # cross-platform truth (strict)
pytest tests/test_cull.py tests/test_precision_heif.py tests/test_precision_raw.py
python packaging/guards.py    # precision → perf → build → packaged gates, ~15 min
```

CI (`.github/workflows/`) runs the same gates on GitHub-hosted macOS/Windows runners
from committed seed samples. Build targets:

```bash
uv pip install pyinstaller
python packaging/build.py            # standalone CLI onedir (used by the precision guards)
python packaging/build_gui.py        # desktop GUI packages: setup / portable / DMG
```

Further reading: [`results/performance_baseline.md`](results/performance_baseline.md)
(measured numbers, platform baselines), [`docs/P4_LABELING.md`](docs/P4_LABELING.md)
(P4 labeling guide).

## License

Licensed under the [Apache License 2.0](LICENSE).
