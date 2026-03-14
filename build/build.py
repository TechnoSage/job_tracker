"""
build.py — Commercial-grade build + installer pipeline.

Produces a fully portable Windows installer (.exe) from the Python source.
The installer bundles every dependency so the app runs on any Windows 10+
machine with no Python or extra software required.

Steps
-----
  1. Ensure all Python dependencies from requirements.txt are installed
  2. Ensure Inno Setup 6 is installed (auto-downloads if missing)
  3. PyInstaller  — packages app + all deps into a standalone folder
  4. Inno Setup   — wraps the folder into a distributable Setup_*.exe

Output
------
  dist/JobTracker_Setup_1.0.0.exe   (ready to ship / sell)

Usage
-----
  python build/build.py               # full build (steps 1-4)
  python build/build.py --bundle-only # steps 1-3 only (skip installer)
  python build/build.py --clean       # wipe previous outputs first
  python build/build.py --help

Mac OS
------
  PyInstaller also supports macOS.  Run on a Mac to produce a .app bundle.
  DMG creation via create-dmg or hdiutil can be added as a future step.

Commercial Distribution — License Summary
-----------------------------------------
All components used in this pipeline are royalty-free for commercial use:

  PyInstaller 6.x         MIT License      ✓ free commercial use, no royalties
  Inno Setup 6.7          Inno Setup License (Jordan Russell)
                                           ✓ free commercial use, no royalties
  Flask / Jinja2          BSD-3-Clause     ✓ free commercial use
  Werkzeug                BSD-3-Clause     ✓ free commercial use
  SQLAlchemy              MIT              ✓ free commercial use
  APScheduler             MIT              ✓ free commercial use
  Requests                Apache 2.0       ✓ free commercial use
  BeautifulSoup4          MIT              ✓ free commercial use
  feedparser              BSD-2-Clause     ✓ free commercial use
  icalendar               BSD-3-Clause     ✓ free commercial use
  pytz                    MIT              ✓ free commercial use
  pypdf                   BSD-3-Clause     ✓ free commercial use
  python-docx             MIT              ✓ free commercial use
  openpyxl                MIT              ✓ free commercial use
  google-auth / API       Apache 2.0       ✓ free commercial use
  msal                    MIT              ✓ free commercial use
  certifi                 MPL-2.0          ✓ free commercial use
  charset-normalizer      MIT              ✓ free commercial use
  Python runtime DLLs     Python PSF       ✓ free commercial redistribution
  VC++ Runtime DLLs       Microsoft Distributable Code
                                           ✓ free redistribution when bundled
                                             with your compiled application

  ⚠ PyArmor (optional)  Proprietary — requires a PAID commercial license
                         if you enable the PyArmor protection mode and
                         distribute commercially.  See: pyarmor.dashingsoft.com
                         Do NOT enable USE_PYARMOR without purchasing a license.

  ⚠ Nuitka commercial   The free Nuitka edition is Apache 2.0 (✓ free).
                         "Nuitka Commercial" adds extra features but is paid.
                         This pipeline uses only the free edition.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import urllib.request
from pathlib import Path

# ── Locate project root and import build config ───────────────────────────────

BUILD_DIR    = Path(__file__).parent.resolve()
PROJECT_ROOT = BUILD_DIR.parent
sys.path.insert(0, str(BUILD_DIR))
import build_config as C   # noqa: E402

# ── Output directory helpers ──────────────────────────────────────────────────

def _output_root() -> Path:
    """Absolute path to the configured output root (e.g. D:\\Compile Playground\\JobTracker)."""
    base = Path(C.OUTPUT_DIR)
    return base if base.is_absolute() else PROJECT_ROOT / C.OUTPUT_DIR


def _dist_path() -> Path:
    """Absolute path to the 'Bundle Only' subdir where PyInstaller writes its output."""
    return _output_root() / "Bundle Only"


# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(text: str) -> None:
    width = 62
    print(f"\n{'─' * width}")
    print(f"  {text}")
    print(f"{'─' * width}")


def _run(cmd: list, cwd: Path | None = None, check: bool = True,
         env: dict | None = None) -> int:
    """Stream a subprocess to stdout; exit on failure unless check=False."""
    printable = " ".join(str(c) for c in cmd)
    print(f"\n>>> {printable}\n")
    result = subprocess.run(cmd, cwd=str(cwd or PROJECT_ROOT), env=env)
    if check and result.returncode != 0:
        print(f"\n[ERROR] Command exited with code {result.returncode}")
        sys.exit(result.returncode)
    return result.returncode


def write_build_meta() -> Path:
    """Write build_meta.json to the project root before bundling.

    This file is bundled into the compiled exe (via --add-data) so the frozen
    app can read it from sys._MEIPASS at startup to seed runtime settings such
    as support_contact_email.  The file is safe to gitignore.
    """
    meta = {
        "support_email": getattr(C, "SUPPORT_EMAIL", ""),
        "app_name":      C.APP_NAME,
        "app_version":   C.APP_VERSION,
        "publisher":     C.APP_PUBLISHER,
    }
    path = PROJECT_ROOT / "build_meta.json"
    path.write_text(json.dumps(meta, indent=2), encoding="utf-8")
    print(f"[build_meta] Written: {path}")
    return path


def _find_iscc() -> Path | None:
    """Locate Inno Setup's command-line compiler ISCC.exe."""
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return Path(p)
    which = shutil.which("ISCC") or shutil.which("iscc")
    return Path(which) if which else None


