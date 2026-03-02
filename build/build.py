"""
build.py — Automated build + installer pipeline for Job Tracker.

Produces a Windows installer (.exe) from the Python source using one of
three protection levels configured in build_config.py.

Usage
-----
  python build/build.py                     # full build (bundle + installer)
  python build/build.py --bundle-only       # bundle only, skip Inno Setup
  python build/build.py --installer-only    # repackage existing bundle
  python build/build.py --clean             # wipe previous outputs first
  python build/build.py --help

Build tool prerequisites (install into your DEV environment, not the app):
  pip install pyinstaller
  pip install pyarmor          # only needed when USE_PYARMOR = True
  pip install nuitka           # only needed when USE_NUITKA = True
  Inno Setup 6 from https://jrsoftware.org/isdl.php
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

# ── Locate project root and import build config ───────────────────────────────

BUILD_DIR    = Path(__file__).parent.resolve()
PROJECT_ROOT = BUILD_DIR.parent
sys.path.insert(0, str(BUILD_DIR))
import build_config as C   # noqa: E402  (intentional late import)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _banner(text: str) -> None:
    width = 62
    print(f"\n{'─' * width}")
    print(f"  {text}")
    print(f"{'─' * width}")


def _run(cmd: list, cwd: Path | None = None, check: bool = True) -> int:
    """Stream a subprocess to stdout; exit on failure unless check=False."""
    printable = " ".join(str(c) for c in cmd)
    print(f"\n>>> {printable}\n")
    result = subprocess.run(cmd, cwd=str(cwd or PROJECT_ROOT))
    if check and result.returncode != 0:
        print(f"\n[ERROR] Command exited with code {result.returncode}")
        sys.exit(result.returncode)
    return result.returncode


def _require_tool(name: str, import_name: str | None = None,
                  pip_package: str | None = None) -> None:
    """Abort with a helpful message if a Python package is missing."""
    try:
        __import__(import_name or name)
    except ImportError:
        pkg = pip_package or name
        print(f"\n[ERROR] '{name}' is not installed in this Python environment.")
        print(f"        Run:  pip install {pkg}")
        sys.exit(1)


def _find_iscc() -> Path | None:
    """Locate Inno Setup's command-line compiler (ISCC.exe)."""
    candidates = [
        r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
        r"C:\Program Files\Inno Setup 6\ISCC.exe",
        r"C:\Program Files (x86)\Inno Setup 5\ISCC.exe",
        r"C:\Program Files\Inno Setup 5\ISCC.exe",
    ]
    for p in candidates:
        if os.path.isfile(p):
            return Path(p)
    which = shutil.which("ISCC") or shutil.which("iscc")
    return Path(which) if which else None


def clean_build() -> None:
    for name in ("build_output", "obf_src", C.OUTPUT_DIR):
        p = PROJECT_ROOT / name
        if p.exists():
            print(f"  Removing {p} …")
            shutil.rmtree(p, ignore_errors=True)

# ── Step 1: PyArmor obfuscation (optional) ───────────────────────────────────

def run_pyarmor() -> Path:
    """
    Encrypt all Python source files with PyArmor and return the output dir.
    The obfuscated directory is used as the source root for PyInstaller.
    """
    _require_tool("pyarmor")
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

    # Mirror non-Python assets so PyInstaller can find them
    for name in ("templates", "static"):
        src = PROJECT_ROOT / name
        dst = obf_dir / name
        if src.exists():
            if dst.exists():
                shutil.rmtree(dst)
            shutil.copytree(src, dst)

    return obf_dir

# ── Step 2a: PyInstaller bundle ───────────────────────────────────────────────

