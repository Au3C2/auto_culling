# Auto-Culling — Agent Rules

## Project Overview

F1 motorsport burst-photo binary classifier. Learns which shots a photographer keeps (label=1) vs discards (label=0). PyTorch fine-tuning pipeline + ONNX export for production inference.

## Language

- All code, comments, and docstrings: **English**
- All communication with the user: **Chinese**

## Environment

- Python 3.10, managed by `uv` at `/home/au3c2/.local/bin/uv` (NOT on PATH)
- Always activate venv before running Python: `source .venv/bin/activate`
- Or use full path: `/home/au3c2/.local/bin/uv run <script>`
- GPU: NVIDIA RTX 4070 Ti (12 GB VRAM), CUDA 12.4, PyTorch 2.6.0+cu124
- Project root: `/home/au3c2/auto_culling/`

## Tooling Conventions

- Path operations: always use `pathlib.Path`, never string concatenation
- CLI arguments: always use `argparse` with `ArgumentDefaultsHelpFormatter`
- No `cd <dir> && <cmd>` — use `workdir` parameter or activate venv instead
- Shell scripts: must activate `.venv` first (`source .venv/bin/activate`) since `uv` is not on PATH

## Architecture & Training

- Supported archs: `resnet18`, `resnet50`, `resnext50`, `mobilenetv3`
- All models output a single logit → `BCEWithLogitsLoss` (binary)
- Fine-tune mode: freeze all → unfreeze last 2 backbone blocks + head
  - ResNet/ResNeXt: `layer3` + `layer4` + `fc`
  - MobileNetV3: `features[-2]` + `features[-1]` + `classifier`
- Classification head always has `Dropout(0.3)` before `Linear(in_features, 1)`
- Training is **step-based** (not epoch-based)
- AMP (`torch.amp`), `WeightedRandomSampler`, gradient clipping (`max_norm=1.0`)
- Loss: `LabelSmoothingBCELoss` with `pos_weight` support (defined in `train.py`)

## Anti-Overfitting (v2 settings — do not regress)

- `--label-smoothing 0.1`
- `--weight-decay 5e-4`
- `Dropout(p=0.3)` in head
- Unfreeze `layer3` + `layer4` (not just `layer4`)
- Train augmentation: `RandAugment(n=2, m=9)` + `GaussianBlur(k=5)` + `RandomGrayscale(p=0.1)`
- **No spatial crop augmentation** — culling depends on full-frame composition

## Dataset

- Images live in `dataset/cache/*.jpg` (512×512 JPEG, pre-decoded from HIF)
- `dataset/img/` is empty — original HIF files deleted
- CSVs: `dataset/train_info.csv` (5975 rows), `dataset/test_info.csv` (1494 rows)
- `img_path` column in CSVs still has old `.HIF` paths — `CullingDataset` resolves to cache automatically
- Label distribution: 38.9% keep / 61.1% discard

## Checkpoints

All v2 best checkpoints are in `checkpoints/<arch>_finetune_v2/best.pt`.
Checkpoint dict keys: `model_state_dict`, `optimizer_state_dict`, `scheduler_state_dict`, `step`, `val_f1`, `arch`.

## ONNX

- Exported with opset 17, dynamic batch axis
- Output files: `onnx_models/<arch>.onnx`
- Inference provider priority: `CUDAExecutionProvider` → `CPUExecutionProvider`
- Preprocessing must match eval transform: SquarePad → Resize(224) → ToTensor → ImageNet Normalize

## Key Files

| File | Purpose |
|---|---|
| `src/auto_culling/model.py` | `build_model(arch, finetune, pretrained)` factory |
| `src/auto_culling/dataset.py` | `CullingDataset`, `build_dataloaders`, `SquarePad` |
| `src/auto_culling/train.py` | Training loop, `LabelSmoothingBCELoss`, `EarlyStopping` |
| `export_onnx.py` | Export `.pt` → `.onnx` + ORT sanity check |
| `infer_onnx.py` | Batch inference on photo dir → `keep/` / `discard/` + `scores.csv` |
| `benchmark_onnx.sh` | One-shot export + throughput benchmark |
| `run_finetune_v2.sh` | Training launcher (v2 hyper-parameters) |
| `REPORT.md` | Full experiment log (Chinese) |