def _find_signtool() -> Path | None:
    """Locate signtool.exe from the Windows 10/11 SDK (x64 preferred, newest first)."""
    import glob as _glob
    patterns = [
        r"C:\Program Files (x86)\Windows Kits\10\bin\*\x64\signtool.exe",
        r"C:\Program Files\Windows Kits\10\bin\*\x64\signtool.exe",
        r"C:\Program Files (x86)\Windows Kits\10\App Certification Kit\signtool.exe",
    ]
    for pattern in patterns:
        matches = sorted(_glob.glob(pattern), reverse=True)  # newest SDK version first
        if matches:
            return Path(matches[0])
    which = shutil.which("signtool")
    return Path(which) if which else None


def run_signtool(target: Path, description: str = "") -> None:
    """Sign a PE executable with SHA-256 and an RFC-3161 timestamp.

    Reads certificate path/password from build_config (SIGN_PFX, SIGN_PFX_PASSWORD,
    SIGN_TIMESTAMP_URL).  No-ops if SIGN_PFX is not set or the file is missing —
    the build continues without signing rather than failing.
    """
    pfx_path = getattr(C, "SIGN_PFX", None)
    if not pfx_path:
        print("  [Sign] SIGN_PFX not configured — skipping code signing.")
        return
    pfx = Path(pfx_path)
    if not pfx.is_file():
        print(f"  [Sign] PFX not found: {pfx} — skipping.")
        return
    signtool = _find_signtool()
    if not signtool:
        print("  [Sign] signtool.exe not found — install Windows SDK and retry.")
        return

    ts_url  = getattr(C, "SIGN_TIMESTAMP_URL", "http://timestamp.digicert.com")
    pfx_pw  = getattr(C, "SIGN_PFX_PASSWORD", "") or ""

    _banner(f"Code Signing — {target.name}")
    cmd = [
        str(signtool), "sign",
        "/fd", "SHA256",     # file digest algorithm
        "/tr", ts_url,       # RFC-3161 timestamp authority
        "/td", "sha256",     # timestamp digest algorithm
        "/f",  str(pfx),
    ]
    if pfx_pw:
        cmd += ["/p", pfx_pw]
    if description:
        cmd += ["/d", description]
    cmd += ["/v", str(target)]
    rc = _run(cmd, check=False)
    if rc == 0:
        print(f"  [Sign] OK — {target.name}")
    else:
        print(f"  [Sign] WARN — signtool exited {rc}; build continues unsigned.")


# ── Step 0a: Ensure all Python dependencies are installed ─────────────────────

def ensure_python_deps() -> None:
    """
    Read requirements.txt and pip-install any missing packages.
    This guarantees PyInstaller can find every import at bundle time.
    """
    req_file = PROJECT_ROOT / "requirements.txt"
    if not req_file.is_file():
        print("[INFO] No requirements.txt found — skipping dependency check.")
        return

    _banner("Checking / installing Python dependencies")
    _run([
        sys.executable, "-m", "pip", "install",
        "--quiet",          # suppress per-package noise
        "--upgrade",        # ensure latest compatible versions
        "-r", str(req_file),
    ])
    print("[OK] All Python dependencies are up to date.")


# ── Step 0b: Ensure Inno Setup 6 is installed ─────────────────────────────────

_INNO_INSTALLER_URL = (
    "https://github.com/jrsoftware/issrc/releases/download/is-6_7_1/innosetup-6.7.1.exe"
)
_INNO_INSTALLER_LOCAL = BUILD_DIR / "_innosetup_installer.exe"