def run_pyinstaller(src_root: Path) -> Path:
    """
    Bundle with PyInstaller (--onedir, no console window).
    Returns the directory that Inno Setup should package.
    """
    _require_tool("PyInstaller", import_name="PyInstaller")

    build_work = PROJECT_ROOT / "build_output"
    build_work.mkdir(exist_ok=True)
    dist_out   = build_work / "dist"

    entry = src_root / "run.py"

    icon_args: list[str] = []
    if C.ICON_FILE:
        ico = PROJECT_ROOT / C.ICON_FILE
        if ico.is_file():
            icon_args = ["--icon", str(ico)]
        else:
            print(f"[WARNING] ICON_FILE not found: {ico}")

    hidden = []
    for imp in C.HIDDEN_IMPORTS:
        hidden += ["--hidden-import", imp]

    # Data files — templates and static first, then EXTRA_DATA
    sep = ";" if sys.platform == "win32" else ":"
    data_args: list[str] = [
        "--add-data", f"{src_root / 'templates'}{sep}templates",
        "--add-data", f"{src_root / 'static'}{sep}static",
    ]
    for src_rel, dst in C.EXTRA_DATA:
        data_args += ["--add-data", f"{PROJECT_ROOT / src_rel}{sep}{dst}"]

    # Include the PyArmor runtime package if obfuscation was used
    if C.USE_PYARMOR:
        for rt_dir in src_root.glob("pyarmor_runtime_*"):
            data_args += ["--add-data", f"{rt_dir}{sep}{rt_dir.name}"]

    _banner(f"PyInstaller — bundling {entry.name}")
    _run([
        sys.executable, "-m", "PyInstaller",
        "--onedir",
        "--windowed",                          # no console window
        "--name",     C.APP_EXE_NAME,
        "--distpath", str(dist_out),
        "--workpath", str(build_work / "work"),
        "--specpath", str(build_work),
        "--noconfirm",
        *icon_args,
        *hidden,
        *data_args,
        str(entry),
    ])

    bundle = dist_out / C.APP_EXE_NAME
    if not bundle.is_dir():
        print(f"[ERROR] Expected bundle directory not found: {bundle}")
        sys.exit(1)
    return bundle

# ── Step 2b: Nuitka compile ───────────────────────────────────────────────────

def run_nuitka() -> Path:
    """
    Compile with Nuitka (Python → C → native machine code).
    Returns the standalone distribution directory.
    """
    _require_tool("nuitka")

    nuitka_out = PROJECT_ROOT / "build_output" / "nuitka"
    nuitka_out.mkdir(parents=True, exist_ok=True)

    icon_args: list[str] = []
    if C.ICON_FILE:
        ico = PROJECT_ROOT / C.ICON_FILE
        if ico.is_file():
            icon_args = [f"--windows-icon-from-ico={ico}"]

    include_pkgs = [
        "--include-package=flask",
        "--include-package=jinja2",
        "--include-package=werkzeug",
        "--include-package=sqlalchemy",
        "--include-package=apscheduler",
        "--include-package=bs4",
        "--include-package=feedparser",
        "--include-package=icalendar",
        "--include-package=pytz",
        "--include-package=requests",
        "--include-package=dotenv",
        "--include-package=google",
        "--include-package=googleapiclient",
        "--include-package=msal",
        "--include-package=pypdf",
        "--include-package=docx",
        "--include-package=PIL",
    ]

    _banner("Nuitka — compiling to native code (this may take several minutes)")
    _run([
        sys.executable, "-m", "nuitka",
        "--standalone",
        "--assume-yes-for-downloads",
        "--windows-disable-console",
        f"--windows-company-name={C.APP_PUBLISHER}",
        f"--windows-product-name={C.APP_NAME}",
        f"--windows-file-version={C.APP_VERSION}.0",
        f"--output-dir={nuitka_out}",
        f"--output-filename={C.APP_EXE_NAME}.exe",
        f"--include-data-dir={PROJECT_ROOT / 'templates'}=templates",
        f"--include-data-dir={PROJECT_ROOT / 'static'}=static",
        *icon_args,
        *include_pkgs,
        str(PROJECT_ROOT / "run.py"),
    ])

    # Nuitka names the output directory after the entry script: run.dist
    bundle = nuitka_out / "run.dist"
    if not bundle.is_dir():
        print(f"[ERROR] Expected Nuitka output not found: {bundle}")
        sys.exit(1)
    return bundle

# ── Step 3: Generate Inno Setup script and run the compiler ──────────────────

