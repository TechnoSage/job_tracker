"""
scheduler.py — APScheduler jobs: daily scans at 8 AM & 8 PM, reminders, etc.
"""
import json
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)


# ── Live scan progress state ──────────────────────────────────────────────────

_scan_lock = threading.Lock()
_scan_state: dict = {
    "running":        False,
    "phase":          "",       # "fetching" | "processing" | "done" | "error"
    "total_sources":  0,
    "done_sources":   0,
    "current_source": "",
    "current_url":    "",
    "sources":        [],       # completed: [{name, url, status, jobs}]
    "total_fetched":  0,
    "total_new":      0,
    "total_matched":  0,
    "error":          "",
}


def get_scan_progress() -> dict:
    with _scan_lock:
        return {
            "running":        _scan_state["running"],
            "phase":          _scan_state["phase"],
            "total_sources":  _scan_state["total_sources"],
            "done_sources":   _scan_state["done_sources"],
            "current_source": _scan_state["current_source"],
            "current_url":    _scan_state["current_url"],
            "sources":        list(_scan_state["sources"]),
            "total_fetched":  _scan_state["total_fetched"],
            "total_new":      _scan_state["total_new"],
            "total_matched":  _scan_state["total_matched"],
            "error":          _scan_state["error"],
        }


# ---------------------------------------------------------------------------
# Core scan task (runs inside Flask app context)
# ---------------------------------------------------------------------------

