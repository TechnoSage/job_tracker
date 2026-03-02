"""
email_checker.py — Monitor IMAP inboxes for job-application reply emails.

For each enabled response-email account configured on the Settings page this
module will:
  1. Connect via IMAP (SSL or STARTTLS).
  2. Fetch all UNSEEN messages in the inbox.
  3. Classify each email as "declined", "interview", or unrecognised.
  4. Match the email to an active Application by sender domain / company
     name / job title.
  5. Update the Application status and add an auto-note with the date.
  6. Save the raw .eml to  email_responses/{declined|interview|unmatched}/
     inside the project directory.
  7. Mark the message as read in IMAP.
  8. Create an in-app Notification.
"""

from __future__ import annotations

import email as _email_mod
import imaplib
import json
import logging
import os
import re
import ssl
from datetime import datetime
from email.header import decode_header as _decode_header
from urllib.parse import urlparse

logger = logging.getLogger("email_checker")

# ---------------------------------------------------------------------------
# Filesystem layout
# ---------------------------------------------------------------------------
PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
EMAIL_RESPONSES_DIR = os.path.join(PROJECT_DIR, "email_responses")

# ---------------------------------------------------------------------------
# Classification keyword lists
# ---------------------------------------------------------------------------
_DECLINE_PHRASES = [
    "unfortunately",
    "regret to inform",
    "regret that we",
    "not moving forward",
    "not selected",
    "position has been filled",
    "will not be moving",
    "decided to move forward with other",
    "decided to go with another",
    "at this time we will not",
    "not a match at this time",
    "no longer accepting",
    "not be pursuing your application",
    "closed the position",
    "filled by another candidate",
    "not be continuing with your application",
    "will not be proceeding",
    "not be able to move forward",
    "we appreciate your interest but",
    "not a fit at this time",
    "have gone with another",
    "chosen to move forward with a different",
    "decided not to move forward",
    "regrettably",
]