## TensorBoard Logging

Scalars logged per step: `train/loss`, `val/loss`, `val/acc`, `val/f1`, `val/auc`, `test/*`, `lr`.
Logs saved to `checkpoints/<run>/tb_logs/`.

## v2 Benchmark Results (do not overwrite without re-running experiments)

| Model | Test F1 | Peak ONNX img/s |
|---|---|---|
| resnext50 | 0.7680 (best) | 42 |
| resnet18 | 0.7554 | 165 |
| resnet50 | 0.7555 | 57 |
| mobilenetv3 | 0.7279 | 218 (fastest) |

## Runtime Performance Baseline (master/develop @ CUDA, 2026-08-22)

Authoritative detail in `results/performance_baseline.md`. Benchmarks: `--workers 4 --dry-run`,
`onnxruntime-gpu 1.23.2`, CUDAExecutionProvider, RTX 4070 Ti. Keep this baseline.

End-to-end: JPG 5.9 / HEIF 6.4 / ARW 4.4 / NEF 3.3 img/s. Serial budget ≈300 ms/frame:
decode 186–218 ms (JPG-Pillow / HEIF-ffmpeg+resize) is the dominant bottleneck (62%+);
RAW 462/484 ms via per-file exiftool spawn. Inference is NOT the bottleneck: CUDA
session.run 8.2 ms, CPU 28 ms. Real runs also pay 399 ms/file exiftool metadata sync
(dry-run hides it).

Established facts:
- yolo batch inference is a LOSS (batch=8 = 0.5×; model too small, H2D bound) — do not batch YOLO.
- All 3 ONNX (f1/yolov8n 640px + p4 224px, opset 17) have dynamic batch dims.
- README's 35/52 img/s is UNVERIFIED legacy (no artifacts; unreproducible at any commit; max 7.8 img/s).
- Engine: ThreadPoolExecutor over burst groups; Python postprocess (8400-row argmax, 12.4 ms)
  is GIL-amplified at workers=4 (detect 49.6 vs 33 ms single-thread).
- CUDA support needs `ensure_nvidia_runtime_on_path()` (detector.py) for nvidia wheel DLLs.

Optimization status (2026-08-22, all gates green at workers=4):
- #2 vectorized postprocess — KEPT (detect −39%, equal-op)
- #3 decode process pool + single-consumer inference — KEPT (HEIF 6.4→11.5,
  +80%; fixed CUDA non-determinism)
- #6 batch metadata write (exiftool stay_open) — KEPT (60 vs 458 ms/file, 7.6×)
- #A decode resize → cv2.INTER_AREA — KEPT (user-accepted upward-only score
  drift at boundaries; ~8× faster resize; baselines re-locked)
- #B sharpness → cv2.dft(float32) + unshifted mask — KEPT (20.8→~5.2 ms, 4×;
  HF diff < 2e-9, scores unchanged)
- #1 ffmpeg -vf scale — REJECTED (pixel drift). #4 RAW batch extract — DROPPED
  (no pixel-safe path). #5 sharpness-in-worker — DROPPED (ROI coupling).
  #7 sharpness pool — REVERTED (zero E2E gain; decode supply + consumer were
  balanced). #8 EXIF stay_open — REVERTED (unvalidated, imperceptible).