def run_job_scan(app):
    """
    Full job scan pipeline:
      1. Fetch from all sources
      2. Score each job
      3. Persist new jobs to DB
      4. Send email notification for new matches
    """
    from extensions import db
    from models import Job, ScanLog, Notification, Setting, ScraperSource
    from scrapers import registry as scraper_registry, lookup_company_info
    from filter_engine import score_job, categorise_job, is_relevant, location_passes, detect_job_duration
    from email_service import send_new_jobs_notification
    from config import Config

    cfg = Config()
    start = time.time()
    new_matches = []

    # Initialise live progress state
    with _scan_lock:
        _scan_state.update(
            running=True, phase="starting",
            total_sources=0, done_sources=0,
            current_source="", current_url="",
            sources=[], total_fetched=0,
            total_new=0, total_matched=0, error="",
        )

    def _on_source(event, display_name, url, count):
        with _scan_lock:
            if event == "start":
                _scan_state["current_source"] = display_name
                _scan_state["current_url"]    = url
            else:
                _scan_state["done_sources"]  += 1
                _scan_state["total_fetched"] += count
                _scan_state["sources"].append({
                    "name":   display_name,
                    "url":    url,
                    "status": event,
                    "jobs":   count,
                })
                _scan_state["current_source"] = ""
                _scan_state["current_url"]    = ""

    try:
        with app.app_context():
            # Count enabled sources so the progress bar has a denominator
            total_src = ScraperSource.query.filter_by(is_enabled=True).count()
            with _scan_lock:
                _scan_state["total_sources"] = total_src
                _scan_state["phase"] = "fetching"

            log = ScanLog(scan_date=datetime.utcnow(), source="all", status="running")
            db.session.add(log)
            db.session.commit()

            try:
                # Read filter settings from DB (user-configurable via Settings page)
                applicant_state = Setting.get("applicant_location", "Remote")
                applicant_miles = int(Setting.get("applicant_location_miles", "25"))
                allowed_durations = [
                    d.strip() for d in Setting.get("job_duration_filter", "").split(",") if d.strip()
                ]
                min_match_score = int(Setting.get("min_match_score", str(cfg.MIN_MATCH_SCORE)))

                # Skills: use resume if "Use Resume" is toggled on, otherwise use manual fields
                if Setting.get("use_resume", "false") == "true":
                    required_skills  = [s.strip() for s in Setting.get("resume_required_skills",  "").split(",") if s.strip()]
                    preferred_skills = [s.strip() for s in Setting.get("resume_preferred_skills", "").split(",") if s.strip()]
                else:
                    _req_str  = Setting.get("required_skills",  ",".join(cfg.REQUIRED_SKILLS))
                    _pref_str = Setting.get("preferred_skills", ",".join(cfg.PREFERRED_SKILLS))
                    required_skills  = [s.strip() for s in _req_str.split(",")  if s.strip()]
                    preferred_skills = [s.strip() for s in _pref_str.split(",") if s.strip()]

                # Fetch from all enabled sources with per-source progress callbacks
                raw_jobs, _statuses = scraper_registry.fetch_all_enabled(app, on_source=_on_source)
                log.jobs_found = len(raw_jobs)

                with _scan_lock:
                    _scan_state["phase"] = "processing"

                new_count = 0
                matched_count = 0
                per_source_new: dict = {}  # ScraperSource.name -> new matching job count

                for raw in raw_jobs:
                    ext_id = raw.get("external_id", "")
                    if not ext_id:
                        continue

                    # Skip if already in DB
                    if Job.query.filter_by(external_id=ext_id).first():
                        continue

                    title = raw.get("title", "")
                    description = raw.get("description", "")
                    tags = raw.get("tags", [])

                    score, matched = score_job(
                        title, description, tags,
                        required_skills=required_skills,
                        preferred_skills=preferred_skills,
                    )

                    if not is_relevant(score, min_match_score):
                        continue

                    # Location filter — skip jobs outside the configured state/radius
                    if not location_passes(raw.get("location", ""), applicant_state, applicant_miles):
                        continue

                    # Duration detection and optional filter
                    duration = detect_job_duration(
                        title, description, tags,
                        salary_range=raw.get("salary_range", ""),
                    )
                    if allowed_durations and duration and duration not in allowed_durations:
                        continue

                    category = categorise_job(title, description)
                    matched_count += 1

                    co_info = lookup_company_info(raw.get("company", ""))
                    job = Job(
                        external_id=ext_id,
                        title=title,
                        company=raw.get("company", ""),
                        location=raw.get("location", "Remote"),
                        is_remote=True,
                        job_duration=duration or None,
                        job_category=category,
                        description=description,
                        tags=json.dumps(tags),
                        salary_range=raw.get("salary_range", ""),
                        url=raw.get("url", ""),
                        source=raw.get("source", ""),
                        posted_date=raw.get("posted_date"),
                        found_date=datetime.utcnow(),
                        match_score=score,
                        matched_skills=json.dumps(matched),
                        status="new",
                        company_address=co_info.get("company_address", ""),
                        company_phone=co_info.get("company_phone", ""),
                    )
                    db.session.add(job)
                    new_count += 1
                    new_matches.append(job)
                    raw_src = raw.get("source", "")
                    if raw_src:
                        per_source_new[raw_src] = per_source_new.get(raw_src, 0) + 1

                db.session.flush()   # assign IDs before notifications

                # Update each source's last_jobs_found to reflect only criteria-matching new jobs
                for src_name, cnt in per_source_new.items():
                    src_row = ScraperSource.query.filter_by(name=src_name).first()
                    if src_row is not None:
                        src_row.last_jobs_found = cnt
                # Sources that ran successfully but had 0 new matching jobs → reset to 0
                for src_name, status in _statuses.items():
                    if status == "" and src_name not in per_source_new:
                        src_row = ScraperSource.query.filter_by(name=src_name).first()
                        if src_row is not None:
                            src_row.last_jobs_found = 0

                # Create in-app notifications for new matches
                for job in new_matches:
                    notif = Notification(
                        type="new_job",
                        title=f"New match: {job.title}",
                        message=f"{job.company} — Score {job.match_score}",
                        job_id=job.id,
                    )
                    db.session.add(notif)

                db.session.commit()

                log.jobs_new = new_count
                log.jobs_matched = matched_count
                log.status = "success"
                log.duration_seconds = round(time.time() - start, 2)
                db.session.commit()

                with _scan_lock:
                    _scan_state["total_new"]     = new_count
                    _scan_state["total_matched"] = matched_count
                    _scan_state["phase"]         = "done"

                logger.info(
                    "Scan complete: %d fetched, %d new, %d matched (%.1fs)",
                    len(raw_jobs), new_count, matched_count, time.time() - start,
                )

                # Discover new skills from scraped job tags and persist them
                _update_discovered_skills(raw_jobs)

                # Send email notification
                if new_matches:
                    send_new_jobs_notification(cfg, new_matches)

            except Exception as exc:
                logger.exception("Job scan failed: %s", exc)
                log.status = "error"
                log.error_message = str(exc)
                log.duration_seconds = round(time.time() - start, 2)
                db.session.commit()
                with _scan_lock:
                    _scan_state["phase"] = "error"
                    _scan_state["error"] = str(exc)

    except Exception as exc:
        logger.exception("Job scan outer error: %s", exc)
        with _scan_lock:
            _scan_state["phase"] = "error"
            _scan_state["error"] = str(exc)

    finally:
        with _scan_lock:
            _scan_state["running"] = False


