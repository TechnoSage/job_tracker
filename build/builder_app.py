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
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

from flask import Flask, jsonify, render_template, request, send_from_directory

# ── Log output sanitisation ────────────────────────────────────────────────────
# Many build tools (pip, Nuitka, PyArmor, ISCC) emit ANSI colour/cursor-control
# sequences.  These render as garbage characters (e.g. ←[31m) in the browser log
# panel.  Strip them before every _append() call.
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;]*[mGKHFJSTA-Za-z]'   # CSI / SGR sequences  (e.g. \x1b[31m)
    r'|\x1b[()=>]'                       # charset / mode shifts (e.g. \x1b(B)
    r'|\x08'                             # backspace control char
    r'|\r'                               # bare carriage return (Windows CR)
)


def _sanitize(s: str) -> str:
    """Strip ANSI escape sequences and stray control characters from a log line."""
    s = _ANSI_RE.sub('', s)
    # Replace any remaining non-printable chars (except tab) with a safe repr
    return ''.join(c if (c >= ' ' or c == '\t') else f'\\x{ord(c):02x}' for c in s)


# ── IFileOpenDialog C# — modern Windows Explorer folder picker ────────────────
# Used by the /api/gh/browse-folder route.  Compiled once per PowerShell call.

_FOLDER_PICKER_CS = """
using System;
using System.Runtime.InteropServices;

public class FolderPicker {
    [ComImport, Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IFileOpenDialog {
        [PreserveSig] int Show(IntPtr hwnd);
        void SetFileTypes(uint c, IntPtr t);
        void SetFileTypeIndex(uint i);
        void GetFileTypeIndex(out uint i);
        void Advise(IntPtr sink, out uint cookie);
        void Unadvise(uint cookie);
        void SetOptions(uint fos);
        void GetOptions(out uint fos);
        void SetDefaultFolder(IntPtr si);
        void SetFolder(IntPtr si);
        void GetFolder(out IntPtr si);
        void GetCurrentSelection(out IntPtr si);
        void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string n);
        void GetFileName([MarshalAs(UnmanagedType.LPWStr)] out string n);
        void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string title);
        void SetOkButtonLabel([MarshalAs(UnmanagedType.LPWStr)] string text);
        void SetFileNameLabel([MarshalAs(UnmanagedType.LPWStr)] string lbl);
        void GetResult(out IntPtr si);
        void AddPlace(IntPtr si, int fdap);
        void SetDefaultExtension([MarshalAs(UnmanagedType.LPWStr)] string ext);
        void Close(int hr);
        void SetClientGuid(ref Guid guid);
        void ClearClientData();
        void SetFilter(IntPtr filter);
        void GetResults(out IntPtr items);
        void GetSelectedItems(out IntPtr items);
    }

    [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),
     InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    interface IShellItem {
        void BindToHandler(IntPtr pbc, ref Guid bhid, ref Guid riid, out IntPtr ppv);
        void GetParent(out IShellItem ppsi);
        void GetDisplayName(uint sigdn, [MarshalAs(UnmanagedType.LPWStr)] out string name);
        void GetAttributes(uint mask, out uint attribs);
        void Compare(IShellItem psi, uint hint, out int order);
    }

    [DllImport("shell32.dll", CharSet=CharSet.Unicode)]
    static extern int SHCreateItemFromParsingName(
        string path, IntPtr pbc, ref Guid riid,
        [MarshalAs(UnmanagedType.Interface)] out IShellItem ppv);

    [DllImport("user32.dll")]
    static extern IntPtr GetForegroundWindow();

    static Guid CLSID = new Guid("DC1C5A9C-E88A-4dde-A5A1-60F82A20AEF7");
    static Guid IID_SI = new Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE");

    public static string Pick(string initial) {
        var dlg = (IFileOpenDialog)Activator.CreateInstance(Type.GetTypeFromCLSID(CLSID));
        try {
            uint opts; dlg.GetOptions(out opts);
            // FOS_PICKFOLDERS (0x20) | FOS_FORCEFILESYSTEM (0x40)
            dlg.SetOptions(opts | 0x20 | 0x40);
            dlg.SetTitle("Select Folder");
            if (!string.IsNullOrEmpty(initial)) {
                IShellItem si;
                if (SHCreateItemFromParsingName(initial, IntPtr.Zero, ref IID_SI, out si) == 0)
                    dlg.SetFolder(Marshal.GetIUnknownForObject(si));
            }
            // Pass the current foreground window as owner so the dialog
            // appears in front of the browser rather than behind it.
            if (dlg.Show(GetForegroundWindow()) != 0) return "";
            IntPtr ptr; dlg.GetResult(out ptr);
            var item = (IShellItem)Marshal.GetObjectForIUnknown(ptr);
            string path; item.GetDisplayName(0x80058000, out path);  // SIGDN_FILESYSPATH
            return path ?? "";
        } finally { Marshal.ReleaseComObject(dlg); }
    }
}
"""

# ── Paths ─────────────────────────────────────────────────────────────────────

BUILD_DIR     = Path(__file__).parent.resolve()
PROJECT_ROOT  = BUILD_DIR.parent
SETTINGS_FILE  = BUILD_DIR / "build_settings.json"

# mkcert CA root — installed by: mkcert -install
# Used so Python's urllib can verify the local HTTPS cert without disabling SSL.
_MKCERT_CA = Path(os.environ.get("LOCALAPPDATA", r"C:\Users\User\AppData\Local")) / "mkcert" / "rootCA.pem"
_LOCAL_CERT = PROJECT_ROOT / "certs" / "localhost.pem"
_LOCAL_KEY  = PROJECT_ROOT / "certs" / "localhost-key.pem"
PROJECTS_FILE  = BUILD_DIR / "build_projects.json"
LICENSES_DB    = BUILD_DIR / "build_licenses.db"

# ── License database ───────────────────────────────────────────────────────────

_LICENSE_TEMPLATES = [
    {
        "name": "As-Is / No Warranty (Custom)",
        "category": "Protective",
        "spdx": "custom-as-is",
        "source_url": "",
        "text": """\
AS-IS SOFTWARE LICENSE
======================

Copyright (c) {year} {publisher}

This software is provided "AS IS", without warranty of any kind, express or
implied, including but not limited to the warranties of merchantability,
fitness for a particular purpose, and non-infringement.  In no event shall
the authors or copyright holders be liable for any claim, damages, or other
liability, whether in an action of contract, tort, or otherwise, arising from,
out of, or in connection with the software or the use or other dealings in the
software.

USE AT YOUR OWN RISK.  The entire risk as to the quality and performance of
the software is with you.  Should the software prove defective, you assume the
cost of all necessary servicing, repair, or correction.

You are granted a non-exclusive, non-transferable license to use this software
for personal or internal business purposes only.  Redistribution in source or
binary form is not permitted without the prior written consent of the copyright
holder.
""",
    },
    {
        "name": "MIT License",
        "category": "Open Source",
        "spdx": "MIT",
        "source_url": "https://spdx.org/licenses/MIT.json",
        "text": """\
MIT License

Copyright (c) {year} {publisher}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
""",
    },
    {
        "name": "The Unlicense (Public Domain)",
        "category": "Open Source",
        "spdx": "Unlicense",
        "source_url": "https://spdx.org/licenses/Unlicense.json",
        "text": """\
This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or distribute
this software, either in source code form or as a compiled binary, for any
purpose, commercial or non-commercial, and by any means.

In jurisdictions that recognize copyright laws, the author or authors of this
software dedicate any and all copyright interest in the software to the public
domain. We make this dedication for the benefit of the public at large and to
the detriment of our heirs and successors. We intend this dedication to be an
overt act of relinquishment in perpetuity of all present and future rights to
this software under copyright law.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT.  IN NO EVENT SHALL THE
AUTHORS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN
ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

For more information, please refer to <https://unlicense.org>
""",
    },
    {
        "name": "ISC License",
        "category": "Open Source",
        "spdx": "ISC",
        "source_url": "https://spdx.org/licenses/ISC.json",
        "text": """\
ISC License

Copyright (c) {year} {publisher}

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES WITH
REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF MERCHANTABILITY
AND FITNESS.  IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY SPECIAL, DIRECT,
INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES WHATSOEVER RESULTING FROM
LOSS OF USE, DATA OR PROFITS, WHETHER IN AN ACTION OF CONTRACT, NEGLIGENCE OR
OTHER TORTIOUS ACTION, ARISING OUT OF OR IN CONNECTION WITH THE USE OR
PERFORMANCE OF THIS SOFTWARE.
""",
    },
    {
        "name": "Apache License 2.0",
        "category": "Open Source",
        "spdx": "Apache-2.0",
        "source_url": "https://spdx.org/licenses/Apache-2.0.json",
        "text": """\
Apache License
Version 2.0, January 2004
http://www.apache.org/licenses/

Copyright (c) {year} {publisher}

Licensed under the Apache License, Version 2.0 (the "License"); you may not
use this file except in compliance with the License.  You may obtain a copy of
the License at:

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software distributed
under the License is distributed on an "AS IS" BASIS, WITHOUT WARRANTIES OR
CONDITIONS OF ANY KIND, either express or implied.  See the License for the
specific language governing permissions and limitations under the License.

DISCLAIMER: This software is provided "as is" with no warranty of any kind.
The authors and contributors shall not be liable for any damages arising from
the use of this software.
""",
    },
    {
        "name": "Proprietary — Personal Use Only",
        "category": "Commercial",
        "spdx": "custom-personal",
        "source_url": "",
        "text": """\
PROPRIETARY SOFTWARE LICENSE — PERSONAL USE ONLY
=================================================

Copyright (c) {year} {publisher}.  All rights reserved.

This software and its source code are the proprietary and confidential property
of {publisher}.  Unauthorized copying, redistribution, modification, reverse
engineering, or decompilation of this software, in whole or in part, is
strictly prohibited.

GRANT OF LICENSE: {publisher} grants you a non-exclusive, non-transferable,
revocable license to install and use one copy of this software solely for your
own personal, non-commercial purposes on a single device that you own or control.

DISCLAIMER OF WARRANTIES: THIS SOFTWARE IS PROVIDED "AS IS" AND "AS AVAILABLE"
WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED.  {publisher} EXPRESSLY
DISCLAIMS ALL WARRANTIES, INCLUDING BUT NOT LIMITED TO IMPLIED WARRANTIES OF
MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE, AND NON-INFRINGEMENT.

LIMITATION OF LIABILITY: IN NO EVENT SHALL {publisher} BE LIABLE FOR ANY
INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING
BUT NOT LIMITED TO LOSS OF DATA, BUSINESS INTERRUPTION, OR LOSS OF PROFITS)
ARISING OUT OF THE USE OR INABILITY TO USE THIS SOFTWARE, EVEN IF ADVISED OF
THE POSSIBILITY OF SUCH DAMAGE.

USE AT YOUR OWN RISK.
""",
    },
    {
        "name": "Proprietary — Commercial",
        "category": "Commercial",
        "spdx": "custom-commercial",
        "source_url": "",
        "text": """\
COMMERCIAL SOFTWARE LICENSE AGREEMENT
======================================

Copyright (c) {year} {publisher}.  All rights reserved.

IMPORTANT — READ CAREFULLY: This License Agreement is a legal agreement between
you (the "Licensee") and {publisher} (the "Licensor") for the use of this
software product (the "Software").

1. GRANT OF LICENSE. Subject to the terms of this Agreement, Licensor grants
   Licensee a non-exclusive, non-transferable license to install and use the
   Software on the number of devices covered by the purchased license.

2. RESTRICTIONS. Licensee may not: (a) copy, modify, or distribute the
   Software; (b) reverse engineer, decompile, or disassemble the Software;
   (c) rent, lease, or lend the Software to third parties; (d) use the Software
   to provide services to third parties without a separate agreement.

3. DISCLAIMER OF WARRANTIES. THE SOFTWARE IS PROVIDED "AS IS" WITHOUT WARRANTY
   OF ANY KIND.  LICENSOR DISCLAIMS ALL WARRANTIES, EXPRESS OR IMPLIED,
   INCLUDING WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE,
   TITLE, AND NON-INFRINGEMENT.

4. LIMITATION OF LIABILITY. TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW,
   IN NO EVENT SHALL LICENSOR BE LIABLE FOR ANY INDIRECT, INCIDENTAL, SPECIAL,
   CONSEQUENTIAL, OR EXEMPLARY DAMAGES, OR DAMAGES FOR LOSS OF PROFITS, REVENUE,
   DATA, BUSINESS, GOODWILL, OR OTHER INTANGIBLE LOSSES, ARISING OUT OF OR IN
   CONNECTION WITH THIS AGREEMENT OR THE USE OF THE SOFTWARE.

5. INDEMNIFICATION. LICENSEE AGREES TO INDEMNIFY AND HOLD LICENSOR HARMLESS
   FROM ANY CLAIMS, DAMAGES, OR EXPENSES ARISING FROM LICENSEE'S USE OF THE
   SOFTWARE OR BREACH OF THIS AGREEMENT.

6. GOVERNING LAW. This Agreement shall be governed by the laws of the
   jurisdiction in which Licensor is located.

USE OF THIS SOFTWARE CONSTITUTES ACCEPTANCE OF ALL TERMS ABOVE.
""",
    },
    {
        "name": "BSD 2-Clause (Simplified)",
        "category": "Open Source",
        "spdx": "BSD-2-Clause",
        "source_url": "https://spdx.org/licenses/BSD-2-Clause.json",
        "text": """\
BSD 2-Clause License

Copyright (c) {year} {publisher}
All rights reserved.

Redistribution and use in source and binary forms, with or without modification,
are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS" AND
ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE IMPLIED
WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED.  IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
""",
    },
]

def _init_license_db():
    """Create the licenses DB and seed it with built-in templates if empty."""
    import datetime
    con = sqlite3.connect(str(LICENSES_DB))
    con.execute("""
        CREATE TABLE IF NOT EXISTS licenses (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL,
            category   TEXT NOT NULL DEFAULT '',
            spdx       TEXT NOT NULL DEFAULT '',
            text       TEXT NOT NULL DEFAULT '',
            source_url TEXT NOT NULL DEFAULT '',
            scraped_at TEXT NOT NULL DEFAULT ''
        )
    """)
    con.commit()
    # Seed only if table is empty
    if con.execute("SELECT COUNT(*) FROM licenses").fetchone()[0] == 0:
        now = datetime.datetime.utcnow().isoformat()
        con.executemany(
            "INSERT INTO licenses(name,category,spdx,text,source_url,scraped_at)"
            " VALUES(:name,:category,:spdx,:text,:source_url,:scraped_at)",
            [{**t, "scraped_at": now} for t in _LICENSE_TEMPLATES],
        )
        con.commit()
    con.close()

