"""
models.py — SQLAlchemy database models
"""
import json
from datetime import datetime
from extensions import db


class Job(db.Model):
    __tablename__ = "jobs"

    id = db.Column(db.Integer, primary_key=True)
    external_id = db.Column(db.String(300), unique=True, nullable=False)
    title = db.Column(db.String(300), nullable=False)
    company = db.Column(db.String(200))
    location = db.Column(db.String(200))
    is_remote = db.Column(db.Boolean, default=True)
    # Full Time | Part Time | Contract | Internship | Temporary | Seasonal | Freelance | Per Diem
    job_duration = db.Column(db.String(50))
    # software | customer_service | it | general
    job_category = db.Column(db.String(50), default="general")
    description = db.Column(db.Text)
    tags = db.Column(db.Text)          # JSON list of tags/skills
    salary_range = db.Column(db.String(100))
    url = db.Column(db.String(1000))
    source = db.Column(db.String(100))  # remoteok | remotive | wwr | indeed
    posted_date = db.Column(db.DateTime)
    found_date = db.Column(db.DateTime, default=datetime.utcnow)
    match_score = db.Column(db.Integer, default=0)
    matched_skills = db.Column(db.Text)  # JSON list
    # new | saved | viewed | applied | archived
    status = db.Column(db.String(50), default="new")
    company_address = db.Column(db.String(500))
    company_phone = db.Column(db.String(100))
    is_active = db.Column(db.Boolean, default=True)

    application = db.relationship(
        "Application", backref="job", uselist=False, cascade="all, delete-orphan"
    )

    def get_tags(self):
        try:
            return json.loads(self.tags) if self.tags else []
        except Exception:
            return []

    def get_matched_skills(self):
        try:
            return json.loads(self.matched_skills) if self.matched_skills else []
        except Exception:
            return []

    def status_badge(self):
        badges = {
            "new": "primary",
            "saved": "info",
            "viewed": "secondary",
            "applied": "warning",
            "archived": "secondary",
        }
        return badges.get(self.status, "secondary")

    def score_badge(self):
        if self.match_score >= 70:
            return "success"
        if self.match_score >= 40:
            return "warning"
        return "secondary"


class Application(db.Model):
    __tablename__ = "applications"

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=False)
    applied_date = db.Column(db.DateTime)
    # draft | submitted | phone_screen | interview | offer | rejected | withdrawn
    status = db.Column(db.String(50), default="draft")
    cover_letter = db.Column(db.Text)
    resume_version = db.Column(db.String(100))
    notes = db.Column(db.Text)
    next_follow_up = db.Column(db.DateTime)
    interview_date = db.Column(db.DateTime)
    # phone | video | in_person | technical
    interview_type = db.Column(db.String(50))
    salary_offered = db.Column(db.Integer)
    contact_name = db.Column(db.String(200))
    contact_email = db.Column(db.String(200))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(
        db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow
    )

    STATUS_LABELS = {
        "draft": ("Draft", "secondary"),
        "submitted": ("Submitted", "primary"),
        "phone_screen": ("Phone Screen", "info"),
        "interview": ("Interview", "warning"),
        "offer": ("Offer", "success"),
        "rejected": ("Rejected", "danger"),
        "withdrawn": ("Withdrawn", "dark"),
    }

    def status_label(self):
        return self.STATUS_LABELS.get(self.status, ("Unknown", "secondary"))[0]

    def status_color(self):
        return self.STATUS_LABELS.get(self.status, ("Unknown", "secondary"))[1]

    def is_overdue_followup(self):
        if self.next_follow_up and self.status in ("submitted", "phone_screen"):
            return self.next_follow_up < datetime.utcnow()
        return False


class ScanLog(db.Model):
    __tablename__ = "scan_logs"

    id = db.Column(db.Integer, primary_key=True)
    scan_date = db.Column(db.DateTime, default=datetime.utcnow)
    source = db.Column(db.String(100))
    jobs_found = db.Column(db.Integer, default=0)
    jobs_new = db.Column(db.Integer, default=0)
    jobs_matched = db.Column(db.Integer, default=0)
    status = db.Column(db.String(50))   # success | error | partial
    error_message = db.Column(db.Text)
    duration_seconds = db.Column(db.Float)


