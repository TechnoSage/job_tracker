<#
.SYNOPSIS
    Creates a "Job Tracker.lnk" shortcut on the Windows Desktop.

.DESCRIPTION
    Run this script once to create the desktop shortcut.
    The shortcut is self-contained and can be copied/moved anywhere.

    Double-clicking the shortcut will:
      • Start the Job Tracker Flask server (minimised in the taskbar)
      • Open the dashboard automatically in Google Chrome (or your default browser)

    To STOP the server, click the "Job Tracker Server" button in the taskbar
    and close that window (or press Ctrl+C inside it).

.NOTES
    If you move the job_tracker folder, re-run this script to update the shortcut.
#>

param(
    [string]$ShortcutName = "Job Tracker",
    [string]$Destination  = [Environment]::GetFolderPath("Desktop")
)

# ── Paths ────────────────────────────────────────────────────────────────────

$ScriptDir   = Split-Path -Parent $MyInvocation.MyCommand.Definition
$LauncherPs1 = Join-Path $ScriptDir "start_job_tracker.ps1"
$ShortcutPath = Join-Path $Destination "$ShortcutName.lnk"

Write-Host ""
Write-Host "  Job Tracker — Desktop Shortcut Creator" -ForegroundColor Cyan
Write-Host "  ────────────────────────────────────────" -ForegroundColor DarkGray
Write-Host ""

# Validate launcher exists
if (-not (Test-Path $LauncherPs1)) {
    Write-Host "  [ERROR] start_job_tracker.ps1 not found in:" -ForegroundColor Red
    Write-Host "          $ScriptDir" -ForegroundColor Red
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}

# Validate destination
if (-not (Test-Path $Destination)) {
    Write-Host "  [ERROR] Destination folder does not exist:" -ForegroundColor Red
    Write-Host "          $Destination" -ForegroundColor Red
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}

# ── Build the shortcut ───────────────────────────────────────────────────────

# The shortcut calls PowerShell invisibly; PowerShell opens Chrome.
# -WindowStyle Hidden means no PowerShell console window appears.
$psArgs = "-WindowStyle Hidden -ExecutionPolicy Bypass -NonInteractive " +
          "-File `"$LauncherPs1`""

$WshShell = New-Object -ComObject WScript.Shell
$lnk = $WshShell.CreateShortcut($ShortcutPath)

$lnk.TargetPath      = "powershell.exe"
$lnk.Arguments       = $psArgs
$lnk.WorkingDirectory = $ScriptDir
$lnk.Description     = "Start Job Tracker server and open the dashboard in Chrome"
$lnk.WindowStyle     = 1   # 1 = Normal (PowerShell hides itself via -WindowStyle Hidden)

# ── Icon selection (best available on this machine) ──────────────────────────

$iconChosen = $false

# 1st choice: Chrome icon (most recognisable for a web-app shortcut)
$chromePaths = @(
    "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
    "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
    "$env:LocalAppData\Google\Chrome\Application\chrome.exe"
)
foreach ($cp in $chromePaths) {
    if (Test-Path $cp) {
        $lnk.IconLocation = "$cp,0"
        $iconChosen = $true
        break
    }
}

# 2nd choice: briefcase / work icon from imageres.dll (Windows 10/11)
if (-not $iconChosen) {
    $imageres = "$env:SystemRoot\System32\imageres.dll"
    if (Test-Path $imageres) {
        $lnk.IconLocation = "$imageres,11"
        $iconChosen = $true
    }
}

# 3rd choice: globe icon from shell32.dll (always present)
if (-not $iconChosen) {
    $lnk.IconLocation = "$env:SystemRoot\System32\shell32.dll,14"
}

$lnk.Save()

# ── Verify it was created ────────────────────────────────────────────────────

if (Test-Path $ShortcutPath) {
    Write-Host "  [OK] Shortcut created successfully!" -ForegroundColor Green
    Write-Host ""
    Write-Host "       $ShortcutPath" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "  How to use:" -ForegroundColor Yellow
    Write-Host "    • Double-click the shortcut to launch Job Tracker" -ForegroundColor White
    Write-Host "    • Chrome (or your default browser) will open automatically" -ForegroundColor White
    Write-Host "    • The server runs minimised in your taskbar" -ForegroundColor White
    Write-Host "    • Close the 'Job Tracker Server' taskbar window to stop it" -ForegroundColor White
    Write-Host ""
    Write-Host "  Tip: you can drag the shortcut anywhere — Desktop, taskbar," -ForegroundColor DarkGray
    Write-Host "       Start menu, or a network share." -ForegroundColor DarkGray
    Write-Host ""
} else {
    Write-Host "  [ERROR] Shortcut file was not created. Check permissions." -ForegroundColor Red
    Write-Host ""
    Read-Host "  Press Enter to exit"
    exit 1
}

# Offer to open Explorer at the Desktop
$answer = Read-Host "  Open Desktop folder now? (Y/n)"
if ($answer -eq "" -or $answer -match '^[Yy]') {
    Start-Process explorer.exe -ArgumentList ("/select," + $ShortcutPath)
}
Write-Host ""