Details: results/performance_baseline.md "Attempted optimizations log".
Current perf-gate protocol numbers (authoritative): JPG 7.21 / HEIF 4.49 /
ARW 3.15 / NEF 3.42 img/s. Consumer bottleneck (after #B) is now detect
(~24 ms, mostly GPU run) + half the sharpness; decode supply still balances at
workers=4.

2026-08-22 second round (machine-state drift ±40% between sessions; use
interleaved A/Bs):
- #15 RAW persistent exiftool stay_open session (`-b -w` file framing) — KEPT
  (extraction byte-identical, 33 vs 460 ms/file; ARW +14%, NEF +21% on gates).
- #17 np.asarray after convert("RGB") — KEPT (pixel-identical, −10 ms/decode).
- #21 P4 warm-up moved into load_models() — KEPT (no output change; removes
  ~1 s first-frame stall from the timed window).
- #13 JPG draft DCT decode — REVERTED (flips P4 integrity on 2/6 JPGs:
  keep→reject). #18 cv2 letterbox + P4-ROI — REVERTED (4 gates fail, P4 knife-
  edge, both directions). #19 CPU affinity partitioning — REVERTED (no gain;
  inflation is memory-bandwidth). #22 P4-skip for sharpness-vetoed frames —
  REVERTED (raw of vetoed frames loses penalty → raw gates fail). #16 worker
  count 2/3/4/6 — no effect. P4's ROI decision boundary is knife-edge wrt any
  ~1-LSB pixel change: the decode/letterbox/P4-ROI preprocessing is FROZEN.
STEADY-STATE FINDING: consumer serial ≈ 84 ms/frame (~12 fps) is the cap for
ALL formats; decode supply has 2× slack (decode_wait 12-19 ms). 20 fps needs
consumer ≤ 50 ms, which requires machine fast-state (detect 16 ms idle) +
lower bandwidth contention — not reachable with pixel-identical ops alone on
this 8-core box. Verified after round 2: JPG 7.27 / HEIF 4.40 / ARW 3.39 /
NEF 3.84 img/s (gate protocol); precision 6/6 green.

2026-08-23 third round — **P4 v2 retrain unlocked the frozen pixel path**:
- P4 v2 (`models/p4_car_model.onnx`, retrained via `train/train_p4_multitask.py`
  with resize-kernel randomization + camera jitter): labeled-val kernel flip
  rate 4.8%→2.6% (9-kernel), production gates: all keep/reject decisions
  unchanged on 70 gate files, 2 kept files +1 star. Legacy model backed up at
  `p4_model_checkpoints/p4_car_model_v1_legacy.onnx`. Robustness eval:
  `eval/eval_p4_robustness.py`; labeling guide: `docs/P4_LABELING.md`.
- UNFROZEN and KEPT: #24 JPG libjpeg draft DCT decode (~180→65 ms worker CPU),
  #25 cv2 letterbox (`detect_numpy`) + cv2 P4 ROI (consumer de-GIL).
  Interleaved A/B: draft+cv2 gives JPG +25% E2E (8.9-9.2 vs 7.1-7.3 img/s);
  HEIF/ARW/NEF within machine drift. Precision gates re-locked 2026-08-23
  (JPG/HEIF/RAW @ workers=4, deterministic across consecutive runs).

**DONE — CUDA concurrency non-determinism FIXED via optimization #3** (2026-08-22):
engine now decodes via `ProcessPoolExecutor` and runs inference on a single
consumer thread with one session; gates green 3×3 consecutive at workers=4 and
unlocked back to workers=4 (score rake defaults changed). Throughput after #3
(workers=4, dry-run): JPG 6.7 / HEIF 11.5 / ARW 4.7 / NEF 4.6 img/s (HEIF
+80%). `--workers` now means decode-pool size, not thread groups.