_INTERVIEW_PHRASES = [
    "would like to schedule an interview",
    "invite you to interview",
    "schedule a call",
    "schedule a time to",
    "phone interview",
    "video interview",
    "next steps",
    "move forward with you",
    "advance to the next",
    "move to the next stage",
    "pleased to invite",
    "excited to invite",
    "we'd like to meet",
    "we would like to meet",
    "discuss the opportunity",
    "discuss your application",
    "discuss the role",
    "phone screen",
    "technical interview",
    "onsite interview",
    "available for a call",
    "set up a time to speak",
    "looking forward to speaking",
    "like to speak with you",
    "like to connect with you",
    "interested in scheduling",
    "excited about your background",
    "would love to connect",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _decode_str(value) -> str:
    """Decode a potentially RFC-2047-encoded email header value to a string."""
    if not value:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    parts = _decode_header(value)
    decoded = []
    for part, charset in parts:
        if isinstance(part, bytes):
            decoded.append(part.decode(charset or "utf-8", errors="replace"))
        else:
            decoded.append(str(part))
    return " ".join(decoded)


def _extract_body(msg) -> str:
    """Extract the best plain-text body from an email.Message object."""
    if msg.is_multipart():
        # Prefer text/plain
        for part in msg.walk():
            if part.get_content_type() == "text/plain":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    return payload.decode(charset, errors="replace")
        # Fallback: strip HTML from text/html
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    raw_html = payload.decode(charset, errors="replace")
                    return re.sub(r"<[^>]+>", " ", raw_html)
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            return payload.decode(charset, errors="replace")
    return ""


def _classify(subject: str, body: str) -> str | None:
    """Return 'declined', 'interview', or None based on keyword analysis."""
    text = (subject + " " + body[:3000]).lower()
    for phrase in _DECLINE_PHRASES:
        if phrase in text:
            return "declined"
    for phrase in _INTERVIEW_PHRASES:
        if phrase in text:
            return "interview"
    return None


def _sender_domain(from_header: str) -> str:
    """Extract the domain from a From header — 'hr@acme.com' → 'acme.com'."""
    m = re.search(r"@([\w.\-]+)", from_header)
    return m.group(1).lower() if m else ""


def _match_application(sender_domain: str, subject: str, body: str, apps,
                        exclude_app_ids=None,
                        confirmed_pairs=None,
                        rejected_pairs=None) -> object | None:
    """
    Try to match an email to one of the active Application objects.

    Priority order:
      0. Sender domain matches a previously user-confirmed pairing (learned).
      1. Sender domain matches the job URL domain.
      2. Company name appears in subject or body.
      3. Job title appears in subject or body.

    exclude_app_ids: set/list of app IDs to skip (already rejected for this email).
    confirmed_pairs: set of (domain, app_id) tuples confirmed correct by the user.
    rejected_pairs:  set of (domain, app_id) tuples confirmed wrong by the user.

    Returns the first matched Application or None.
    """
    exclude_ids = set(exclude_app_ids or [])
    confirmed   = confirmed_pairs or set()
    rejected    = rejected_pairs  or set()

    # Filter out apps in the exclude/rejected lists
    def _eligible(app):
        if app.id in exclude_ids:
            return False
        if sender_domain and (sender_domain, app.id) in rejected:
            return False
        return True

    eligible = [a for a in apps if _eligible(a)]

    subject_l = subject.lower()
    body_l    = body[:4000].lower()

    # Pass 0: previously confirmed domain→app pairing (learned)
    if sender_domain and confirmed:
        for app in eligible:
            if (sender_domain, app.id) in confirmed:
                return app

    # Pass 1: domain match against job URL
    if sender_domain:
        for app in eligible:
            if app.job and app.job.url:
                try:
                    job_domain = urlparse(app.job.url).netloc.lower().lstrip("www.")
                    s_base = sender_domain.lstrip("www.")
                    if s_base and (s_base in job_domain or job_domain.endswith("." + s_base)
                                   or job_domain == s_base):
                        return app
                except Exception:
                    pass

    # Pass 2: company name in subject or body
    for app in eligible:
        if app.job and app.job.company:
            co = app.job.company.lower().strip()
            if len(co) >= 3 and (co in subject_l or co in body_l):
                return app

    # Pass 3: job title in subject or body
    for app in eligible:
        if app.job and app.job.title:
            title = app.job.title.lower().strip()
            if len(title) >= 5 and (title in subject_l or title in body_l):
                return app

    return None


_MICROSOFT_HOSTS = ("outlook.office365.com", "outlook.live.com", "imap-mail.outlook.com",
                    "hotmail.com", "outlook.com", "live.com")


def _friendly_imap_error(exc: Exception, imap_host: str = "") -> str:
    """
    Convert a raw IMAP exception into a human-readable message.
    Provides specific guidance for Microsoft / Outlook accounts.
    """
    raw = str(exc).lower()
    host_lower = imap_host.lower()
    is_microsoft = any(h in host_lower for h in _MICROSOFT_HOSTS)

    # Microsoft LOGIN failed — most common cause: basic auth disabled / app password needed
    if "login failed" in raw or "authentication failed" in raw or "authenticat" in raw:
        if is_microsoft:
            return (
                "Microsoft rejected the password. Personal Outlook / Hotmail / Live accounts "
                "require an App Password — your regular Microsoft password will not work. "
                "Go to account.microsoft.com → Security → Advanced security options → "
                "App passwords, generate one for 'Mail', and use that here. "
                "Also make sure IMAP is enabled: outlook.live.com → Settings → "
                "Mail → Sync email → POP and IMAP."
            )
        return "Login failed — check your email address and password."

    # IMAP not enabled
    if "imap" in raw and ("not enabled" in raw or "disabled" in raw or "not allowed" in raw):
        if is_microsoft:
            return (
                "IMAP access is disabled on this account. Enable it at: "
                "outlook.live.com → Settings (gear) → View all Outlook settings → "
                "Mail → Sync email → toggle 'IMAP' on."
            )
        return "IMAP access is disabled. Enable it in your email account settings."

    # Connection / network errors
    if "connection refused" in raw or "timed out" in raw or "nodename" in raw:
        return f"Cannot reach the IMAP server — check host/port settings. ({exc})"

    # SSL certificate errors
    if "ssl" in raw or "certificate" in raw:
        return f"SSL/TLS error — the server certificate could not be verified. ({exc})"

    return str(exc)


def _safe_filename(text: str, max_len: int = 50) -> str:
    """Sanitize a string for use in a filename."""
    safe = re.sub(r'[\\/:*?"<>|]', "_", text or "")
    safe = re.sub(r"\s+", "_", safe).strip("_.")
    return safe[:max_len] or "unknown"


def _save_eml(raw_bytes: bytes, classification: str, company: str, subject: str) -> str:
    """Save the raw email bytes as a .eml file. Returns the saved path."""
    folder = os.path.join(EMAIL_RESPONSES_DIR, classification)
    os.makedirs(folder, exist_ok=True)
    stamp   = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    co_part = _safe_filename(company)
    su_part = _safe_filename(subject)
    fname   = f"{stamp}_{co_part}_{su_part}.eml"
    path    = os.path.join(folder, fname)
    with open(path, "wb") as fh:
        fh.write(raw_bytes)
    return path


# ---------------------------------------------------------------------------
# Core account processor
# ---------------------------------------------------------------------------

def _process_account(account: dict, flask_app, progress_cb=None) -> dict:
    """
    Connect to one IMAP account and process unread messages.
    Returns {"ok": bool, "message": str, "found": int}.
    progress_cb: optional callable(str) to report progress lines.
    """
    def _log(msg):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    imap_host = account.get("imap_host", "").strip()
    imap_port = int(account.get("imap_port") or 993)
    use_ssl   = bool(account.get("use_ssl", True))
    username  = account.get("email", "").strip()
    password  = account.get("password", "")

    if not imap_host or not username or not password:
        msg = "Incomplete account configuration."
        if progress_cb:
            progress_cb(f"  ERROR: {msg}")
        return {"ok": False, "message": msg, "found": 0}

    # Connect
    _log(f"  Connecting to {imap_host}:{imap_port} …")
    try:
        if use_ssl:
            ctx  = ssl.create_default_context()
            imap = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=ctx)
        else:
            imap = imaplib.IMAP4(imap_host, imap_port)
            imap.starttls()
        imap.login(username, password)
        _log(f"  Logged in as {username}")
    except Exception as exc:
        msg = _friendly_imap_error(exc, imap_host)
        if progress_cb:
            progress_cb(f"  ERROR: {msg}")
        return {"ok": False, "message": msg, "found": 0}

    found = 0
    try:
        imap.select("INBOX")
        _, data = imap.search(None, "UNSEEN")
        uids = data[0].split() if data and data[0] else []

        if not uids:
            _log("  No new messages.")
            return {"ok": True, "message": "No new messages.", "found": 0}

        _log(f"  Found {len(uids)} unseen message(s) — scanning…")

        with flask_app.app_context():
            from extensions import db
            from models import Application, Notification, EmailReview, EmailMatchFeedback

            # Load all active applications once
            active_apps = (
                Application.query
                .filter(Application.status.notin_(["draft", "withdrawn", "rejected"]))
                .all()
            )

            for uid in uids:
                try:
                    _, msg_data = imap.fetch(uid, "(RFC822)")
                    raw_bytes = msg_data[0][1]
                    msg       = _email_mod.message_from_bytes(raw_bytes)

                    subject   = _decode_str(msg.get("Subject", ""))
                    from_hdr  = _decode_str(msg.get("From", ""))
                    body      = _extract_body(msg)
                    s_domain  = _sender_domain(from_hdr)

                    classification = _classify(subject, body)
                    if not classification:
                        _log(f"  Skipped (unrecognised): \"{subject[:60]}\" from {from_hdr[:50]}")
                        continue

                    _log(f"  [{classification.upper()}] \"{subject[:55]}\" from {from_hdr[:45]}")

                    # Load learned feedback for this sender domain
                    confirmed_pairs = set()
                    rejected_pairs  = set()
                    for fb in EmailMatchFeedback.query.filter_by(sender_domain=s_domain).all():
                        pair = (fb.sender_domain, fb.app_id)
                        (confirmed_pairs if fb.is_confirmed else rejected_pairs).add(pair)

                    matched_app = _match_application(
                        s_domain, subject, body, active_apps,
                        confirmed_pairs=confirmed_pairs,
                        rejected_pairs=rejected_pairs,
                    )

                    company = (
                        matched_app.job.company
                        if matched_app and matched_app.job
                        else (s_domain or "Unknown Company")
                    )

                    if matched_app:
                        _log(f"    Matched to: {company} — {matched_app.job.title if matched_app.job else '?'}")
                    else:
                        _log(f"    No match found — queued for manual review")

                    # Save .eml file
                    folder_name = classification if matched_app else "unmatched"
                    eml_path = _save_eml(raw_bytes, folder_name, company, subject)

                    # Queue for user review — status update deferred until confirmed
                    review = EmailReview(
                        account_email=username,
                        sender=from_hdr,
                        subject=subject,
                        body_preview=body[:3000],
                        classification=classification,
                        eml_path=eml_path,
                        suggested_app_id=matched_app.id if matched_app else None,
                    )
                    db.session.add(review)
                    db.session.commit()

                    logger.info(
                        "Email review queued: %s from %s → app %s (%s)",
                        classification, from_hdr,
                        matched_app.id if matched_app else "unmatched",
                        company,
                    )

                    # Mark as read in IMAP
                    imap.store(uid, "+FLAGS", "\\Seen")
                    found += 1

                except Exception as exc:
                    logger.warning("Error processing email uid %s: %s", uid, exc)
                    if progress_cb:
                        progress_cb(f"    Warning: error on message — {exc}")

    finally:
        try:
            imap.logout()
        except Exception:
            pass

    result_msg = f"Done — {found} response(s) queued for review." if found else "Done — no matching responses found."
    _log(f"  {result_msg}")
    return {"ok": True, "message": result_msg, "found": found}


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def check_all_accounts(flask_app, progress_cb=None) -> list[dict]:
    """
    Check all enabled response-email accounts.
    Updates each account's last_checked / last_status in the DB.
    Returns a list of per-account result dicts.
    progress_cb: optional callable(str) to report progress lines.
    """
    def _log(msg):
        logger.info(msg)
        if progress_cb:
            progress_cb(msg)

    with flask_app.app_context():
        from models import Setting
        raw = Setting.get("response_email_accounts", "[]")
        try:
            accounts = json.loads(raw)
        except Exception:
            accounts = []

    enabled = [a for a in accounts if a.get("enabled", True)]
    if not enabled:
        _log("No enabled email accounts configured.")
        return []

    _log(f"Checking {len(enabled)} account(s)…")

    results = []
    for account in enabled:
        email_addr = account.get("email", "?")
        _log(f"Account: {email_addr}")
        result = _process_account(account, flask_app, progress_cb=progress_cb)
        result["account_id"]    = account.get("id", "")
        result["account_email"] = email_addr
        results.append(result)

        # Persist last_checked / last_status back to the DB
        with flask_app.app_context():
            from models import Setting
            raw = Setting.get("response_email_accounts", "[]")
            try:
                all_accounts = json.loads(raw)
            except Exception:
                all_accounts = []
            for acc in all_accounts:
                if acc.get("id") == account.get("id"):
                    acc["last_checked"] = datetime.now().isoformat()
                    acc["last_status"]  = "ok" if result["ok"] else "error"
                    acc["last_message"] = result["message"]
                    break
            Setting.set("response_email_accounts", json.dumps(all_accounts))

    return results


def test_connection(imap_host: str, imap_port: int, use_ssl: bool,
                    username: str, password: str) -> tuple[bool, str]:
    """
    Attempt an IMAP login and return (success, message).
    Used by the Settings page "Test Connection" button.
    """
    try:
        if use_ssl:
            ctx  = ssl.create_default_context()
            imap = imaplib.IMAP4_SSL(imap_host, imap_port, ssl_context=ctx)
        else:
            imap = imaplib.IMAP4(imap_host, imap_port)
            imap.starttls()
        imap.login(username, password)
        imap.logout()
        return True, "Connection successful."
    except Exception as exc:
        return False, _friendly_imap_error(exc, imap_host)