def _update_discovered_skills(raw_jobs: list) -> None:
    """
    Extract tags from raw job dicts and persist any skills not already in the
    static taxonomy to the 'discovered_skills' DB setting.
    Called inside an active app context.
    """
    try:
        from skills_taxonomy import get_all_skills_flat
        from models import Setting
        from extensions import db

        known = get_all_skills_flat()

        discovered_raw = Setting.get("discovered_skills", "{}")
        try:
            discovered = json.loads(discovered_raw)
        except Exception:
            discovered = {}

        sub_key = "From Job Scans"
        existing_new = set(s.lower() for s in discovered.get(sub_key, []))
        additions = list(discovered.get(sub_key, []))
        changed = False

        for raw in raw_jobs:
            for tag in raw.get("tags", []):
                tag_clean = tag.strip()
                if not tag_clean:
                    continue
                tag_lower = tag_clean.lower()
                if tag_lower not in known and tag_lower not in existing_new:
                    additions.append(tag_clean)
                    existing_new.add(tag_lower)
                    changed = True

        if changed:
            discovered[sub_key] = additions
            Setting.set("discovered_skills", json.dumps(discovered))
            logger.info("Discovered %d new skills from job tags.", len(additions))
    except Exception as exc:
        logger.warning("Skill discovery failed: %s", exc)


def run_followup_reminders(app):
    """Check for overdue follow-ups and send email reminders."""
    from extensions import db
    from analytics import overdue_followups
    from email_service import send_follow_up_reminder
    from config import Config

    cfg = Config()
    with app.app_context():
        overdue = overdue_followups()
        for app_obj in overdue:
            send_follow_up_reminder(cfg, app_obj)
            logger.info(
                "Follow-up reminder sent for application %d (%s @ %s)",
                app_obj.id, app_obj.job.title, app_obj.job.company,
            )