Gates (2026-08-22): precision = `tests/test_cull.py` +
`tests/test_precision_heif.py` (24 HEIF) + `tests/test_precision_raw.py`
(20 ARW + 20 NEF), at `--workers 4`; performance =
`benchmarks/run_benchmarks.py` (thresholds: JPG 4.2 / HEIF 3.0 / ARW 2.3 /
NEF 1.9 img/s; measured after #3: 6.84/3.73/2.98/3.15).

## macOS platform (Apple M4, 2026-08-24)

Dev machine: MacBook M4 (10 cores, 24 GB), Python 3.10.20 (uv venv), pyav
17.1.0, exiftool 13.50, ffmpeg 8.0. Precision gates re-locked on macOS
(platform decode LSB diffs vs Windows; all 64 HEIF/ARW/NEF ratings
identical, raw drift <= 0.035 — see tests/ headers and
results/performance_baseline.md "macOS platform baseline").

macOS-specific optimizations (all zero-drift vs the macOS gate lock):
- **P4 model runs on CPUExecutionProvider on darwin** (cull/p4_classifier.py):
  CoreML partitions 20/77 nodes and costs 16.6 ms vs 5.0 ms CPU. Logit diff
  vs CoreML <= 0.011, never crosses a decision; scoring chain 18.4 ->
  23.4 fps serial (26.7 fps at 4 threads). CoreML EP for YOLO stays
  (27 ms vs 51 ms CPU; CoreML EP options are unsupported in ORT 1.23.2 on
  this build).
- **EXIF scan sharded across 4 exiftool processes** (cull/exif_reader.py,
  argv file lists, `-@ -` kept for > 400 files): 8.1 vs 18.5 ms/file on M4;
  field-identical output verified; feeds burst grouping only.
- **In-process VideoToolbox HEIF hardware decoding** (cull/loader.py):
  Decodes in 12.4 ms vs 21.8 ms soft (1.76x faster). Color metadata alignment
  (propagating JPEG full range AVCOL_RANGE_JPEG) ensures 100% bit-identical
  RGB output (0 drift, 0 flips across all 24 HEIFs). Active by default on macOS
  with automatic fallback to software decoding.
- `--consumer-threads` 2/4 is a LOSS end-to-end on M4 (13.6 -> 11.4 -> 9.0
  img/s interleaved A/B); default 1 stays (differs from nothing on Windows).
- **YOLO CoreML ANE/compute-units REJECTED** (2026-08-24): option keys are
  CamelCase (`MLComputeUnits`, `ModelFormat`, `RequireStaticInputShapes`);
  ANE is -16% on the YOLO stage alone but SLOWER on the full scoring chain
  (submit-wait schedule vs the CPU sharpness/P4 stages).
- **STATIC-GRAPH YOLO KEPT** (2026-08-25): ultralytics exports are symbolic
  on ALL dims (`batch/height/width`) — freeze all three, constant-fold with
  onnxsim (`models/f1_yolov8n_static.onnx`), then RequireStaticInputShapes
  qualifies 227/231 nodes in 3 partitions (vs 7/233-of-318 dynamic). Full
  scoring chain 40.2 -> 26.4 ms/frame; scoring chain 37.5 fps serial;
  end-to-end JPG 17.41 img/s. Darwin-only branch in LiteYOLO (engine always
  runs batch=1); Windows keeps the dynamic model. Gate: 0 rating flips,
  9 raw entries re-locked (~3% P4 knife-edge drift <=0.6, rest <=0.012).

macOS final numbers (gate protocol, workers=4): JPG 17.41 / HEIF 8.79 /
ARW 6.91 / NEF 8.05 img/s vs Windows 10.8 / 5.9 / 4.5 / 4.7. Scoring chain
serial 37.5 fps — the 20 fps scoring-chain target is exceeded by 87%.

## RAW inner-JPEG hard-decode (2026-08-27, ABANDONED)

ARW/NEF inner previews are all 8-bit SOF0 baseline, 4:2:2. Every in-process
persistent hard-decode path was measured (VideoToolbox persistent session
via ctypes, ImageIO memory-source thumbnail, Core Image): the floor is
VideoToolbox 46 ms or ImageIO thumbnail 39 ms — only -7% to -20% vs the
gate-locked cv2 REDUCED_2 path (49 ms), but with max pixel drift 24-54
(chroma-upsample kernel + YCC fixed-point rounding, isolated by synthetic
grayscale/4:4:4/4:2:2 ablation; full-res already max=54). Alignment is
impossible — ImageIO has no exposed upsample/rounding knobs, its private
libJPEG.dylib is fused into the dyld cache, and switching introduces a
permanent Windows/macOS baseline fork. Decided: abandon hard-decode,
keep the zero-drift cv2 path. Only zero-drift lever left for ARW is
range I/O (IFD+JPEG within first 6% of file). Details in
results/performance_baseline.md hard-decode section.
2026-08-27 TRY 3/4: non-darwin JPG/HEIF HWAccel probing scaffolds landed in
cull/loader.py (ffmpeg -hwaccels / pyav HWAccel), dead code on darwin (7/7
gates pass); performance must be proven on the non-darwin runner.

2026-08-27 Dedicated macOS Performance & Precision Guards Locked (KEPT 68f9934):
- benchmarks/run_benchmarks.py dedicated macOS thresholds locked: JPG 14.0, HEIF 6.0,
  ARW 5.0, NEF 5.5 img/s (replaces obsolete Windows 4070Ti baselines 4.2/3.0/2.3/1.9).
- tests/test_cull.py parameterized across workers=(1, 4, 6) for concurrency determinism.
- All 9/9 precision gates and 4/4 performance gates strictly green.

2026-08-27 perf gate REWORK: split into setup tax + per-format steady-state —
- run_benchmarks.py now measures, per format on the ~500-file protocol
  (JPG 504 / HEIF 504 / ARW 500 / NEF 500, hard-linked, --dry-run):
  - setup tax (process start -> [90%] Analyzing, guarded by a wide ceiling),
  - steady E2E (files / [90%]->[95%] window, guarded at baseline x 0.90).
  Baselines locked 2026-08-27 on Apple M4 (idle, interleaved, cooldown 20s
  between formats; full 4-format gate ~3.1 min):
  source: JPG 83.5 / HEIF 65.5 / ARW 49.9 / NEF 70.0 img/s;
  onedir: JPG 82.4 / HEIF 62.6 / ARW 48.7 / NEF 67.9 img/s.
  Setup ceilings: source 8.0s, onedir 12.0s (2x headroom).
- Heat/thermal drift is real on this fanless M4 (continuous full-load runs
  drop steady by 10-30%: measured JPG 82->61, NEF 68->58). ALWAYS interleave
  and cooldown; never trust a serial long batch for baselines.
- Unified guard entrypoint: `python packaging/guards.py` runs 5 guards —
  source precision (9 gates) -> source perf -> packaging build (onedir) ->
  packaged precision (4 gates) -> packaged perf. Full suite ~12-15 min.
  Requires the ~1.3 GB camera datasets (test_import/test_arw/test_nef) present.


## GUI Packaging (Tauri, flat install layout — feature/tauri-gui, 2026-09-12)

- Pipeline: `python packaging/build_gui.py` → PyInstaller onedir (`engine.spec`,
  `CULL_ONEDIR=1`) → stage to `src-tauri/resources/engine/` → `tauri build`
  (`--bundles nsis` on Windows, `app` on darwin) → collect artifacts + `.sha256`
  into `dist/`. `--skip-engine` (alias `--skip-sidecar`) skips the PyInstaller step.
- Install layout is FLAT on both platforms — one directory, no nested `sidecar/`:
  `auto_culling.exe` (GUI) + `auto_culling_cli.exe` (console CLI) +
  `auto_culling_engine.exe` (windowed engine, spawned by the GUI over Stdio JSON
  Lines) + `WebView2Loader.dll` + `lib/` (PyInstaller deps via
  `contents_directory="lib"`, default was `_internal`; holds `models/` and
  `external/exiftool/`). Windows NSIS installs to `%LOCALAPPDATA%\AutoCulling`
  (`installMode: currentUser`); the portable zip mirrors the same layout; macOS
  maps it into `AutoCulling.app/Contents/Resources/` root.
- Flattening mechanism: `tauri.conf.json` resource map entry
  `"resources/engine": ""` — an empty target makes Tauri walk the directory flat
  into the resource root (tauri-utils Walk branch, `dest.join(strip_prefix)`).
- Engine resolution (`src-tauri/src/main.rs`, release builds): candidates are
  exe-dir root, macOS `Contents/MacOS → ../Resources` fallback, then
  `resource_dir()`. Dev mode (repo checkout with `.venv`) runs
  `python cull_photos.py --json-lines` instead — a sandbox install INSIDE the
  repo tree therefore hits dev mode; test bundled-engine spawn outside the repo.
- GUI↔engine event channel: engine startup failures emit `engine-error`
  (renamed from `sidecar-error`); engine spawn logs go to `gui.log` next to the
  GUI binary (`spawn bundled engine` / `engine spawned and alive` markers used
  by `packaging/test_gui_package.py`).
- Verified 2026-09-12: gui-guards #24 green on both platforms (DMG mount test +
  NSIS silent install + portable extraction + engine spawn); local Windows
  NSIS silent install layout + bundled-engine spawn outside repo tree.

