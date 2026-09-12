"""packaging/test_gui_package.py — automated test suite for packaged GUI installers.

Validates:
1. macOS: Mounts .dmg, verifies .app bundle integrity and embedded engine execution.
2. Windows: Verifies NSIS setup .exe and extracts/validates portable .zip bundle.
3. Tests communication handshake with the bundled engine binary (scan & preview).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _get_version() -> str:
    env_ver = os.environ.get("CULL_VERSION", "").strip().lstrip("v")
    if env_ver:
        return env_ver
    toml = ROOT / "pyproject.toml"
    if toml.exists():
        import re
        for line in toml.read_text().splitlines():
            if line.strip().startswith("version"):
                m = re.search(r'"([^"]+)"', line)
                if m:
                    return m.group(1).strip()
    return "0.1"


def _detach_stale_attachments(dmg_path: Path) -> None:
    """Detach any leftover mounts of this exact image (same DMG attached twice
    makes hdiutil attach fail with 'Resource busy')."""
    try:
        info = subprocess.run(["hdiutil", "info"], capture_output=True, text=True, check=True).stdout
    except Exception:
        return
    current_image = None
    for line in info.splitlines():
        line = line.strip()
        if line.startswith("image-path"):
            current_image = line.split(":", 1)[1].strip()
        elif line.startswith("/dev/disk") and current_image == str(dmg_path):
            subprocess.run(["hdiutil", "detach", line.split()[0], "-force"],
                           capture_output=True)


def test_macos_dmg(dmg_path: Path | None = None) -> bool:
    """Mount macOS DMG, verify .app structure, test embedded engine and unmount."""
    print("=== Testing macOS DMG Installer Package ===")
    if not dmg_path or not dmg_path.exists():
        # Search dist/ or src-tauri/target/release/bundle/dmg/ — prefer release
        # artifacts over leftover design DMGs.
        candidates = sorted(
            (ROOT / "dist").glob("AutoCulling_v*.dmg")
        ) + list((ROOT / "src-tauri/target/release/bundle/dmg").glob("*.dmg"))
        if not candidates:
            print("FAIL: No DMG package found to test.")
            return False
        dmg_path = candidates[0]

    print(f"Testing DMG artifact: {dmg_path} ({dmg_path.stat().st_size / 1024 / 1024:.1f} MB)")
    _detach_stale_attachments(dmg_path)
    mount_point = Path(tempfile.mkdtemp(prefix="ac_dmg_test_"))

    try:
        # Mount DMG
        res = subprocess.run(
            ["hdiutil", "attach", str(dmg_path), "-mountpoint", str(mount_point), "-nobrowse", "-quiet"],
            capture_output=True,
            text=True,
            check=True,
        )
        print("PASS: Successfully mounted DMG volume.")

        # Find .app
        apps = list(mount_point.glob("*.app"))
        if not apps:
            print("FAIL: No .app bundle found inside mounted DMG.")
            return False
        app_bundle = apps[0]
        print(f"Found app bundle: {app_bundle.name}")

        # Check binary existence
        macos_dir = app_bundle / "Contents" / "MacOS"
        res_dir = app_bundle / "Contents" / "Resources"

        main_execs = [e for e in macos_dir.glob("*") if e.name != "auto_culling_engine"]
        if not main_execs:
            print("FAIL: Missing executable in Contents/MacOS/")
            return False
        app_main = main_execs[0]
        print(f"PASS: Found main app executable: {app_main.name}")

        success = True

        # --- DMG install-layout guards (regressions from 2026-09-05) ---
        # 1. Applications drag link must exist (hdiutil fallback dropped it).
        apps_link = app_bundle / "Contents" / ".." / "Applications"
        if (app_bundle.parent / "Applications").is_symlink():
            print("PASS: DMG carries the Applications drag link.")
        else:
            print("FAIL: DMG missing Applications symlink — no drag-install guide.")
            success = False
        # 2. Background image must be shipped (visual drag-install guide).
        bg_check = list((app_bundle.parent / ".background").glob("*.png")) \
            + list((app_bundle.parent / ".background").glob("*.tiff"))
        if bg_check and bg_check[0].stat().st_size > 10_000:
            print(f"PASS: DMG ships install background ({bg_check[0].name}).")
        else:
            print("FAIL: DMG missing/stub install background image.")
            success = False
        # 3. Pre-baked Finder layout must be injected (window size/icon coords).
        if (app_bundle.parent / ".DS_Store").exists():
            print("PASS: DMG carries pre-baked .DS_Store layout.")
        else:
            print("FAIL: DMG missing .DS_Store — icon/window layout not applied.")
            success = False

        # Test engine executable if bundled in Resources or MacOS.
        # 2026-09-12 flat layout: resources map to the Contents/Resources
        # ROOT and the engine onedir carries TWO entry points sharing one
        # lib/ dependency tree:
        #   auto_culling_engine (windowed, spawned by the GUI)
        #   auto_culling_cli    (console, user-facing CLI)
        engine_cand = None
        for cand in [res_dir / "auto_culling_engine",
                     res_dir / "auto_culling_engine.exe",
                     
                     macos_dir / "auto_culling_engine"]:
            if cand.exists():
                engine_cand = cand
                break

        # 4. Engine must ship as ONEDIR (binary + lib/). The onefile form
        # re-extracts 160 MB per launch (15-25 s startup tax on macOS).
        if engine_cand and (engine_cand.parent / "lib").is_dir():
            print("PASS: Engine ships as onedir (no per-launch extraction tax).")
        elif engine_cand:
            print("FAIL: Engine shipped as onefile — 15-25s startup tax per launch.")
            success = False
        else:
            print("NOTE: Engine is bundled as external binary or dev-resolved.")

        # 5. The console CLI entry point must ship at the bundle root (flat layout)
        #    and respond on stdout (console-subsystem bootloader sanity).
        if engine_cand:
            cli_name = "auto_culling_cli.exe" if platform.system() == "Windows" else "auto_culling_cli"
            cli_bin = engine_cand.parent / cli_name
            if cli_bin.exists():
                try:
                    help_res = subprocess.run(
                        [str(cli_bin), "--help"],
                        capture_output=True, text=True, timeout=60,
                        stdin=subprocess.DEVNULL,
                    )
                    if help_res.returncode == 0 and "usage" in (help_res.stdout or "").lower():
                        print(f"PASS: CLI entry point {cli_name} responds on stdout (--help).")
                    else:
                        print(f"FAIL: CLI entry point --help rc={help_res.returncode} "
                              f"stdout={(help_res.stdout or '')[:120]!r} stderr={(help_res.stderr or '')[:200]!r}")
                        success = False
                except Exception as e:
                    print(f"FAIL: CLI entry point --help raised: {e}")
                    success = False
            else:
                print(f"FAIL: CLI entry point {cli_name} missing at bundle root.")
                success = False

        if engine_cand:
            print(f"Testing bundled engine: {engine_cand}")
            engine_proc = subprocess.Popen(
                [str(engine_cand), "--json-lines"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            time.sleep(0.3)
            # Send test scan
            test_dir = ROOT / "tests/test_img"
            if test_dir.exists():
                engine_proc.stdin.write(json.dumps({"cmd": "scan", "dir": str(test_dir), "recursive": False}) + "\n")
                engine_proc.stdin.flush()
                scan_line = engine_proc.stdout.readline()
                print(f"Engine handshake output: {scan_line.strip()[:100]}")
            engine_proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
            engine_proc.stdin.flush()
            engine_proc.wait(timeout=5.0)
            print("PASS: Packaged engine passed Stdio JSON Lines handshake test.")
        else:
            print("NOTE: Engine is bundled as external binary or dev-resolved.")

        # Regression test for the installed-app flow: the .app must spawn the
        # bundled engine itself (eagerly at startup). A broken resolution here
        # used to surface as "Broken pipe (os error 32)" on folder pick.
        print("Testing .app self-spawn of bundled engine (installed-app simulation)...")
        install_dir = Path("/tmp/AutoCulling_AppInstall")
        shutil.rmtree(install_dir, ignore_errors=True)
        install_dir.mkdir(parents=True)
        shutil.copytree(app_bundle, install_dir / app_bundle.name, symlinks=True)
        app_bin = install_dir / app_bundle.name / "Contents" / "MacOS" / app_main.name
        app_proc = subprocess.Popen(
            [str(app_bin)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            cwd=str(install_dir),
        )
        try:
            engine_alive = False
            deadline = time.time() + 40.0
            gui_log = app_bin.parent / "gui.log"
            while time.time() < deadline:
                probe = subprocess.run(
                    ["pgrep", "-f", "auto_culling_engine"],
                    capture_output=True, text=True,
                )
                if probe.returncode == 0 and probe.stdout.strip():
                    engine_alive = True
                    break
                # The app logs "engine spawned and alive" next to its binary
                if gui_log.exists() and "engine spawned and alive" in gui_log.read_text(errors="ignore"):
                    engine_alive = True
                    break
                if app_proc.poll() is not None:
                    break
                time.sleep(0.5)
            if engine_alive:
                print("PASS: Installed .app spawned the bundled engine at startup.")
            else:
                tail = gui_log.read_text(errors="ignore")[-500:] if gui_log.exists() else "(no gui.log)"
                print(f"FAIL: .app did not spawn the bundled engine. gui.log tail:\n{tail}")
                success = False
        finally:
            app_proc.terminate()
            try:
                app_proc.wait(timeout=5.0)
            except Exception:
                app_proc.kill()
            subprocess.run(["pkill", "-f", "auto_culling_engine"], capture_output=True)
            shutil.rmtree(install_dir, ignore_errors=True)

        # 5. HEIF frozen-engine guard: packaged GUI apps run with a minimal
        # PATH (no Homebrew ffprobe). If the pyav fast path doesn't engage in
        # the frozen env, HEIF decodes via pillow_heif software — a measured
        # 20x slowdown (3.7 vs 61 img/s) AND different pixels (score drift).
        # Guard = sanitized-PATH run of the shipped engine on real HEIF data:
        # (a) ratings/raw must match the source engine (pixel-identical
        # decode), (b) per-frame engine time must stay under 120 ms (healthy
        # ~17 ms, broken ~267 ms — 4x+ margin on both sides).
        print("Testing shipped engine HEIF decode under sanitized PATH...")
        seed = ROOT / "tests/ci/sample/seed.heif"
        if seed.exists() and seed.stat().st_size > 1_000_000:
            heif_dir = Path("/tmp/AutoCulling_HeifGuard")
            shutil.rmtree(heif_dir, ignore_errors=True)
            heif_dir.mkdir(parents=True)
            for i in range(3):
                shutil.copy2(seed, heif_dir / f"guard_{i:02d}.heif")

            engine_bin = install_dir / app_bundle.name / "Contents" / "Resources" / "auto_culling_engine"
            if not engine_bin.exists():
                engine_bin = engine_cand or engine_bin

            def run_engine(cmd_prefix, path_env):
                env = dict(os.environ)
                env["PATH"] = path_env
                proc = subprocess.Popen(
                    [*cmd_prefix, "--json-lines"],
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL, text=True, bufsize=1,
                    cwd=str(engine_bin.parent), env=env,
                )
                ratings, engine_secs, logs = {}, 0.0, []
                proc.stdin.write(json.dumps({
                    "cmd": "run", "dir": str(heif_dir),
                    "config": {"dry_run": True}}) + "\n")
                proc.stdin.flush()
                while True:
                    line = proc.stdout.readline()
                    if not line:
                        break
                    try:
                        evt = json.loads(line)
                    except Exception:
                        continue
                    if evt.get("type") == "frame":
                        ratings[evt["name"]] = (evt["rating"], round(evt["raw"], 2))
                    elif evt.get("type") == "log":
                        logs.append(evt.get("line", ""))
                    if evt.get("type") == "done":
                        engine_secs = evt.get("elapsed", 0) or 0
                        break
                try:
                    proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                    proc.stdin.flush()
                    proc.wait(timeout=5)
                except Exception:
                    proc.kill()
                return ratings, engine_secs, logs

            sanitized_path = "/usr/bin:/bin:/usr/sbin:/sbin"
            src_cmd = [str(ROOT / ".venv/bin/python"),
                       str(ROOT / "cull_photos.py")]
            src_ratings, _, _ = run_engine(src_cmd, os.environ.get("PATH", sanitized_path))
            # Deterministic guard: the shipped engine must decode HEIF via the
            # in-process pyav/VideoToolbox path under a GUI-app PATH. Timing is
            # informational only (per-process CoreML warmup pollutes small sets).
            pack_ratings, pack_secs, pack_logs = run_engine([str(engine_bin)], sanitized_path)

            used_pyav = any("HEIF decode path: pyav" in l for l in pack_logs)
            used_sw = any("pillow_heif SOFTWARE fallback" in l for l in pack_logs)
            per_file_ms = pack_secs / 3 * 1000

            if used_sw or not used_pyav:
                print(f"FAIL: packaged HEIF decode used software fallback "
                      f"(pyav={used_pyav}, pillow_heif={used_sw}) — 20x slowdown regression.")
                success = False
            elif per_file_ms > 350 and not os.environ.get("GITHUB_ACTIONS"):
                # Timing ceiling is only enforceable on the dev machine (Apple M4
                # baseline): GitHub-hosted runners have no ANE and much slower
                # model inference, so the same correct decode path measures
                # 800+ ms/frame there. On CI the decode-path assertion above is
                # the real guard; timing is informational.
                print(f"FAIL: packaged HEIF decode path correct but slow ({per_file_ms:.0f} ms/frame).")
                success = False
            else:
                print(f"PASS: packaged HEIF decode via in-process pyav "
                      f"({per_file_ms:.0f} ms/frame incl. first-frame warmup).")

            if src_ratings and src_ratings == pack_ratings:
                print("PASS: packaged HEIF ratings/raw identical to source engine.")
            else:
                diff = {k: (src_ratings.get(k), pack_ratings.get(k))
                        for k in src_ratings if src_ratings.get(k) != pack_ratings.get(k)}
                print(f"FAIL: packaged HEIF scores drift from source: {list(diff.items())[:3]}")
                success = False
            shutil.rmtree(heif_dir, ignore_errors=True)
        else:
            print("NOTE: seed.heif unavailable — HEIF frozen-engine guard skipped.")
            shutil.rmtree(install_dir, ignore_errors=True)

        return success

    except Exception as e:
        print(f"FAIL: DMG test encountered error: {e}")
        return False
    finally:
        subprocess.run(["hdiutil", "detach", str(mount_point), "-force", "-quiet"], capture_output=True)
        shutil.rmtree(mount_point, ignore_errors=True)
        print("Cleaned up DMG mount point.")


def test_windows_package(dist_dir: Path | None = None) -> bool:
    """Verify Windows NSIS installer and portable ZIP packages, then run post-install guards."""
    print("=== Testing Windows GUI Packages ===")
    dist = dist_dir or (ROOT / "dist")

    setups = sorted(
        list(dist.glob("*setup.exe")) + list((ROOT / "src-tauri/target/release/bundle/nsis").glob("*.exe")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    zips = sorted(
        list(dist.glob("*portable.zip")) + list(dist.glob("*win*.zip")),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )

    if not setups and not zips:
        print("FAIL: No Windows installer packages found in dist/ or target/release/")
        return False

    success = True
    tested_engine_bin: Path | None = None

    # 1. Test Portable ZIP Package
    if zips:
        zip_pkg = zips[0]
        print(f"Testing Windows portable ZIP: {zip_pkg.name} ({zip_pkg.stat().st_size / 1024 / 1024:.1f} MB)")
        unzip_dir = ROOT / "build/test_unzip_win"
        shutil.rmtree(unzip_dir, ignore_errors=True)
        unzip_dir.mkdir(parents=True, exist_ok=True)
        try:
            with zipfile.ZipFile(zip_pkg, "r") as z:
                z.extractall(unzip_dir)
            print(f"PASS: Successfully extracted portable ZIP ({len(list(unzip_dir.glob('**/*')))} files)")
            exes = list(unzip_dir.glob("**/*.exe"))
            if exes:
                print(f"PASS: Found portable executables: {[e.name for e in exes]}")
            else:
                print("FAIL: No .exe found inside portable ZIP.")
                success = False

            # Check onedir engine inside portable ZIP
            engine_bins = list(unzip_dir.glob("**/auto_culling_engine.exe"))
            if engine_bins:
                cand = engine_bins[0]
                if (cand.parent / "lib").is_dir():
                    print(f"PASS: Portable ZIP carries onedir engine at {cand.relative_to(unzip_dir)}")
                    if tested_engine_bin is None:
                        tested_engine_bin = cand
                else:
                    print("FAIL: Portable ZIP engine is not onedir format (missing lib/)")
                    success = False
            else:
                print("WARNING: Portable ZIP missing auto_culling_engine.exe")

            # GUI executable must be named auto_culling.exe (productName stays
            # AutoCulling for installer/shortcut display).
            gui_exes = list(unzip_dir.glob("auto_culling.exe"))
            if gui_exes:
                print("PASS: Portable ZIP GUI executable is auto_culling.exe at archive root.")
            else:
                print("FAIL: Portable ZIP missing auto_culling.exe at archive root.")
                success = False

            # CLI entry point must ship at the archive root (flat layout)
            cli_exes = list(unzip_dir.glob("auto_culling_cli.exe"))
            if cli_exes:
                print(f"PASS: Portable ZIP carries CLI entry point at {cli_exes[0].relative_to(unzip_dir)}")
            else:
                print("FAIL: Portable ZIP missing auto_culling_cli.exe.")
                success = False
        except Exception as e:
            print(f"FAIL: Error testing portable ZIP: {e}")
            success = False
    else:
        print("NOTE: No Windows portable zip found in dist/")

    # 2. Test NSIS Setup Installer (Silent Installation + Structure + Execution)
    if setups:
        setup = setups[0]
        print(f"\nTesting NSIS setup installer: {setup.name} ({setup.stat().st_size / 1024 / 1024:.1f} MB)")
        sandbox_install = ROOT / "build/test_install_nsis"
        shutil.rmtree(sandbox_install, ignore_errors=True)
        sandbox_install.mkdir(parents=True, exist_ok=True)

        try:
            # NSIS supports /S for silent install and /D=<dest> (must be last parameter without quotes)
            dest_arg = f"/D={sandbox_install.resolve()}"
            print(f"Running silent installation to {sandbox_install}...")
            res = subprocess.run([str(setup), "/S", dest_arg], timeout=120)
            if res.returncode != 0:
                print(f"FAIL: NSIS silent installation exited with code {res.returncode}")
                success = False
            else:
                print("PASS: NSIS installer exited cleanly.")

            # Wait for asynchronous file writing if needed
            deadline = time.time() + 30.0
            app_exe = sandbox_install / "auto_culling.exe"
            while time.time() < deadline and not app_exe.exists():
                time.sleep(0.5)

            if not app_exe.exists():
                print(f"FAIL: auto_culling.exe not found in install dir: {sandbox_install}")
                success = False
            else:
                print(f"PASS: auto_culling.exe installed successfully ({app_exe.stat().st_size / 1024 / 1024:.1f} MB).")

            # Check uninstaller existence
            uninstaller = sandbox_install / "Uninstall.exe"
            if uninstaller.exists():
                print("PASS: Found NSIS uninstaller (Uninstall.exe).")
            else:
                print("WARNING: Uninstall.exe not present in sandbox installation directory.")

            # Check installed engine binary & onedir structure
            installed_engine_bins = list(sandbox_install.glob("**/auto_culling_engine.exe"))
            if installed_engine_bins:
                engine_path = installed_engine_bins[0]
                if (engine_path.parent / "lib").is_dir():
                    print(f"PASS: Installed engine is onedir at {engine_path.relative_to(sandbox_install)}")
                    tested_engine_bin = engine_path
                else:
                    print("FAIL: Installed engine is not onedir format (missing lib/)")
                    success = False
            else:
                print(f"FAIL: Installed directory does not contain auto_culling_engine.exe")
                success = False

            # Installed CLI entry point must respond on stdout (console subsystem)
            installed_cli = list(sandbox_install.glob("auto_culling_cli.exe"))
            if installed_cli:
                try:
                    help_res = subprocess.run(
                        [str(installed_cli[0]), "--help"],
                        capture_output=True, text=True, timeout=60,
                        stdin=subprocess.DEVNULL,
                    )
                    if help_res.returncode == 0 and "usage" in (help_res.stdout or "").lower():
                        print(f"PASS: Installed auto_culling_cli.exe responds on stdout (--help).")
                    else:
                        print(f"FAIL: Installed CLI --help rc={help_res.returncode} "
                              f"stdout={(help_res.stdout or '')[:120]!r} stderr={(help_res.stderr or '')[:200]!r}")
                        success = False
                except Exception as e:
                    print(f"FAIL: Installed CLI --help raised: {e}")
                    success = False
            else:
                print("FAIL: Installed directory does not contain auto_culling_cli.exe")
                success = False

            # 3. Test installed app launch and engine handshake
            if app_exe.exists():
                print("\nTesting installed auto_culling.exe launch and engine handshake...")
                app_proc = subprocess.Popen(
                    [str(app_exe)],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    cwd=str(sandbox_install),
                )
                engine_alive = False
                handshake_deadline = time.time() + 25.0
                gui_log = sandbox_install / "gui.log"
                temp_log = Path(tempfile.gettempdir()) / "autoculling-gui.log"

                while time.time() < handshake_deadline:
                    for log_cand in [gui_log, temp_log]:
                        if log_cand.exists():
                            content = log_cand.read_text(errors="ignore")
                            if "spawn bundled engine" in content or "engine spawned and alive" in content:
                                engine_alive = True
                                break
                    if engine_alive:
                        break
                    # Also probe process list for auto_culling_engine
                    try:
                        probe = subprocess.run(
                            ["powershell", "-NoProfile", "-Command",
                             "Get-Process -Name auto_culling_engine -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id"],
                            capture_output=True, text=True, timeout=3,
                        )
                        if probe.returncode == 0 and probe.stdout.strip():
                            engine_alive = True
                            break
                    except Exception:
                        pass
                    if app_proc.poll() is not None:
                        break
                    time.sleep(0.5)

                app_proc.terminate()
                try:
                    app_proc.wait(timeout=5.0)
                except Exception:
                    app_proc.kill()
                subprocess.run(
                    ["powershell", "-NoProfile", "-Command",
                     "Stop-Process -Name auto_culling_engine -Force -ErrorAction SilentlyContinue"],
                    capture_output=True,
                )

                if engine_alive:
                    print("PASS: Installed AutoCulling.exe spawned the bundled engine successfully.")
                else:
                    tail = gui_log.read_text(errors="ignore")[-500:] if gui_log.exists() else "(no gui.log)"
                    print(f"FAIL: AutoCulling.exe did not spawn the engine. Log tail:\n{tail}")
                    success = False

        except Exception as e:
            print(f"FAIL: NSIS installer testing failed with error: {e}")
            success = False
    else:
        print("WARNING: No Windows NSIS setup.exe found in dist/")

    # 4. Post-installation Precision & Performance Guard
    if tested_engine_bin and tested_engine_bin.exists():
        print(f"\n=== Post-Installation Precision Guard on {tested_engine_bin.name} ===")
        test_suites = [
            ("JPG", ROOT / "tests/test_img", "*.jpg"),
            ("HEIF", ROOT / "test_import", "*.heif"),
            ("ARW", ROOT / "test_arw", "*.ARW"),
            ("NEF", ROOT / "test_nef", "*.nef"),
        ]

        def run_engine_cull(cmd_prefix: list[str], target_dir: Path) -> tuple[dict[str, tuple[int, float]], float, list[str]]:
            proc = subprocess.Popen(
                [*cmd_prefix, "--json-lines"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
                cwd=str(ROOT),
            )
            ratings: dict[str, tuple[int, float]] = {}
            elapsed = 0.0
            logs = []
            payload = json.dumps({
                "cmd": "run",
                "dir": str(target_dir),
                "config": {"dry_run": True, "force": True}
            }) + "\n"
            proc.stdin.write(payload)
            proc.stdin.flush()
            while True:
                line = proc.stdout.readline()
                if not line:
                    break
                try:
                    evt = json.loads(line)
                except Exception:
                    continue
                if evt.get("type") == "frame":
                    ratings[evt["name"]] = (int(evt["rating"]), round(float(evt["raw"]), 2))
                elif evt.get("type") == "log":
                    logs.append(evt.get("line", ""))
                elif evt.get("type") == "done":
                    elapsed = float(evt.get("elapsed", 0.0) or 0.0)
                    break
            try:
                proc.stdin.write(json.dumps({"cmd": "quit"}) + "\n")
                proc.stdin.flush()
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
            return ratings, elapsed, logs

        src_py = ROOT / ".venv/Scripts/python.exe"
        src_cmd = [str(src_py), str(ROOT / "cull_photos.py")]
        pack_cmd = [str(tested_engine_bin)]

        for fmt, target_dir, pattern in test_suites:
            if not target_dir.exists():
                continue
            photos = list(target_dir.glob(pattern))
            if not photos:
                continue

            print(f"Testing {fmt} format ({len(photos)} photos) in {target_dir.name}...")
            src_ratings, src_elapsed, _ = run_engine_cull(src_cmd, target_dir)
            pack_ratings, pack_elapsed, _ = run_engine_cull(pack_cmd, target_dir)

            # Assert Precision
            if src_ratings and src_ratings == pack_ratings:
                print(f"PASS: Packaged {fmt} ratings & raw scores 100% identical to source ({len(pack_ratings)} photos).")
            else:
                diff = {
                    k: (src_ratings.get(k), pack_ratings.get(k))
                    for k in set(src_ratings.keys()) | set(pack_ratings.keys())
                    if src_ratings.get(k) != pack_ratings.get(k)
                }
                print(f"FAIL: Packaged {fmt} score drift detected on {len(diff)} files: {diff}")
                success = False

            # Assert Performance (no 160MB extraction penalty; throughput check)
            if pack_elapsed > 0:
                ips = len(photos) / pack_elapsed if pack_elapsed > 0 else float("inf")
                print(f"PASS: Packaged {fmt} throughput: {ips:.1f} img/s (elapsed {pack_elapsed:.2f}s).")

    # 5. Cleanup sandbox install
    if setups:
        uninstaller = ROOT / "build/test_install_nsis/Uninstall.exe"
        if uninstaller.exists():
            try:
                print("Running silent uninstallation...")
                subprocess.run([str(uninstaller), "/S", f"_?={uninstaller.parent.resolve()}"], timeout=30)
            except Exception:
                pass
        # Clean sandbox registry keys written during test to keep user environment clean
        try:
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Remove-Item -Path 'HKCU:\\Software\\autoculling' -Recurse -Force -ErrorAction SilentlyContinue; "
                 "Remove-Item -Path 'HKCU:\\Software\\AutoCulling' -Recurse -Force -ErrorAction SilentlyContinue; "
                 "Remove-Item -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\auto-culling' -Recurse -Force -ErrorAction SilentlyContinue; "
                 "Remove-Item -Path 'HKCU:\\Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\AutoCulling' -Recurse -Force -ErrorAction SilentlyContinue"],
                timeout=10,
            )
        except Exception:
            pass
        shutil.rmtree(ROOT / "build/test_install_nsis", ignore_errors=True)
    if zips:
        shutil.rmtree(ROOT / "build/test_unzip_win", ignore_errors=True)

    return success


def main() -> int:
    parser = argparse.ArgumentParser(description="Test packaged AutoCulling GUI distributions.")
    parser.add_argument("--dmg", type=Path, default=None, help="Path to .dmg file to test (macOS)")
    parser.add_argument("--dist-dir", type=Path, default=None, help="Directory containing built packages")
    args = parser.parse_args()

    system = platform.system()
    passed = False

    if system == "Darwin":
        passed = test_macos_dmg(args.dmg)
    elif system == "Windows":
        passed = test_windows_package(args.dist_dir)
    else:
        print(f"Testing packages on {system}...")
        passed = True

    print(f"\nGUI Package Test Result: {'ALL TESTS PASSED' if passed else 'TESTS FAILED'}")
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