class Notification(db.Model):
    __tablename__ = "notifications"

    id = db.Column(db.Integer, primary_key=True)
    # new_job | follow_up | interview | offer | reminder
    type = db.Column(db.String(50))
    title = db.Column(db.String(300))
    message = db.Column(db.Text)
    is_read = db.Column(db.Boolean, default=False)
    job_id = db.Column(db.Integer, db.ForeignKey("jobs.id"), nullable=True)
    application_id = db.Column(
        db.Integer, db.ForeignKey("applications.id"), nullable=True
    )
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    is_archived = db.Column(db.Boolean, default=False)
    is_acknowledged = db.Column(db.Boolean, default=False)

    job = db.relationship("Job", foreign_keys=[job_id])

    def icon(self):
        icons = {
            "new_job": "bi-briefcase-fill",
            "follow_up": "bi-clock-fill",
            "interview": "bi-calendar-check-fill",
            "offer": "bi-star-fill",
            "reminder": "bi-bell-fill",
            "unemployment_alarm": "bi-exclamation-triangle-fill",
            "scan_error": "bi-x-circle-fill",
        }
        return icons.get(self.type, "bi-info-circle-fill")

    def color(self):
        colors = {
            "new_job": "primary",
            "follow_up": "warning",
            "interview": "info",
            "offer": "success",
            "reminder": "secondary",
            "unemployment_alarm": "danger",
            "scan_error": "danger",
        }
        return colors.get(self.type, "secondary")


class ScraperSource(db.Model):
    """Tracks all job scraping sources — built-in and user-added."""
    __tablename__ = "scraper_sources"

    id = db.Column(db.Integer, primary_key=True)
    # Unique slug used in code (e.g. "linkedin", "my_rss")
    name = db.Column(db.String(100), unique=True, nullable=False)
    display_name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.String(500))
    # builtin | rss | json_api
    source_type = db.Column(db.String(50), default="builtin")
    # URL template for rss/json_api types; may include {query} placeholder
    url_template = db.Column(db.String(1000))
    # JSON list of search query strings to iterate over
    search_terms = db.Column(db.Text)
    is_enabled = db.Column(db.Boolean, default=True)
    disabled_reason = db.Column(db.Text, nullable=True)
    is_builtin = db.Column(db.Boolean, default=False)
    # Stats from last run
    last_run = db.Column(db.DateTime)
    last_status = db.Column(db.String(50))   # success | error | disabled
    last_jobs_found = db.Column(db.Integer, default=0)
    last_error = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    # Health check results (updated by source_health.run_health_check)
    health_status = db.Column(db.String(20))   # current | broken | None
    health_checked_at = db.Column(db.DateTime)

    def get_search_terms(self) -> list:
        try:
            return json.loads(self.search_terms) if self.search_terms else []
        except Exception:
            return []

    def status_color(self):
        colors = {"success": "success", "error": "danger", "disabled": "secondary"}
        return colors.get(self.last_status, "secondary")


class EmailReview(db.Model):
    """Queues email-matched responses for user review before any status update."""
    __tablename__ = "email_reviews"

    id = db.Column(db.Integer, primary_key=True)
    account_email = db.Column(db.String(200))
    sender = db.Column(db.String(300))
    subject = db.Column(db.String(500))
    body_preview = db.Column(db.Text)      # first ~3000 chars of plain-text body
    classification = db.Column(db.String(50))  # declined | interview
    eml_path = db.Column(db.String(500))
    suggested_app_id = db.Column(db.Integer, db.ForeignKey("applications.id"), nullable=True)
    # pending | confirmed | dismissed
    review_status = db.Column(db.String(50), default="pending")
    rejected_app_ids = db.Column(db.Text, default="[]")  # JSON list of app IDs tried and rejected
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    suggested_app = db.relationship("Application", foreign_keys=[suggested_app_id])

    def get_rejected_ids(self):
        try:
            return json.loads(self.rejected_app_ids or "[]")
        except Exception:
            return []


class EmailMatchFeedback(db.Model):
    """Learning data: user-confirmed and user-rejected sender-domain → application pairings."""
    __tablename__ = "email_match_feedback"

    id = db.Column(db.Integer, primary_key=True)
    sender_domain = db.Column(db.String(200), index=True)
    app_id = db.Column(db.Integer, db.ForeignKey("applications.id"))
    is_confirmed = db.Column(db.Boolean)   # True = correct match, False = wrong match
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class Setting(db.Model):
    __tablename__ = "settings"

    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(100), unique=True, nullable=False)
    value = db.Column(db.Text)
    description = db.Column(db.String(300))

    @classmethod
    def get(cls, key, default=None):
        row = cls.query.filter_by(key=key).first()
        return row.value if row else default

    @classmethod
    def set(cls, key, value, description=None):
        row = cls.query.filter_by(key=key).first()
        if row:
            row.value = str(value)
        else:
            row = cls(key=key, value=str(value), description=description)
            db.session.add(row)
        db.session.commit()