## Packaging (single-file PyInstaller, macOS-first)

CLI-engine packaging used by the precision/perf guards and the release gates
(`packaging/build.py`, spec `cull_photos.spec`; planned to converge on
`engine.spec`):

- Pipeline: `python packaging/build.py` (onefile, copies to root) / additional
  `--onedir` (directory form). One cross-platform spec `cull_photos.spec`
  branches on the platform: darwin ships the FROZEN onnxsim graphs
  (f1_yolov8n_static 640 / yolov8n_static 640 / p4_car_model_static_ane 224)
  UNDER THE BASE NAMES → the packaged runtime needs no `*_static` sibling;
  Windows keeps dynamic exports + self-contained exiftool.exe. Exiftool on
  macOS runs the bundled perl script via system perl (`/usr/bin/perl` ships
  with macOS); bundled `external/exiftool/lib` in the archive.
- Source changes required for standalone resolution (both zero-drift in the
  source pipeline; 9/9 source gates stay green):
  1. `cull/detector.py` `_has_concrete_input_shape()` — LiteYOLO darwin
     branch falls back to a shape probe (fully-concrete input dims) when no
     `*_static` sibling exists, so the packaged frozen models get the same
     pinned CoreML options (RequireStaticInputShapes=1, CPUAndNeuralEngine).
  2. `cull/p4_classifier.py` — model path falls back to `_MEIPASS` via
     `get_resource_path`; same shape probe substitutes the `_static_ane`
     detection for packed (single-file) bundles.