# ── Settings helpers ──────────────────────────────────────────────────────────

_DEFAULTS: dict = {
    "APP_NAME":            "Job Tracker",
    "APP_VERSION":         "1.0.0",
    "APP_DESCRIPTION":     "Personal job search tracker",
    "APP_PUBLISHER":       "Your Name or Company",
    "APP_URL":             "https://github.com/yourname/job_tracker",
    "APP_SUPPORT_URL":     "https://github.com/yourname/job_tracker/issues",
    "APP_EXE_NAME":        "JobTracker",
    "OUTPUT_DIR":          r"D:\Compile Playground\JobTracker",
    "DEFAULT_INSTALL_DIR": r"{autopf}\JobTracker",
    "REQUIRE_ADMIN":       False,
    "DESKTOP_ICON":        True,
    "START_MENU_ICON":     True,
    "ADD_TO_STARTUP":      False,
    "ICON_FILE":           "",
    "APP_ICON_FILE":       "",
    "LICENSE_FILE":        "",
    "USE_PYARMOR":         False,
    "USE_NUITKA":          False,
    "GIT_REPO_DIR":        "",   # repo root; "" = use PROJECT_ROOT
    "GIT_REMOTE_URL":      "",   # GitHub remote URL, e.g. https://github.com/user/repo.git
    "GIT_DEV_BRANCH":      "development",
    "GIT_MAIN_BRANCH":     "main",
    "MERGE_SOURCE":        "development",
    "MERGE_TARGET":        "main",
    "BACKUP_SRC":          "",
    "BACKUP_DEST":         "",
    "BACKUP_SCHEDULE":     "manual",
    "VERSION_INCREMENT":   "keep",
    "DAEMON_NAME":         "Build_Dash",
    "DAEMON_ICON":         "",
    "DAEMON_BUILD_DIR":    "",
    "APP_PORT":            "5000",
    "README_FILE":         "",
    "LICENSE_TEMPLATE":    "",
    "SUPPORT_EMAIL":       "",
    "SOUND_SUCCESS":       "",
    "SOUND_FAIL":          "",
    "SIGN_PFX":            "",         # path to .pfx code-signing certificate; "" = skip signing
    "SIGN_PFX_PASSWORD":  "",         # PFX password (stored locally, never committed)
    "SIGN_TIMESTAMP_URL": "http://timestamp.digicert.com",
    "CHANGELOG_TRIGGER":   "minor",   # "major"|"minor"|"patch"|"never"
    # Extra (source_rel, dest_in_bundle) pairs for projects that need files outside
    # templates/ and static/ — e.g. [["dashboard.html", "."], ["Voices and Sounds", "Voices and Sounds"]]
    "EXTRA_DATA":          [],
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


# ── Project profile helpers ────────────────────────────────────────────────────

def _load_projects() -> dict:
    if PROJECTS_FILE.exists():
        try:
            return json.loads(PROJECTS_FILE.read_text("utf-8"))
        except Exception:
            pass
    return {"active": None, "projects": {}, "recent": []}


def _save_projects(data: dict) -> None:
    PROJECTS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _git_cwd() -> str:
    """Return the git working-tree root from settings, or PROJECT_ROOT."""
    d = _load_settings().get("GIT_REPO_DIR", "").strip()
    if d and Path(d).is_dir():
        return d
    return str(PROJECT_ROOT)


def _gh_run(args: list, cwd: str | None = None, timeout: int = 15) -> tuple:
    """Run a gh CLI command, return (stdout, stderr, returncode)."""
    result = subprocess.run(
        ["gh"] + args,
        capture_output=True, text=True, timeout=timeout, cwd=cwd,
    )
    return result.stdout.strip(), result.stderr.strip(), result.returncode


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


def _open_explorer_foreground(path: str) -> None:
    """Open Windows Explorer at *path* and force the window to the foreground.

    Uses Shell.Application COM to open the folder, waits briefly for the window
    to appear, then calls ShowWindow + SetForegroundWindow via P/Invoke so the
    Explorer window appears on top of the browser.
    """
    safe = path.replace("'", "''")
    # PowerShell here-string with inline C# for user32 P/Invoke.
    # The "@ closing marker MUST start at column 0 in the file — we write via
    # a temp .ps1 file so indentation in this Python string maps directly.
    # C# block — AttachThreadInput bypasses Windows foreground-lock restriction.
    # Plain SetForegroundWindow alone is blocked when the caller is not the
    # current foreground process (e.g. Python/Flask behind a browser tab).
    cs = (
        "using System;\n"
        "using System.Runtime.InteropServices;\n"
        "public class WinFg {\n"
        "    [DllImport(\"user32.dll\")] public static extern bool SetForegroundWindow(IntPtr h);\n"
        "    [DllImport(\"user32.dll\")] public static extern bool ShowWindow(IntPtr h, int n);\n"
        "    [DllImport(\"user32.dll\")] public static extern bool BringWindowToTop(IntPtr h);\n"
        "    [DllImport(\"user32.dll\")] public static extern IntPtr GetForegroundWindow();\n"
        "    [DllImport(\"user32.dll\")] public static extern int GetWindowThreadProcessId(IntPtr hWnd, IntPtr lpdwProcessId);\n"
        "    [DllImport(\"kernel32.dll\")] public static extern int GetCurrentThreadId();\n"
        "    [DllImport(\"user32.dll\")] public static extern bool AttachThreadInput(int a, int b, bool attach);\n"
        "    public static void ForceToFront(IntPtr hwnd) {\n"
        "        IntPtr fg = GetForegroundWindow();\n"
        "        int fgThread = GetWindowThreadProcessId(fg, IntPtr.Zero);\n"
        "        int myThread = GetCurrentThreadId();\n"
        "        if (fgThread != myThread) AttachThreadInput(fgThread, myThread, true);\n"
        "        ShowWindow(hwnd, 9);\n"
        "        SetForegroundWindow(hwnd);\n"
        "        BringWindowToTop(hwnd);\n"
        "        if (fgThread != myThread) AttachThreadInput(fgThread, myThread, false);\n"
        "    }\n"
        "}\n"
    )
    ps_script = (
        f"$path = '{safe}'\n"
        "$shell = New-Object -ComObject Shell.Application\n"
        "$shell.Explore($path)\n"
        "Start-Sleep -Milliseconds 800\n"
        f"Add-Type @\"\n{cs}\"@\n"
        "$win = $null\n"
        "foreach ($w in ($shell.Windows())) {\n"
        "    try {\n"
        "        if ($w.Document.Folder.Self.Path -eq $path) { $win = $w; break }\n"
        "    } catch {}\n"
        "}\n"
        "if ($win) { [WinFg]::ForceToFront([IntPtr]$win.HWND) }\n"
    )
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".ps1", delete=False, encoding="utf-8"
        ) as f:
            f.write(ps_script)
            tmp_path = f.name
        subprocess.Popen([
            "powershell", "-NoProfile", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-File", tmp_path,
        ])
    finally:
        if tmp_path:
            # Delay cleanup — PowerShell reads the file asynchronously
            def _cleanup(p=tmp_path):
                import time as _time
                _time.sleep(8)
                try:
                    os.unlink(p)
                except Exception:
                    pass
            threading.Thread(target=_cleanup, daemon=True).start()


def _kill_running_app(exe_name: str, port: int | None = None) -> None:
    """Kill any running instances of the target app before building.

    Two strategies run in sequence:
    1. By exe name — catches compiled .exe releases (taskkill /F /IM <exe>.exe).
    2. By port PID — catches dev-mode python servers; reads netstat -ano to find
       the PID owning APP_PORT and kills it with taskkill /F /PID.
    """
    import time as _time

    # ── Strategy 1: kill by exe name ────────────────────────────────────────
    if exe_name:
        target = exe_name if exe_name.lower().endswith(".exe") else exe_name + ".exe"
        _append(f"[PRE-BUILD] Checking for compiled instance of '{target}'…")
        try:
            result = subprocess.run(
                ["taskkill", "/F", "/IM", target],
                capture_output=True, text=True, timeout=10,
            )
            if result.returncode == 0:
                _append(f"[PRE-BUILD] Terminated compiled instance(s) of '{target}'.")
            else:
                msg = (result.stdout + result.stderr).strip()
                if "not found" in msg.lower() or "no tasks" in msg.lower():
                    _append(f"[PRE-BUILD] No compiled instance of '{target}' found.")
                else:
                    _append(f"[PRE-BUILD] taskkill: {msg or 'no output'}")
        except Exception as exc:
            _append(f"[WARN] Could not run taskkill for '{target}': {exc}")

    # ── Strategy 2: kill by port PID (dev-mode / any process on APP_PORT) ───
    if port:
        _append(f"[PRE-BUILD] Checking port {port} for any running app server…")
        try:
            ns = subprocess.run(
                ["netstat", "-ano"],
                capture_output=True, text=True, timeout=10,
            )
            pid: int | None = None
            for line in ns.stdout.splitlines():
                if f"127.0.0.1:{port}" in line and "LISTENING" in line:
                    parts = line.strip().split()
                    if parts:
                        try:
                            pid = int(parts[-1])
                        except ValueError:
                            pass
                    break
            if pid and pid > 4:  # PID 4 = Windows System; skip
                kill = subprocess.run(
                    ["taskkill", "/F", "/PID", str(pid)],
                    capture_output=True, text=True, timeout=10,
                )
                if kill.returncode == 0:
                    _append(f"[PRE-BUILD] Terminated PID {pid} (server on port {port}).")
                    _time.sleep(0.5)  # brief pause so the OS releases the port
                else:
                    msg = (kill.stdout + kill.stderr).strip()
                    _append(f"[PRE-BUILD] Could not kill PID {pid}: {msg}")
            else:
                _append(f"[PRE-BUILD] Port {port} is free.")
        except Exception as exc:
            _append(f"[WARN] Could not check port {port}: {exc}")


def _find_windows_defender() -> str | None:
    """Locate MpCmdRun.exe (Windows Defender CLI scanner). Returns path or None."""
    # Static path — all Windows versions
    static = Path(r"C:\Program Files\Windows Defender\MpCmdRun.exe")
    if static.is_file():
        return str(static)
    # Dynamic versioned platform folder (Windows 10/11 Defender updates)
    platform_root = Path(r"C:\ProgramData\Microsoft\Windows Defender\Platform")
    if platform_root.is_dir():
        for d in sorted(platform_root.iterdir(), reverse=True):
            mp = d / "MpCmdRun.exe"
            if mp.is_file():
                return str(mp)
    # Last resort: PATH search
    try:
        r = subprocess.run(["where", "MpCmdRun.exe"], capture_output=True, text=True, timeout=5)
        if r.returncode == 0:
            first = r.stdout.strip().splitlines()[0].strip()
            if first:
                return first
    except Exception:
        pass
    return None


def _do_virus_scan(scan_dir: str) -> bool:
    """Run Windows Defender on scan_dir before compiling.

    Returns True  → clean (build may continue).
    Returns False → threats found (build must halt; status set to 'error').
    """
    SEP = "=" * 56
    _append(SEP)
    _append("  STAGE: Virus / Malware Scan")
    _append(f"  Scanning source: {scan_dir}")
    _append(SEP)

    defender = _find_windows_defender()
    if not defender:
        _append("[WARN] Windows Defender (MpCmdRun.exe) not found on this machine.")
        _append("[WARN] Skipping virus scan — run a manual scan before distributing.")
        return True  # non-blocking: no Defender means we can't scan; warn and continue

    _append(f"[OK]   Windows Defender: {defender}")
    _append("[SCAN] Starting custom scan…")
    try:
        result = subprocess.run(
            [defender, "-Scan", "-ScanType", "3", "-File", scan_dir, "-DisableRemediation"],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300,
        )
        # Stream any output Defender produced
        output = (result.stdout + result.stderr).strip()
        for line in output.splitlines():
            clean = _sanitize(line.rstrip())
            if clean:
                _append(clean)

        if result.returncode == 0:
            _append("[OK]   Scan complete — no threats detected. Source is clean.")
            return True
        elif result.returncode == 2:
            _append("[ERROR] THREAT DETECTED by Windows Defender!")
            _append("[ERROR] Build halted — compiling infected code is not allowed.")
            _append("[ERROR] Quarantine or remove the threat, then rebuild.")
            _set_status("error")
            return False
        else:
            # Any other non-zero exit (e.g. scan engine error, timeout) — warn, don't block
            _append(f"[WARN] Defender scan finished with unexpected status ({result.returncode}).")
            _append("[WARN] Could not confirm source is clean — verify manually before release.")
            return True

    except subprocess.TimeoutExpired:
        _append("[WARN] Virus scan timed out (> 5 min) — skipping. Scan manually before release.")
        return True
    except Exception as exc:
        _append(f"[WARN] Virus scan error: {exc} — skipping.")
        return True


def _run_build_process(cmd: list[str]) -> None:
    """Run a subprocess (build or pip install), stream its output to _build_log."""
    _append("-" * 56)
    _append("Command: " + " ".join(str(c) for c in cmd))
    _append("-" * 56)
    _utf8_env = os.environ.copy()
    _utf8_env["PYTHONIOENCODING"] = "utf-8"
    _utf8_env["PYTHONUTF8"]       = "1"       # force UTF-8 for all Python subprocesses
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_utf8_env,
        )
        for line in proc.stdout:
            _append(_sanitize(line.rstrip()))
        proc.wait()
        _append("")
        _append("-" * 56)
        _append(f"Process exited with code {proc.returncode}")
        _append("-" * 56)
        _set_status("done" if proc.returncode == 0 else "error")
    except Exception as exc:
        _append(f"[EXCEPTION] {exc}")
        _set_status("error")


def _resolve_output_dir(settings: dict) -> Path:
    """Return the absolute output root dir from settings, creating it if needed."""
    repo_dir = Path(settings.get("GIT_REPO_DIR", "").strip() or str(PROJECT_ROOT))
    out_rel  = settings.get("OUTPUT_DIR", r"D:\Compile Playground\JobTracker").strip()
    if not out_rel:
        exe_name = settings.get("APP_EXE_NAME", "JobTracker").strip() or "JobTracker"
        out_rel  = rf"D:\Compile Playground\{exe_name}"
    return Path(out_rel) if Path(out_rel).is_absolute() else (repo_dir / out_rel)