def run_unemployment_alarms(app):
    """
    Daily check (9:05 AM):
      1. If today is the day after the benefit end date, fire an upload reminder.
      2. If the current claim week is 3/2/1 day(s) from its end and the required
         applications haven't been met, fire an escalating shortage alarm.
    The upload reminder fires even when weekly alarms are disabled.
    """
    from datetime import date, timedelta, datetime as dt
    from extensions import db
    from models import Application, Notification, Setting

    with app.app_context():
        if Setting.get("unemployment_enabled", "false") != "true":
            return

        start_str = Setting.get("unemployment_start_date", "")
        end_str   = Setting.get("unemployment_end_date", "")
        if not start_str:
            return

        try:
            start_date = dt.strptime(start_str, "%Y-%m-%d").date()
        except ValueError:
            return
        try:
            end_date = (dt.strptime(end_str, "%Y-%m-%d").date() if end_str
                        else start_date.replace(year=start_date.year + 1) - timedelta(days=1))
        except (ValueError, OverflowError):
            end_date = start_date.replace(year=start_date.year + 1) - timedelta(days=1)

        today = date.today()

        def _already_notified_today(title):
            today_start = dt.combine(today, dt.min.time())
            today_end   = dt.combine(today + timedelta(days=1), dt.min.time())
            return Notification.query.filter(
                Notification.type == "unemployment_alarm",
                Notification.title == title,
                Notification.created_at >= today_start,
                Notification.created_at <  today_end,
            ).first() is not None

        # ── 1. Upload reminder: day after benefit end date ────────────────
        upload_day = end_date + timedelta(days=1)
        if today == upload_day:
            title = "Upload Jobs to Unemployment Site"
            if not _already_notified_today(title):
                unemp_state = Setting.get("unemployment_state", "State")
                notif = Notification(
                    type="unemployment_alarm",
                    title=title,
                    message=(
                        f"📤 Today is the day to upload all your job applications to the "
                        f"{unemp_state} unemployment site. "
                        f"Your benefit period ended {end_date.strftime('%B %d, %Y')}."
                    ),
                )
                db.session.add(notif)
                db.session.commit()
                logger.info("Upload reminder notification created.")

        # ── 2. Weekly shortage alarms ─────────────────────────────────────
        alarm_3 = Setting.get("unemployment_alarm_3day", "false") == "true"
        alarm_2 = Setting.get("unemployment_alarm_2day", "false") == "true"
        alarm_1 = Setting.get("unemployment_alarm_1day", "false") == "true"
        if not any([alarm_3, alarm_2, alarm_1]):
            return

        required     = int(Setting.get("unemployment_required_per_week", "3"))
        week_start   = int(Setting.get("unemployment_week_start", "0"))
        week_end_day = int(Setting.get("unemployment_week_end", "5"))

        # Locate the current claim period
        days_offset      = (week_start - start_date.weekday()) % 7
        current          = start_date + timedelta(days=days_offset)
        days_to_end      = (week_end_day - week_start) % 7
        period_num       = 1
        cur_period_start = cur_period_end = None

        while current <= min(end_date, today + timedelta(days=7)):
            p_end = min(current + timedelta(days=days_to_end), end_date)
            if current <= today <= p_end:
                cur_period_start = current
                cur_period_end   = p_end
                break
            current    += timedelta(days=7)
            period_num += 1

        if not cur_period_start:
            return

        days_left = (cur_period_end - today).days
        alarm_applies = (
            (days_left == 3 and alarm_3) or
            (days_left == 2 and alarm_2) or
            (days_left == 1 and alarm_1)
        )
        if not alarm_applies:
            return

        # Count submitted applications this period
        all_apps = Application.query.filter(Application.status != "draft").all()

        def _app_date(a):
            d = a.applied_date or a.created_at
            return d.date() if d else None

        app_count = sum(
            1 for a in all_apps
            if _app_date(a) and cur_period_start <= _app_date(a) <= cur_period_end
        )
        if app_count >= required:
            return

        shortage = required - app_count
        alarm_title = (
            f"Unemployment Week {period_num}: "
            f"{shortage} more application{'s' if shortage != 1 else ''} needed"
        )
        if _already_notified_today(alarm_title):
            return

        urgency_map  = {1: "URGENT",   2: "Warning",  3: "Reminder"}
        severity_map = {1: "🚨",       2: "⚠️",       3: "📅"}
        urgency = urgency_map.get(days_left, "Reminder")
        icon    = severity_map.get(days_left, "📅")
        message = (
            f"{icon} {urgency} — {days_left} day{'s' if days_left != 1 else ''} left in "
            f"claim week {period_num} ({cur_period_start.strftime('%b %d')}–"
            f"{cur_period_end.strftime('%b %d')}). "
            f"{app_count}/{required} applications submitted."
        )

        notif = Notification(type="unemployment_alarm", title=alarm_title, message=message)
        db.session.add(notif)
        db.session.commit()
        logger.info("Unemployment alarm created: %s", alarm_title)


