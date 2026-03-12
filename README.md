# Job Tracker

A self-hosted, browser-based job search management system built with Python + Flask.
Tracks applications, automates email scanning, matches job listings to your resume,
and comes with a full Windows installer build pipeline.

---

## Features

### Job Tracking
- Add, edit, and archive job applications with status tracking (Applied, Interview, Offer, Rejected, etc.)
- Match score system — each listing is scored against your resume / target criteria
- Filter and sort applications by status, score, date, source, and more
- Follow-up reminder system with configurable intervals

### Automated Job Scanning
- Scans configured job sources (RSS feeds, job boards) on a schedule (8 AM and 8 PM by default)
- De-duplicates listings across scans
- Email notification when high-score matches are found

### Email Integration
- Connects to Gmail or Outlook via OAuth (Google API or MSAL)
- Scans your inbox for application-related emails and links them to the correct application
- Email Response page for reviewing and actioning matched emails

### Scheduler Daemon
- Background Windows process (`scheduler_daemon.py`) that runs scans and reminders independently of the browser session
- Controlled from the Settings page — start, stop, and view PID/status
- Persists across browser restarts via a PID file

### Job Sources (Scrapers)

Manage all job data sources from the **Sources** page (`/scrapers`):

- **Active / Unavailable tabs** — toggle the switch on any source card to move it between tabs. Active sources are included in scans; disabled sources move to the Unavailable tab with an optional reason field.
- **Sort by jobs found** — active source cards are automatically sorted by the number of jobs found in the last scan (most productive sources appear first).
- **Green glow indicator** — source cards that found jobs in the most recent scan display a pulsing green outline and a job count badge.
- **Live card refresh** — after clicking "Run now" on a card, the card stats (job count, last run time, error state) update automatically via polling without a page reload. Cards re-sort in place when the scan completes.
- **Unavailable sources** — the Unavailable tab has two sections:
  - *Disabled by You* — sources you have turned off, with your optional reason shown. Toggle back on to re-enable.
  - *Cannot Be Scraped* — hardcoded informational cards for sources with technical or legal barriers (LinkedIn, Indeed, Glassdoor, JobRight AI).
- **API Key Setup** — sources that require API keys (USAJobs, CareerOneStop) display an amber pulsing outline and an embedded key-setup panel on the card. The panel includes:
  - A "Get Key" button that opens the provider's developer registration page
  - Password input fields for each required credential
  - A "Save & Test" button that writes keys to `.env`, updates the running process, and makes a live test request to confirm the keys work
  - Inline success/error feedback from the test call
- **Health check** — each card shows the source's last health-check status (Current / Broken / Not checked) and the timestamp of the last check.
- **Add custom source** — add any RSS or JSON API source with custom search terms. Custom sources can be renamed, edited, and deleted.

### Analytics
- Dashboard metrics: total applications, response rate, average time-to-response
- Pipeline chart showing applications by stage
- Match quality distribution