def _write_build_log_file(mode: str, settings: dict, start_time) -> "Path | None":
    """Write the full _build_log to a timestamped file in the output dir's Log/ subdir.
    Returns the Path written, or None on failure."""
    import datetime as _dt
    try:
        exe_name   = settings.get("APP_EXE_NAME", "app").strip() or "app"
        version    = settings.get("APP_VERSION",  "1.0.0").strip()
        output_dir = _resolve_output_dir(settings)
        log_dir    = output_dir / "Log"
        log_dir.mkdir(parents=True, exist_ok=True)

        end_time     = _dt.datetime.now()
        ts_short     = start_time.strftime("%Y-%m-%d_%H-%M-%S")
        duration_s   = max(0, int((end_time - start_time).total_seconds()))
        mode_label   = {"full": "Full Build", "bundle-only": "Bundle Only"}.get(mode, mode.title())

        with _build_lock:
            status    = _build_status
            log_lines = list(_build_log)

        status_label = "PASS" if status == "done" else "FAIL"
        fname        = f"{exe_name}_Build_{ts_short}.log"
        fpath        = log_dir / fname
        SEP          = "=" * 72

        # Summarise warnings and errors from the log
        errors   = [l for l in log_lines if l.lstrip().startswith("[ERROR]") or l.lstrip().startswith("[EXCEPTION]")]
        warnings = [l for l in log_lines if l.lstrip().startswith("[WARN]")]

        header = [
            SEP,
            f"  Build Dashboard — Build Log",
            f"  App       : {exe_name}  v{version}",
            f"  Mode      : {mode_label}",
            f"  Result    : {status_label}",
            f"  Started   : {start_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"  Finished  : {end_time.strftime('%Y-%m-%d %H:%M:%S')}",
            f"  Duration  : {duration_s // 60}m {duration_s % 60}s",
            f"  Log file  : {fpath}",
            SEP,
            "",
        ]

        if warnings or errors:
            summary = ["--- SUMMARY ---"]
            for e in errors:   summary.append(f"  [ERROR]  {e.strip()}")
            for w in warnings: summary.append(f"  [WARN]   {w.strip()}")
            summary += ["--- END SUMMARY ---", ""]
        else:
            summary = []

        footer = [
            "",
            SEP,
            f"  Build {status_label}: {mode_label}  |  {exe_name} v{version}  |  {end_time.strftime('%Y-%m-%d %H:%M:%S')}",
            SEP,
        ]

        content = (
            "\n".join(header)
            + ("\n".join(summary) if summary else "")
            + "--- BUILD OUTPUT ---\n"
            + "\n".join(log_lines)
            + "\n--- END BUILD OUTPUT ---\n"
            + "\n".join(footer)
            + "\n"
        )
        fpath.write_text(content, encoding="utf-8")
        return fpath
    except Exception:
        return None


def _pip_install(package: str) -> bool:
    """Install a pip package into the running interpreter, streaming output to the log.
    Returns True on success."""
    cmd = [sys.executable, "-m", "pip", "install", package]
    _append("=" * 56)
    _append(f"Module '{package}' not found — installing via pip...")
    _append("Command: " + " ".join(cmd))
    _append("=" * 56)
    try:
        _utf8_env = os.environ.copy()
        _utf8_env["PYTHONIOENCODING"] = "utf-8"
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=_utf8_env,
        )
        for line in proc.stdout:
            _append(line.rstrip())
        proc.wait()
        if proc.returncode == 0:
            _append(f"[OK] '{package}' installed successfully.")
            _append("")
            return True
        _append(f"[ERROR] pip install '{package}' failed (exit {proc.returncode}).")
        return False
    except Exception as exc:
        _append(f"[EXCEPTION during install] {exc}")
        return False


def _run_build_exe(cmd: list[str]) -> None:
    """Ensure PyInstaller is installed, then run the build command."""
    import importlib.util
    if importlib.util.find_spec("PyInstaller") is None:
        if not _pip_install("pyinstaller"):
            _set_status("error")
            return
    _run_build_process(cmd)


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
                _append(_sanitize(line.rstrip()))
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
            "# Python\n__pycache__/\n*.pyc\n*.pyo\n*.pyd\n\n"
            "# Databases\n*.db\n*.db-journal\n*.sqlite\n*.sqlite3\n\n"
            "# Build outputs\ndist/\nbuild_output/\nobf_src/\n\n"
            "# Virtual environments\nvenv/\n.venv/\nenv/\n\n"
            "# Packaging\n*.egg-info/\n\n"
            "# Secrets\n.env\n\n"
            "# App-specific\nemail_responses/\n*.log\ninstance/\n\n"
            "# Build settings (local paths, not for version control)\nbuild/build_settings.json\n"
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


def _force_rmtree(path: Path, log_fn=None) -> bool:
    """Delete a directory tree using PowerShell Remove-Item -Recurse -Force.

    Passes the path via an environment variable to sidestep all quoting issues.
    Returns True if the path is gone afterwards, False if it still exists.
    """
    if not path.exists():
        return True

    env = {**os.environ, "_BD_RMPATH": str(path)}
    ps_cmd = (
        "Remove-Item -LiteralPath $env:_BD_RMPATH "
        "-Recurse -Force -ErrorAction SilentlyContinue"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NonInteractive", "-Command", ps_cmd],
            env=env, capture_output=True, text=True, timeout=60,
        )
        if result.stderr.strip() and log_fn:
            log_fn(f"  [DBG] Remove-Item stderr: {result.stderr.strip()[:300]}")
    except Exception as e:
        if log_fn:
            log_fn(f"  [WARN] PowerShell delete error: {e}")

    return not path.exists()


def _run_clean(clean_mode: str = "all") -> None:
    """Remove build output directories without running a subprocess.

    clean_mode:
      "temp"   — compile temp dirs only (build_output, obf_src inside project)
      "output" — configured output folder only (exe, installer; Log/ preserved)
      "all"    — both (default)
    """
    settings = _load_settings()
    mode_label = {
        "temp":   "Compile Temp Only",
        "output": "Output Folder Only",
        "all":    "All Build Outputs",
    }.get(clean_mode, clean_mode)
    _append("-" * 56)
    _append(f"Cleaning: {mode_label}")
    _append("-" * 56)

    def _rm(p: Path) -> None:
        """Remove p (file or dir) and log the result."""
        if not p.exists():
            _append(f"  Already gone: {p}")
            return
        ok = _force_rmtree(p, log_fn=_append) if p.is_dir() else _try_unlink(p)
        if ok:
            _append(f"  Removed:      {p}")
        else:
            _append(f"  [WARN] Could not remove: {p}  (in use or permissions?)")

    def _try_unlink(p: Path) -> bool:
        try:
            p.unlink()
            return True
        except Exception:
            return False

    if clean_mode in ("temp", "all"):
        # PyInstaller/Nuitka work directories inside the project tree
        for name in ("build_output", "obf_src"):
            _rm(PROJECT_ROOT / name)
        # Bundle Only is a compile artifact in the output dir — always remove it
        _rm(_resolve_output_dir(settings) / "Bundle Only")

    if clean_mode in ("output", "all"):
        # Configured output root (may be on another drive, e.g. D:\Compile Playground\…)
        # Logs are preserved — use the dedicated Clean Logs action to remove them.
        out_root = _resolve_output_dir(settings)
        if out_root.exists():
            log_dir = out_root / "Log"
            for item in out_root.iterdir():
                if item.resolve() == log_dir.resolve():
                    continue   # skip Log/ subfolder
                _rm(item)
            _append(f"  Cleaned:      {out_root}  (Log/ preserved)")
        else:
            _append(f"  Already gone: {out_root}")

    _append("")
    _append("Clean complete.")
    _set_status("done")


# ── Build history helpers ──────────────────────────────────────────────────────

def _build_history_path(settings: dict | None = None) -> "Path":
    """build_history.json lives in the parent of the output dir so a clean of
    the output folder does not erase the history."""
    if settings is None:
        settings = _load_settings()
    out = _resolve_output_dir(settings)
    parent = out.parent if out.parent != out else out
    return parent / "build_history.json"


def _current_git_commit(cwd: str) -> str:
    """Return the short HEAD commit hash, or empty string on failure."""
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, cwd=cwd, timeout=5,
        )
        return r.stdout.strip() if r.returncode == 0 else ""
    except Exception:
        return ""


def _append_build_history(
    version: str,
    build_type: str,
    mode: str,
    description: str,
    git_commit: str,
    success: bool,
    settings: dict,
) -> None:
    """Prepend a completed build record to build_history.json (newest first)."""
    import datetime as _bh_dt

    hist_path = _build_history_path(settings)
    try:
        entries = json.loads(hist_path.read_text("utf-8")) if hist_path.exists() else []
    except Exception:
        entries = []

    entries.insert(0, {
        "version":     version,
        "build_type":  build_type,   # "Main" | "Incremental" | "HOTFIX"
        "mode":        mode,          # "full" | "bundle-only" | "installer-only"
        "description": description,
        "date":        _bh_dt.datetime.now().isoformat(timespec="seconds"),
        "git_commit":  git_commit,
        "success":     success,
        "settings_snapshot": {
            k: v for k, v in settings.items()
            if k not in ("SOUND_SUCCESS", "SOUND_FAIL")
        },
    })
    entries = entries[:50]   # keep last 50 builds
    try:
        hist_path.parent.mkdir(parents=True, exist_ok=True)
        hist_path.write_text(json.dumps(entries, indent=2), "utf-8")
    except Exception:
        pass


# ── Changelog helpers ─────────────────────────────────────────────────────────

def _changelog_path(settings: dict | None = None) -> Path:
    if settings is None:
        settings = _load_settings()
    repo = Path(settings.get("GIT_REPO_DIR", "").strip() or str(PROJECT_ROOT))
    return repo / "CHANGELOG.json"


def _changelog_md_path(settings: dict | None = None) -> Path:
    return _changelog_path(settings).with_suffix(".md")


def _load_changelog(settings: dict | None = None) -> dict:
    """Load CHANGELOG.json, migrating the old bare-list format if needed."""
    default: dict = {"trigger": "minor", "draft": [], "entries": []}
    path = _changelog_path(settings)
    if not path.exists():
        return default
    try:
        raw = json.loads(path.read_text("utf-8"))
        if isinstance(raw, list):          # migrate old format
            return {**default, "entries": raw}
        if isinstance(raw, dict):
            return {**default, **raw}
    except Exception:
        pass
    return default


def _save_changelog(data: dict, settings: dict | None = None) -> None:
    path = _changelog_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), "utf-8")


def _generate_changelog_md(data: dict) -> str:
    """Serialise CHANGELOG.json dict → human-editable Markdown."""
    lines: list[str] = ["# Change Log", ""]
    lines += ["## DRAFT — Upcoming Changes", ""]
    draft = data.get("draft", [])
    lines += ([f"- {c}" for c in draft] if draft else ["- (no upcoming changes — add them here)"])
    lines += [""]
    for entry in data.get("entries", []):
        ver  = entry.get("version", "?")
        date = entry.get("date", "")
        lines += [f"## v{ver} — {date}", ""]
        changes = entry.get("changes", [])
        lines += ([f"- {c}" for c in changes] if changes else ["- (no changes listed)"])
        lines += [""]
    return "\n".join(lines)


def _parse_changelog_md(text: str) -> dict:
    """Parse a human-edited CHANGELOG.md back into the JSON dict format."""
    import re as _re
    result: dict = {"draft": [], "entries": []}
    section = None   # "draft" | dict (entry)

    for raw in text.splitlines():
        line = raw.rstrip()
        if _re.match(r"^## DRAFT", line, _re.IGNORECASE):
            section = "draft"
        elif m := _re.match(r"^## v([\d][^\s—–-]*)\s*[—–-]+\s*(\S+)", line):
            entry = {"version": m.group(1), "date": m.group(2), "changes": []}
            result["entries"].append(entry)
            section = entry
        elif line.startswith("- ") and section is not None:
            text_part = line[2:].strip()
            if text_part and text_part != "(no upcoming changes — add them here)" \
                    and text_part != "(no changes listed)":
                if section == "draft":
                    result["draft"].append(text_part)
                else:
                    section["changes"].append(text_part)
    return result


def _version_crossed_trigger(old_ver: str, new_ver: str, trigger: str) -> bool:
    """Return True if the version bump crosses the configured trigger boundary."""
    if trigger == "never" or old_ver == new_ver:
        return False
    try:
        def _seg(v: str) -> tuple[int, ...]:
            return tuple(int(x) for x in v.split(".")[:3])
        o, n = _seg(old_ver), _seg(n := new_ver)  # noqa: F841
        o, n = _seg(old_ver), _seg(new_ver)
        if trigger == "major":
            return n[0] > o[0]
        if trigger == "minor":
            return n[0] > o[0] or (n[0] == o[0] and len(n) > 1 and len(o) > 1 and n[1] > o[1])
        if trigger == "patch":
            return n != o
    except Exception:
        pass
    return False


def _find_notepadpp() -> str | None:
    for candidate in (
        r"C:\Program Files\Notepad++\notepad++.exe",
        r"C:\Program Files (x86)\Notepad++\notepad++.exe",
    ):
        if os.path.isfile(candidate):
            return candidate
    return None


# ── Flask application factory ─────────────────────────────────────────────────

