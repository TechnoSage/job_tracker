"""
builder_app.py — Build Dashboard Flask application (port 5001).

Provides a browser-based UI for configuring and running the build pipeline:
  - Edit all build_config settings and persist them to build_settings.json
  - View git status and merge branches (dev -> main)
  - Clean build outputs, run bundle-only or full build
  - Stream build output in real time via log polling
  - Check / install optional code-protection dependencies (PyArmor, Nuitka)
  - Open the license file in Notepad++ (or system default text editor)

Import and run via start_builder.py in the project root.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request

# ── Paths ─────────────────────────────────────────────────────────────────────

BUILD_DIR     = Path(__file__).parent.resolve()
PROJECT_ROOT  = BUILD_DIR.parent
SETTINGS_FILE = BUILD_DIR / "build_settings.json"

# ── Settings helpers ──────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "APP_NAME":            "Job Tracker",
    "APP_VERSION":         "1.0.0",
    "APP_DESCRIPTION":     "Personal job search tracker",
    "APP_PUBLISHER":       "Your Name or Company",
    "APP_URL":             "https://github.com/yourname/job_tracker",
    "APP_SUPPORT_URL":     "https://github.com/yourname/job_tracker/issues",
    "APP_EXE_NAME":        "JobTracker",
    "OUTPUT_DIR":          "dist",
    "DEFAULT_INSTALL_DIR": r"{autopf}\JobTracker",
    "REQUIRE_ADMIN":       True,
    "DESKTOP_ICON":        True,
    "START_MENU_ICON":     True,
    "ADD_TO_STARTUP":      False,
    "ICON_FILE":           "",
    "LICENSE_FILE":        "",
    "USE_PYARMOR":         False,
    "USE_NUITKA":          False,
    "GIT_REPO_DIR":        "",   # repo root; "" = use PROJECT_ROOT
    "GIT_REMOTE_URL":      "",   # GitHub remote URL, e.g. https://github.com/user/repo.git
    "GIT_DEV_BRANCH":      "development",
    "GIT_MAIN_BRANCH":     "main",
}


def _load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return {**_DEFAULTS, **json.loads(SETTINGS_FILE.read_text("utf-8"))}
        except Exception:
            pass
    return dict(_DEFAULTS)


def _save_settings(data: dict) -> None:
    merged = {k: data.get(k, v) for k, v in _DEFAULTS.items()}
    SETTINGS_FILE.write_text(json.dumps(merged, indent=2), encoding="utf-8")


def _git_cwd() -> str:
    """Return the git working-tree root from settings, or PROJECT_ROOT."""
    d = _load_settings().get("GIT_REPO_DIR", "").strip()
    if d and Path(d).is_dir():
        return d
    return str(PROJECT_ROOT)


# ── Build state (shared between threads) ──────────────────────────────────────

_build_lock:   threading.Lock = threading.Lock()
_build_log:    list[str]      = []
_build_status: str            = "idle"   # idle | running | done | error


def _append(line: str) -> None:
    with _build_lock:
        _build_log.append(line)


def _set_status(s: str) -> None:
    global _build_status
    with _build_lock:
        _build_status = s


def _run_build_process(cmd: list[str]) -> None:
    """Run a subprocess (build or pip install), stream its output to _build_log."""
    _append("-" * 56)
    _append("Command: " + " ".join(str(c) for c in cmd))
    _append("-" * 56)
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        for line in proc.stdout:
            _append(line.rstrip())
        proc.wait()
        _append("")
        _append("-" * 56)
        _append(f"Process exited with code {proc.returncode}")
        _append("-" * 56)
        _set_status("done" if proc.returncode == 0 else "error")
    except Exception as exc:
        _append(f"[EXCEPTION] {exc}")
        _set_status("error")


def _git_seq(cmds: list[list[str]], cwd: str) -> bool:
    """Run git command-arg-lists sequentially, streaming output to _build_log.
    Returns True if all exit 0, False on first failure (sets status to error)."""
    for args in cmds:
        full_cmd = ["git"] + args
        _append("-" * 56)
        _append("Git: " + " ".join(str(a) for a in full_cmd))
        _append("-" * 56)
        try:
            proc = subprocess.Popen(
                full_cmd, cwd=cwd,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
            )
            for line in proc.stdout:
                _append(line.rstrip())
            proc.wait()
            _append(f"Exit code: {proc.returncode}")
            if proc.returncode != 0:
                _set_status("error")
                return False
        except FileNotFoundError:
            _append("[ERROR] git not found in PATH")
            _set_status("error")
            return False
        except Exception as exc:
            _append(f"[EXCEPTION] {exc}")
            _set_status("error")
            return False
    return True


def _do_git_setup() -> None:
    """Threaded: init repo, create .gitignore, set remote, ensure branches, checkout dev."""
    settings    = _load_settings()
    cwd         = _git_cwd()
    remote_url  = settings.get("GIT_REMOTE_URL", "").strip()
    dev_branch  = settings.get("GIT_DEV_BRANCH",  "development").strip() or "development"
    main_branch = settings.get("GIT_MAIN_BRANCH", "main").strip() or "main"

    _append("=" * 56)
    _append("Git Setup")
    _append("=" * 56)

    # 1. Init if not already a repo
    git_dir = Path(cwd) / ".git"
    if not git_dir.is_dir():
        _append("Initialising new git repository...")
        if not _git_seq([["init"]], cwd):
            return
    else:
        _append(f"Repository already exists: {cwd}")

    # 2. Create .gitignore at repo root if missing
    gitignore_path = Path(cwd) / ".gitignore"
    if not gitignore_path.exists():
        _append("Creating .gitignore...")
        content = (
            "# Python
__pycache__/
*.pyc
*.pyo
*.pyd

"
            "# Databases
*.db
*.db-journal
*.sqlite
*.sqlite3

"
            "# Build outputs
dist/
build_output/
obf_src/

"
            "# Virtual environments
venv/
.venv/
env/

"
            "# Packaging
*.egg-info/

"
            "# Secrets
.env

"
            "# App-specific
email_responses/
*.log
instance/

"
            "# Build settings (local paths, not for version control)
build/build_settings.json
"
        )
        try:
            gitignore_path.write_text(content, encoding="utf-8")
            _append(f"  Created: {gitignore_path}")
        except Exception as exc:
            _append(f"[ERROR] Could not write .gitignore: {exc}")
            _set_status("error")
            return
    else:
        _append(".gitignore already exists")

    # 3. Set or update remote origin
    if remote_url:
        _append(f"Setting remote origin: {remote_url}")
        existing = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            cwd=cwd, capture_output=True, text=True,
        )
        if existing.returncode == 0:
            if not _git_seq([["remote", "set-url", "origin", remote_url]], cwd):
                return
        else:
            if not _git_seq([["remote", "add", "origin", remote_url]], cwd):
                return
    else:
        _append("No GIT_REMOTE_URL configured — skipping remote setup.")

    # 4. Initial commit if repo is empty (required before branch creation)
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True,
    )
    if head.returncode != 0:
        _append("No commits yet — creating initial commit...")
        if not _git_seq([
            ["add", ".gitignore"],
            ["commit", "-m", "chore: initial commit"],
        ], cwd):
            return

    # 5. Ensure main branch exists
    bl_main = subprocess.run(
        ["git", "branch", "--list", main_branch],
        cwd=cwd, capture_output=True, text=True,
    )
    if not bl_main.stdout.strip():
        _append(f"Creating branch: {main_branch}")
        if not _git_seq([["branch", main_branch]], cwd):
            return
    else:
        _append(f"Branch exists: {main_branch}")

    # 6. Ensure dev branch exists
    bl_dev = subprocess.run(
        ["git", "branch", "--list", dev_branch],
        cwd=cwd, capture_output=True, text=True,
    )
    if not bl_dev.stdout.strip():
        _append(f"Creating branch: {dev_branch}")
        if not _git_seq([["branch", dev_branch]], cwd):
            return
    else:
        _append(f"Branch exists: {dev_branch}")

    # 7. Checkout dev branch
    _append(f"Switching to {dev_branch}...")
    if not _git_seq([["checkout", dev_branch]], cwd):
        return

    _append("")
    _append("Git setup complete.")
    _append(f"  Working dir:  {cwd}")
    _append(f"  Dev branch:   {dev_branch}")
    if remote_url:
        _append(f"  Remote:       origin → {remote_url}")
    _set_status("done")


def _do_commit_push(message: str) -> None:
    """Threaded: checkout dev, stage all, commit, push to origin dev branch."""
    settings    = _load_settings()
    cwd         = _git_cwd()
    dev_branch  = settings.get("GIT_DEV_BRANCH",  "development").strip() or "development"
    remote_url  = settings.get("GIT_REMOTE_URL", "").strip()

    _append("=" * 56)
    _append(f"Commit & Push → {dev_branch}")
    _append("=" * 56)

    if not message:
        _append("[ERROR] Commit message is required.")
        _set_status("error")
        return

    cmds: list[list[str]] = [
        ["checkout", dev_branch],
        ["add", "."],
        ["commit", "-m", message],
    ]
    if remote_url:
        cmds.append(["push", "-u", "origin", dev_branch])
    else:
        _append("No GIT_REMOTE_URL configured — commit only (no push).")

    if not _git_seq(cmds, cwd):
        return

    _append("")
    _append("Commit & push complete.")
    _set_status("done")


def _do_push_branch(branch: str) -> None:
    """Threaded: push a named branch to origin."""
    cwd        = _git_cwd()
    remote_url = _load_settings().get("GIT_REMOTE_URL", "").strip()

    _append("=" * 56)
    _append(f"Push Branch: {branch}")
    _append("=" * 56)

    if not remote_url:
        _append("[ERROR] No GIT_REMOTE_URL configured.")
        _set_status("error")
        return

    if not _git_seq([["push", "-u", "origin", branch]], cwd):
        return

    _append("")
    _append(f"Branch '{branch}' pushed to origin.")
    _set_status("done")


def _run_clean() -> None:
    """Remove build output directories without running a subprocess."""
    settings = _load_settings()
    output_dir = settings.get("OUTPUT_DIR", "dist")
    _append("-" * 56)
    _append("Cleaning build outputs")
    _append("-" * 56)
    for name in ("build_output", "obf_src", output_dir):
        p = PROJECT_ROOT / name
        if p.exists():
            shutil.rmtree(p, ignore_errors=True)
            _append(f"  Removed:      {name}/")
        else:
            _append(f"  Already gone: {name}/")
    _append("")
    _append("Clean complete.")
    _set_status("done")


# ── Flask application factory ─────────────────────────────────────────────────

def create_builder_app() -> Flask:
    app = Flask(__name__, template_folder=str(BUILD_DIR))

    # ── Settings ──────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("builder.html")

    @app.route("/api/settings", methods=["GET"])
    def api_settings_get():
        return jsonify(_load_settings())

    @app.route("/api/settings", methods=["POST"])
    def api_settings_post():
        data = request.get_json(silent=True) or {}
        _save_settings(data)
        return jsonify({"ok": True})

    # ── Git ───────────────────────────────────────────────────────────────────

    @app.route("/api/git/status")
    def api_git_status():
        cwd = _git_cwd()

        def _git(*args: str) -> tuple[str, int]:
            try:
                r = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                return (r.stdout + r.stderr).strip(), r.returncode
            except FileNotFoundError:
                return "git not found in PATH", -1
            except Exception as exc:
                return str(exc), -1

        branch, _   = _git("branch", "--show-current")
        status, _   = _git("status", "--short")
        log, _      = _git("log", "--oneline", "-10")
        branches, _ = _git("branch", "-a")

        return jsonify({
            "current_branch": branch,
            "status":         status,
            "recent_log":     log,
            "branches":       branches,
            "cwd":            cwd,
        })

    @app.route("/api/git/merge", methods=["POST"])
    def api_git_merge():
        cwd    = _git_cwd()
        data   = request.get_json(silent=True) or {}
        source = data.get("source", "dev").strip()
        target = data.get("target", "main").strip()
        if not source or not target:
            return jsonify({"ok": False, "error": "source and target branch names are required"})

        def _git(args: list[str]) -> tuple[str, int]:
            try:
                r = subprocess.run(
                    ["git", *args],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                return (r.stdout + r.stderr).strip(), r.returncode
            except Exception as exc:
                return str(exc), -1

        out, code = _git(["checkout", target])
        if code != 0:
            return jsonify({"ok": False, "error": f"checkout {target}: {out}"})

        out, code = _git(["merge", source, "--no-edit"])
        return jsonify({"ok": code == 0, "output": out,
                        "error": "" if code == 0 else out})

    @app.route("/api/git/setup", methods=["POST"])
    def api_git_setup():
        global _build_log, _build_status
        with _build_lock:
            if _build_status == "running":
                return jsonify({"ok": False, "error": "A process is already running"})
            _build_log    = []
            _build_status = "running"
        t = threading.Thread(target=_do_git_setup, daemon=True)
        t.start()
        return jsonify({"ok": True})

    @app.route("/api/git/commit-push", methods=["POST"])
    def api_git_commit_push():
        global _build_log, _build_status
        data    = request.get_json(silent=True) or {}
        message = data.get("message", "").strip()
        if not message:
            return jsonify({"ok": False, "error": "Commit message is required"})
        with _build_lock:
            if _build_status == "running":
                return jsonify({"ok": False, "error": "A process is already running"})
            _build_log    = []
            _build_status = "running"
        t = threading.Thread(target=_do_commit_push, args=(message,), daemon=True)
        t.start()
        return jsonify({"ok": True})

    @app.route("/api/git/push-branch", methods=["POST"])
    def api_git_push_branch():
        global _build_log, _build_status
        data   = request.get_json(silent=True) or {}
        branch = data.get("branch", "").strip()
        if not branch:
            return jsonify({"ok": False, "error": "branch is required"})
        with _build_lock:
            if _build_status == "running":
                return jsonify({"ok": False, "error": "A process is already running"})
            _build_log    = []
            _build_status = "running"
        t = threading.Thread(target=_do_push_branch, args=(branch,), daemon=True)
        t.start()
        return jsonify({"ok": True})

    # ── File opener ───────────────────────────────────────────────────────────

    @app.route("/api/open-file", methods=["POST"])
    def api_open_file():
        data     = request.get_json(silent=True) or {}
        filepath = data.get("path", "").strip()
        if not filepath:
            return jsonify({"ok": False, "error": "No file path provided"})

        p = Path(filepath)
        if not p.is_absolute():
            p = PROJECT_ROOT / p

        if not p.exists():
            return jsonify({"ok": False, "error": f"File not found: {p}"})

        # Try Notepad++ first
        notepad_paths = [
            r"C:\Program Files\Notepad++\notepad++.exe",
            r"C:\Program Files (x86)\Notepad++\notepad++.exe",
        ]
        for npp in notepad_paths:
            if os.path.isfile(npp):
                subprocess.Popen([npp, str(p)])
                return jsonify({"ok": True, "editor": "Notepad++"})

        # Fall back to system default (e.g. Notepad or whatever is registered)
        os.startfile(str(p))
        return jsonify({"ok": True, "editor": "system default"})

    # ── Dependency check / install ────────────────────────────────────────────

    @app.route("/api/deps/check")
    def api_deps_check():
        tool = request.args.get("tool", "").strip()
        if tool not in ("pyarmor", "nuitka"):
            return jsonify({"installed": False, "error": "Unknown tool"})

        r = subprocess.run(
            [sys.executable, "-m", "pip", "show", tool],
            capture_output=True, text=True, timeout=15,
        )
        installed = r.returncode == 0
        version   = ""
        if installed:
            for line in r.stdout.splitlines():
                if line.lower().startswith("version:"):
                    version = line.split(":", 1)[1].strip()
                    break
        return jsonify({"installed": installed, "version": version})

    @app.route("/api/deps/install", methods=["POST"])
    def api_deps_install():
        global _build_log, _build_status
        data = request.get_json(silent=True) or {}
        tool = data.get("tool", "").strip()
        pip_pkgs = {"pyarmor": "pyarmor>=8.0", "nuitka": "nuitka>=2.0"}
        pkg = pip_pkgs.get(tool)
        if not pkg:
            return jsonify({"ok": False, "error": f"Unknown tool: {tool}"})

        with _build_lock:
            if _build_status == "running":
                return jsonify({"ok": False, "error": "Cannot install while a build is running"})
            _build_log    = []
            _build_status = "running"

        cmd = [sys.executable, "-m", "pip", "install", pkg]
        t   = threading.Thread(target=_run_build_process, args=(cmd,), daemon=True)
        t.start()
        return jsonify({"ok": True})

    # ── Main-app status ───────────────────────────────────────────────────────

    @app.route("/api/check-mainapp")
    def api_check_mainapp():
        """Return whether the main Job Tracker app (port 5000) is reachable."""
        import socket
        try:
            with socket.create_connection(("127.0.0.1", 5000), timeout=0.5):
                return jsonify({"running": True})
        except OSError:
            return jsonify({"running": False})

    # ── Build ─────────────────────────────────────────────────────────────────

    @app.route("/api/build/start", methods=["POST"])
    def api_build_start():
        global _build_log, _build_status
        with _build_lock:
            if _build_status == "running":
                return jsonify({"ok": False, "error": "A build is already running"})
            _build_log    = []
            _build_status = "running"

        data     = request.get_json(silent=True) or {}
        mode     = data.get("mode", "full")
        settings = data.get("settings")
        if settings:
            _save_settings(settings)

        build_py = str(BUILD_DIR / "build.py")
        cmd = [sys.executable, build_py]
        if mode == "bundle-only":
            cmd.append("--bundle-only")
        elif mode == "installer-only":
            cmd.append("--installer-only")

        # Capture git config at request time (before the thread runs)
        loaded      = _load_settings()
        remote_url  = loaded.get("GIT_REMOTE_URL",  "").strip()
        dev_branch  = loaded.get("GIT_DEV_BRANCH",  "development").strip() or "development"
        main_branch = loaded.get("GIT_MAIN_BRANCH", "main").strip() or "main"
        use_git     = bool(remote_url) and mode == "full"

        def _run_full_build() -> None:
            cwd = _git_cwd()
            if use_git:
                _append("=" * 56)
                _append(f"Full Build: checking out '{main_branch}' for production build...")
                _append("=" * 56)
                if not _git_seq([["checkout", main_branch]], cwd):
                    return  # status already set to "error" by _git_seq

            _run_build_process(cmd)  # sets _build_status to "done" or "error"

            if use_git:
                _append("=" * 56)
                _append(f"Build done — switching back to '{dev_branch}'...")
                _append("=" * 56)
                _git_seq([["checkout", dev_branch]], cwd)
                # Do NOT override _build_status here; keep the build result

        t = threading.Thread(target=_run_full_build, daemon=True)
        t.start()
        return jsonify({"ok": True})

    @app.route("/api/build/clean", methods=["POST"])
    def api_build_clean():
        global _build_log, _build_status
        with _build_lock:
            if _build_status == "running":
                return jsonify({"ok": False, "error": "A build is already running"})
            _build_log    = []
            _build_status = "running"

        t = threading.Thread(target=_run_clean, daemon=True)
        t.start()
        return jsonify({"ok": True})

    @app.route("/api/build/log")
    def api_build_log():
        offset = int(request.args.get("offset", 0))
        with _build_lock:
            lines  = _build_log[offset:]
            status = _build_status
            total  = len(_build_log)
        return jsonify({"lines": lines, "status": status, "total": total})

    return app
