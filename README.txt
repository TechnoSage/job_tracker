======================================================================
  Job Tracker  v1.0.0
======================================================================

Personal job search tracker

  Publisher : TechnoSage
  Website   : https://github.com/TechnoSage/job_tracker
  Support   : https://github.com/TechnoSage/job_tracker/issues
  Year      : 2026

──────────────────────────────────────────────────────────────────────
  TABLE OF CONTENTS
──────────────────────────────────────────────────────────────────────
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

──────────────────────────────────────────────────────────────────────
  1. SYSTEM REQUIREMENTS
──────────────────────────────────────────────────────────────────────
  • Windows 10 or Windows 11 (64-bit recommended)
  • 100 MB free disk space (plus space for your data)
  • Internet connection (optional — required for online features only)
  • No Python or additional runtime needed — everything is bundled.

──────────────────────────────────────────────────────────────────────
  2. INSTALLATION
──────────────────────────────────────────────────────────────────────
  1. Double-click  JobTracker_Setup_1.0.0.exe
  2. Follow the on-screen wizard.
  3. The default installation folder is:
       {autopf}\JobTracker
  4. Optionally check "Create Desktop Shortcut" for quick access.
  5. Click Finish — the application will launch automatically.

  To install silently (no GUI):
    JobTracker_Setup_1.0.0.exe /VERYSILENT /SUPPRESSMSGBOXES

──────────────────────────────────────────────────────────────────────
  3. GETTING STARTED / FIRST RUN
──────────────────────────────────────────────────────────────────────
  After installation, Job Tracker will:
    • Open your default web browser to http://127.0.0.1:5000
    • Run a local web server on your machine (no data sent externally)
    • Store all data in the installation folder

  If the browser does not open automatically:
    • Double-click the Job Tracker desktop or Start Menu shortcut.
    • Or open http://127.0.0.1:5000 in any browser manually.

  On first run you may be prompted to configure initial settings.

──────────────────────────────────────────────────────────────────────
  4. KEY FEATURES
──────────────────────────────────────────────────────────────────────
  • Fully portable — runs entirely on your local machine
  • Browser-based interface (no external cloud, no subscriptions)
  • Persistent local database (SQLite) — your data never leaves your PC
  • Automatic browser launch on startup
  • Runs minimised in the background; close the browser tab to keep it running

──────────────────────────────────────────────────────────────────────
  5. DATA & FILES
──────────────────────────────────────────────────────────────────────
  All user data is stored in the installation directory:

    {autopf}\JobTracker\
      JobTracker.exe      — main application executable
      jobtracker.db        — SQLite database (your data)
      jobtracker.log       — application log file
      .env                — local configuration / secrets
      README.txt          — this file
      LICENSE.txt         — software license terms

  IMPORTANT: Back up the .db file before uninstalling or upgrading
  if you want to keep your data.

──────────────────────────────────────────────────────────────────────
  6. UNINSTALLING
──────────────────────────────────────────────────────────────────────
  Via Windows Settings:
    Settings › Apps › Installed apps › Job Tracker › Uninstall

  Via Control Panel:
    Control Panel › Programs › Uninstall a program › Job Tracker

  During uninstall you will be asked whether to remove your data files
  (database, logs, .env).  Choose YES to fully clean up, or NO to keep
  your data for a future reinstall.

──────────────────────────────────────────────────────────────────────
  7. UPDATING
──────────────────────────────────────────────────────────────────────
  Simply run the new JobTracker_Setup_<version>.exe installer over the
  existing installation.  The installer will close the running app,
  replace the program files, and relaunch automatically.
  Your database and settings are preserved.

──────────────────────────────────────────────────────────────────────
  8. TROUBLESHOOTING
──────────────────────────────────────────────────────────────────────
  App does not open / browser shows "Connection Refused"
    • Wait 10–15 seconds and refresh — startup can take a moment.
    • Check the log file:  {autopf}\JobTracker\jobtracker.log
    • Ensure port 5000 is not blocked by a firewall or antivirus.
    • Try restarting the app from the Start Menu shortcut.

  App opens but shows an error page
    • Review the log file for details.
    • Reinstall over the top — this preserves your data.

  Antivirus flags the installer or exe
    • This is a false positive common with PyInstaller-built apps.
    • The source code is available at: https://github.com/TechnoSage/job_tracker
    • Add an exclusion in your antivirus for the installation folder.

  Port 5000 already in use
    • Another application is using port 5000.
    • Stop that application, or configure Job Tracker to use a different port.

──────────────────────────────────────────────────────────────────────
  9. SUPPORT
──────────────────────────────────────────────────────────────────────
  Website : https://github.com/TechnoSage/job_tracker
  Issues  : https://github.com/TechnoSage/job_tracker/issues
  Publisher: TechnoSage

  Please include the contents of jobtracker.log when reporting bugs.

──────────────────────────────────────────────────────────────────────
 10. LICENSE
──────────────────────────────────────────────────────────────────────
  See LICENSE.txt in the installation directory for full license terms.

  Copyright © 2026 TechnoSage. All rights reserved.

======================================================================