def ensure_inno_setup() -> None:
    """
    Check whether Inno Setup 6 (ISCC.exe) is present.
    If not, download and silently install it automatically.
    """
    if _find_iscc():
        print("[OK] Inno Setup already installed.")
        return

    _banner("Inno Setup not found — downloading installer")
    print(f"  Source : {_INNO_INSTALLER_URL}")
    print(f"  Target : {_INNO_INSTALLER_LOCAL}")

    try:
        def _progress(block_num: int, block_size: int, total: int) -> None:
            if total > 0:
                pct = min(100, block_num * block_size * 100 // total)
                print(f"\r  Downloading … {pct}%", end="", flush=True)

        urllib.request.urlretrieve(
            _INNO_INSTALLER_URL, _INNO_INSTALLER_LOCAL, _progress
        )
        print()   # newline after progress

    except Exception as exc:
        print(f"\n[ERROR] Download failed: {exc}")
        print(f"  Please install Inno Setup 6 manually from:")
        print(f"  https://jrsoftware.org/isdl.php")
        sys.exit(1)

    print("  Installing Inno Setup silently …")
    result = subprocess.run(
        [str(_INNO_INSTALLER_LOCAL),
         "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART",
         "/SP-"],
        timeout=120,
    )

    # Clean up the downloaded installer regardless of outcome
    try:
        _INNO_INSTALLER_LOCAL.unlink()
    except Exception:
        pass

    if result.returncode != 0:
        print(f"[ERROR] Inno Setup installer exited with code {result.returncode}")
        sys.exit(1)

    if not _find_iscc():
        print("[ERROR] ISCC.exe still not found after installation.")
        sys.exit(1)

    print("[OK] Inno Setup installed successfully.")


# ── Step 1 (optional): PyArmor obfuscation ────────────────────────────────────

def run_pyarmor() -> Path:
    """
    Encrypt all Python source files with PyArmor.
    Returns the obfuscated source directory (used as input for PyInstaller).
    """
    try:
        import pyarmor  # noqa: F401
    except ImportError:
        print("\n[ERROR] PyArmor is not installed.")
        print("        Run:  pip install pyarmor>=8.0")
        sys.exit(1)

    obf_dir = PROJECT_ROOT / "obf_src"
    obf_dir.mkdir(exist_ok=True)

    py_files = sorted(
        str(f) for f in PROJECT_ROOT.glob("*.py")
        if f.name not in ("setup.py", "conftest.py")
    )

    _banner(f"PyArmor — encrypting {len(py_files)} source files")
    _run([
        sys.executable, "-m", "pyarmor", "gen",
        "--output", str(obf_dir),
        "--recursive",
        *py_files,
    ])

    # Mirror non-Python assets
    for name in ("templates", "static"):
        src = PROJECT_ROOT / name
        dst = obf_dir / name
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

    return obf_dir


# ── Step 2: PyInstaller bundle ─────────────────────────────────────────────────

def run_pyinstaller(src_root: Path) -> Path:
    """
    Bundle the app + every dependency into a standalone folder using PyInstaller.

    --onedir   : produces a folder (not a single .exe) — faster startup,
                 easier for Inno Setup to package, and allows the app to
                 write SQLite databases and logs alongside itself.
    --windowed : no console window (GUI / web-server app).
    --collect-all : pulls in package data files that PyInstaller misses when
                 packages use importlib.resources or pkg_resources internally.

    Returns the bundle directory for Inno Setup to package.
    """
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("\n[ERROR] PyInstaller is not installed.")
        print("        Run:  pip install pyinstaller")
        sys.exit(1)

    build_work = PROJECT_ROOT / "build_output"
    build_work.mkdir(exist_ok=True)
    dist_out = _dist_path()
    dist_out.mkdir(parents=True, exist_ok=True)

    entry = src_root / "run.py"
    sep   = ";" if sys.platform == "win32" else ":"

    # ── Icon ─────────────────────────────────────────────────────────────────
    icon_args: list[str] = []
    if C.ICON_FILE:
        ico = Path(C.ICON_FILE) if Path(C.ICON_FILE).is_absolute() else PROJECT_ROOT / C.ICON_FILE
        if ico.is_file():
            icon_args = ["--icon", str(ico)]
        else:
            print(f"[WARN] Icon file not found: {ico}")

    # ── Hidden imports ────────────────────────────────────────────────────────
    # Modules loaded dynamically at runtime that PyInstaller can't auto-detect.
    hidden_imports = [
        # Email (smtplib / imaplib helpers)
        "email.mime.text", "email.mime.multipart", "email.mime.base",
        "email.mime.application", "email.encoders",
        # SQLAlchemy dialects loaded by engine string
        "sqlalchemy.dialects.sqlite",
        "sqlalchemy.dialects.sqlite.pysqlite",
        # APScheduler components loaded by string config
        "apscheduler.schedulers.background",
        "apscheduler.triggers.cron",
        "apscheduler.triggers.interval",
        "apscheduler.triggers.date",
        "apscheduler.executors.pool",
        "apscheduler.jobstores.sqlalchemy",
        # Jinja2 extensions
        "jinja2.ext",
        # Werkzeug internals
        "werkzeug.serving",
        "werkzeug.debug",
        # Web / data libraries
        "feedparser", "bs4", "icalendar", "pypdf", "docx",
        "pytz", "openpyxl",
        # Google OAuth / API
        "google.auth.transport.requests",
        "google.auth.transport.urllib3",
        "google.oauth2.credentials",
        "google.oauth2.service_account",
        "googleapiclient.discovery",
        "googleapiclient.errors",
        "googleapiclient.http",
        # Microsoft identity
        "msal", "msal.application",
        # Windows-only modules
        "ctypes.wintypes", "winreg",
        # pkg_resources (setuptools)
        "pkg_resources",
    ]
    for h in C.HIDDEN_IMPORTS:
        if h not in hidden_imports:
            hidden_imports.append(h)

    hidden_args = []
    for h in hidden_imports:
        hidden_args += ["--hidden-import", h]

    # ── collect-all: packages with resource files PyInstaller misses ─────────
    # These packages ship data (templates, JSON schemas, certs, etc.) that must
    # be collected along with the .pyc files to work at runtime.
    collect_all_pkgs = [
        "flask",
        "jinja2",
        "werkzeug",
        "sqlalchemy",
        "apscheduler",
        "google",
        "googleapiclient",
        "google_auth_oauthlib",
        "msal",
        "bs4",
        "certifi",          # SSL root certificates
        "charset_normalizer",
    ]
    collect_args = []
    for pkg in collect_all_pkgs:
        collect_args += ["--collect-all", pkg]

    # ── Data files ────────────────────────────────────────────────────────────
    data_args: list[str] = []
    if (src_root / "templates").is_dir():
        data_args += ["--add-data", f"{src_root / 'templates'}{sep}templates"]
    if (src_root / "static").is_dir():
        data_args += ["--add-data", f"{src_root / 'static'}{sep}static"]
    # Bundle icons/ directory (contains tray icon and app icons)
    _icons_dir = PROJECT_ROOT / "icons"
    if _icons_dir.is_dir():
        data_args += ["--add-data", f"{_icons_dir}{sep}icons"]
    # Bundle build_meta.json (written by write_build_meta() before this step)
    _meta = PROJECT_ROOT / "build_meta.json"
    if _meta.is_file():
        data_args += ["--add-data", f"{_meta}{sep}."]
    # Include any extra data declared in build_config
    for src_rel, dst in C.EXTRA_DATA:
        data_args += ["--add-data", f"{PROJECT_ROOT / src_rel}{sep}{dst}"]

    # Include PyArmor runtime if obfuscation was used
    if C.USE_PYARMOR:
        for rt_dir in src_root.glob("pyarmor_runtime_*"):
            data_args += ["--add-data", f"{rt_dir}{sep}{rt_dir.name}"]

    # ── Build ─────────────────────────────────────────────────────────────────
    _banner(f"PyInstaller — bundling {C.APP_NAME} v{C.APP_VERSION}")
    _run([
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--windowed",           # no console window
        "--clean",              # wipe previous PyInstaller cache
        "--noconfirm",          # overwrite output without asking
        "--name",     C.APP_EXE_NAME,
        "--distpath", str(dist_out),
        "--workpath", str(build_work / "work"),
        "--specpath", str(build_work),
        # Embed version info in the Windows .exe metadata
        "--version-file", _write_version_file(build_work),
        *icon_args,
        *collect_args,
        *hidden_args,
        *data_args,
        str(entry),
    ])

    bundle = dist_out / C.APP_EXE_NAME
    if not bundle.is_dir():
        print(f"[ERROR] Expected bundle directory not found: {bundle}")
        sys.exit(1)

    print(f"\n[OK] Bundle: {bundle}")
    return bundle


def _write_version_file(work_dir: Path) -> str:
    """
    Write a PyInstaller version-info file and return its path as a string.
    This embeds proper Windows file metadata (visible in Properties → Details).
    """
    # Version must be four dot-separated integers: 1.0.0.0
    parts = (C.APP_VERSION + ".0.0.0").split(".")[:4]
    parts = [p if p.isdigit() else "0" for p in parts]
    v = ", ".join(parts)

    content = textwrap.dedent(f"""\
        VSVersionInfo(
          ffi=FixedFileInfo(
            filevers=({v}),
            prodvers=({v}),
            mask=0x3f,
            flags=0x0,
            OS=0x40004,
            fileType=0x1,
            subtype=0x0,
            date=(0, 0)
          ),
          kids=[
            StringFileInfo([
              StringTable('040904B0', [
                StringStruct('CompanyName',      '{C.APP_PUBLISHER}'),
                StringStruct('FileDescription',  '{C.APP_NAME}'),
                StringStruct('FileVersion',      '{C.APP_VERSION}'),
                StringStruct('InternalName',     '{C.APP_EXE_NAME}'),
                StringStruct('LegalCopyright',   '© {C.APP_PUBLISHER}'),
                StringStruct('OriginalFilename', '{C.APP_EXE_NAME}.exe'),
                StringStruct('ProductName',      '{C.APP_NAME}'),
                StringStruct('ProductVersion',   '{C.APP_VERSION}'),
              ])
            ]),
            VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])])
          ]
        )
    """)

    vf = work_dir / "version_info.txt"
    work_dir.mkdir(exist_ok=True)
    vf.write_text(content, encoding="utf-8")
    return str(vf)


# ── Step 3: Inno Setup installer ──────────────────────────────────────────────

_ISS_TEMPLATE = """\
; Inno Setup 6.7 script — auto-generated by build.py.
; Re-run  python build/build.py  to regenerate.

[Setup]
AppName={APP_NAME}
AppVersion={APP_VERSION}
AppVerName={APP_NAME} {APP_VERSION}
AppPublisher={APP_PUBLISHER}
AppPublisherURL={APP_URL}
AppSupportURL={APP_SUPPORT_URL}
AppCopyright=Copyright (C) {APP_PUBLISHER}
DefaultDirName={DEFAULT_INSTALL_DIR}
DefaultGroupName={APP_NAME}
AllowNoIcons=yes
OutputDir={OUTPUT_DIR}
OutputBaseFilename={APP_EXE_NAME}_Setup_{APP_VERSION}
{ICON_LINE}
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired={PRIVILEGES}
PrivilegesRequiredOverridesAllowed=dialog
MinVersion=10.0
DisableDirPage=no
DisableProgramGroupPage=yes
UninstallDisplayIcon={{app}}\\{APP_EXE_NAME}.exe
UninstallDisplayName={APP_NAME}
VersionInfoVersion={APP_VERSION}.0
VersionInfoCompany={APP_PUBLISHER}
VersionInfoDescription={APP_NAME} Installer
VersionInfoProductName={APP_NAME}
VersionInfoProductVersion={APP_VERSION}
; Allow upgrading an existing installation without uninstalling first
CloseApplications=yes
CloseApplicationsFilter=*{APP_EXE_NAME}.exe*
RestartApplications=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{{cm:CreateDesktopIcon}}"; \\
    GroupDescription: "{{cm:AdditionalIcons}}"; Flags: {DESKTOP_FLAG}
{STARTUP_TASK}

[Files]
; Complete application bundle — all Python deps included, no runtime required
Source: "{SOURCE_DIR}\\*"; DestDir: "{{app}}"; \\
    Flags: ignoreversion recursesubdirs createallsubdirs
{LICENSE_LINE}
{README_LINE}
{META_LINE}
{MKCERT_LINE}

[Icons]
Name: "{{autoprograms}}\\{APP_NAME}"; Filename: "{{app}}\\{APP_EXE_NAME}.exe"; \\
    WorkingDir: "{{app}}"
Name: "{{autodesktop}}\\{APP_NAME}"; Filename: "{{app}}\\{APP_EXE_NAME}.exe"; \\
    WorkingDir: "{{app}}"; Tasks: desktopicon
{STARTUP_ICON}

[Run]
Filename: "{{app}}\\{APP_EXE_NAME}.exe"; WorkingDir: "{{app}}"; \\
    Description: "{{cm:LaunchProgram,{APP_NAME}}}"; \\
    Flags: nowait postinstall skipifsilent

[UninstallRun]
Filename: "taskkill"; Parameters: "/IM {APP_EXE_NAME}.exe /F"; \\
    Flags: runhidden; StatusMsg: "Stopping {APP_NAME}..."

[Registry]
; Store installation path for future reference / other tools.
; HKA resolves to HKLM for admin installs and HKCU for user-level installs.
Root: HKA; Subkey: "Software\\{APP_PUBLISHER}\\{APP_NAME}"; \\
    ValueType: string; ValueName: "InstallPath"; ValueData: "{{app}}"; \\
    Flags: createvalueifdoesntexist uninsdeletekey

[Code]
// On upgrade: silently close the running app before installing new files.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var
  ResultCode: Integer;
begin
  Exec('taskkill', '/IM {APP_EXE_NAME}.exe /F', '', SW_HIDE, ewWaitUntilTerminated, ResultCode);
  Result := '';
end;

{MKCERT_CODE}

// On uninstall: offer to remove user data (database, logs, .env).
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Msg: String;
begin
  if CurUninstallStep = usPostUninstall then
  begin
    Msg := 'Remove the database, log files, and local settings?' + #13#10 +
           '(Choose No to keep your data for a future reinstall)';
    if MsgBox(Msg, mbConfirmation, MB_YESNO) = IDYES then
    begin
      DelTree(ExpandConstant('{{app}}\\instance'), True, True, True);
      DeleteFile(ExpandConstant('{{app}}\\job_tracker.db'));
      DeleteFile(ExpandConstant('{{app}}\\job_tracker.log'));
      DeleteFile(ExpandConstant('{{app}}\\.env'));
    end;
  end;
end;
"""


def generate_iss(bundle_dir: Path) -> Path:
    iss_dir = PROJECT_ROOT / "build_output"
    iss_dir.mkdir(exist_ok=True)
    iss_path = iss_dir / "installer.iss"

    out_dir = _output_root()
    out_dir.mkdir(parents=True, exist_ok=True)

    icon_line = ""
    if C.ICON_FILE:
        ico = Path(C.ICON_FILE) if Path(C.ICON_FILE).is_absolute() else PROJECT_ROOT / C.ICON_FILE
        if ico.is_file():
            icon_line = f"SetupIconFile={ico}"

    license_line = ""
    if C.LICENSE_FILE:
        lic = Path(C.LICENSE_FILE) if Path(C.LICENSE_FILE).is_absolute() else PROJECT_ROOT / C.LICENSE_FILE
        if lic.is_file():
            license_line = f'Source: "{lic}"; DestDir: "{{app}}"; Flags: ignoreversion'

    readme_line = ""
    if C.README_FILE:
        rdm = Path(C.README_FILE) if Path(C.README_FILE).is_absolute() else PROJECT_ROOT / C.README_FILE
        if rdm.is_file():
            readme_line = f'Source: "{rdm}"; DestDir: "{{app}}"; Flags: ignoreversion'

    # build_meta.json — bundled so the installed exe can read it from its dir
    meta_json = PROJECT_ROOT / "build_meta.json"
    meta_line = ""
    if meta_json.is_file():
        meta_line = f'Source: "{meta_json}"; DestDir: "{{app}}"; Flags: ignoreversion'

    startup_task = startup_icon = ""
    if C.ADD_TO_STARTUP:
        startup_task = (
            f'Name: "startup"; '
            f'Description: "Start {C.APP_NAME} automatically with Windows"; '
            f'GroupDescription: "Startup options"; Flags: unchecked'
        )
        startup_icon = (
            f'Name: "{{{{userstartup}}}}\\{C.APP_NAME}"; '
            f'Filename: "{{{{app}}}}\\{C.APP_EXE_NAME}.exe"; '
            f'WorkingDir: "{{{{app}}}}"; '
            f'Tasks: startup'
        )

    # ── mkcert: bundle exe + generate per-machine HTTPS cert during install ──
    # mkcert is BSD 3-Clause — legal to redistribute and run on end-user machines.
    # Each install generates its own machine-local CA + cert; nothing is shared.
    _mkcert_search = [
        # winget default install location
        Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "WinGet" / "Packages",
        Path(r"C:\Program Files"),
        Path(r"C:\Program Files (x86)"),
        Path(r"C:\tools"),
    ]
    mkcert_exe: Path | None = None
    # Check PATH first
    import shutil as _shutil
    _in_path = _shutil.which("mkcert") or _shutil.which("mkcert.exe")
    if _in_path:
        mkcert_exe = Path(_in_path)
    else:
        for _base in _mkcert_search:
            if _base.is_dir():
                _hits = list(_base.rglob("mkcert.exe"))
                if _hits:
                    mkcert_exe = _hits[0]
                    break

    if mkcert_exe and mkcert_exe.is_file():
        print(f"  [SSL] mkcert found: {mkcert_exe} — will set up HTTPS in installer")
        mkcert_line = (
            f'; mkcert — generates a trusted local HTTPS certificate during install\n'
            f'Source: "{mkcert_exe}"; DestDir: "{{{{app}}}}"; '
            f'DestName: "mkcert.exe"; Flags: ignoreversion'
        )
        mkcert_code = """\
// Generate a locally-trusted HTTPS certificate so the browser shows the
// padlock without any security warning.  mkcert -install adds a local CA
// to Windows and Chrome trust stores; mkcert then signs a cert for
// 127.0.0.1 / localhost using that CA.  Runs under the installer's admin
// context so no UAC prompt appears for the user.
procedure SetupHTTPS();
var
  AppDir, CertDir, Params: String;
  ResultCode: Integer;
begin
  AppDir  := ExpandConstant('{app}');
  CertDir := AppDir + '\\certs';
  ForceDirectories(CertDir);
  // Install the per-machine local CA (idempotent — safe to re-run on upgrade)
  Exec(AppDir + '\\mkcert.exe', '-install', AppDir,
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
  // Generate cert + key for 127.0.0.1 and localhost
  Params := '-cert-file "' + CertDir + '\\localhost.pem"'
          + ' -key-file "' + CertDir + '\\localhost-key.pem"'
          + ' 127.0.0.1 localhost';
  Exec(AppDir + '\\mkcert.exe', Params, AppDir,
       SW_HIDE, ewWaitUntilTerminated, ResultCode);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    SetupHTTPS();
end;\
"""
    else:
        print("  [SSL] mkcert not found — installer will skip HTTPS cert setup")
        mkcert_line = ""
        mkcert_code = ""

    iss_content = _ISS_TEMPLATE.format(
        APP_NAME            = C.APP_NAME,
        APP_VERSION         = C.APP_VERSION,
        APP_PUBLISHER       = C.APP_PUBLISHER,
        APP_URL             = C.APP_URL,
        APP_SUPPORT_URL     = C.APP_SUPPORT_URL,
        APP_EXE_NAME        = C.APP_EXE_NAME,
        DEFAULT_INSTALL_DIR = C.DEFAULT_INSTALL_DIR,
        OUTPUT_DIR          = str(out_dir),
        ICON_LINE           = icon_line,
        LICENSE_LINE        = license_line,
        README_LINE         = readme_line,
        META_LINE           = meta_line,
        MKCERT_LINE         = mkcert_line,
        MKCERT_CODE         = mkcert_code,
        PRIVILEGES          = "admin" if C.REQUIRE_ADMIN else "lowest",
        DESKTOP_FLAG        = "" if C.DESKTOP_ICON else "unchecked",
        STARTUP_TASK        = startup_task,
        STARTUP_ICON        = startup_icon,
        SOURCE_DIR          = str(bundle_dir),
    )

    iss_path.write_text(iss_content, encoding="utf-8")
    print(f"\n  Inno Setup script written: {iss_path}")
    return iss_path


def run_inno_setup(iss_path: Path) -> None:
    iscc = _find_iscc()
    if not iscc:
        print("\n[ERROR] ISCC.exe not found — Inno Setup installation may have failed.")
        print(f"  Run manually:  ISCC.exe \"{iss_path}\"")
        return

    _banner("Inno Setup — compiling installer")
    _run([str(iscc), str(iss_path)])

    installer = PROJECT_ROOT / C.OUTPUT_DIR / f"{C.APP_EXE_NAME}_Setup_{C.APP_VERSION}.exe"
    if installer.is_file():
        size_mb = installer.stat().st_size / 1_048_576
        print(f"\n  ✓ Installer ready: {installer}  ({size_mb:.1f} MB)")
        print(f"    Ready for distribution / commercial sale.")
    else:
        print(f"\n[WARN] Expected installer not found: {installer}")


# ── Nuitka path (kept for future use when Python 3.13 support matures) ────────

def _apply_msvc_env(vs_install_path: str) -> None:
    vcvarsall = Path(vs_install_path) / "VC" / "Auxiliary" / "Build" / "vcvarsall.bat"
    if not vcvarsall.is_file():
        return
    try:
        result = subprocess.run(
            f'call "{vcvarsall}" amd64 > nul 2>&1 && set',
            shell=True, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=60,
        )
        for line in result.stdout.splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                os.environ[k] = v
    except Exception:
        pass


def _detect_nuitka_compiler() -> list[str]:
    _vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    if _vswhere.is_file():
        try:
            def _prop(p: str) -> str:
                r = subprocess.run(
                    [str(_vswhere), "-latest", "-products", "*",
                     "-requires", "Microsoft.VisualStudio.Component.VC.Tools.x86.x64",
                     "-property", p],
                    capture_output=True, text=True, timeout=10,
                )
                return r.stdout.strip() if r.returncode == 0 else ""
            vs_path, vs_ver = _prop("installationPath"), _prop("installationVersion")
            if vs_path and vs_ver:
                # Clear stale SCons MSVC cache so it re-detects the correct path
                _cache = Path(os.environ.get("LOCALAPPDATA","")) / "Nuitka/Nuitka/Cache/scons-msvc-config"
                if _cache.is_file():
                    _cache.unlink()
                _apply_msvc_env(vs_path)
                # Detect the actual installed MSVC toolset version from disk
                # (e.g. VC/Tools/MSVC/14.50.35717 → "14.5") rather than
                # guessing from the VS major version number.
                msvc_tools_dir = Path(vs_path) / "VC" / "Tools" / "MSVC"
                msvc_ver = None
                if msvc_tools_dir.is_dir():
                    toolsets = sorted(msvc_tools_dir.iterdir(), reverse=True)
                    for ts in toolsets:
                        parts = ts.name.split(".")
                        # MSVC toolset dirs: "14.50.35717" → Nuitka --msvc=14.5
                        # The minor part "50" means generation 5.x; take first digit only.
                        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
                            msvc_ver = f"{parts[0]}.{parts[1][0]}"
                            break
                if not msvc_ver:
                    # Fallback: derive from VS major version
                    major = int(vs_ver.split(".")[0])
                    msvc_ver = {18: "14.5", 17: "14.3", 16: "14.2", 15: "14.1"}.get(major, "14.3")
                print(f"  [Nuitka] Using MSVC {msvc_ver} from {vs_path}")
                return [f"--msvc={msvc_ver}"]
        except Exception:
            pass
    if (zig := shutil.which("zig")):
        if "site-packages" not in zig.lower() and "nuitka" not in zig.lower():
            return ["--zig"]
    if shutil.which("clang"):
        return ["--clang"]
    print("\n[ERROR] No C compiler found for Nuitka. Switch Protection to PyInstaller.")
    sys.exit(1)


def run_nuitka() -> Path:
    """
    Compile the app to native machine code using Nuitka.
    Requires MSVC (Visual Studio Community or Professional with C++ workload).
    Produces a standalone folder — no Python runtime needed on target machines.
    """
    import importlib.util as _ilu
    nuitka_out = PROJECT_ROOT / "build_output" / "nuitka"
    nuitka_out.mkdir(parents=True, exist_ok=True)
    icon_args = ([f"--windows-icon-from-ico={Path(C.ICON_FILE)}"]
                 if C.ICON_FILE and Path(C.ICON_FILE).is_file() else [])
    # All packages are gated by find_spec so builds against projects that
    # don't install the full job_tracker dependency set (e.g. build_dashboard)
    # don't fail with "module not found" from Nuitka.
    all_pkgs = [
        "flask","jinja2","werkzeug","sqlalchemy","apscheduler",
        "bs4","feedparser","icalendar","pytz","requests","dotenv",
        "docx","PIL","google","googleapiclient","msal","pypdf","openpyxl",
    ]
    pkgs = [f"--include-package={p}" for p in all_pkgs if _ilu.find_spec(p)]
    # Note: do NOT manually include vcruntime140.dll / msvcp140.dll —
    # Nuitka with MSVC bundles the VC++ runtime automatically and will
    # raise a FATAL conflict error if we add them again.
    _banner("Nuitka — compiling to native code")
    _run([sys.executable, "-m", "nuitka",
          "--standalone", "--assume-yes-for-downloads",
          "--windows-console-mode=disable",
          "--noinclude-unittest-mode=nofollow",
          f"--windows-company-name={C.APP_PUBLISHER}",
          f"--windows-product-name={C.APP_NAME}",
          f"--windows-file-version={C.APP_VERSION}.0",
          f"--output-dir={nuitka_out}",
          f"--output-filename={C.APP_EXE_NAME}.exe",
          *([ f"--include-data-dir={PROJECT_ROOT/'templates'}=templates" ]
             if (PROJECT_ROOT / "templates").is_dir() else []),
          *([ f"--include-data-dir={PROJECT_ROOT/'static'}=static" ]
             if (PROJECT_ROOT / "static").is_dir() else []),
          *([ f"--include-data-dir={PROJECT_ROOT/'icons'}=icons" ]
             if (PROJECT_ROOT / "icons").is_dir() else []),
          *icon_args, *_detect_nuitka_compiler(), *pkgs,
          str(PROJECT_ROOT / "run.py")])
    bundle = nuitka_out / "run.dist"
    if not bundle.is_dir():
        print(f"[ERROR] Nuitka output not found: {bundle}")
        sys.exit(1)

    # Copy output to Bundle Only/{APP_EXE_NAME} so test pipeline finds it
    # in the same location as PyInstaller output.
    target = _dist_path() / C.APP_EXE_NAME
    _dist_path().mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    shutil.copytree(bundle, target, dirs_exist_ok=True)
    print(f"\n[OK] Bundle copied to: {target}")
    return target


# ── Utilities ─────────────────────────────────────────────────────────────────

def pre_build_clean() -> None:
    """Remove previous compile outputs before a fresh build.

    Deletes only artefacts that would cause conflicts (intermediate Nuitka/
    PyInstaller work dirs, the previous Bundle Only/<ExeName>/ folder, and the
    obfuscated source tree).  The Log/ directory and any existing installer
    .exe files are left untouched.
    """
    _banner("Pre-build clean — removing previous compile outputs")
    # Intermediate build dirs inside the project root
    for name in ("build_output", "obf_src"):
        p = PROJECT_ROOT / name
        if p.exists():
            print(f"  Removing old build dir : {p}")
            shutil.rmtree(p, ignore_errors=True)
        else:
            print(f"  Already gone           : {p}")
    # Previous Bundle Only/<ExeName>/ directory in the output drive
    bundle_target = _dist_path() / C.APP_EXE_NAME
    if bundle_target.exists():
        print(f"  Removing old bundle    : {bundle_target}")
        shutil.rmtree(bundle_target, ignore_errors=True)
        if bundle_target.exists():
            print(f"  [WARN] Could not fully remove {bundle_target} — files may be locked")
    else:
        print(f"  Already gone           : {bundle_target}")
    print("\n  Pre-build clean complete — ready for fresh compile.")


def clean_build() -> None:
    for name in ("build_output", "obf_src"):
        p = PROJECT_ROOT / name
        if p.exists():
            print(f"  Removing {p} …")
            shutil.rmtree(p, ignore_errors=True)
    out = _output_root()
    if out.exists():
        print(f"  Removing {out} …")
        shutil.rmtree(out, ignore_errors=True)


def _post_build_cleanup() -> None:
    """
    Remove intermediate build artefacts that are no longer needed after a
    successful build.  Final outputs (Bundle Only/, Log/, installer .exe) and
    the Nuitka incremental-rebuild cache (run.build/) are kept; everything
    else is deleted to reclaim disk space.
    """
    _banner("Post-build cleanup — removing intermediates")
    removed: list[str] = []
    kept:    list[str] = []

    def _rm(p: Path) -> None:
        if not p.exists():
            return
        try:
            if p.is_dir():
                shutil.rmtree(p)
            else:
                p.unlink()
            removed.append(str(p))
        except Exception as e:
            print(f"  [WARN] Could not remove {p}: {e}")

    build_out = PROJECT_ROOT / "build_output"

    # PyInstaller work directory (analysis cache, hook intermediates, .pyz files)
    _rm(build_out / "work")

    # Auto-generated version-info file written before PyInstaller runs
    _rm(build_out / "version_info.txt")

    # Auto-generated PyInstaller .spec file
    for spec in build_out.glob("*.spec"):
        _rm(spec)

    # Auto-generated Inno Setup script (installer already built from it)
    _rm(build_out / "installer.iss")

    # PyArmor obfuscated source tree (already bundled into the exe)
    _rm(PROJECT_ROOT / "obf_src")

    # Nuitka: the raw run.dist/ has already been copied to Bundle Only/ — remove it.
    # run.build/ is the incremental compilation cache — keep it.
    nuitka_dist = build_out / "nuitka" / "run.dist"
    nuitka_build = build_out / "nuitka" / "run.build"
    if nuitka_dist.exists():
        _rm(nuitka_dist)
    if nuitka_build.exists():
        kept.append(str(nuitka_build))

    # If build_output/ is now empty (PyInstaller mode with no cache left), remove it
    if build_out.exists():
        remaining = list(build_out.iterdir())
        if not remaining:
            _rm(build_out)
        elif remaining == [build_out / "nuitka"] or all(
            p.name == "nuitka" for p in remaining
        ):
            # Only the nuitka cache dir remains — that's intentional
            pass

    if removed:
        for r in removed:
            print(f"  Removed: {r}")
    else:
        print("  Nothing to remove.")

    if kept:
        for k in kept:
            print(f"  Kept   : {k}  (Nuitka incremental cache)")

    print("  Intermediates cleaned.")


# ── Entry point ────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build a portable Windows installer for distribution",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Protection modes (set via the Build Dashboard):
              PyInstaller          — standard; bundles .pyc bytecode
              PyInstaller+PyArmor — AES-256 encrypted bytecode
              Nuitka               — compiled to native machine code (needs C compiler)
        """),
    )
    parser.add_argument("--clean",          action="store_true",
                        help="Remove all previous build output before building")
    parser.add_argument("--bundle-only",    action="store_true",
                        help="Bundle with PyInstaller only — skip Inno Setup")
    parser.add_argument("--installer-only", action="store_true",
                        help="Skip bundling; repackage an existing build_output/dist/")
    parser.add_argument("--project-dir",    default="",
                        help="Root directory of the project to build "
                             "(overrides default of build.py's parent directory)")
    args = parser.parse_args()

    # Allow Build Dashboard to compile any project by passing --project-dir
    if args.project_dir:
        global PROJECT_ROOT
        PROJECT_ROOT = Path(args.project_dir).resolve()

    protection = (
        "Nuitka (native machine code)"              if C.USE_NUITKA  else
        "PyArmor + PyInstaller (encrypted bytecode)" if C.USE_PYARMOR else
        "PyInstaller (portable standalone bundle)"
    )

    print(f"\n{'═' * 62}")
    print(f"  {C.APP_NAME}  v{C.APP_VERSION}  —  build.py")
    print(f"  Protection : {protection}")
    print(f"  Output     : {_output_root()}")
    print(f"{'═' * 62}")

    if args.clean:
        _banner("Clean — removing previous outputs")
        clean_build()

    # Step 0: ensure prerequisites exist before doing any real work
    ensure_python_deps()
    if not args.bundle_only:
        ensure_inno_setup()

    if not args.installer_only:
        # Step 0b: write build_meta.json so the bundled app picks up runtime settings
        write_build_meta()

        # Step 0c: always wipe previous compile outputs for a clean slate
        pre_build_clean()

        # Step 1 (optional): PyArmor obfuscation
        src_root = run_pyarmor() if (C.USE_PYARMOR and not C.USE_NUITKA) else PROJECT_ROOT

        # Step 2: bundle
        bundle_dir = run_nuitka() if C.USE_NUITKA else run_pyinstaller(src_root)

        # Step 2b: sign the compiled app exe
        _app_exe = bundle_dir / f"{C.APP_EXE_NAME}.exe"
        if _app_exe.is_file():
            run_signtool(_app_exe, C.APP_NAME)

    else:
        bundle_dir = _dist_path() / C.APP_EXE_NAME
        if not bundle_dir.is_dir():
            print(f"\n[ERROR] No existing bundle at: {bundle_dir}")
            print("  Run without --installer-only to build first.")
            sys.exit(1)

    # Step 3: Inno Setup installer
    if not args.bundle_only:
        _banner("Inno Setup — generating installer script")
        iss_path = generate_iss(bundle_dir)
        run_inno_setup(iss_path)

        # Step 3b: sign the installer
        _installer = _output_root() / f"{C.APP_EXE_NAME}_Setup_{C.APP_VERSION}.exe"
        if _installer.is_file():
            run_signtool(_installer, f"{C.APP_NAME} Installer")

    _post_build_cleanup()

    print(f"\n{'═' * 62}")
    print(f"  Build complete.")
    print(f"{'═' * 62}\n")


if __name__ == "__main__":
    main()