def create_builder_app() -> Flask:
    app = Flask(__name__, template_folder=str(BUILD_DIR))
    _init_license_db()

    # ── Settings ──────────────────────────────────────────────────────────────

    @app.route("/")
    def index():
        return render_template("builder.html")

    @app.route("/help")
    def builder_help():
        return render_template("builder_help.html")

    # ── License DB routes ──────────────────────────────────────────────────────

    @app.route("/api/licenses")
    def api_licenses_list():
        _init_license_db()
        con = sqlite3.connect(str(LICENSES_DB))
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT id, name, category, spdx, source_url, scraped_at FROM licenses ORDER BY category, name"
        ).fetchall()
        con.close()
        return jsonify([dict(r) for r in rows])

    @app.route("/api/licenses/<int:lic_id>")
    def api_license_text(lic_id):
        _init_license_db()
        con = sqlite3.connect(str(LICENSES_DB))
        row = con.execute("SELECT * FROM licenses WHERE id=?", (lic_id,)).fetchone()
        con.close()
        if not row:
            return jsonify({"ok": False, "error": "Not found"}), 404
        return jsonify({"ok": True, "id": row[0], "name": row[1], "category": row[2],
                        "spdx": row[3], "text": row[4]})

    @app.route("/api/licenses/apply", methods=["POST"])
    def api_licenses_apply():
        """Write the selected license text to LICENSE.txt in the project repo root."""
        import datetime
        data     = request.get_json(silent=True) or {}
        lic_id   = data.get("id")
        settings = _load_settings()
        repo_dir = settings.get("GIT_REPO_DIR", "").strip() or str(PROJECT_ROOT)
        lic_file = settings.get("LICENSE_FILE", "LICENSE.txt").strip() or "LICENSE.txt"
        dest     = Path(repo_dir) / lic_file

        _init_license_db()
        con = sqlite3.connect(str(LICENSES_DB))
        row = con.execute("SELECT text, name FROM licenses WHERE id=?", (lic_id,)).fetchone()
        con.close()
        if not row:
            return jsonify({"ok": False, "error": "License not found"})

        year      = datetime.datetime.now().year
        publisher = settings.get("APP_PUBLISHER", "")
        text      = row[0].replace("{year}", str(year)).replace("{publisher}", publisher)

        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
            return jsonify({"ok": True, "path": str(dest), "name": row[1]})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/licenses/refresh", methods=["POST"])
    def api_licenses_refresh():
        """Pull license texts for SPDX-listed entries from spdx.org and update the DB."""
        import datetime, urllib.request
        _init_license_db()
        con = sqlite3.connect(str(LICENSES_DB))
        rows = con.execute(
            "SELECT id, spdx, source_url FROM licenses WHERE source_url != ''"
        ).fetchall()
        updated, errors = 0, []
        for lic_id, spdx, url in rows:
            try:
                with urllib.request.urlopen(url, timeout=8) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                    # SPDX JSON has "licenseText" or "standardLicenseTemplate"
                    text = (payload.get("licenseText") or
                            payload.get("standardLicenseTemplate") or "").strip()
                if text:
                    con.execute(
                        "UPDATE licenses SET text=?, scraped_at=? WHERE id=?",
                        (text, datetime.datetime.utcnow().isoformat(), lic_id),
                    )
                    updated += 1
            except Exception as exc:
                errors.append(f"{spdx}: {exc}")
        con.commit()
        con.close()
        return jsonify({"ok": True, "updated": updated, "errors": errors})

    # ── README helpers ────────────────────────────────────────────────────────

    def _readme_path():
        """Return resolved Path for the README file, or None if not configured."""
        settings = _load_settings()
        raw = settings.get("README_FILE", "").strip() or "README.txt"
        p = Path(raw)
        if not p.is_absolute():
            p = Path(_git_cwd()) / raw
        return p

    @app.route("/api/readme/check")
    def api_readme_check():
        p = _readme_path()
        return jsonify({"exists": p.exists(), "path": str(p)})

    @app.route("/api/readme/generate", methods=["POST"])
    def api_readme_generate():
        """Generate a default README.txt with app-specific content from settings."""
        import datetime
        settings  = _load_settings()
        app_name  = settings.get("APP_NAME", "Application")
        version   = settings.get("APP_VERSION", "1.0.0")
        desc      = settings.get("APP_DESCRIPTION", "").strip()
        publisher = settings.get("APP_PUBLISHER", "").strip()
        app_url   = settings.get("APP_URL", "").strip()
        support   = settings.get("APP_SUPPORT_URL", "").strip()
        exe_name  = settings.get("APP_EXE_NAME", app_name.replace(" ", ""))
        install_d = settings.get("DEFAULT_INSTALL_DIR", rf"{{autopf}}\{app_name}")
        year      = datetime.date.today().year

        content = f"""\
{"=" * 70}
  {app_name}  v{version}
{"=" * 70}

{desc or f"{app_name} — a Windows desktop application."}

  Publisher : {publisher or "Unknown"}
  Website   : {app_url   or "N/A"}
  Support   : {support   or "N/A"}
  Year      : {year}

{"─" * 70}
  TABLE OF CONTENTS
{"─" * 70}
  1. System Requirements
  2. Installation
  3. Getting Started / First Run
  4. Key Features
  5. Data & Files
  6. Uninstalling
  7. Updating
  8. Troubleshooting
  9. Support
 10. License

{"─" * 70}
  1. SYSTEM REQUIREMENTS
{"─" * 70}
  • Windows 10 or Windows 11 (64-bit recommended)
  • 100 MB free disk space (plus space for your data)
  • Internet connection (optional — required for online features only)
  • No Python or additional runtime needed — everything is bundled.

{"─" * 70}
  2. INSTALLATION
{"─" * 70}
  1. Double-click  {exe_name}_Setup_{version}.exe
  2. Follow the on-screen wizard.
  3. The default installation folder is:
       {install_d}
  4. Optionally check "Create Desktop Shortcut" for quick access.
  5. Click Finish — the application will launch automatically.

  To install silently (no GUI):
    {exe_name}_Setup_{version}.exe /VERYSILENT /SUPPRESSMSGBOXES

{"─" * 70}
  3. GETTING STARTED / FIRST RUN
{"─" * 70}
  After installation, {app_name} will:
    • Open your default web browser to http://127.0.0.1:5000
    • Run a local web server on your machine (no data sent externally)
    • Store all data in the installation folder

  If the browser does not open automatically:
    • Double-click the {app_name} desktop or Start Menu shortcut.
    • Or open http://127.0.0.1:5000 in any browser manually.

  On first run you may be prompted to configure initial settings.

{"─" * 70}
  4. KEY FEATURES
{"─" * 70}
  • Fully portable — runs entirely on your local machine
  • Browser-based interface (no external cloud, no subscriptions)
  • Persistent local database (SQLite) — your data never leaves your PC
  • Automatic browser launch on startup
  • Runs minimised in the background; close the browser tab to keep it running

{"─" * 70}
  5. DATA & FILES
{"─" * 70}
  All user data is stored in the installation directory:

    {install_d}\\
      {exe_name}.exe      — main application executable
      {exe_name.lower()}.db        — SQLite database (your data)
      {exe_name.lower()}.log       — application log file
      .env                — local configuration / secrets
      README.txt          — this file
      LICENSE.txt         — software license terms

  IMPORTANT: Back up the .db file before uninstalling or upgrading
  if you want to keep your data.

{"─" * 70}
  6. UNINSTALLING
{"─" * 70}
  Via Windows Settings:
    Settings › Apps › Installed apps › {app_name} › Uninstall

  Via Control Panel:
    Control Panel › Programs › Uninstall a program › {app_name}

  During uninstall you will be asked whether to remove your data files
  (database, logs, .env).  Choose YES to fully clean up, or NO to keep
  your data for a future reinstall.

{"─" * 70}
  7. UPDATING
{"─" * 70}
  Simply run the new {exe_name}_Setup_<version>.exe installer over the
  existing installation.  The installer will close the running app,
  replace the program files, and relaunch automatically.
  Your database and settings are preserved.

{"─" * 70}
  8. TROUBLESHOOTING
{"─" * 70}
  App does not open / browser shows "Connection Refused"
    • Wait 10–15 seconds and refresh — startup can take a moment.
    • Check the log file:  {install_d}\\{exe_name.lower()}.log
    • Ensure port 5000 is not blocked by a firewall or antivirus.
    • Try restarting the app from the Start Menu shortcut.

  App opens but shows an error page
    • Review the log file for details.
    • Reinstall over the top — this preserves your data.

  Antivirus flags the installer or exe
    • This is a false positive common with PyInstaller-built apps.
    • The source code is available at: {app_url or "the project repository"}
    • Add an exclusion in your antivirus for the installation folder.

  Port 5000 already in use
    • Another application is using port 5000.
    • Stop that application, or configure {app_name} to use a different port.

{"─" * 70}
  9. SUPPORT
{"─" * 70}
  Website : {app_url   or "N/A"}
  Issues  : {support   or "N/A"}
  Publisher: {publisher or "N/A"}

  Please include the contents of {exe_name.lower()}.log when reporting bugs.

{"─" * 70}
 10. LICENSE
{"─" * 70}
  See LICENSE.txt in the installation directory for full license terms.

  Copyright © {year} {publisher or app_name}. All rights reserved.

{"=" * 70}
"""

        try:
            p = _readme_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
            return jsonify({"ok": True, "path": str(p)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/settings", methods=["GET"])
    def api_settings_get():
        return jsonify(_load_settings())

    @app.route("/api/settings", methods=["POST"])
    def api_settings_post():
        data = request.get_json(silent=True) or {}
        _save_settings(data)
        return jsonify({"ok": True})

    # ── Project profiles ──────────────────────────────────────────────────────

    @app.route("/api/projects")
    def api_projects_list():
        from datetime import datetime as _dt
        data = _load_projects()
        result = []
        for name in data.get("recent", []):
            p = data["projects"].get(name, {})
            result.append({
                "name": name,
                "last_saved": p.get("last_saved", ""),
            })
        return jsonify({"ok": True, "projects": result, "active": data.get("active")})

    @app.route("/api/projects/save", methods=["POST"])
    def api_projects_save():
        from datetime import datetime as _dt
        body = request.get_json(silent=True) or {}
        name = body.get("name", "").strip()
        settings = body.get("settings", {})
        if not name:
            return jsonify({"ok": False, "error": "Name required"})
        data = _load_projects()
        data["projects"][name] = {
            "name": name,
            "last_saved": _dt.now().strftime("%Y-%m-%d %H:%M"),
            "settings": settings,
        }
        recent = [r for r in data.get("recent", []) if r != name]
        recent.insert(0, name)
        data["recent"] = recent[:20]
        data["active"] = name
        _save_projects(data)
        # Mirror to build_settings.json so the dashboard opens with this project
        _save_settings(settings)
        return jsonify({"ok": True})

    @app.route("/api/projects/load", methods=["POST"])
    def api_projects_load():
        body = request.get_json(silent=True) or {}
        name = body.get("name", "").strip()
        data = _load_projects()
        p = data["projects"].get(name)
        if not p:
            return jsonify({"ok": False, "error": "Project not found"})
        settings = {**_DEFAULTS, **p.get("settings", {})}
        # Bump to front of recent list
        recent = [r for r in data.get("recent", []) if r != name]
        recent.insert(0, name)
        data["recent"] = recent
        data["active"] = name
        _save_projects(data)
        # Mirror to build_settings.json
        _save_settings(settings)
        return jsonify({"ok": True, "settings": settings})

    @app.route("/api/projects/delete", methods=["POST"])
    def api_projects_delete():
        body = request.get_json(silent=True) or {}
        name = body.get("name", "").strip()
        data = _load_projects()
        data["projects"].pop(name, None)
        data["recent"] = [r for r in data.get("recent", []) if r != name]
        if data.get("active") == name:
            data["active"] = data["recent"][0] if data["recent"] else None
        _save_projects(data)
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

    def _find_text_editors():
        """Scan common paths for text editors installed on this Windows machine."""
        import shutil, glob as _glob
        editors = [{"name": "Notepad", "exe": "notepad.exe"}]

        candidates = [
            ("Notepad++",        [r"C:\Program Files\Notepad++\notepad++.exe",
                                  r"C:\Program Files (x86)\Notepad++\notepad++.exe"]),
            ("VS Code",          [os.path.expandvars(r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe"),
                                  r"C:\Program Files\Microsoft VS Code\Code.exe"]),
            ("Sublime Text",     [r"C:\Program Files\Sublime Text\sublime_text.exe",
                                  r"C:\Program Files\Sublime Text 3\sublime_text.exe",
                                  r"C:\Program Files\Sublime Text 2\sublime_text.exe"]),
            ("Notepad2",         [r"C:\Program Files\Notepad2\Notepad2.exe",
                                  r"C:\Windows\Notepad2.exe"]),
            ("WordPad",          [r"C:\Program Files\Windows NT\Accessories\wordpad.exe"]),
            ("Atom",             [os.path.expandvars(r"%LOCALAPPDATA%\atom\atom.exe")]),
            ("Geany",            [r"C:\Program Files\Geany\bin\geany.exe"]),
        ]
        for name, paths in candidates:
            for p in paths:
                if os.path.isfile(p):
                    editors.append({"name": name, "exe": p})
                    break

        # VS Code in PATH
        if not any(e["name"] == "VS Code" for e in editors):
            code = shutil.which("code")
            if code:
                editors.append({"name": "VS Code", "exe": code})

        # Microsoft Word (glob for Office version-agnostic path)
        if not any(e["name"] == "Microsoft Word" for e in editors):
            word_globs = [
                r"C:\Program Files\Microsoft Office\root\Office*\WINWORD.EXE",
                r"C:\Program Files (x86)\Microsoft Office\root\Office*\WINWORD.EXE",
                r"C:\Program Files\Microsoft Office*\WINWORD.EXE",
            ]
            for pat in word_globs:
                matches = _glob.glob(pat)
                if matches:
                    editors.append({"name": "Microsoft Word", "exe": matches[0]})
                    break

        return editors

    @app.route("/api/text-editors")
    def api_text_editors():
        return jsonify({"editors": _find_text_editors()})

    def _resolve_and_ensure(filepath: str):
        """Resolve path, auto-create empty text file if missing. Returns Path or raises."""
        p = Path(filepath)
        if not p.is_absolute():
            p = PROJECT_ROOT / p
        if not p.exists():
            if p.suffix.lower() in (".txt", ".md", ".rst", ""):
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text("", encoding="utf-8")
            else:
                raise FileNotFoundError(f"File not found: {p}")
        return p

    @app.route("/api/open-file", methods=["POST"])
    def api_open_file():
        data     = request.get_json(silent=True) or {}
        filepath = data.get("path", "").strip()
        if not filepath:
            return jsonify({"ok": False, "error": "No file path provided"})
        try:
            p = _resolve_and_ensure(filepath)
        except FileNotFoundError as e:
            return jsonify({"ok": False, "error": str(e)})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Could not create file: {e}"})

        # Try Notepad++ first, fall back to system default
        for npp in [r"C:\Program Files\Notepad++\notepad++.exe",
                    r"C:\Program Files (x86)\Notepad++\notepad++.exe"]:
            if os.path.isfile(npp):
                subprocess.Popen([npp, str(p)])
                return jsonify({"ok": True, "editor": "Notepad++"})
        os.startfile(str(p))
        return jsonify({"ok": True, "editor": "system default"})

    @app.route("/api/open-file-with", methods=["POST"])
    def api_open_file_with():
        """Open a file with a specific editor exe chosen by the user."""
        data        = request.get_json(silent=True) or {}
        filepath    = data.get("path", "").strip()
        editor_exe  = data.get("editor", "").strip()
        editor_name = data.get("editor_name", "editor").strip()
        if not filepath:
            return jsonify({"ok": False, "error": "No file path provided"})
        try:
            p = _resolve_and_ensure(filepath)
        except FileNotFoundError as e:
            return jsonify({"ok": False, "error": str(e)})
        except Exception as e:
            return jsonify({"ok": False, "error": f"Could not create file: {e}"})
        try:
            if editor_exe:
                subprocess.Popen([editor_exe, str(p)])
            else:
                os.startfile(str(p))
            return jsonify({"ok": True, "editor": editor_name or editor_exe or "system default"})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/open-folder", methods=["POST"])
    def api_open_folder():
        data    = request.get_json(silent=True) or {}
        folder  = data.get("path", "").strip()
        if not folder:
            # Default to the configured output dir
            settings = _load_settings()
            folder = str(_resolve_output_dir(settings))
        p = Path(folder)
        if not p.is_absolute():
            settings = _load_settings()
            base = Path(settings.get("GIT_REPO_DIR", "").strip() or str(PROJECT_ROOT))
            p = base / folder
        # Open Explorer in the foreground
        try:
            target = str(p if p.exists() else (p.parent if p.parent.exists() else PROJECT_ROOT))
            _open_explorer_foreground(target)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

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

        data        = request.get_json(silent=True) or {}
        mode        = data.get("mode", "full")
        build_type  = data.get("build_type", "Main").strip()   # "Main"|"Incremental"|"HOTFIX"
        description = data.get("description", "").strip()
        settings    = data.get("settings")
        if settings:
            _save_settings(settings)

        # Capture git config at request time (before the thread runs)
        loaded      = _load_settings()

        build_py = str(BUILD_DIR / "build.py")
        cmd = [sys.executable, build_py]
        # When a Project Profile points at a different project, pass its root so
        # build.py compiles that project instead of job_tracker.
        _repo_dir = loaded.get("GIT_REPO_DIR", "").strip()
        if _repo_dir and Path(_repo_dir).is_dir() and Path(_repo_dir).resolve() != PROJECT_ROOT.resolve():
            cmd += ["--project-dir", _repo_dir]
        if mode == "bundle-only":
            cmd.append("--bundle-only")
        elif mode == "installer-only":
            cmd.append("--installer-only")
        remote_url  = loaded.get("GIT_REMOTE_URL",  "").strip()
        dev_branch  = loaded.get("GIT_DEV_BRANCH",  "development").strip() or "development"
        main_branch = loaded.get("GIT_MAIN_BRANCH", "main").strip() or "main"
        use_git     = bool(remote_url) and mode == "full"
        ver_increment = loaded.get("VERSION_INCREMENT", "keep").strip()  # keep|patch|minor|major
        ver_next      = loaded.get("APP_VERSION", "1.0.0").strip()

        def _run_full_build() -> None:
            import datetime as _bdt
            _build_start = _bdt.datetime.now()
            cwd = _git_cwd()

            # ── Build initiated header (visible in display log and .log file) ──
            mode_label = {"full": "Full Build", "bundle-only": "Bundle Only"}.get(mode, mode.title())
            exe_name   = loaded.get("APP_EXE_NAME", "").strip() or loaded.get("APP_NAME", "app").strip()
            version    = loaded.get("APP_VERSION", "1.0.0").strip()
            SEP = "=" * 56
            _append(SEP)
            _append(f"  BUILD STARTED: {mode_label}")
            _append(f"  App     : {exe_name}  v{version}")
            _append(f"  Started : {_build_start.strftime('%Y-%m-%d %H:%M:%S')}")
            _append(SEP)

            # ── Kill any running instances of the app before building ──
            _app_port = int(loaded.get("APP_PORT", "5000") or "5000")
            _kill_running_app(loaded.get("APP_EXE_NAME", "").strip(), port=_app_port)

            # ── Pre-create output directory structure ──────────────────────
            _out_dir = _resolve_output_dir(loaded)
            (_out_dir / "Bundle Only").mkdir(parents=True, exist_ok=True)
            (_out_dir / "Log").mkdir(parents=True, exist_ok=True)
            _append(f"Output dir : {_out_dir}")

            # ── Virus / malware scan of source before compile ──────────────
            if not _do_virus_scan(cwd):
                return  # threat found; _do_virus_scan already set status to "error"

            # ── Version file update (on dev branch, before checkout main) ──
            vf = Path(cwd) / "VERSION"
            if ver_increment != "keep":
                current_ver = vf.read_text(encoding="utf-8").strip() if vf.is_file() else "1.0.0"
                new_ver = _bump_version(current_ver, ver_increment)
                _append(f"Version: {current_ver} → {new_ver} ({ver_increment} bump)")
            else:
                new_ver = ver_next
                _append(f"Version: {new_ver} (keeping as-is)")
            try:
                vf.write_text(new_ver + "\n", encoding="utf-8")
                settings_now = _load_settings()
                settings_now["APP_VERSION"] = new_ver
                _save_settings(settings_now)
                # ── Changelog: check trigger and promote draft → version entry ──
                try:
                    import datetime as _dt
                    _cl_settings = _load_settings()
                    cl_data     = _load_changelog(_cl_settings)
                    cl_trigger  = _cl_settings.get("CHANGELOG_TRIGGER", "minor")
                    old_ver_cl  = current_ver if ver_increment != "keep" else new_ver
                    triggered   = _version_crossed_trigger(old_ver_cl, new_ver, cl_trigger)

                    # Ensure a version entry exists for new_ver
                    existing = {e.get("version") for e in cl_data.get("entries", [])}
                    if new_ver not in existing:
                        new_entry: dict = {
                            "version": new_ver,
                            "date":    _dt.date.today().isoformat(),
                            "changes": [],
                        }
                        if triggered and cl_data.get("draft"):
                            new_entry["changes"] = list(cl_data["draft"])
                            cl_data["draft"]     = []
                            _append(f"CHANGELOG: trigger={cl_trigger} fired — "
                                    f"{len(new_entry['changes'])} draft item(s) "
                                    f"promoted to v{new_ver}")
                        else:
                            if cl_data.get("draft"):
                                _append(f"CHANGELOG: {len(cl_data['draft'])} draft item(s) "
                                        f"pending (trigger={cl_trigger} not met for "
                                        f"{old_ver_cl}→{new_ver})")
                            else:
                                _append(f"CHANGELOG: no draft items — "
                                        f"entry for v{new_ver} created (empty)")
                        cl_data.setdefault("entries", []).insert(0, new_entry)
                        _save_changelog(cl_data, _cl_settings)
                    else:
                        _append(f"CHANGELOG: entry for v{new_ver} already exists")
                except Exception as cl_exc:
                    _append(f"[WARN] Could not update CHANGELOG.json: {cl_exc}")
            except Exception as exc:
                _append(f"[WARN] Could not write VERSION file: {exc}")

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

            # Write timestamped build log file to output dir, announce path in log
            _log_file = _write_build_log_file(mode, loaded, _build_start)
            if _log_file:
                _append("")
                _append("=" * 56)
                _append(f"[OK] Build log saved: {_log_file}")
                _append("=" * 56)

            # ── Record this build in build_history.json ──────────────────────
            with _build_lock:
                _build_succeeded = (_build_status == "done")
            _append_build_history(
                version=new_ver,
                build_type=build_type,
                mode=mode,
                description=description,
                git_commit=_current_git_commit(_git_cwd()) if use_git else "",
                success=_build_succeeded,
                settings=loaded,
            )

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

        data       = request.get_json(silent=True) or {}
        clean_mode = data.get("clean_mode", "all")  # "temp" | "output" | "all"

        t = threading.Thread(target=lambda: _run_clean(clean_mode), daemon=True)
        t.start()
        return jsonify({"ok": True})

    @app.route("/api/build/clean-logs", methods=["POST"])
    def api_build_clean_logs():
        """Delete every file inside the output Log/ subdirectory."""
        settings = _load_settings()
        log_dir  = _resolve_output_dir(settings) / "Log"

        deleted = 0
        errors  = []
        if log_dir.exists():
            for f in log_dir.iterdir():
                try:
                    if f.is_file():
                        f.unlink()
                        deleted += 1
                    elif f.is_dir():
                        shutil.rmtree(f, ignore_errors=True)
                        deleted += 1
                except Exception as e:
                    errors.append(str(e))
        else:
            return jsonify({"ok": True, "deleted": 0, "note": "Log directory does not exist"})

        if errors:
            return jsonify({"ok": False, "deleted": deleted, "errors": errors})
        return jsonify({"ok": True, "deleted": deleted, "log_dir": str(log_dir)})

    @app.route("/api/build/log")
    def api_build_log():
        offset = int(request.args.get("offset", 0))
        with _build_lock:
            lines  = _build_log[offset:]
            status = _build_status
            total  = len(_build_log)
        return jsonify({"lines": lines, "status": status, "total": total})

    # ── Build test (run compiled exe, test-install) ───────────────────────────

    @app.route("/api/build/test-run", methods=["POST"])
    def api_build_test_run():
        """Launch compiled exe from Bundle Only/ subdir, poll port — verbose log + pipeline log file."""
        import time, urllib.request, datetime
        settings   = _load_settings()
        exe_name   = settings.get("APP_EXE_NAME", "app").strip()
        port       = int(settings.get("APP_PORT", "5000") or "5000")
        output_dir = _resolve_output_dir(settings)
        (output_dir / "Bundle Only").mkdir(parents=True, exist_ok=True)
        (output_dir / "Log").mkdir(parents=True, exist_ok=True)
        bundle     = output_dir / "Bundle Only" / exe_name
        exe_path   = bundle / f"{exe_name}.exe"

        _now   = datetime.datetime.now
        def _ts(): return _now().strftime("%H:%M:%S.%f")[:-3]
        vlog   = []
        def V(tag, msg): vlog.append(f"[{_ts()}] [{tag:<7}]  {msg}")

        date_str          = _now().strftime("%Y-%m-%d_%H-%M-%S")
        pipeline_log_path = output_dir / "Log" / f"{exe_name}_Output_Pipeline_{date_str}.log"

        def _flush(status_label):
            try:
                pipeline_log_path.parent.mkdir(parents=True, exist_ok=True)
                SEP    = "=" * 72
                errors = [l for l in vlog if "[ERROR]" in l or "[STDERR]" in l]
                warns  = [l for l in vlog if "[WARN]"  in l]
                header = [
                    SEP,
                    f"  Build Dashboard — Test Output Log",
                    f"  App       : {exe_name}",
                    f"  Mode      : Bundle Run (test-run)",
                    f"  Result    : {status_label}",
                    f"  Timestamp : {_now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"  Log file  : {pipeline_log_path}",
                    SEP,
                    "",
                ]
                if errors or warns:
                    summary = ["--- SUMMARY ---"]
                    for e in errors: summary.append(f"  {e.strip()}")
                    for w in warns:  summary.append(f"  {w.strip()}")
                    summary += ["--- END SUMMARY ---", ""]
                else:
                    summary = []
                footer = ["", SEP, f"  Test {status_label}: Bundle Run  |  {exe_name}  |  {_now().strftime('%Y-%m-%d %H:%M:%S')}", SEP, ""]
                pipeline_log_path.write_text(
                    "\n".join(header)
                    + ("\n".join(summary) if summary else "")
                    + "--- TEST OUTPUT ---\n"
                    + "\n".join(vlog)
                    + "\n--- END TEST OUTPUT ---\n"
                    + "\n".join(footer),
                    encoding="utf-8"
                )
            except Exception:
                pass

        # Build SSL context for polling — use mkcert CA if available
        import ssl as _ssl
        def _mk_ssl_ctx():
            ctx = _ssl.create_default_context()
            if _MKCERT_CA.is_file():
                ctx.load_verify_locations(str(_MKCERT_CA))
            else:
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
            return ctx
        _ssl_ctx = _mk_ssl_ctx()

        # Determine scheme: mirror what run.py does — HTTPS only if the bundle
        # itself contains certs/localhost.pem next to the exe.
        # Do NOT use _LOCAL_CERT (source-tree certs) — those are irrelevant to
        # the compiled exe which has its own working directory.
        _bundle_has_certs = (bundle / "certs" / "localhost.pem").exists()
        _scheme = "https" if _bundle_has_certs else "http"
        _poll_url = f"{_scheme}://127.0.0.1:{port}"

        V("START",  "=== Bundle Run Test Pipeline ===")
        V("INFO",   f"Exe      : {exe_path}")
        V("INFO",   f"Poll URL : {_poll_url}")
        V("INFO",   f"Log file : {pipeline_log_path}")

        if not exe_path.exists():
            V("WARN",  f"Exact path not found: {exe_path}")
            bundle_only_dir = output_dir / "Bundle Only"
            V("INFO",  f"Searching {bundle_only_dir} for any matching exe…")
            candidates = sorted(bundle_only_dir.rglob(f"{exe_name}.exe")) if bundle_only_dir.is_dir() else []
            if not candidates:
                candidates = [
                    p for p in bundle_only_dir.rglob("*.exe")
                    if p.stem.lower() == exe_name.lower()
                ] if bundle_only_dir.is_dir() else []
            if candidates:
                exe_path = candidates[0]
                bundle   = exe_path.parent
                V("LOCATE", f"Found at : {exe_path}")
            else:
                if bundle_only_dir.is_dir():
                    contents = [str(p) for p in bundle_only_dir.rglob("*.exe")]
                    if contents:
                        V("INFO", f"Other .exe files found in {bundle_only_dir}:")
                        for c in contents[:10]:
                            V("INFO", f"  {c}")
                    else:
                        V("INFO", f"{bundle_only_dir} contains no .exe files.")
                else:
                    V("INFO", f"{bundle_only_dir} does not exist.")
                V("ERROR", f"Exe '{exe_name}.exe' not found in Bundle Only/")
                V("HINT",  "Run a 'Bundle Only' build first.")
                _flush("FAIL — exe not found")
                return jsonify({"ok": False, "step": "locate",
                                "error": f"Exe not found: {exe_path}. Run a Bundle build first.",
                                "verbose_log": vlog, "pipeline_log": str(pipeline_log_path)})

        V("LOCATE", f"Found    : {exe_path}")
        V("LOCATE", f"Size     : {exe_path.stat().st_size:,} bytes")
        V("LOCATE", f"Modified : {datetime.datetime.fromtimestamp(exe_path.stat().st_mtime):%Y-%m-%d %H:%M:%S}")

        # Kill any existing process on the port
        V("PREP",   f"Clearing port {port}…")
        try:
            kr = subprocess.run(
                ["powershell", "-Command",
                 f"$p = (Get-NetTCPConnection -LocalPort {port} -EA SilentlyContinue).OwningProcess; "
                 f"if ($p) {{ $p | % {{ Stop-Process -Id $_ -Force -EA SilentlyContinue }}; "
                 f"Write-Output \"Killed PID(s): $($p -join ', ')\" }} else {{ Write-Output 'Port already free.' }}"],
                capture_output=True, text=True, timeout=10
            )
            V("PREP", kr.stdout.strip() or "Port cleared.")
        except Exception as ex:
            V("WARN",  f"Port-clear non-fatal: {ex}")

        # Launch
        V("LAUNCH", f"Spawning : {exe_path}")
        try:
            proc = subprocess.Popen(
                [str(exe_path)], cwd=str(bundle),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            V("LAUNCH", f"PID      : {proc.pid}")
        except Exception as e:
            V("ERROR", f"Launch failed: {e}")
            _flush("FAIL — launch error")
            return jsonify({"ok": False, "step": "launch", "error": str(e),
                            "verbose_log": vlog, "pipeline_log": str(pipeline_log_path)})

        url         = _poll_url
        success     = False
        responded_at = -1
        V("CONNECT", f"Polling {url} — up to 20 attempts (1 s interval)…")
        for i in range(20):
            time.sleep(1)
            try:
                _ctx_arg = {"context": _ssl_ctx} if url.startswith("https") else {}
                resp = urllib.request.urlopen(url, timeout=2, **_ctx_arg)
                V("CONNECT", f"  [{i+1:02d}/20] HTTP {resp.status} OK — responded in {i+1}s ✓")
                success      = True
                responded_at = i + 1
                break
            except Exception as pe:
                V("POLL",   f"  [{i+1:02d}/20] No response — {type(pe).__name__}: {pe}")

        # Collect full app log
        V("LOG",    "Scanning bundle dir for app log files…")
        log_lines = []
        for log_name in [f"{exe_name.lower()}.log", "job_tracker.log", "app.log"]:
            lp = bundle / log_name
            if lp.exists():
                try:
                    content   = lp.read_text(errors="replace")
                    log_lines = content.splitlines()
                    V("LOG",  f"Found : {lp} ({len(log_lines)} lines, {lp.stat().st_size:,} bytes)")
                except Exception as le:
                    V("WARN", f"Could not read {lp}: {le}")
                break
        if not log_lines:
            V("LOG", "No app log file found in bundle directory.")
        else:
            V("LOG", "--- app log (full) ---")
            for ln in log_lines:
                vlog.append(f"  {ln}")
            V("LOG", "--- end app log ---")

        # Collect process stderr (non-blocking best-effort)
        try:
            proc.stdout.close()
            raw_err = b""
            import select as _sel
            if hasattr(_sel, "select"):
                ready = _sel.select([proc.stderr], [], [], 0.5)[0]
                if ready:
                    raw_err = proc.stderr.read(65536)
            proc.stderr.close()
            if raw_err:
                V("STDERR", "--- process stderr ---")
                for ln in raw_err.decode("utf-8", errors="replace").splitlines()[:80]:
                    vlog.append(f"  {ln}")
                V("STDERR", "--- end stderr ---")
        except Exception:
            pass

        if success:
            V("VERIFY",  f"Port {port} verified responding.")
            V("REPORT",  f"PASS — bundle test completed. App online after {responded_at}s.")
            _flush(f"PASS  (responded in {responded_at}s)")
            return jsonify({"ok": True, "step": "running", "url": url,
                            "log": log_lines[-20:], "verbose_log": vlog,
                            "pipeline_log": str(pipeline_log_path),
                            "message": f"App responded on {url} after {responded_at}s"})

        V("ERROR",  f"TIMEOUT — port {port} did not respond within 20 s.")
        V("REPORT", "FAIL — bundle test failed.")
        _flush("FAIL  (timeout after 20 s)")
        return jsonify({"ok": False, "step": "timeout", "log": log_lines[-30:],
                        "verbose_log": vlog, "pipeline_log": str(pipeline_log_path),
                        "error": f"App did not respond on {url} within 20 s"})

    @app.route("/api/build/test-install", methods=["POST"])
    def api_build_test_install():
        """Run the installer silently, launch installed exe, poll port — verbose log + pipeline log file."""
        import glob, time, urllib.request, tempfile, datetime
        settings   = _load_settings()
        exe_name   = settings.get("APP_EXE_NAME", "app").strip()
        version    = settings.get("APP_VERSION", "1.0.0").strip()
        port       = int(settings.get("APP_PORT", "5000") or "5000")
        output_dir = _resolve_output_dir(settings)
        (output_dir / "Bundle Only").mkdir(parents=True, exist_ok=True)
        (output_dir / "Log").mkdir(parents=True, exist_ok=True)

        _now  = datetime.datetime.now
        def _ts(): return _now().strftime("%H:%M:%S.%f")[:-3]
        vlog  = []
        def V(tag, msg): vlog.append(f"[{_ts()}] [{tag:<7}]  {msg}")

        date_str          = _now().strftime("%Y-%m-%d_%H-%M-%S")
        pipeline_log_path = output_dir / "Log" / f"{exe_name}_Output_Pipeline_{date_str}.log"

        def _flush(status_label):
            try:
                pipeline_log_path.parent.mkdir(parents=True, exist_ok=True)
                SEP    = "=" * 72
                errors = [l for l in vlog if "[ERROR]" in l or "[STDERR]" in l]
                warns  = [l for l in vlog if "[WARN]"  in l]
                header = [
                    SEP,
                    f"  Build Dashboard — Test Output Log",
                    f"  App       : {exe_name}  v{version}",
                    f"  Mode      : Full-Build Install Test (test-install)",
                    f"  Result    : {status_label}",
                    f"  Timestamp : {_now().strftime('%Y-%m-%d %H:%M:%S')}",
                    f"  Log file  : {pipeline_log_path}",
                    SEP,
                    "",
                ]
                if errors or warns:
                    summary = ["--- SUMMARY ---"]
                    for e in errors: summary.append(f"  {e.strip()}")
                    for w in warns:  summary.append(f"  {w.strip()}")
                    summary += ["--- END SUMMARY ---", ""]
                else:
                    summary = []
                footer = ["", SEP, f"  Test {status_label}: Install Test  |  {exe_name} v{version}  |  {_now().strftime('%Y-%m-%d %H:%M:%S')}", SEP, ""]
                pipeline_log_path.write_text(
                    "\n".join(header)
                    + ("\n".join(summary) if summary else "")
                    + "--- TEST OUTPUT ---\n"
                    + "\n".join(vlog)
                    + "\n--- END TEST OUTPUT ---\n"
                    + "\n".join(footer),
                    encoding="utf-8"
                )
            except Exception:
                pass

        # Build SSL context for polling
        import ssl as _ssl
        def _mk_ssl_ctx():
            ctx = _ssl.create_default_context()
            if _MKCERT_CA.is_file():
                ctx.load_verify_locations(str(_MKCERT_CA))
            else:
                ctx.check_hostname = False
                ctx.verify_mode = _ssl.CERT_NONE
            return ctx
        _ssl_ctx = _mk_ssl_ctx()
        # Mirror what run.py does: HTTPS only when the bundle contains certs.
        # The bundle used to create the installer is in Bundle Only/<exe_name>/.
        _inst_bundle = output_dir / "Bundle Only" / exe_name
        _inst_has_certs = (_inst_bundle / "certs" / "localhost.pem").exists()
        _scheme   = "https" if _inst_has_certs else "http"
        _poll_url = f"{_scheme}://127.0.0.1:{port}"

        V("START",  "=== Full-Build Install Test Pipeline ===")
        V("INFO",   f"App      : {exe_name}  v{version}")
        V("INFO",   f"Out dir  : {output_dir}")
        V("INFO",   f"Poll URL : {_poll_url}")
        V("INFO",   f"Log file : {pipeline_log_path}")

        # Find installer
        installer = output_dir / f"{exe_name}_Setup_{version}.exe"
        if not installer.exists():
            candidates = sorted(output_dir.glob(f"{exe_name}_Setup_*.exe"), reverse=True)
            installer  = candidates[0] if candidates else None
        if not installer or not installer.exists():
            V("ERROR", f"Installer not found in: {output_dir}")
            V("HINT",  "Run a 'Full Build' first.")
            _flush("FAIL — installer not found")
            return jsonify({"ok": False, "step": "locate",
                            "error": f"Installer not found in {output_dir}. Run a Full Build first.",
                            "verbose_log": vlog, "pipeline_log": str(pipeline_log_path)})

        V("LOCATE", f"Found    : {installer}")
        V("LOCATE", f"Size     : {installer.stat().st_size:,} bytes")
        V("LOCATE", f"Modified : {datetime.datetime.fromtimestamp(installer.stat().st_mtime):%Y-%m-%d %H:%M:%S}")

        _local_appdata = Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
        test_dir = _local_appdata / "_bd_test" / exe_name
        test_dir.mkdir(parents=True, exist_ok=True)
        V("PREP",   f"Test install dir : {test_dir}")

        # Kill any process on the port
        V("PREP",   f"Clearing port {port}…")
        try:
            kr = subprocess.run(
                ["powershell", "-Command",
                 f"$p = (Get-NetTCPConnection -LocalPort {port} -EA SilentlyContinue).OwningProcess; "
                 f"if ($p) {{ $p | % {{ Stop-Process -Id $_ -Force -EA SilentlyContinue }}; "
                 f"Write-Output \"Killed PID(s): $($p -join ', ')\" }} else {{ Write-Output 'Port already free.' }}"],
                capture_output=True, text=True, timeout=10
            )
            V("PREP", kr.stdout.strip() or "Port cleared.")
        except Exception as ex:
            V("WARN",  f"Port-clear non-fatal: {ex}")

        # Run installer silently — use PowerShell Start-Process -Verb RunAs so UAC
        # elevation is granted even when the installer requires admin privileges.
        V("INSTALL", f"Running installer (elevated): /VERYSILENT /DIR={test_dir}")
        V("INSTALL", "A UAC prompt will appear — please approve to continue the test.")
        install_start = _now()
        try:
            # Write exit code to a temp file because Start-Process -Verb RunAs
            # spawns an elevated child that subprocess cannot capture directly.
            import tempfile as _tf
            ec_file = Path(_tf.mktemp(suffix=".txt"))
            _ec_ps  = str(ec_file).replace("\\", "\\\\")
            _ins_ps = str(installer).replace("\\", "\\\\")
            _dir_ps = str(test_dir).replace("\\", "\\\\")
            ps_cmd = (
                f"$p = Start-Process -FilePath '{_ins_ps}' "
                f"-ArgumentList '/VERYSILENT','/SUPPRESSMSGBOXES','/DIR={_dir_ps}','/NORESTART' "
                f"-Verb RunAs -Wait -PassThru; "
                f"$ec = if ($null -eq $p.ExitCode) {{ 0 }} else {{ [int]$p.ExitCode }}; "
                f"$ec | Out-File -FilePath '{_ec_ps}' -Encoding ascii -NoNewline"
            )
            ir = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps_cmd],
                capture_output=True, text=True, timeout=180
            )
            # Read exit code written by the elevated process
            exit_code = ir.returncode  # fallback
            if ec_file.exists():
                try:
                    exit_code = int(ec_file.read_text(encoding="ascii").strip())
                except ValueError:
                    pass
                ec_file.unlink(missing_ok=True)
            elapsed = (_now() - install_start).total_seconds()
            V("INSTALL", f"Installer exited in {elapsed:.1f}s — return code: {exit_code}")
            if ir.stdout.strip():
                V("INSTALL", f"stdout: {ir.stdout.strip()[:500]}")
            if ir.stderr.strip():
                V("INSTALL", f"stderr: {ir.stderr.strip()[:500]}")
            if exit_code not in (0, 1):   # Inno Setup: 0=ok, 1=success with reboot
                V("WARN",   f"Non-zero exit code: {exit_code} (may still have installed)")
        except subprocess.TimeoutExpired:
            V("ERROR", "Installer timed out after 180 s.")
            _flush("FAIL — installer timeout")
            return jsonify({"ok": False, "step": "install", "error": "Installer timed out after 180 s",
                            "verbose_log": vlog, "pipeline_log": str(pipeline_log_path)})
        except Exception as e:
            V("ERROR", f"Installer error: {e}")
            _flush("FAIL — installer error")
            return jsonify({"ok": False, "step": "install", "error": str(e),
                            "verbose_log": vlog, "pipeline_log": str(pipeline_log_path)})

        installed_exe = test_dir / f"{exe_name}.exe"
        if not installed_exe.exists():
            # Try one level deeper
            found = list(test_dir.rglob(f"{exe_name}.exe"))
            installed_exe = found[0] if found else installed_exe
        if not installed_exe.exists():
            V("ERROR", f"Installed exe not found at: {installed_exe}")
            V("INFO",  f"Contents of {test_dir}: {list(test_dir.iterdir())[:20]}")
            _flush("FAIL — installed exe not found")
            return jsonify({"ok": False, "step": "install",
                            "error": f"Installer finished but exe not found at {installed_exe}",
                            "verbose_log": vlog, "pipeline_log": str(pipeline_log_path)})

        V("INSTALL", f"Installed exe : {installed_exe}")

        # Launch installed exe
        V("LAUNCH", f"Spawning : {installed_exe}")
        try:
            proc = subprocess.Popen(
                [str(installed_exe)], cwd=str(installed_exe.parent),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)
            )
            V("LAUNCH", f"PID      : {proc.pid}")
        except Exception as e:
            V("ERROR", f"Launch failed: {e}")
            _flush("FAIL — launch error")
            return jsonify({"ok": False, "step": "launch", "error": str(e),
                            "verbose_log": vlog, "pipeline_log": str(pipeline_log_path)})

        url         = _poll_url
        success     = False
        responded_at = -1
        V("CONNECT", f"Polling {url} — up to 25 attempts (1 s interval)…")
        for i in range(25):
            time.sleep(1)
            try:
                _ctx_arg = {"context": _ssl_ctx} if url.startswith("https") else {}
                resp = urllib.request.urlopen(url, timeout=2, **_ctx_arg)
                V("CONNECT", f"  [{i+1:02d}/25] HTTP {resp.status} OK — responded in {i+1}s ✓")
                success      = True
                responded_at = i + 1
                break
            except Exception as pe:
                V("POLL",   f"  [{i+1:02d}/25] No response — {type(pe).__name__}: {pe}")

        # Collect full app log from test_dir
        V("LOG",    f"Scanning install dir for app log files: {test_dir}")
        log_lines = []
        for log_name in [f"{exe_name.lower()}.log", "job_tracker.log", "app.log"]:
            lp = test_dir / log_name
            if not lp.exists():
                lp = installed_exe.parent / log_name
            if lp.exists():
                try:
                    content   = lp.read_text(errors="replace")
                    log_lines = content.splitlines()
                    V("LOG",  f"Found : {lp} ({len(log_lines)} lines, {lp.stat().st_size:,} bytes)")
                except Exception as le:
                    V("WARN", f"Could not read {lp}: {le}")
                break
        if not log_lines:
            V("LOG", "No app log found in install directory.")
        else:
            V("LOG", "--- app log (full) ---")
            for ln in log_lines:
                vlog.append(f"  {ln}")
            V("LOG", "--- end app log ---")

        # Collect process stderr (best-effort)
        try:
            proc.stdout.close()
            raw_err = b""
            import select as _sel
            if hasattr(_sel, "select"):
                ready = _sel.select([proc.stderr], [], [], 0.5)[0]
                if ready:
                    raw_err = proc.stderr.read(65536)
            proc.stderr.close()
            if raw_err:
                V("STDERR", "--- process stderr ---")
                for ln in raw_err.decode("utf-8", errors="replace").splitlines()[:80]:
                    vlog.append(f"  {ln}")
                V("STDERR", "--- end stderr ---")
        except Exception:
            pass

        if success:
            V("VERIFY",  f"Port {port} verified responding.")
            V("REPORT",  f"PASS — install test completed. App online after {responded_at}s.")
            _flush(f"PASS  (responded in {responded_at}s)")
            return jsonify({"ok": True, "step": "running", "url": url,
                            "install_dir": str(test_dir), "log": log_lines[-20:],
                            "verbose_log": vlog, "pipeline_log": str(pipeline_log_path),
                            "message": f"Installed app responded on {url} after {responded_at}s"})

        V("ERROR",  f"TIMEOUT — port {port} did not respond within 25 s.")
        V("REPORT", "FAIL — install test failed.")
        _flush("FAIL  (timeout after 25 s)")
        return jsonify({"ok": False, "step": "timeout",
                        "install_dir": str(test_dir), "log": log_lines[-30:],
                        "verbose_log": vlog, "pipeline_log": str(pipeline_log_path),
                        "error": f"Installed app did not respond on {url} within 25 s"})

    @app.route("/api/build/kill-app", methods=["POST"])
    def api_build_kill_app():
        """Kill any process listening on the app's configured port."""
        settings = _load_settings()
        port = int(settings.get("APP_PORT", "5000") or "5000")
        try:
            subprocess.run(
                ["powershell", "-Command",
                 f"Get-NetTCPConnection -LocalPort {port} -ErrorAction SilentlyContinue "
                 f"| Select-Object -ExpandProperty OwningProcess "
                 f"| ForEach-Object {{ Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue }}"],
                capture_output=True, timeout=10
            )
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    # ── Build history ─────────────────────────────────────────────────────────

    @app.route("/api/build/history")
    def api_build_history():
        """Return the list of recorded builds (newest first)."""
        settings = _load_settings()
        path = _build_history_path(settings)
        if not path.exists():
            return jsonify({"ok": True, "history": []})
        try:
            entries = json.loads(path.read_text("utf-8"))
            return jsonify({"ok": True, "history": entries})
        except Exception as e:
            return jsonify({"ok": False, "history": [], "error": str(e)})

    @app.route("/api/build/history/restore", methods=["POST"])
    def api_build_history_restore():
        """Restore settings from a historical build so the user can rebuild it.
        Returns the mode, version, type and description from that build entry."""
        data  = request.get_json(silent=True) or {}
        index = int(data.get("index", 0))

        settings = _load_settings()
        path     = _build_history_path(settings)
        try:
            entries = json.loads(path.read_text("utf-8"))
            entry   = entries[index]
        except Exception as e:
            return jsonify({"ok": False, "error": f"History entry not found: {e}"})

        snapshot = entry.get("settings_snapshot", {})
        if snapshot:
            settings.update(snapshot)
            settings["APP_VERSION"]       = entry.get("version", settings.get("APP_VERSION"))
            settings["VERSION_INCREMENT"] = "keep"   # lock version — do not auto-bump
            _save_settings(settings)

        return jsonify({
            "ok":          True,
            "mode":        entry.get("mode", "full"),
            "version":     entry.get("version", ""),
            "build_type":  entry.get("build_type", "Main"),
            "description": entry.get("description", ""),
        })

    # ── Build Dashboard own changelog ─────────────────────────────────────────

    @app.route("/api/bd-changelog")
    def api_bd_changelog():
        """Return the Build Dashboard's own What's New changelog."""
        path = BUILD_DIR / "bd_changelog.json"
        if not path.exists():
            return jsonify({"ok": True, "entries": []})
        try:
            raw     = json.loads(path.read_text("utf-8"))
            entries = raw if isinstance(raw, list) else raw.get("entries", [])
            return jsonify({"ok": True, "entries": entries})
        except Exception as e:
            return jsonify({"ok": False, "entries": [], "error": str(e)})

    # ── Changelog ─────────────────────────────────────────────────────────────

    @app.route("/api/changelog")
    def api_changelog_get():
        settings = _load_settings()
        data = _load_changelog(settings)
        # Merge the trigger from settings (single source of truth)
        data["trigger"] = settings.get("CHANGELOG_TRIGGER", "minor")
        return jsonify({"ok": True, "changelog": data})

    @app.route("/api/changelog/draft", methods=["PATCH"])
    def api_changelog_draft():
        """Save the draft change list (list of strings or newline-separated text)."""
        body  = request.get_json(silent=True) or {}
        raw   = body.get("draft", [])
        if isinstance(raw, str):
            items = [l.lstrip("- ").strip() for l in raw.splitlines() if l.strip()]
        else:
            items = [str(s).strip() for s in raw if str(s).strip()]
        settings = _load_settings()
        data = _load_changelog(settings)
        data["draft"] = items
        _save_changelog(data, settings)
        return jsonify({"ok": True, "draft": items})

    @app.route("/api/changelog/trigger", methods=["PATCH"])
    def api_changelog_trigger():
        body    = request.get_json(silent=True) or {}
        trigger = body.get("trigger", "minor")
        if trigger not in ("major", "minor", "patch", "never"):
            return jsonify({"ok": False, "error": "Invalid trigger value"})
        settings = _load_settings()
        settings["CHANGELOG_TRIGGER"] = trigger
        _save_settings(settings)
        return jsonify({"ok": True, "trigger": trigger})

    @app.route("/api/changelog/open-editor", methods=["POST"])
    def api_changelog_open_editor():
        """Export CHANGELOG.json → CHANGELOG.md then open in Notepad++ (or Notepad)."""
        settings = _load_settings()
        data     = _load_changelog(settings)
        md_path  = _changelog_md_path(settings)
        try:
            md_path.write_text(_generate_changelog_md(data), "utf-8")
        except Exception as e:
            return jsonify({"ok": False, "error": f"Could not write CHANGELOG.md: {e}"})

        editor = _find_notepadpp() or "notepad.exe"
        try:
            subprocess.Popen([editor, str(md_path)])
            return jsonify({"ok": True, "editor": editor, "file": str(md_path)})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/changelog/reload-md", methods=["POST"])
    def api_changelog_reload_md():
        """Parse CHANGELOG.md back into CHANGELOG.json (called after user saves in Notepad++)."""
        settings = _load_settings()
        md_path  = _changelog_md_path(settings)
        if not md_path.exists():
            return jsonify({"ok": False, "error": "CHANGELOG.md not found — open editor first"})
        try:
            parsed = _parse_changelog_md(md_path.read_text("utf-8"))
        except Exception as e:
            return jsonify({"ok": False, "error": f"Parse error: {e}"})

        data = _load_changelog(settings)
        data["draft"]   = parsed.get("draft", [])
        data["entries"] = parsed.get("entries", [])
        _save_changelog(data, settings)
        return jsonify({"ok": True, "changelog": data})

    # ── Settings PATCH (single-key update) ───────────────────────────────────

    @app.route("/api/settings", methods=["PATCH"])
    def api_settings_patch():
        data = request.get_json(silent=True) or {}
        current = _load_settings()
        current.update(data)
        _save_settings(current)
        return jsonify({"ok": True})

    # ── Version control ───────────────────────────────────────────────────────

    def _version_file_path() -> Path:
        cwd = _git_cwd()
        return Path(cwd) / "VERSION"

    def _read_version_from_branch(branch: str) -> str:
        """Read VERSION file content from a specific git branch."""
        cwd = _git_cwd()
        try:
            r = subprocess.run(
                ["git", "show", f"{branch}:VERSION"],
                capture_output=True, text=True, cwd=cwd, timeout=5
            )
            if r.returncode == 0:
                return r.stdout.strip() or "1.0.0"
        except Exception:
            pass
        # Fall back to local file
        vf = _version_file_path()
        if vf.is_file():
            return vf.read_text(encoding="utf-8").strip() or "1.0.0"
        return "1.0.0"

    def _bump_version(version: str, part: str) -> str:
        """Increment major, minor, or patch in a semver string."""
        parts = version.split(".")
        while len(parts) < 3:
            parts.append("0")
        try:
            major, minor, patch = int(parts[0]), int(parts[1]), int(parts[2])
        except ValueError:
            return version
        if part == "major":
            return f"{major + 1}.0.0"
        if part == "minor":
            return f"{major}.{minor + 1}.0"
        return f"{major}.{minor}.{patch + 1}"  # patch

    @app.route("/api/version", methods=["GET"])
    def api_version_get():
        settings   = _load_settings()
        main_branch = settings.get("GIT_MAIN_BRANCH", "main").strip() or "main"
        current     = _read_version_from_branch(main_branch)
        # Also expose what is saved in build settings as the "next" version
        next_ver    = settings.get("APP_VERSION", current)
        return jsonify({"ok": True, "current": current, "next": next_ver})

    @app.route("/api/version", methods=["POST"])
    def api_version_set():
        """Write a new version to the VERSION file and save to APP_VERSION setting."""
        data    = request.get_json(silent=True) or {}
        version = data.get("version", "").strip()
        if not version:
            return jsonify({"ok": False, "error": "No version provided"})
        try:
            _version_file_path().write_text(version + "\n", encoding="utf-8")
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})
        settings = _load_settings()
        settings["APP_VERSION"] = version
        _save_settings(settings)
        return jsonify({"ok": True, "version": version})

    # ── GitHub CLI integration ─────────────────────────────────────────────────

    @app.route("/api/gh/auth-status")
    def gh_auth_status():
        try:
            out, err, rc = _gh_run(["auth", "status"])
            combined = out + "\n" + err
            logged_in = rc == 0
            username = None
            for line in combined.splitlines():
                if " as " in line:
                    parts = line.split(" as ")
                    if len(parts) > 1:
                        username = parts[1].split()[0]
                        break
            return jsonify({"logged_in": logged_in, "username": username})
        except Exception as e:
            return jsonify({"logged_in": False, "username": None, "error": str(e)})

    @app.route("/api/gh/auth-login", methods=["POST"])
    def gh_auth_login():
        try:
            subprocess.Popen(
                ["powershell", "-Command",
                 "Start-Process powershell -ArgumentList '-NoExit','-Command','gh auth login'"],
                shell=False,
            )
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/gh/repos")
    def gh_repos():
        try:
            out, err, rc = _gh_run(
                ["repo", "list", "--json", "name,url,isPrivate,description", "--limit", "100"],
                timeout=20,
            )
            repos = json.loads(out) if rc == 0 and out else []
            return jsonify({"ok": rc == 0, "repos": repos})
        except Exception as e:
            return jsonify({"ok": False, "repos": [], "error": str(e)})

    @app.route("/api/gh/validate-repo", methods=["POST"])
    def gh_validate_repo():
        data = request.get_json(silent=True) or {}
        url = data.get("url", "").strip()
        if not url:
            return jsonify({"ok": False, "exists": False})
        try:
            m = re.search(r'github\.com[:/]([^/]+/[^/.]+)', url)
            if not m:
                return jsonify({"ok": False, "exists": False})
            repo_id = m.group(1).rstrip(".git")
            out, err, rc = _gh_run(["repo", "view", repo_id, "--json", "name"], timeout=10)
            return jsonify({"ok": True, "exists": rc == 0})
        except Exception as e:
            return jsonify({"ok": False, "exists": False, "error": str(e)})

    @app.route("/api/gh/browse-folder", methods=["POST"])
    def gh_browse_folder():
        data = request.get_json(silent=True) or {}
        initial = data.get("initial", r"C:\Users\User\OneDrive\GitHub Repositories")
        # Reject URLs and non-absolute paths before os.path.isdir (avoids UNC hang on URLs)
        _looks_like_path = bool(
            initial and len(initial) >= 2
            and (initial[1] == ":" or initial.startswith("\\\\"))
        )
        if not _looks_like_path:
            initial = r"C:\Users\User\OneDrive\GitHub Repositories"
        # Walk up to the nearest existing ancestor so IFileOpenDialog always opens
        # (e.g. D:\Compile Playground\NewApp where D:\Compile Playground doesn't exist yet)
        init_path = Path(initial)
        while init_path and not init_path.is_dir():
            parent = init_path.parent
            if parent == init_path:
                break
            init_path = parent
        initial = str(init_path) if init_path.is_dir() else r"C:\Users\User\OneDrive\GitHub Repositories"
        initial_safe = initial.replace("'", "''")
        tmp_path = None
        try:
            # Write to a .ps1 temp file — avoids PowerShell -Command heredoc quoting issues
            ps_script = (
                f"$initial = '{initial_safe}'\n"
                "Add-Type @\"\n"
                + _FOLDER_PICKER_CS +
                "\n\"@\n"
                "Write-Output ([FolderPicker]::Pick($initial))\n"
            )
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".ps1", delete=False, encoding="utf-8"
            ) as f:
                f.write(ps_script)
                tmp_path = f.name

            result = subprocess.run(
                ["powershell", "-STA", "-ExecutionPolicy", "Bypass", "-File", tmp_path],
                capture_output=True, text=True, timeout=60,
            )
            # stdout may contain warnings before the path; take the last non-empty line
            lines = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
            path  = lines[-1] if lines else ""
            if path and os.path.isdir(path):
                return jsonify({"ok": True, "path": path})
            # C# dialog returned no valid path — retry from a known-good root
            safe_root = r"C:\Users\User\OneDrive\GitHub Repositories"
            if os.path.isdir(safe_root) and safe_root != initial:
                safe_safe = safe_root.replace("'", "''")
                retry_script = (
                    f"$initial = '{safe_safe}'\n"
                    "Add-Type @\"\n"
                    + _FOLDER_PICKER_CS +
                    "\n\"@\n"
                    "Write-Output ([FolderPicker]::Pick($initial))\n"
                )
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".ps1", delete=False, encoding="utf-8"
                ) as f2:
                    f2.write(retry_script)
                    tmp_path2 = f2.name
                try:
                    result2 = subprocess.run(
                        ["powershell", "-STA", "-ExecutionPolicy", "Bypass", "-File", tmp_path2],
                        capture_output=True, text=True, timeout=60,
                    )
                    lines2 = [ln.strip() for ln in result2.stdout.splitlines() if ln.strip()]
                    path2  = lines2[-1] if lines2 else ""
                    if path2 and os.path.isdir(path2):
                        return jsonify({"ok": True, "path": path2})
                finally:
                    try: os.unlink(tmp_path2)
                    except Exception: pass
            return jsonify({"ok": False, "path": None,
                            "error": result.stderr.strip() or "No folder selected"})
        except Exception as e:
            return jsonify({"ok": False, "path": None, "error": str(e)})
        finally:
            if tmp_path:
                try: os.unlink(tmp_path)
                except Exception: pass

    @app.route("/api/gh/browse-file", methods=["POST"])
    def gh_browse_file():
        data = request.get_json(silent=True) or {}
        filter_desc = data.get("filter_desc", "All Files").replace("'", "''")
        filter_ext  = data.get("filter_ext",  "*.*").replace("'", "''")
        initial     = data.get("initial", "").replace("'", "''")
        try:
            ps_lines = [
                "Add-Type -AssemblyName System.Windows.Forms",
                "$o = New-Object System.Windows.Forms.Form",
                "$o.TopMost = $true",
                "$o.WindowState = 'Minimized'",
                "$o.ShowInTaskbar = $false",
                "$o.Show()",
                "$d = New-Object System.Windows.Forms.OpenFileDialog",
                f"$d.Filter = '{filter_desc}|{filter_ext}'",
                f"$d.InitialDirectory = '{initial}'",
                "$d.CheckFileExists = $true",
                "$null = $d.ShowDialog($o)",
                "$o.Dispose()",
                "Write-Output $d.FileName",
            ]
            result = subprocess.run(
                ["powershell", "-STA", "-Command", "\n".join(ps_lines)],
                capture_output=True, text=True, timeout=60,
            )
            path = result.stdout.strip()
            if path:
                return jsonify({"ok": True, "path": path})
            return jsonify({"ok": False, "path": None})
        except Exception as e:
            return jsonify({"ok": False, "path": None, "error": str(e)})

    @app.route("/api/gh/find-local-repo", methods=["POST"])
    def gh_find_local_repo():
        """Check if a GitHub repo exists as a local directory under the GitHub Repositories base."""
        data = request.get_json(silent=True) or {}
        repo_name = data.get("repo_name", "").strip()
        if not repo_name:
            return jsonify({"ok": False, "path": None})
        base_dir = r"C:\Users\User\OneDrive\GitHub Repositories"
        candidate = os.path.join(base_dir, repo_name)
        if os.path.isdir(candidate):
            return jsonify({"ok": True, "path": candidate})
        return jsonify({"ok": False, "path": None})

    @app.route("/api/gh/prs")
    def gh_prs():
        settings = _load_settings()
        repo_dir = settings.get("GIT_REPO_DIR", "").strip() or str(PROJECT_ROOT)
        if not os.path.isdir(repo_dir):
            return jsonify({"ok": False, "prs": [], "error": "No valid repo dir"})
        try:
            out, err, rc = _gh_run(
                ["pr", "list", "--json",
                 "number,title,state,url,headRefName,baseRefName,author", "--limit", "20"],
                cwd=repo_dir, timeout=15,
            )
            prs = json.loads(out) if rc == 0 and out else []
            return jsonify({"ok": rc == 0, "prs": prs})
        except Exception as e:
            return jsonify({"ok": False, "prs": [], "error": str(e)})

    @app.route("/api/gh/pr/create", methods=["POST"])
    def gh_pr_create():
        data     = request.get_json(silent=True) or {}
        title    = data.get("title", "").strip()
        body     = data.get("body",  "").strip()
        base     = data.get("base",  "main").strip()
        head     = data.get("head",  "").strip()
        settings = _load_settings()
        repo_dir = settings.get("GIT_REPO_DIR", "").strip() or str(PROJECT_ROOT)
        if not title:
            return jsonify({"ok": False, "error": "Title is required"})
        try:
            args = ["pr", "create", "--title", title, "--body", body, "--base", base]
            if head:
                args += ["--head", head]
            out, err, rc = _gh_run(args, cwd=repo_dir, timeout=30)
            return jsonify({"ok": rc == 0, "output": out or err})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    # ── Local Backup ───────────────────────────────────────────────────────────

    @app.route("/api/backup/run", methods=["POST"])
    def backup_run():
        settings = _load_settings()
        src  = settings.get("BACKUP_SRC", "").strip() or settings.get("GIT_REPO_DIR", "").strip() or str(PROJECT_ROOT)
        dest = settings.get("BACKUP_DEST", "").strip()
        if not os.path.isdir(src):
            return jsonify({"ok": False, "error": "Source repo dir not found"})
        if not dest:
            return jsonify({"ok": False, "error": "No backup destination configured"})
        try:
            os.makedirs(dest, exist_ok=True)
            result = subprocess.run(
                ["robocopy", src, dest, "/MIR", "/R:2", "/W:1", "/NFL", "/NDL", "/NJH"],
                capture_output=True, text=True, timeout=300,
            )
            # robocopy exit codes 0–7 = success; 8+ = error
            ok     = result.returncode < 8
            output = (result.stdout or result.stderr or "")[-2000:]
            return jsonify({"ok": ok, "output": output})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    # ── Build Dash Daemon ──────────────────────────────────────────────────────

    @app.route("/api/daemon/status")
    def daemon_status():
        pid_file = BUILD_DIR / "build_dash_daemon.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                import psutil
                proc = psutil.Process(pid)
                if proc.is_running():
                    return jsonify({"running": True, "pid": pid})
            except Exception:
                pass
            pid_file.unlink(missing_ok=True)
        return jsonify({"running": False, "pid": None})

    @app.route("/api/daemon/start", methods=["POST"])
    def daemon_start():
        daemon_script = BUILD_DIR / "build_dash_daemon.py"
        if not daemon_script.exists():
            return jsonify({"ok": False, "error": "build_dash_daemon.py not found"})
        try:
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            subprocess.Popen([sys.executable, str(daemon_script)], creationflags=flags)
            return jsonify({"ok": True})
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

    @app.route("/api/daemon/stop", methods=["POST"])
    def daemon_stop():
        pid_file = BUILD_DIR / "build_dash_daemon.pid"
        if pid_file.exists():
            try:
                pid = int(pid_file.read_text().strip())
                import psutil
                psutil.Process(pid).terminate()
            except Exception:
                pass
            pid_file.unlink(missing_ok=True)
        return jsonify({"ok": True})

    # ── Pipeline Sounds ───────────────────────────────────────────────────────

    @app.route("/api/sounds/scan", methods=["GET"])
    def sounds_scan():
        """Return a list of audio files found in well-known Windows sound dirs."""
        _AUDIO_EXTS = {".wav", ".mp3", ".ogg", ".m4a", ".flac", ".aiff", ".wma"}
        search_dirs = [
            os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Media"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         r"Microsoft\Windows\Themes"),
        ]
        sounds = []
        seen = set()
        for d in search_dirs:
            if not os.path.isdir(d):
                continue
            for fname in sorted(os.listdir(d)):
                if os.path.splitext(fname)[1].lower() in _AUDIO_EXTS:
                    full = os.path.join(d, fname)
                    if full not in seen:
                        seen.add(full)
                        sounds.append({
                            "name": os.path.splitext(fname)[0],
                            "path": full,
                            "dir":  os.path.basename(d),
                        })
        return jsonify({"ok": True, "sounds": sounds})

    @app.route("/api/sounds/play", methods=["POST"])
    def sounds_play():
        """Play an audio file server-side (non-blocking)."""
        data = request.get_json(silent=True) or {}
        path = data.get("path", "").strip()
        if not path or not os.path.isfile(path):
            return jsonify({"ok": False, "error": "File not found"})
        ext = os.path.splitext(path)[1].lower()
        try:
            if ext == ".wav":
                import winsound  # stdlib, Windows only
                winsound.PlaySound(
                    path,
                    winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
                )
            else:
                # For MP3/other formats use Windows Media Player via PowerShell
                safe = path.replace("'", "''")
                ps = (
                    "Add-Type -AssemblyName presentationCore; "
                    "$mp = New-Object System.Windows.Media.MediaPlayer; "
                    f"$mp.Open([uri]'file:///{safe}'); "
                    "$mp.Play(); Start-Sleep -Seconds 10"
                )
                subprocess.Popen(
                    ["powershell", "-STA", "-WindowStyle", "Hidden", "-Command", ps],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            return jsonify({"ok": True})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @app.route("/api/build/inject-meta", methods=["POST"])
    def build_inject_meta():
        """Write build_meta.json to the target app root before each build.

        The target app reads this file at startup and seeds runtime-configurable
        settings (e.g. support_contact_email) into its own database.  The file is
        also bundled into the compiled exe via --add-data so the frozen app can
        read it from sys._MEIPASS.
        """
        settings = _load_settings()
        repo_dir = settings.get("GIT_REPO_DIR", "").strip()
        if not repo_dir:
            repo_dir = str(PROJECT_ROOT)
        if not os.path.isdir(repo_dir):
            return jsonify({"ok": False, "error": f"Repo dir not found: {repo_dir}"})
        meta = {
            "support_email":  settings.get("SUPPORT_EMAIL", ""),
            "app_name":       settings.get("APP_NAME", ""),
            "app_version":    settings.get("APP_VERSION", ""),
            "publisher":      settings.get("APP_PUBLISHER", ""),
        }
        meta_path = os.path.join(repo_dir, "build_meta.json")
        try:
            Path(meta_path).write_text(json.dumps(meta, indent=2), encoding="utf-8")
            return jsonify({"ok": True, "path": meta_path})
        except Exception as exc:
            return jsonify({"ok": False, "error": str(exc)})

    @app.route("/api/daemon/build-exe", methods=["POST"])
    def daemon_build_exe():
        global _build_log, _build_status
        settings      = _load_settings()
        name          = (settings.get("DAEMON_NAME", "") or "Build_Dash").strip()
        icon          = settings.get("DAEMON_ICON", "").strip()
        build_dir     = settings.get("DAEMON_BUILD_DIR", "").strip()
        daemon_script = BUILD_DIR / "build_dash_daemon.py"
        if not daemon_script.exists():
            return jsonify({"ok": False, "error": "build_dash_daemon.py not found"})
        if build_dir:
            os.makedirs(build_dir, exist_ok=True)
        with _build_lock:
            if _build_status == "running":
                return jsonify({"ok": False, "error": "A build is already running"})
            _build_log    = []
            _build_status = "running"
        cmd = [sys.executable, "-m", "PyInstaller", "--onefile", "--noconsole",
               f"--name={name}", str(daemon_script)]
        if build_dir:
            cmd.insert(-1, f"--distpath={build_dir}")
        if icon and os.path.isfile(icon):
            cmd.insert(-1, f"--icon={icon}")
        t = threading.Thread(target=_run_build_exe, args=(cmd,), daemon=True)
        t.start()
        return jsonify({"ok": True})

    # ── Documentation status ──────────────────────────────────────────────────

    @app.route("/api/docs/status")
    def api_docs_status():
        """
        Compare Flask-registered routes against doc_manifest.json.
        Returns lists of documented and undocumented routes.
        """
        manifest_path = BUILD_DIR / "doc_manifest.json"
        try:
            with open(manifest_path, encoding="utf-8") as f:
                manifest = json.load(f)
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)})

        # Collect all routes documented in the manifest
        documented_routes: set[str] = set()
        for section_list in [
            manifest.get("builder_help", {}).get("sections", []),
            manifest.get("job_tracker_help", {}).get("sections", []),
        ]:
            for section in section_list:
                for route in section.get("routes", []):
                    documented_routes.add(route)

        # Collect all Flask routes (skip static and internal helpers)
        _skip = {"static", "index", "builder_help"}
        all_routes: list[str] = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint in _skip or rule.rule.startswith("/static"):
                continue
            all_routes.append(rule.rule)

        undocumented = sorted(r for r in all_routes if r not in documented_routes)
        documented   = sorted(r for r in all_routes if r in documented_routes)

        # Collect section metadata from manifest for display
        sections = []
        for sec in manifest.get("builder_help", {}).get("sections", []):
            sections.append({
                "id":      sec["id"],
                "feature": sec["feature"],
                "updated": sec.get("updated", ""),
            })

        return jsonify({
            "ok":           True,
            "last_updated": manifest.get("last_updated", ""),
            "total":        len(all_routes),
            "documented":   documented,
            "undocumented": undocumented,
            "sections":     sections,
        })

    # ── Favicon ───────────────────────────────────────────────────────────────
    @app.route("/favicon.ico")
    def favicon():
        return send_from_directory(BUILD_DIR, "builder_icon.ico", mimetype="image/x-icon")

    # ── All-tabs watchdog ─────────────────────────────────────────────────────
    # Every browser tab registers its own unique tabId via /api/heartbeat?tab=<id>.
    # The server shuts down only when all known tabs have disconnected.
    import time as _time

    _tabs: dict[str, float] = {}          # tabId -> last beat timestamp
    _tabs_lock = threading.Lock()
    _ever_had_tab: list[bool] = [False]
    _BEAT_TIMEOUT = 30.0

    @app.route("/api/heartbeat", methods=["POST", "GET"])
    def api_heartbeat():
        tab_id = request.args.get("tab", "")
        if tab_id:
            with _tabs_lock:
                _tabs[tab_id] = _time.monotonic()
                _ever_had_tab[0] = True
        return "", 204

    @app.route("/api/tab-close", methods=["POST", "GET"])
    def api_tab_close():
        tab_id = request.args.get("tab", "")
        if tab_id:
            with _tabs_lock:
                _tabs.pop(tab_id, None)
        return "", 204

    def _watchdog() -> None:
        while not _ever_had_tab[0]:
            _time.sleep(2)
        while True:
            _time.sleep(10)
            now = _time.monotonic()
            with _tabs_lock:
                stale = [tid for tid, t in list(_tabs.items()) if now - t > _BEAT_TIMEOUT]
                for tid in stale:
                    _tabs.pop(tid, None)
                alive = bool(_tabs)
            if not alive:
                os._exit(0)

    threading.Thread(target=_watchdog, daemon=True, name="bd-watchdog").start()

    return app