_ISS_TEMPLATE = """\
; Inno Setup 6 script — generated by build.py, do not edit.
; Re-run  python build/build.py  to regenerate.

[Setup]
AppName={APP_NAME}
AppVersion={APP_VERSION}
AppPublisher={APP_PUBLISHER}
AppPublisherURL={APP_URL}
AppSupportURL={APP_SUPPORT_URL}
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
DisableDirPage=no
DisableProgramGroupPage=yes
UninstallDisplayIcon={{app}}\\{APP_EXE_NAME}.exe
VersionInfoVersion={APP_VERSION}.0
VersionInfoCompany={APP_PUBLISHER}
VersionInfoDescription={APP_DESCRIPTION}

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{{cm:CreateDesktopIcon}}"; \\
    GroupDescription: "{{cm:AdditionalIcons}}"; Flags: {DESKTOP_FLAG}
{STARTUP_TASK}

[Files]
; Full application bundle from PyInstaller / Nuitka output
Source: "{SOURCE_DIR}\\*"; DestDir: "{{app}}"; \\
    Flags: ignoreversion recursesubdirs createallsubdirs
{LICENSE_LINE}

[Icons]
Name: "{{autoprograms}}\\{APP_NAME}"; Filename: "{{app}}\\{APP_EXE_NAME}.exe"
Name: "{{autodesktop}}\\{APP_NAME}"; Filename: "{{app}}\\{APP_EXE_NAME}.exe"; \\
    Tasks: desktopicon
{STARTUP_ICON}

[Run]
; Offer to launch the app immediately after installation
Filename: "{{app}}\\{APP_EXE_NAME}.exe"; \\
    Description: "{{cm:LaunchProgram,{APP_NAME}}}"; \\
    Flags: nowait postinstall skipifsilent

[UninstallRun]
; Stop the running app before uninstalling
Filename: "taskkill"; Parameters: "/IM {APP_EXE_NAME}.exe /F"; \\
    Flags: runhidden; StatusMsg: "Stopping {APP_NAME}..."

[Code]
// Ask the user whether to keep or remove user-created data on uninstall.
procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usPostUninstall then
  begin
    if MsgBox('Remove the database, log file, and settings (.env)?', \\
              mbConfirmation, MB_YESNO) = IDYES then
    begin
      DeleteFile(ExpandConstant('{{app}}\\job_tracker.db'));
      DeleteFile(ExpandConstant('{{app}}\\job_tracker.log'));
      DeleteFile(ExpandConstant('{{app}}\\.env'));
    end;
  end;
end;
"""


def generate_iss(bundle_dir: Path) -> Path:
    """Fill in _ISS_TEMPLATE with values from build_config and write the file."""
    iss_dir = PROJECT_ROOT / "build_output"
    iss_dir.mkdir(exist_ok=True)
    iss_path = iss_dir / "installer.iss"

    out_dir = (PROJECT_ROOT / C.OUTPUT_DIR).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    # Optional icon
    icon_line = ""
    if C.ICON_FILE:
        ico = PROJECT_ROOT / C.ICON_FILE
        if ico.is_file():
            icon_line = f"SetupIconFile={ico}"

    # Optional license
    license_line = ""
    if C.LICENSE_FILE:
        lic = PROJECT_ROOT / C.LICENSE_FILE
        if lic.is_file():
            license_line = f'Source: "{lic}"; DestDir: "{{app}}"; Flags: ignoreversion'

    # Optional startup registry entry
    startup_task = startup_icon = ""
    if C.ADD_TO_STARTUP:
        startup_task = textwrap.dedent("""\
            Name: "startup"; Description: "Start {APP_NAME} automatically with Windows"; \\
                GroupDescription: "Startup options"; Flags: unchecked""").format(
            APP_NAME=C.APP_NAME
        )
        startup_icon = textwrap.dedent("""\
            Name: "{{userstartup}}\\{APP_NAME}"; \\
                Filename: "{{app}}\\{APP_EXE_NAME}.exe"; \\
                Tasks: startup""").format(
            APP_NAME=C.APP_NAME,
            APP_EXE_NAME=C.APP_EXE_NAME,
        )

    iss_content = _ISS_TEMPLATE.format(
        APP_NAME          = C.APP_NAME,
        APP_VERSION       = C.APP_VERSION,
        APP_PUBLISHER     = C.APP_PUBLISHER,
        APP_URL           = C.APP_URL,
        APP_SUPPORT_URL   = C.APP_SUPPORT_URL,
        APP_DESCRIPTION   = C.APP_DESCRIPTION,
        APP_EXE_NAME      = C.APP_EXE_NAME,
        DEFAULT_INSTALL_DIR = C.DEFAULT_INSTALL_DIR,
        OUTPUT_DIR        = str(out_dir),
        ICON_LINE         = icon_line,
        LICENSE_LINE      = license_line,
        PRIVILEGES        = "admin" if C.REQUIRE_ADMIN else "lowest",
        DESKTOP_FLAG      = "" if C.DESKTOP_ICON else "unchecked",
        STARTUP_TASK      = startup_task,
        STARTUP_ICON      = startup_icon,
        SOURCE_DIR        = str(bundle_dir),
    )

    iss_path.write_text(iss_content, encoding="utf-8")
    print(f"\n  Inno Setup script: {iss_path}")
    return iss_path