def run_interview_reminders(app):
    """Send 24-hour advance reminders for upcoming interviews."""
    from extensions import db
    from models import Application
    from email_service import send_interview_reminder
    from config import Config
    from datetime import timedelta

    cfg = Config()
    with app.app_context():
        now = datetime.utcnow()
        window_start = now
        window_end = now + timedelta(hours=25)
        upcoming = (
            Application.query
            .filter(
                Application.interview_date >= window_start,
                Application.interview_date <= window_end,
                Application.status == "interview",
            )
            .all()
        )
        for app_obj in upcoming:
            send_interview_reminder(cfg, app_obj)
            logger.info("Interview reminder sent for application %d", app_obj.id)


# ---------------------------------------------------------------------------
# Scheduler initialisation
# ---------------------------------------------------------------------------

def reschedule_scan_jobs(app):
    """
    Read scan settings from DB and reschedule (or remove) the auto_scan job.
    Safe to call at startup or immediately after settings are saved.
    """
    from extensions import scheduler
    from apscheduler.triggers.cron import CronTrigger
    from models import Setting

    # Read settings inside app context
    with app.app_context():
        auto_enabled   = Setting.get("scan_auto_enabled", "true") == "true"
        frequency      = Setting.get("scan_frequency", "daily")
        scan_times     = Setting.get("scan_times", "08:00,20:00")
        scan_weekdays  = Setting.get("scan_weekdays", "0,1,2,3,4")
        scan_monthdays = Setting.get("scan_monthdays", "1")
        tz             = Setting.get("timezone", "America/New_York")

    # Remove legacy and existing scan jobs
    for job_id in ("morning_scan", "evening_scan", "auto_scan"):
        try:
            scheduler.remove_job(job_id)
        except Exception:
            pass

    if not auto_enabled:
        logger.info("Automated scans disabled — no scan jobs scheduled.")
        return

    # Parse "HH:MM,HH:MM" → comma-separated hours for CronTrigger
    hours_parts = []
    for t in scan_times.split(","):
        t = t.strip()
        if t:
            h = t.split(":")[0].lstrip("0") or "0"
            if h not in hours_parts:
                hours_parts.append(h)
    hours_str = ",".join(hours_parts) if hours_parts else "8"

    trigger_kwargs: dict = {"hour": hours_str, "minute": 0, "timezone": tz}
    if frequency == "weekly":
        trigger_kwargs["day_of_week"] = scan_weekdays
    elif frequency == "monthly":
        trigger_kwargs["day"] = scan_monthdays

    scheduler.add_job(
        func=run_job_scan,
        args=[app],
        trigger=CronTrigger(**trigger_kwargs),
        id="auto_scan",
        replace_existing=True,
        misfire_grace_time=600,
    )

    logger.info(
        "Auto scan scheduled — frequency=%s, times=%s, tz=%s",
        frequency, scan_times, tz,
    )


def init_scheduler(app):
    """Register all scheduled jobs with APScheduler."""
    from extensions import scheduler
    from apscheduler.triggers.cron import CronTrigger
    from models import Setting

    with app.app_context():
        tz = Setting.get("timezone", "America/New_York")

    # Set up scan jobs from DB settings
    reschedule_scan_jobs(app)

    # Follow-up reminders at 9 AM daily
    scheduler.add_job(
        func=run_followup_reminders,
        args=[app],
        trigger=CronTrigger(hour=9, minute=0, timezone=tz),
        id="followup_reminders",
        replace_existing=True,
    )

    # Interview reminders at 8:30 AM daily
    scheduler.add_job(
        func=run_interview_reminders,
        args=[app],
        trigger=CronTrigger(hour=8, minute=30, timezone=tz),
        id="interview_reminders",
        replace_existing=True,
    )

    # Unemployment claim deadline alarms at 9:05 AM daily
    scheduler.add_job(
        func=run_unemployment_alarms,
        args=[app],
        trigger=CronTrigger(hour=9, minute=5, timezone=tz),
        id="unemployment_alarms",
        replace_existing=True,
    )

    if not scheduler.running:
        scheduler.start()

    logger.info("Scheduler started (tz=%s)", tz)