- Test harness: `CULL_EXE=<binary>` makes `tests/test_package.py`,
  `tests/score_gate.py` (HEIF/ARW/NEF gates) and `benchmarks/run_benchmarks.py`
  run the packaged executable instead of the source CLI. `run_benchmarks.py`
  prewarms the binary's dylib closure (`--no-prewarm` to disable).
- macOS cold-start signature tax: the kernel verifies the adhoc code
  signature of EVERY bundled Mach-O on first load (inode-keyed cache).
  The onefile form extracts to a fresh temp dir per run → ~15-25 s tax
  per launch (JPG gate 60 imgs → ~2.5 img/s measured). The onedir form has
  stable inodes → tax paid once per boot, then source-identical throughput.
  Perf gate verification runs against the onedir artifact with prewarm:
  JPG 17.6 / HEIF 7.7 / ARW 6.7 / NEF 6.9 img/s (4/4 green, 2026-08-27).
- Packaged-binary precision gates: 4/4 green on BOTH forms (JPG 6 + HEIF 24
  + ARW 20 + NEF 20, ratings and raw_score ±0.005 identical to baseline).
  Binary size: onefile 160.9 MiB (zlib CArchive): cv2 48 + models 33 +
  av 18 + onnxruntime 16 + exiftool 14 + scipy 12 + rest ≈ 11 MiB.
  scipy.fft is NOT excludable (its __init__ hard-imports _fftlog →
  scipy.special → scipy.linalg); reverting to cv2.dft would cost ~8.7x the
  per-frame FFT time (0.34 → 2.95 ms on M4) for ~12 MiB — rejected.