def run_inno_setup(iss_path: Path) -> None:
    iscc = _find_iscc()
    if not iscc:
        print("\n[WARNING] Inno Setup compiler (ISCC.exe) not found.")
        print("  Install Inno Setup 6 from https://jrsoftware.org/isdl.php")
        print(f"  Then run manually:\n    ISCC.exe \"{iss_path}\"")
        return

    _banner("Inno Setup — building installer")
    _run([str(iscc), str(iss_path)])

    installer = (
        PROJECT_ROOT / C.OUTPUT_DIR /
        f"{C.APP_EXE_NAME}_Setup_{C.APP_VERSION}.exe"
    )
    if installer.is_file():
        size_mb = installer.stat().st_size / 1_048_576
        print(f"\n  ✓ Installer ready: {installer}  ({size_mb:.1f} MB)")
    else:
        print(f"\n[WARNING] Expected installer not found at {installer}")

# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Build the Job Tracker Windows installer",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            Protection modes (set in build_config.py):
              Plain PyInstaller    — fastest; bytecode can be decompiled
              PyInstaller+PyArmor — bytecode AES-encrypted; harder to reverse
              Nuitka               — compiled to native code; strongest protection
        """),
    )
    parser.add_argument("--clean",          action="store_true",
                        help="Remove previous build output before building")
    parser.add_argument("--bundle-only",    action="store_true",
                        help="Create the bundle but skip Inno Setup packaging")
    parser.add_argument("--installer-only", action="store_true",
                        help="Skip bundling; repackage an existing build_output/dist/")
    args = parser.parse_args()

    protection = (
        "Nuitka (native machine code)"     if C.USE_NUITKA  else
        "PyArmor + PyInstaller (encrypted bytecode)" if C.USE_PYARMOR else
        "PyInstaller (standard bytecode)"
    )

    print(f"\n{'═' * 62}")
    print(f"  {C.APP_NAME}  v{C.APP_VERSION}  —  build.py")
    print(f"  Protection : {protection}")
    print(f"  Output     : {PROJECT_ROOT / C.OUTPUT_DIR}")
    print(f"{'═' * 62}")

    if args.clean:
        _banner("Clean — removing previous outputs")
        clean_build()

    if not args.installer_only:
        # Step 1 (optional): PyArmor obfuscation
        if C.USE_PYARMOR and not C.USE_NUITKA:
            src_root = run_pyarmor()
        else:
            src_root = PROJECT_ROOT

        # Step 2: bundle
        if C.USE_NUITKA:
            bundle_dir = run_nuitka()
        else:
            bundle_dir = run_pyinstaller(src_root)

    else:
        # Reuse an existing bundle
        bundle_dir = PROJECT_ROOT / "build_output" / "dist" / C.APP_EXE_NAME
        if not bundle_dir.is_dir():
            print(f"\n[ERROR] No existing bundle found at {bundle_dir}")
            print("  Run without --installer-only to build first.")
            sys.exit(1)

    # Step 3 (optional): Inno Setup
    if not args.bundle_only:
        _banner("Inno Setup — generating installer script")
        iss_path = generate_iss(bundle_dir)
        run_inno_setup(iss_path)

    print(f"\n{'═' * 62}")
    print("  Build complete.")
    print(f"{'═' * 62}\n")


if __name__ == "__main__":
    main()
