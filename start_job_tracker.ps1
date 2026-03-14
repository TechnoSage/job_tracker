#Requires -Version 5.0
<#
.SYNOPSIS
    Job Tracker - Windows Launcher

.DESCRIPTION
    1. Checks whether the Flask server is already running on port 5000.
    2. If not, locates Python and starts run.py in a minimised window.
    3. Polls until the server is ready (up to 30 seconds).
    4. Opens the dashboard in Google Chrome, or the default browser.

.NOTES
    Called by the desktop shortcut created with create_shortcut.ps1.
    Do NOT move this file without re-running create_shortcut.ps1.
#>

$AppDir   = $PSScriptRoot
$CertFile = Join-Path $AppDir "certs\localhost.pem"
$Scheme   = if (Test-Path $CertFile) { "https" } else { "http" }
$AppUrl   = "${Scheme}://127.0.0.1:5000"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

function Show-Error($Msg) {
    Add-Type -AssemblyName PresentationFramework | Out-Null
    [System.Windows.MessageBox]::Show(
        $Msg,
        "Job Tracker",
        [System.Windows.MessageBoxButton]::OK,
        [System.Windows.MessageBoxImage]::Error
    ) | Out-Null
}

# Win32 API for focusing an existing window
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class Win32 {
    [DllImport("user32.dll")] public static extern bool SetForegroundWindow(IntPtr h);
    [DllImport("user32.dll")] public static extern bool ShowWindow(IntPtr h, int cmd);
    [DllImport("user32.dll")] public static extern bool IsIconic(IntPtr h);
}
"@ -ErrorAction SilentlyContinue

function Open-Or-Focus-Browser($Url) {
    # Look for any Chrome window whose title contains "Job Tracker"
    # (all pages in the app include "Job Tracker" in their <title>)
    $existing = Get-Process -Name "chrome" -ErrorAction SilentlyContinue |
        Where-Object { $_.MainWindowHandle -ne [IntPtr]::Zero -and
                       $_.MainWindowTitle -like "*Job Tracker*" } |
        Select-Object -First 1

    if ($existing) {
        $hwnd = $existing.MainWindowHandle
        if ([Win32]::IsIconic($hwnd)) { [Win32]::ShowWindow($hwnd, 9) }  # SW_RESTORE
        [Win32]::SetForegroundWindow($hwnd)
        return
    }

    # No existing window found — open a fresh one
    $chromePaths = @(
        "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
        "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
        "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
    )
    foreach ($cp in $chromePaths) {
        if (Test-Path $cp) {
            Start-Process -FilePath $cp -ArgumentList "--new-window", $Url
            return
        }
    }
    # Fall back to whatever the system default browser is
    Start-Process $Url
}

function Test-PortListening($Port) {
    $hits = netstat -an 2>$null | Select-String "127\.0\.0\.1:$Port\s.*LISTENING"
    return ($null -ne $hits -and @($hits).Count -gt 0)
}

function Test-ServerAlive($Url) {
    # Verify the server actually responds (not just that the port is bound).
    # Accepts any HTTP status — even 4xx/5xx means the server is running.
    # Bypasses SSL cert validation for loopback so mkcert certs always work.
    try {
        if ($Url.StartsWith("https")) {
            [System.Net.ServicePointManager]::ServerCertificateValidationCallback = { $true }
        }
        $req          = [System.Net.WebRequest]::Create($Url)
        $req.Timeout  = 2500
        $resp         = $req.GetResponse()
        $resp.Dispose()
        return $true
    } catch [System.Net.WebException] {
        if ($null -ne $_.Exception.Response) { $_.Exception.Response.Dispose(); return $true }
        return $false
    } catch {
        return $false
    }
}

function Get-PortPid($Port) {
    # Return the PID owning 127.0.0.1:$Port in LISTENING state, or $null.
    $line = netstat -ano 2>$null |
            Select-String "127\.0\.0\.1:$Port\s.*LISTENING" |
            Select-Object -First 1
    if ($line) {
        $parts  = ($line.Line).Trim() -split '\s+'
        $pidStr = $parts[-1]
        $pidVal = 0
        if ([int]::TryParse($pidStr, [ref]$pidVal) -and $pidVal -gt 4) { return $pidVal }
    }
    return $null
}

