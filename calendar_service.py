"""
calendar_service.py — Generate .ics calendar files, a live calendar feed,
                       and web-calendar deep links (no OAuth required).
"""
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytz
from icalendar import Calendar, Event, vText


_INTERVIEW_TYPE_LABELS = {
    "phone":     "Phone Screen",
    "video":     "Video Call",
    "technical": "Technical Round",
    "in_person": "In-Person",
}


# ---------------------------------------------------------------------------
# Live ICS feed  (subscribe once in Google/Outlook — auto-syncs all events)
# ---------------------------------------------------------------------------

def generate_feed_ics(applications, tz_name: str = "UTC") -> bytes:
    """
    Generate a complete ICS calendar feed containing all interview events
    and follow-up reminders for the given applications list.

    tz_name should be a pytz-compatible timezone string (e.g. "America/New_York").
    Naive datetimes stored in the DB are treated as already being in that timezone,
    then converted to UTC for maximum calendar-app compatibility.

    Subscribe to this URL in Google Calendar → Other calendars → From URL,
    or in Outlook → Add calendar → Subscribe from web.
    Google/Outlook will poll it periodically and keep the calendar in sync.
    """
    try:
        tz = pytz.timezone(tz_name)
    except Exception:
        tz = pytz.UTC

    now_utc = datetime.now(pytz.UTC)

    cal = Calendar()
    cal.add("prodid", "-//Job Tracker//job-tracker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("x-wr-calname", vText("Job Tracker"))
    cal.add("x-wr-caldesc", vText("Interviews and follow-up reminders from Job Tracker"))
    cal.add("x-wr-timezone", vText(tz_name))
    # Hint to calendar clients: refresh every 15 minutes
    cal.add("x-published-ttl", "PT15M")
    cal.add("refresh-interval;value=duration", "PT15M")

    for app in applications:
        job = app.job
        if not job:
            continue

        # ── Interview event (timed) ────────────────────────────────────────
        if app.interview_date:
            # Localize naive datetime (stored as user's local time) → UTC
            try:
                local_dt = tz.localize(app.interview_date, is_dst=None)
            except Exception:
                local_dt = tz.localize(app.interview_date)
            utc_dt = local_dt.astimezone(pytz.UTC)

            ev = Event()
            ev.add("summary", f"Interview: {job.title} @ {job.company}")
            ev.add("dtstart", utc_dt)
            ev.add("dtend", utc_dt + timedelta(hours=1))
            ev.add("dtstamp", now_utc)
            ev["uid"] = f"interview-{app.id}@job-tracker"
            itype = _INTERVIEW_TYPE_LABELS.get(app.interview_type or "", "")
            desc_parts = [
                f"Position: {job.title}",
                f"Company: {job.company}",
                f"Type: {itype or app.interview_type or 'TBD'}",
                f"Job URL: {job.url}",
            ]
            if app.contact_name:
                desc_parts.append(f"Contact: {app.contact_name}")
            if app.notes:
                desc_parts.append(f"\nNotes: {app.notes}")
            ev.add("description", "\n".join(desc_parts))
            ev.add("location", itype or "Remote / TBD")
            cal.add_component(ev)

        # ── Follow-up reminder (all-day, no timezone conversion needed) ────
        if app.next_follow_up:
            ev = Event()
            itype = _INTERVIEW_TYPE_LABELS.get(app.interview_type or "", "")
            type_suffix = f" ({itype})" if itype else ""
            ev.add("summary", f"Follow-up{type_suffix}: {job.title} @ {job.company}")
            ev.add("dtstart", app.next_follow_up.date())
            ev.add("dtend", (app.next_follow_up + timedelta(days=1)).date())
            ev.add("dtstamp", now_utc)
            ev["uid"] = f"followup-{app.id}@job-tracker"
            ev.add("description", (
                f"Remember to follow up on your application for:\n"
                f"{job.title} at {job.company}\n{job.url}"
            ))
            cal.add_component(ev)

    return cal.to_ical()


# ---------------------------------------------------------------------------
# Single-event .ics downloads
# ---------------------------------------------------------------------------

def create_interview_ics(application, duration_minutes: int = 60) -> bytes:
    """
    Returns an .ics file as bytes for the interview stored in *application*.
    """
    job = application.job
    interview_dt = application.interview_date or datetime.now()

    cal = Calendar()
    cal.add("prodid", "-//Job Tracker//job-tracker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "REQUEST")

    event = Event()
    event.add("summary", f"Interview: {job.title} @ {job.company}")
    event.add("dtstart", interview_dt)
    event.add("dtend", interview_dt + timedelta(minutes=duration_minutes))
    event.add("dtstamp", datetime.now())
    event["uid"] = str(uuid.uuid4())

    description_parts = [
        f"Position: {job.title}",
        f"Company: {job.company}",
        f"Type: {application.interview_type or 'TBD'}",
        f"Job URL: {job.url}",
    ]
    if application.contact_name:
        description_parts.append(f"Contact: {application.contact_name}")
    if application.contact_email:
        description_parts.append(f"Email: {application.contact_email}")
    if application.notes:
        description_parts.append(f"\nNotes: {application.notes}")

    event.add("description", "\n".join(description_parts))
    event.add("location", application.interview_type or "Remote / TBD")

    cal.add_component(event)
    return cal.to_ical()


def create_followup_ics(application) -> bytes:
    """
    Returns an .ics reminder for a follow-up action.
    """
    job = application.job
    followup_dt = application.next_follow_up or datetime.now()

    cal = Calendar()
    cal.add("prodid", "-//Job Tracker//job-tracker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")

    event = Event()
    itype = _INTERVIEW_TYPE_LABELS.get(application.interview_type or "", "")
    type_suffix = f" ({itype})" if itype else ""
    event.add("summary", f"Follow-up{type_suffix}: {job.title} @ {job.company}")
    event.add("dtstart", followup_dt.date())
    event.add("dtend", (followup_dt + timedelta(days=1)).date())
    event.add("dtstamp", datetime.now())
    event["uid"] = str(uuid.uuid4())
    event.add("description", (
        f"Remember to follow up on your application for:\n"
        f"{job.title} at {job.company}\n{job.url}"
    ))

    cal.add_component(event)
    return cal.to_ical()


def create_combined_ics(application) -> bytes:
    """
    Returns a single .ics file containing both the interview event and the
    follow-up reminder for this application — imports both in one click.
    """
    job = application.job
    now = datetime.now()

    cal = Calendar()
    cal.add("prodid", "-//Job Tracker//job-tracker//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")

    # ── Interview event (only if date is set) ─────────────────────────────
    if application.interview_date:
        ev = Event()
        ev.add("summary", f"Interview: {job.title} @ {job.company}")
        ev.add("dtstart", application.interview_date)
        ev.add("dtend", application.interview_date + timedelta(hours=1))
        ev.add("dtstamp", now)
        ev["uid"] = str(uuid.uuid4())
        itype = _INTERVIEW_TYPE_LABELS.get(application.interview_type or "", "")
        desc_parts = [
            f"Position: {job.title}",
            f"Company: {job.company}",
            f"Type: {itype or application.interview_type or 'TBD'}",
            f"Job URL: {job.url}",
        ]
        if application.contact_name:
            desc_parts.append(f"Contact: {application.contact_name}")
        if application.contact_email:
            desc_parts.append(f"Email: {application.contact_email}")
        if application.notes:
            desc_parts.append(f"\nNotes: {application.notes}")
        ev.add("description", "\n".join(desc_parts))
        ev.add("location", itype or "Remote / TBD")
        cal.add_component(ev)

    # ── Follow-up reminder (only if date is set) ──────────────────────────
    if application.next_follow_up:
        ev = Event()
        itype = _INTERVIEW_TYPE_LABELS.get(application.interview_type or "", "")
        type_suffix = f" ({itype})" if itype else ""
        ev.add("summary", f"Follow-up{type_suffix}: {job.title} @ {job.company}")
        ev.add("dtstart", application.next_follow_up.date())
        ev.add("dtend", (application.next_follow_up + timedelta(days=1)).date())
        ev.add("dtstamp", now)
        ev["uid"] = str(uuid.uuid4())
        ev.add("description", (
            f"Remember to follow up on your application for:\n"
            f"{job.title} at {job.company}\n{job.url}"
        ))
        cal.add_component(ev)

    return cal.to_ical()


# ---------------------------------------------------------------------------
# Web-calendar deep-link helpers (no OAuth required — opens calendar in browser)
# ---------------------------------------------------------------------------

def _google_url(title: str, description: str, dates_str: str) -> str:
    """Build a Google Calendar event-creation deep link."""
    params = {"action": "TEMPLATE", "text": title, "dates": dates_str,
              "details": description}
    return "https://calendar.google.com/calendar/render?" + urlencode(params)


def _outlook_url(title: str, description: str, startdt: str, enddt: str) -> str:
    """Build an Outlook Web (live.com) event-creation deep link."""
    params = {"subject": title, "body": description,
              "startdt": startdt, "enddt": enddt,
              "path": "/calendar/action/compose", "rru": "addevent"}
    return "https://outlook.live.com/calendar/0/deeplink/compose?" + urlencode(params)


def interview_web_links(application) -> dict:
    """Return Google and Outlook deep links for the interview event."""
    job = application.job
    dt = application.interview_date or datetime.now()
    end_dt = dt + timedelta(hours=1)
    title = f"Interview: {job.title} @ {job.company}"
    desc = (
        f"Position: {job.title}\nCompany: {job.company}\n"
        f"Type: {application.interview_type or 'TBD'}\nJob URL: {job.url}"
    )
    fmt = "%Y%m%dT%H%M%S"
    google = _google_url(title, desc, f"{dt.strftime(fmt)}/{end_dt.strftime(fmt)}")
    outlook = _outlook_url(title, desc, dt.strftime("%Y-%m-%dT%H:%M:%S"),
                           end_dt.strftime("%Y-%m-%dT%H:%M:%S"))
    return {"google": google, "outlook": outlook}


def followup_web_links(application) -> dict:
    """Return Google and Outlook deep links for the follow-up reminder."""
    job = application.job
    dt = application.next_follow_up or datetime.now()
    itype = _INTERVIEW_TYPE_LABELS.get(application.interview_type or "", "")
    type_suffix = f" ({itype})" if itype else ""
    title = f"Follow-up{type_suffix}: {job.title} @ {job.company}"
    desc = (
        f"Remember to follow up on your application for:\n"
        f"{job.title} at {job.company}\n{job.url}"
    )
    next_day = dt + timedelta(days=1)
    google = _google_url(title, desc,
                         f"{dt.strftime('%Y%m%d')}/{next_day.strftime('%Y%m%d')}")
    outlook = _outlook_url(title, desc,
                           dt.strftime("%Y-%m-%d"), next_day.strftime("%Y-%m-%d"))
    return {"google": google, "outlook": outlook, "title": title,
            "date": dt.strftime("%b %d, %Y")}