### HTTPS (mkcert)
- Optional locally-trusted HTTPS using [mkcert](https://github.com/FiloSottile/mkcert)
- No browser security warnings — certificate is trusted by Windows and Chrome
- Certificates are generated on first run when `certs/localhost.pem` is present

---

## Requirements

- Python 3.11 or 3.13 (3.10+ should work)
- Windows 10/11 (Linux/macOS supported for development, installer requires Windows)
- pip packages: see `requirements.txt`

```
pip install -r requirements.txt
```

---

## Running in Development

```bash
python run.py
```

The app opens in your default browser at `http://127.0.0.1:5000` (or `https://` if mkcert certs are present).

The console window is hidden automatically on Windows — logs write to `job_tracker.log` in the project root.

---

## Project Structure

```
job_tracker/
├── run.py                  # Entry point — starts Flask + APScheduler, opens browser
├── app.py                  # Flask app factory (create_app)
├── models.py               # SQLAlchemy ORM models
├── scheduler.py            # APScheduler job definitions (scan, reminders)
├── scheduler_daemon.py     # Standalone background daemon process
├── requirements.txt        # Python dependencies
├── templates/              # Jinja2 HTML templates (Bootstrap 5)
│   ├── base.html           # Shared layout with navbar and topbar
│   ├── help.html           # In-app help & documentation
│   └── ...
├── static/                 # CSS, JS, images
├── build/                  # Build Dashboard (embedded, port 5001)
│   ├── builder_app.py      # Build Dashboard Flask app
│   ├── builder.html        # Build Dashboard UI
│   ├── builder_help.html   # Build Dashboard help documentation
│   ├── build.py            # PyInstaller + Inno Setup pipeline
│   ├── build_config.py     # Build settings loader
│   └── build_dash_daemon.py# Build Dashboard background daemon
├── start_builder.ps1       # PowerShell launcher for the Build Dashboard
├── start_job_tracker.ps1   # PowerShell launcher for Job Tracker
└── instance/               # SQLite database (gitignored)
```

---

## Git Workflow

| Branch | Purpose |
|--------|---------|
| `development` | All active development — default working branch |
| `main` | Production releases only — built and tagged via the Build Dashboard |

All code changes go to `development`. The Build Dashboard automates checkout of `main`, compiles the installer, and returns to `development`.

---

## Build Dashboard (port 5001)

The embedded Build Dashboard compiles Job Tracker into a distributable Windows installer.

**Start it:**
```powershell
.\start_builder.ps1
```
Or:
```bash
python start_builder.py
```

Then open `http://127.0.0.1:5001` in your browser.

### Output Directory Structure

All build artefacts go to a configured Output Dir (default: `D:\Compile Playground\job_tracker\`):

```
D:\Compile Playground\job_tracker\
├── JobTracker_Setup_2.1.0.exe     ← distributable installer
├── Bundle Only\
│   └── JobTracker\               ← PyInstaller/Nuitka bundle
│       ├── JobTracker.exe
│       └── ...
└── Log\
    └── JobTracker_Build_2026-03-12_14-00-00.log
```

### Build Dashboard Features

- **App Info** — configure name, version, publisher, URLs, icon
- **Code Protection** — Plain PyInstaller / PyArmor (AES-256) / Nuitka (native machine code)
- **License File** — generate and embed a software license
- **README Generator** — auto-generate a project README
- **Git Integration** — status, commit, push, merge dev→main, pull requests
- **GitHub Auth** — `gh` CLI auth badge; repo picker modal; remote URL validation
- **Pull Requests** — list and create PRs from the dashboard
- **CI/CD Pipeline** — animated five-stage build pipeline (Deps → Tools → Protect → Bundle → Installer)
- **Test Pipeline** — auto-launches compiled exe after each build and polls the port to verify it starts
- **Version Management** — Major / Minor / Patch increment buttons; CHANGELOG.json updated automatically
- **Local Backup** — robocopy mirror of the project to a secondary drive on a configurable schedule
- **Build Dash Daemon** — background Windows process for scheduled backups; controllable from the UI

---

## HTTPS Setup (Optional)

1. Install [mkcert](https://github.com/FiloSottile/mkcert): `winget install FiloSottile.mkcert`
2. Run `mkcert -install` (adds a local CA to Windows + Chrome trust store)
3. In the project root: `mkdir certs && cd certs && mkcert 127.0.0.1 localhost`
4. Rename the generated files to `localhost.pem` and `localhost-key.pem`
5. Restart `run.py` — it detects the certs automatically and switches to HTTPS

---

## Logging

- `job_tracker.log` — application log (Flask requests, scheduler events, errors)
- Build logs → `<OutputDir>\Log\` (timestamped, written by the Build Dashboard)
- Test pipeline logs → `<OutputDir>\Log\<ExeName>_Output_Pipeline_<timestamp>.log`

ANSI escape codes are stripped from all log files automatically.

---

## GitHub

Repository: https://github.com/TechnoSage/job_tracker
Issues / Support: https://github.com/TechnoSage/job_tracker/issues