function Find-Python {
    # WindowsApps\python.exe is an App Execution Alias stub that only works
    # interactively (it can open the Microsoft Store instead of running Python).
    # Skip it and find the real executable.
    $waRoot = [System.IO.Path]::Combine($env:LOCALAPPDATA, 'Microsoft', 'WindowsApps')

    # Step 1: Standard installer locations (Program Files / LocalAppData Programs)
    foreach ($root in @(
        "$env:LOCALAPPDATA\Programs\Python",
        "$env:ProgramFiles\Python",
        "C:\Python313", "C:\Python312", "C:\Python311", "C:\Python310", "C:\Python39"
    )) {
        if (Test-Path $root) {
            $exe = Get-ChildItem $root -Filter "python.exe" -Recurse -ErrorAction SilentlyContinue |
                   Where-Object { $_.FullName -notmatch "\\Scripts\\" } |
                   Select-Object -First 1
            if ($exe) { return $exe.FullName }
        }
    }

    # Step 2: Search PATH entries, but skip the WindowsApps root-level stub
    foreach ($cmd in @("python3", "python")) {
        $found = Get-Command $cmd -ErrorAction SilentlyContinue
        if ($found) {
            $src = $found.Source
            if ([System.IO.Path]::GetDirectoryName($src) -ieq $waRoot) { continue }
            return $src
        }
    }

    # Step 3: Microsoft Store Python - locate the App Execution Alias via package metadata.
    #         pkg.InstallLocation is C:\Program Files\WindowsApps (ACL-blocked, can't execute).
    #         pkg.PackageFamilyName gives the AEA subdirectory under $waRoot instead.
    try {
        $pkg = Get-AppxPackage -Name "PythonSoftwareFoundation.Python*" -ErrorAction SilentlyContinue |
               Sort-Object Version -Descending | Select-Object -First 1
        if ($pkg) {
            $exe = Join-Path $waRoot (Join-Path $pkg.PackageFamilyName "python.exe")
            if (Test-Path $exe) { return $exe }
        }
    } catch {}

    # Step 4: Direct (non-recursive) scan of WindowsApps package subdirectories.
    #         Get-ChildItem -Recurse fails here due to filesystem virtualisation.
    if (Test-Path $waRoot) {
        $subDirs = Get-ChildItem $waRoot -Directory -ErrorAction SilentlyContinue |
                   Where-Object { $_.Name -like "PythonSoftwareFoundation*" } |
                   Sort-Object LastWriteTime -Descending
        foreach ($dir in $subDirs) {
            $exe = Join-Path $dir.FullName "python.exe"
            if (Test-Path $exe) { return $exe }
        }
    }

    return $null
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# Guard against double-click: acquire a named system mutex so only one
# instance of this launcher runs at a time.  A second click within the
# same moment will find the mutex already held and exit immediately.
$mutex = [System.Threading.Mutex]::new($false, "Global\JobTrackerLauncher")
if (-not $mutex.WaitOne(0)) {
    # Another instance is already running — silently do nothing.
    $mutex.Dispose()
    exit 0
}

try {
    # 1. Already running?
    if (Test-PortListening 5000) {
        if (Test-ServerAlive $AppUrl) {
            # Server is alive — just bring the browser to focus.
            Open-Or-Focus-Browser $AppUrl
            Start-Sleep -Seconds 2
            exit 0
        }
        # Port occupied but server not responding (crashed / zombie process).
        # Kill the owner so we can bind the port cleanly on restart.
        $oldPid = Get-PortPid 5000
        if ($oldPid) {
            try { Stop-Process -Id $oldPid -Force -ErrorAction SilentlyContinue } catch {}
            Start-Sleep -Milliseconds 800
        }
        # Fall through to start a fresh server below.
    }

    # 2. Locate Python executable
    $pythonExe = Find-Python
    if (-not $pythonExe) {
        Show-Error (
            "Python was not found in your PATH." + [Environment]::NewLine + [Environment]::NewLine +
            "Please install Python from https://www.python.org" + [Environment]::NewLine +
            "and check 'Add Python to PATH' during installation."
        )
        exit 1
    }

    # 3. Verify run.py exists
    $runPy = Join-Path $AppDir "run.py"
    if (-not (Test-Path $runPy)) {
        Show-Error (
            "Cannot find run.py in:" + [Environment]::NewLine + $AppDir + [Environment]::NewLine +
            [Environment]::NewLine +
            "Please ensure the shortcut points to the job_tracker folder."
        )
        exit 1
    }

    # 4. Launch the Python server.
    #    Start-Process uses ShellExecuteEx which correctly resolves Microsoft Store
    #    Python App Execution Aliases.  The & call operator and CMD batch files both
    #    use CreateProcess and fail with "Access is denied" on AEA executables.
    Start-Process -FilePath $pythonExe `
                  -ArgumentList "`"$runPy`"" `
                  -WorkingDirectory $AppDir `
                  -WindowStyle Minimized

    # 5. Poll port 5000 until the server is listening (max 30 seconds).
    $maxSeconds = 30
    for ($i = 0; $i -lt $maxSeconds; $i++) {
        Start-Sleep -Seconds 1
        if (Test-PortListening 5000) { break }
    }

    # 6. Open the browser once Flask is ready.
    Open-Or-Focus-Browser $AppUrl
    Start-Sleep -Seconds 2   # hold mutex so a concurrent double-click exits early
    exit 0

} finally {
    # Always release the mutex so a legitimate later launch can proceed.
    try { $mutex.ReleaseMutex() } catch {}
    $mutex.Dispose()
}
