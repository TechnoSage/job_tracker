"""
email_service.py — Send emails via SMTP (applications, notifications, reminders)
"""
import logging
import os
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

logger = logging.getLogger(__name__)


def _build_smtp(config):
    """Connect and authenticate an SMTP session."""
    server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT)
    server.ehlo()
    server.starttls()
    server.login(config.SMTP_USER, config.SMTP_PASSWORD)
    return server


def _send(config, to: str, subject: str, body_text: str, body_html: str = None,
          attachments: list = None) -> bool:
    """Low-level send helper. Returns True on success."""
    if not config.SMTP_USER or not config.SMTP_PASSWORD:
        logger.warning("Email not configured — skipping send to %s", to)
        return False

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = config.FROM_EMAIL or config.SMTP_USER
    msg["To"] = to

    msg.attach(MIMEText(body_text, "plain"))
    if body_html:
        msg.attach(MIMEText(body_html, "html"))

    # Attachments (e.g. resume PDF)
    for path in (attachments or []):
        if os.path.isfile(path):
            with open(path, "rb") as f:
                part = MIMEBase("application", "octet-stream")
                part.set_payload(f.read())
            encoders.encode_base64(part)
            filename = os.path.basename(path)
            part.add_header("Content-Disposition", f'attachment; filename="{filename}"')
            msg.attach(part)

    try:
        server = _build_smtp(config)
        server.sendmail(msg["From"], [to], msg.as_string())
        server.quit()
        logger.info("Email sent to %s: %s", to, subject)
        return True
    except Exception as exc:
        logger.error("Email send failed: %s", exc)
        return False


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------

def send_application(config, job, cover_letter_text: str) -> bool:
    """
    Send a job application email to the applicant's notification inbox
    (ready to copy-paste or forward).
    """
    subject = f"Application: {job.title} at {job.company}"
    text = (
        f"Job: {job.title}\n"
        f"Company: {job.company}\n"
        f"URL: {job.url}\n\n"
        f"--- Cover Letter ---\n\n"
        f"{cover_letter_text}"
    )
    html = f"""
    <h2>{job.title} — {job.company}</h2>
    <p><a href="{job.url}">View Job Posting</a></p>
    <hr>
    <h3>Cover Letter</h3>
    <pre style="font-family:Georgia,serif;white-space:pre-wrap">{cover_letter_text}</pre>
    """
    attachments = [config.RESUME_PATH] if config.RESUME_PATH else []
    return _send(config, config.NOTIFY_EMAIL, subject, text, html, attachments)


def send_new_jobs_notification(config, jobs: list) -> bool:
    """Notify the user of newly matched jobs from today's scan."""
    if not jobs:
        return True
    subject = f"Job Tracker: {len(jobs)} new matching job(s) found"
    lines = []
    html_rows = []
    for j in jobs:
        lines.append(f"• [{j.match_score}] {j.title} @ {j.company} — {j.url}")
        html_rows.append(
            f'<tr><td>{j.match_score}</td><td>{j.title}</td>'
            f'<td>{j.company}</td><td><a href="{j.url}">View</a></td></tr>'
        )
    text = "New matching jobs:\n\n" + "\n".join(lines)
    html = f"""
    <h2>New Matching Jobs ({len(jobs)})</h2>
    <table border="1" cellpadding="6" style="border-collapse:collapse">
      <tr><th>Score</th><th>Title</th><th>Company</th><th>Link</th></tr>
      {''.join(html_rows)}
    </table>
    """
    return _send(config, config.NOTIFY_EMAIL, subject, text, html)


def send_follow_up_reminder(config, application) -> bool:
    """Remind the user to follow up on an application."""
    job = application.job
    subject = f"Follow-up Reminder: {job.title} at {job.company}"
    text = (
        f"It's time to follow up on your application for:\n\n"
        f"  {job.title} @ {job.company}\n"
        f"  Applied: {application.applied_date.strftime('%B %d, %Y') if application.applied_date else 'unknown'}\n"
        f"  Job URL: {job.url}\n\n"
        f"  Contact: {application.contact_name or 'Hiring Manager'}"
        f" <{application.contact_email}>" if application.contact_email else ""
    )
    return _send(config, config.NOTIFY_EMAIL, subject, text)


def send_interview_reminder(config, application) -> bool:
    """Send a reminder about an upcoming interview."""
    job = application.job
    interview_dt = application.interview_date
    subject = f"Interview Reminder: {job.title} at {job.company}"
    text = (
        f"Interview Reminder\n\n"
        f"  Position: {job.title}\n"
        f"  Company:  {job.company}\n"
        f"  Date/Time: {interview_dt.strftime('%A, %B %d, %Y at %I:%M %p') if interview_dt else 'TBD'}\n"
        f"  Type: {application.interview_type or 'TBD'}\n"
        f"  Notes: {application.notes or ''}\n"
    )
    return _send(config, config.NOTIFY_EMAIL, subject, text)
