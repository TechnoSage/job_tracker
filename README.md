# Job Tracker

A self-hosted, browser-based job search management system built with Python + Flask.
Tracks applications through the hiring pipeline, automates job scanning, monitors reply emails, generates unemployment compliance reports, and comes with a full Windows installer build pipeline.

---

## Table of Contents

1. [Features Overview](#features-overview)
2. [Requirements](#requirements)
3. [Installation & Running](#installation--running)
4. [Pages & Features](#pages--features)
   - [Dashboard](#dashboard)
   - [Job Listings](#job-listings)
   - [Applications](#applications)
   - [Email Response](#email-response)
   - [Analytics](#analytics)
   - [Sources (Scrapers)](#sources-scrapers)
   - [Unemployment Tracker](#unemployment-tracker)
   - [Notifications](#notifications)
   - [Settings](#settings)
   - [Changelog](#changelog)
   - [Server Logs](#server-logs)
5. [Job Scoring & Filtering](#job-scoring--filtering)
6. [Scheduler Daemon](#scheduler-daemon)
7. [Cover Letters](#cover-letters)
8. [Calendar Integration](#calendar-integration)
9. [Resume Parsing](#resume-parsing)
10. [HTTPS Setup](#https-setup)
11. [Project Structure](#project-structure)
12. [Git Workflow](#git-workflow)
13. [Build Dashboard](#build-dashboard)
14. [Logging](#logging)

---

## Features Overview

| Page | Key Capabilities |
|------|-----------------|
| **Dashboard** | Live metrics, recent job matches, follow-up reminders, scan logs, pending email reviews |
| **Job Listings** | Filter/sort by category, source, score, duration; archive, save, apply, quick-apply |
| **Applications** | Status pipeline tracking, interview scheduling, follow-up reminders, ICS calendar export |
| **Email Response** | Inbox monitoring, decline/interview classification, manual matching interface |
| **Analytics** | Pipeline funnel, source breakdown, weekly activity chart, score distribution |
| **Sources** | Enable/disable job sources, add custom RSS/API, API key setup, health checks, skill discovery |
| **Unemployment** | Claim week tracking, per-week application counting, CSV/Excel export for state submission |
| **Notifications** | All alerts (jobs, interviews, offers, reminders), read/archive/dismiss, auto-cleanup |
| **Settings** | Profile, skills, email accounts, scheduling, colors, calendar, unemployment toggle, DB tools |

---

## Requirements

- Python 3.11 or 3.13 (3.10+ should work)
- Windows 10/11 (Linux/macOS supported for development; installer requires Windows)
- pip packages: see `requirements.txt`

```bash
pip install -r requirements.txt
```

---

## Installation & Running

```bash
python run.py
```

The app opens in your default browser at `http://127.0.0.1:5000` (or `https://` if mkcert certificates are present).

The console window is hidden automatically on Windows. All log output goes to `job_tracker.log` in the project root.

---

## Pages & Features

### Dashboard

The home page at `/`. Shows a real-time snapshot of your job search.

**Metrics row:**
- Total jobs found | Applications submitted | Interviews scheduled | Offers received
- New jobs (since last scan) | Response rate | Offer rate

**Content panels:**
- **Recent Job Matches** — top-scored new listings, clickable rows to view detail
- **Follow-up Reminders** — applications due for follow-up in the next 7 days
- **Scan Logs** — last 20 scan events with source counts and timestamps
- **Pending Email Reviews** — count of inbox emails awaiting classification; links to Email Response page

---

### Job Listings

Full job board at `/jobs`. Filters and sorts all non-archived listings.

**Filters (top row):**
- Category (Software, IT, Customer Service, General)
- Source (any active scraper)
- Status (New, Saved, Viewed, Applied, Archived)
- Minimum match score (slider)
- Job duration (Full Time, Part Time, Contract, Internship, Temporary, Seasonal, Freelance, Per Diem)

**Per-row actions:** View detail · Save · Archive · Apply · Quick Apply

**Job detail page** (`/jobs/<id>`):
- Full description, matched skills list, company info, salary range
- Generate cover letter · Send application email · Apply / Quick Apply buttons

**Posted Date Color Coding** — three configurable age tiers highlight how fresh each listing is (colors set in Settings → Posted Date Colors).

---

### Applications

Track every application at `/applications`. Status pipeline:

`Draft → Submitted → Phone Screen → Interview → Offer → Rejected / Withdrawn`

**Application detail page** (`/applications/<id>`):
- Status dropdown, applied date
- Interview section: date, type (Phone / Video / In-Person / Technical)
- Follow-up date picker
- Contact info: hiring manager name & email
- Salary offered
- Notes free-text area
- Cover letter (editable)
- **Calendar export buttons** — download `.ics` files for: Interview only · Follow-up only · All events combined

---

### Email Response

Automatic inbox monitoring at `/email-response`.

Configure one or more email accounts in **Settings → Response Email Monitoring**:
- **Gmail** — App Password method (2FA required)
- **Outlook / Hotmail** — App Password method
- **Custom IMAP** — any IMAP-compatible mailbox

**How it works:**
1. The checker scans each configured inbox for emails related to job applications.
2. Emails are classified as **Declined** or **Interview** using keyword matching (42 decline phrases, 29 interview phrases).
3. Matched emails are auto-linked to the most likely application.
4. Unconfirmed matches appear on the Email Response page for your review.

**Page layout:**
- **Confirmed Matches** (top) — auto-matched emails already linked to an application
- **Unmatched Applications** (left panel) — applications waiting to be linked
- **Pending Reviews** (right panel) — emails awaiting your confirmation; click to expand the email preview, then confirm or reject the suggested match

**Check Now** button triggers an immediate inbox scan without waiting for the next scheduled check.

---

### Analytics

Charts and stats at `/analytics`.

**Metric cards:** Total Jobs · Applications · Interviews · Offers · Rejected · Response Rate · Offer Rate

**Charts:**
- **Application Pipeline** — bar chart by status stage
- **Jobs by Source** — doughnut showing which scrapers produced the most listings
- **Weekly Activity** — line chart (8-week history) of jobs found vs. applications submitted
- **Jobs by Category** — doughnut breakdown (Software, IT, Customer Service, General)
- **Match Score Distribution** — histogram of all job scores

---

### Sources (Scrapers)

Manage all job data sources at `/scrapers`.

#### Active / Unavailable Tabs

Each source card has a toggle switch in the top-right corner:
- **Toggle ON** → card stays in the **Active** tab; source is included in all scans
- **Toggle OFF** → confirmation prompt appears with an optional reason field; card moves to the **Unavailable** tab under "Disabled by You"
- Toggle ON again from the Unavailable tab to re-enable the source

#### Built-In Sources

| Source | Type | Notes |
|--------|------|-------|
| RemoteOK | Built-in | Fully remote jobs (remoteok.io) |
| Remotive | Built-in | Remote-first jobs (remotive.io) |
| We Work Remotely | Built-in | Remote-only positions |
| ZipRecruiter | Built-in | General job board |
| LinkedIn | Built-in | Limited availability |
| USAJobs | Built-in (API) | U.S. federal government jobs — **requires free API key** |
| CareerOneStop | Built-in (API) | Dept. of Labor jobs — **requires free API key** |

#### API Key Setup (USAJobs & CareerOneStop)

Sources requiring API keys display an **amber pulsing outline** when credentials are not yet configured.
Each such card shows a key-setup panel:
1. Click **Get Key** — opens the provider's developer registration page in a new tab
2. Enter your credentials in the password fields (one per required key/token)
3. Click **Save & Test** — credentials are written to `.env`, the running process is updated immediately, and a live test call is made to confirm they work
4. Inline feedback shows success or the specific error returned by the API

#### Custom Sources

Click **+ Add Source** to add your own RSS feed or JSON API scraper:
- Provide a display name, source type (RSS / JSON API), and URL template
- Add optional search terms (one per line or comma-separated)
- Custom sources can be renamed, edited, and deleted from their card

#### Card Features (per source)

- **Sort order** — active cards are automatically sorted by jobs found in the last scan (most productive first)
- **Green glow** — cards that found jobs in the most recent scan pulse with a soft green outline
- **Jobs badge** — job count badge appears in the card header when jobs were found
- **Live refresh** — clicking "Run now" polls the server every 2 seconds and updates the card stats (count, timestamp, error) without a page reload; cards re-sort in place when the scan completes
- **Health status** — each card shows Current / Broken / Not Checked with the check timestamp
- **Error detail** — if a scan fails, the error message appears in a red box on the card

#### Unavailable Tab

Two read-only sections at the bottom:
- **Disabled by You** — your deactivated sources with your optional reason; toggle back on to reactivate
- **Cannot Be Scraped** — informational cards for sources blocked by technical or legal constraints (LinkedIn full API, Indeed, Glassdoor, JobRight AI) with an explanation of why each cannot be automated

#### Source Discovery & Health Checks

Buttons at the top of the Sources page:
- **Check Health** — tests all active sources for connectivity; updates Current / Broken badges
- **Discover Sources** — scans common job board patterns for available RSS/API feeds to add
- **Discover Boards** — searches major job boards (LinkedIn, Indeed, etc.) for skill-based search URLs

---

### Unemployment Tracker

> **Must be enabled in Settings before the page appears in the navigation.**
>
> Go to **Settings → Unemployment** and check **Enable Unemployment Tracking**.

Once enabled, the **Unemployment** link appears in the sidebar and the page is accessible at `/unemployment`.

**What it tracks:**
- Claim week periods (start date and length configurable in Settings → Unemployment)
- Number of job applications submitted during each claim week
- Whether the weekly required minimum has been met (configurable; default is 3 per week)
- Running total across all claim weeks

**Page contents:**
- **Summary cards** — benefits start date, end date, total claim weeks, total applications across all weeks
- **Claim weeks table** — each row is one claim week showing the date range, application count, and a neon checkmark ✓ when the requirement is met
- **Expand a week** — click any row to see the individual job applications counted toward that week

**Export options:**
- **CSV** — comma-separated file of all claim weeks with counts; suitable for state agency submission
- **Excel** — formatted spreadsheet with the same data
- **Print / Printable Summary** — browser print dialog with a clean, two-column layout

**Settings for Unemployment (Settings → Unemployment):**

| Setting | Description |
|---------|-------------|
| Enable Unemployment Tracking | Shows/hides the Unemployment page in the nav |
| State | Your state (displayed on export headers) |
| State Agency URL | Link to your state's unemployment portal (shown on the page) |
| Claim Week Start Date | The first day of your initial claim week |
| Required Applications Per Week | Minimum applications needed (triggers notification if not met) |

---

### Notifications

All system alerts at `/notifications`.

**Notification types:**
- `new_job` — a high-scoring new job was found
- `follow_up` — an application is overdue for follow-up
- `interview` — an upcoming interview reminder
- `offer` — offer received alert
- `reminder` — general reminder
- `unemployment_alarm` — weekly application requirement not yet met
- `scan_error` — a scraper failed during a scan

**Actions per notification:** Mark read · Archive · Dismiss

**Auto-cleanup** (configured in Settings → Notifications):
- Auto-archive notifications older than N days (default: 30)
- Auto-delete archived notifications older than N days (default: 60)

---

### Settings

Configuration center at `/settings`. Sections can be reordered by drag-and-drop (Manual layout) or auto-balanced (Auto layout). Toggle between layout modes at the top of the page.

#### Your Profile
- Applicant name, email, phone (country code + number), location (state + radius in miles)
- Years of experience, LinkedIn URL, GitHub URL
- Resume file path (browse button opens a file picker)

#### Response Email Monitoring
- Add Gmail, Outlook, or custom IMAP accounts
- Per-account toggle (enable/disable checking)
- **Test** button validates IMAP connectivity before saving
- **Check Now** button triggers an immediate inbox scan
- Accounts show last-checked time and connection status

#### Cover Letters
- Toggle AI-generated vs. template-based cover letters
- OpenAI model selection (gpt-4o-mini default)
- OpenAI API key input

#### Notifications
- Auto-archive: toggle + days threshold
- Auto-delete archived: toggle + days threshold

#### Calendar Integration
- Live `.ics` feed URL (copy to Google Calendar / Outlook)
- Regenerate Token button (invalidates old URL, creates new one)

#### Job Filtering
- Minimum match score (0–100 slider)
- Required skills (comma-separated; each match = +20 pts)
- Preferred skills (comma-separated; each match = +8 pts)
- Job duration filter (check only the durations you want; leave all unchecked to see all)
- Use Resume toggle — extract required/preferred skills automatically from your resume file

#### Scheduled Scans
- Enable/disable automatic scans
- Frequency: Daily · Weekly · Monthly
- Scan times (add multiple HH:MM times; e.g. 08:00, 20:00)
- Weekdays selector (for weekly frequency)
- Month days selector (for monthly frequency)
- Timezone selector

#### Posted Date Colors
Three configurable age tiers:
- Tier 1 — jobs posted within N days (default 7) show in color A (default green)
- Tier 2 — jobs posted within N days (default 30) show in color B (default orange)
- Tier 3 — older jobs show in grey

#### Unemployment
- **Enable Unemployment Tracking** checkbox — controls whether the Unemployment page appears
- State selector
- State agency URL
- Claim week start date
- Required applications per week

#### Database Tools
- **Reset Database** — deletes all jobs, applications, and scan data (irreversible)
- **Repair Database** — runs schema migrations to add any missing columns
- **Backup** — downloads a copy of the SQLite database file

---

### Changelog

Version history at `/changelog`. Lists all releases with feature additions, bug fixes, and changes. Updated automatically by the Build Dashboard when a new version is compiled.

---

### Server Logs

Live log viewer at `/server`.

- Displays Flask application log output in the browser
- **Capture** button freezes the current log content for inspection
- **Clear** button empties the displayed log
- Useful for debugging scraper errors, email check failures, or scheduler issues without opening a terminal

---

## Job Scoring & Filtering

Every job fetched by a scraper is scored before being saved.

**Score calculation:**
- Each **required skill** matched in the title, description, or tags: **+20 points**
- Each **preferred skill** matched: **+8 points**
- Maximum score is uncapped (multiple skill matches accumulate)

**Filters applied before saving:**
1. Score must meet or exceed your **Minimum Match Score** (Settings → Job Filtering)
2. Location must pass the radius filter (remote jobs always pass)
3. Job duration must match your duration filter (if one is set)

**Skill sources:**
- Manually entered required/preferred skills (Settings → Job Filtering)
- Skills extracted from your resume file (if "Use Resume" is enabled)

**Job categories** (auto-detected from title/description keywords):
- Software, IT, Customer Service, General

**Job durations** (auto-detected):
Full Time, Part Time, Contract, Internship, Temporary, Seasonal, Freelance, Per Diem

---

## Scheduler Daemon

`scheduler_daemon.py` — a standalone background process that runs job scans and follow-up reminders independently of the browser session.

**Starting the daemon:**
- From Settings: use the Start / Stop daemon controls
- From command line: `python scheduler_daemon.py`
- Arguments:
  - `--once` — run one scan cycle then exit
  - `--no-tray` — disable the system tray icon

**Behavior:**
- Reads schedule configuration from the database at runtime (no restart needed after settings change)
- Runs morning and evening scans (times configured in Settings → Scheduled Scans)
- Sends email notifications when high-score matches are found
- Sends follow-up reminder notifications every 24 hours
- Logs to `logs/scheduler_daemon.log`
- Writes a PID file (`scheduler_daemon.pid`) so the web app can check its status

**System Tray Icon (Windows):**
- Appears in the Windows taskbar notification area when the daemon is running
- Right-click menu: **Scan Now** · **Open Job Tracker** · **Exit**
- Icon is auto-installed (requires `pystray` and `Pillow`; installed automatically on first launch if missing)
- Tray icon can be disabled from Settings (`show_tray_icon = false`)

---

## Cover Letters

Two modes (configured in Settings → Cover Letters):

**Template-based (default):**
- Fills a built-in template with job title, company name, applicant name, matched skills, and years of experience
- Works offline, no API key needed

**AI-powered (optional):**
- Uses OpenAI API (gpt-4o-mini by default, configurable)
- Generates a tailored cover letter using the full job description and your profile
- Falls back to the template automatically if the API call fails or no key is configured
- Requires an OpenAI API key in Settings → Cover Letters

Cover letters can be edited after generation from the application detail page before sending.

---

## Calendar Integration

**Per-application ICS export** — download `.ics` files from any application detail page:
- Interview appointment only
- Follow-up reminder only
- Combined (all events for that application)

**Live feed URL** — a secret-token `.ics` URL that can be subscribed to in Google Calendar, Outlook, or any calendar app. The feed includes all upcoming interviews and follow-up reminders across all applications.

- Copy the feed URL from **Settings → Calendar Integration**
- Click **Regenerate Token** to invalidate the current URL and get a new one (useful if the URL is compromised)

---

## Resume Parsing

Enable in **Settings → Job Filtering → Use Resume**.

- Supported formats: PDF, DOCX
- The parser extracts skill keywords from the resume text
- Extracted skills are split into **required** and **preferred** categories (configurable)
- These replace the manually entered skill lists when "Use Resume" is active
- Skills discovered during job scans are also tracked (`discovered_skills` setting)

---

## HTTPS Setup (Optional)

1. Install [mkcert](https://github.com/FiloSottile/mkcert): `winget install FiloSottile.mkcert`
2. Run `mkcert -install` (adds a local CA to Windows + Chrome trust store)
3. In the project root: `mkdir certs && cd certs && mkcert 127.0.0.1 localhost`
4. Rename the generated files to `localhost.pem` and `localhost-key.pem`
5. Restart `run.py` — it detects the certs and switches to HTTPS automatically

No browser security warnings — the certificate is trusted by Windows and Chrome.

---

## Project Structure

```
job_tracker/
├── run.py                    # Entry point — starts Flask + APScheduler, opens browser
├── app.py                    # Flask app factory (create_app), all routes
├── models.py                 # SQLAlchemy ORM models (Job, Application, Notification, ScraperSource, Setting, …)
├── scrapers.py               # Scraper registry, built-in scraper classes, UNAVAILABLE_SOURCES list
├── filter_engine.py          # Job scoring, categorisation, location filtering, duration detection
├── scheduler.py              # APScheduler job definitions (scan, follow-up reminders, email check)
├── scheduler_daemon.py       # Standalone background daemon + system tray icon
├── resume_parser.py          # PDF/DOCX skill extractor
├── calendar_service.py       # Google Calendar / Outlook ICS integration
├── config.py                 # Config class — reads .env / environment variables
├── extensions.py             # SQLAlchemy db object (avoids circular imports)
├── requirements.txt          # Python dependencies
├── templates/                # Jinja2 HTML templates (Bootstrap 5 dark theme)
│   ├── base.html             # Shared layout: sidebar nav, topbar, hints system
│   ├── dashboard.html        # Home — metrics, recent matches, scan logs
│   ├── jobs.html             # Job listings with filters
│   ├── job_detail.html       # Single job view
│   ├── applications.html     # Application pipeline list
│   ├── application_detail.html # Application form + calendar export
│   ├── email_response.html   # Email monitoring & manual match interface
│   ├── analytics.html        # Charts and stats
│   ├── scrapers.html         # Sources management page
│   ├── unemployment.html     # Claim week tracker
│   ├── notifications.html    # Notification centre
│   ├── settings.html         # Full settings page
│   ├── changelog.html        # Version history
│   ├── server.html           # Live server log viewer
│   └── help.html             # In-app help
├── static/                   # CSS, JS, images
├── build/                    # Build Dashboard (separate Flask app, port 5001)
│   ├── builder_app.py        # Build Dashboard Flask app (~47 routes)
│   ├── builder.html          # Build Dashboard UI
│   ├── builder_help.html     # Build Dashboard help
│   ├── build.py              # PyInstaller + Inno Setup pipeline
│   ├── build_config.py       # Build settings loader
│   └── build_dash_daemon.py  # Build Dashboard background daemon
├── start_builder.ps1         # PowerShell launcher for the Build Dashboard
├── start_builder.py          # Python launcher for the Build Dashboard
├── start_job_tracker.ps1     # PowerShell launcher for Job Tracker
├── certs/                    # mkcert TLS certificates (gitignored)
├── instance/                 # SQLite database — job_tracker.db (gitignored)
└── logs/                     # Daemon and scheduler logs (gitignored)
```

---

## Git Workflow

| Branch | Purpose |
|--------|---------|
| `development` | All active development — default working branch |
| `main` | Production releases only — built and tagged via Build Dashboard |

All code changes go to `development`. The Build Dashboard automates checkout of `main`, compiles the installer, then returns to `development`.

**Remote:** `https://github.com/TechnoSage/job_tracker`

---

## Build Dashboard (port 5001)

A separate Flask application that compiles Job Tracker into a distributable Windows installer.

**Start it:**
```powershell
.\start_builder.ps1
```
Or:
```bash
python start_builder.py
```
Then open `http://127.0.0.1:5001`.

### Features

- **App Info** — configure name, version, publisher, URLs, icon
- **Code Protection** — Plain PyInstaller / PyArmor (AES-256 obfuscation) / Nuitka (native machine code)
- **License File** — generate and embed a software license (EULA)
- **README Generator** — auto-generate a project README
- **Git Integration** — status, commit, push, merge dev→main
- **GitHub Auth** — `gh` CLI auth badge; repo picker modal; remote URL validation with green/red pulse
- **Pull Requests** — list open PRs and create new ones from the dashboard
- **CI/CD Pipeline** — animated five-stage build pipeline: Deps → Tools → Protect → Bundle → Installer
- **Test Pipeline** — auto-launches compiled exe after build and polls the port to verify startup
- **Version Management** — Major / Minor / Patch increment buttons; CHANGELOG.json updated automatically
- **Local Backup** — robocopy mirror of the project to a secondary drive on a configurable schedule
- **Build Dash Daemon** — background Windows process for scheduled backups; start/stop from the UI; compile to `.exe` with custom tray icon

### Build Output

```
D:\Compile Playground\job_tracker\
├── JobTracker_Setup_<version>.exe     ← distributable installer
├── Bundle Only\
│   └── JobTracker\                    ← PyInstaller / Nuitka bundle
│       ├── JobTracker.exe
│       └── …
└── Log\
    └── JobTracker_Build_<timestamp>.log
```

---

## Logging

| Log | Location | Contents |
|-----|----------|----------|
| Application log | `job_tracker.log` | Flask requests, scheduler events, scraper results, errors |
| Daemon log | `logs/scheduler_daemon.log` | Background process activity |
| Build logs | `<OutputDir>/Log/` | Timestamped build output per compilation run |
| Test pipeline logs | `<OutputDir>/Log/<ExeName>_Output_Pipeline_<timestamp>.log` | Exe startup verification |

ANSI escape codes are stripped from all log files automatically.

---

## GitHub

Repository: https://github.com/TechnoSage/job_tracker
Issues / Support: https://github.com/TechnoSage/job_tracker/issues