- cv2 MUST stay opencv-python 5.0.0.93 (full): the headless build flips the
  knife-edge file IMG_20260314_160318_240.jpg (3→-1 at workers=4/6) — keep
  the .venv untouched by pip swaps; a mixed cv2 directory also flips it.

## CI (GitHub Actions) — four workflows

1. **gui-guards** (`.github/workflows/guards-gui.yml`) — GUI packaging guard.
   Triggers: push to develop/master/**feature/tauri-gui**, PRs, manual. Two jobs:
   `gui-macos` (DMG build + mount test: .app structure, engine onedir `lib/`
   layout, CLI `--help`, stdio handshake, installed-app self-spawn, HEIF
   frozen-engine guard) and `gui-windows` (NSIS silent install + portable zip
   extraction + engine spawn). Uploads both platform artifacts. This is the
   ACTIVE guard on feature/tauri-gui.
2. **culling-guards** (`.github/workflows/guards.yml`) — engine source+packaged
   gates; ALL jobs run on macos-14 despite the neutral name. Triggers:
   develop/master/PR only (feature branches are NOT covered). Jobs:
   `perf-calibrate` (manual-dispatch only, writes `tests/ci/ci_config.json`),
   `precision`, `perf-source`, `perf-packaged`, `deterministic-cpu`.
3. **culling-guards-windows** (`.github/workflows/guards-windows.yml`) —
   windows-latest, develop/master/PR only. Jobs: `deterministic-cpu`
   (70-file strict gate), `gpu-alignment` (CUDA vs deterministic truth),
   `perf-seed`. NOT a symmetric twin of #2 — different job set.
4. **release** (`.github/workflows/release.yml`) — tag push `v*` / manual
   dispatch. Per platform: precision gate (`build.py --onedir`) + perf gate +
   GUI build (`build_gui.py`) + warm-start smoke; publishes a draft release
   with setup/portable/dmg + per-artifact `.sha256`. CLI is bundled inside
   every GUI package (no separate CLI artifacts since v0.3). Release does not
   gate on guard runs — it re-runs its own precision/perf gates.

Shared facts:
- Seeds: `tests/ci/sample/` = ONE file per format (~70 MB total, .gitignore
  carve-out), replicated to ~500 files at runtime — no camera datasets in the
  repo.
- Precision (no calibration): `ci_seed_precision.py --compare` scores the
  same replicated dataset with source + packaged binary and asserts per-file
  raw_score equality (±0.002 tolerance — source alone jitters ±0.0004
  run-to-run from ANE/P4) and rating-multiset equality. Per-copy ratings are
  NOT uniform (identical EXIF → one burst → Top-N downgrades), which is why
  the gate is consistency-based.
- Performance: `run_benchmarks.py --seed-dir tests/ci/sample --baseline-file
  tests/ci/ci_config.json --tolerance 0.85`. GitHub-hosted macOS runners have
  NO ANE + different silicon → baselines MUST be measured on the runner via
  the manual `perf-calibrate` workflow and committed to
  `tests/ci/ci_config.json`; skipped until then. Local seed-protocol reference
  (Apple M4, source, w4): JPG 84.5 / HEIF 42.6 / ARW 48.7 / NEF 69.9.
- Known gaps / planned rework: (a) rename to a consistent scheme
  (`engine-guards-macos.yml` / `engine-guards-windows.yml` / `gui-guards.yml`,
  filename = workflow name — `guards-gui.yml` currently registers as
  `gui-guards`); (b) guards should consume the `engine.spec` onedir instead of
  building a second `cull_photos.spec` onedir; (c) feature branches only get
  gui-guards — merge to master via PR so the engine gates run.
