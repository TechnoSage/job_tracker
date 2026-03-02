"""
analytics.py — Calculate statistics for the analytics dashboard
"""
from datetime import datetime, timedelta
from collections import defaultdict

from models import Job, Application, ScanLog, Notification
from extensions import db


def pipeline_counts() -> dict:
    """Return application counts by status for the funnel chart."""
    statuses = ["submitted", "phone_screen", "interview", "offer", "rejected", "withdrawn"]
    result = {}
    for s in statuses:
        result[s] = Application.query.filter_by(status=s).count()
    result["draft"] = Application.query.filter_by(status="draft").count()
    return result


def jobs_by_source() -> dict:
    """Return count of jobs found per source (for pie chart)."""
    rows = (
        db.session.query(Job.source, db.func.count(Job.id))
        .group_by(Job.source)
        .all()
    )
    return {source: count for source, count in rows}


def jobs_by_category() -> dict:
    rows = (
        db.session.query(Job.job_category, db.func.count(Job.id))
        .group_by(Job.job_category)
        .all()
    )
    return {cat: count for cat, count in rows}


def weekly_activity(weeks: int = 8) -> dict:
    """
    Returns jobs found and applications submitted per week for the past *weeks* weeks.
    """
    labels = []
    jobs_found = []
    apps_submitted = []

    now = datetime.utcnow()
    for i in range(weeks - 1, -1, -1):
        week_start = (now - timedelta(weeks=i)).replace(
            hour=0, minute=0, second=0, microsecond=0
        ) - timedelta(days=now.weekday())  # Monday
        week_end = week_start + timedelta(days=7)

        labels.append(week_start.strftime("%b %d"))
        jobs_found.append(
            Job.query.filter(
                Job.found_date >= week_start,
                Job.found_date < week_end,
            ).count()
        )
        apps_submitted.append(
            Application.query.filter(
                Application.applied_date >= week_start,
                Application.applied_date < week_end,
                Application.status != "draft",
            ).count()
        )

    return {"labels": labels, "jobs_found": jobs_found, "applications": apps_submitted}


def score_distribution() -> dict:
    """Histogram of match scores in 10-point buckets."""
    buckets = defaultdict(int)
    jobs = Job.query.with_entities(Job.match_score).all()
    for (score,) in jobs:
        bucket = (score // 10) * 10
        bucket_label = f"{bucket}-{bucket + 9}"
        buckets[bucket_label] += 1
    return dict(sorted(buckets.items()))


def key_metrics() -> dict:
    total_jobs = Job.query.count()
    new_jobs = Job.query.filter_by(status="new").count()
    saved_jobs = Job.query.filter_by(status="saved").count()
    total_apps = Application.query.filter(Application.status != "draft").count()
    interviews = Application.query.filter(
        Application.status.in_(["interview", "phone_screen"])
    ).count()
    offers = Application.query.filter_by(status="offer").count()
    rejected = Application.query.filter_by(status="rejected").count()

    response_rate = round((interviews / total_apps * 100), 1) if total_apps else 0
    offer_rate = round((offers / total_apps * 100), 1) if total_apps else 0

    last_scan = ScanLog.query.order_by(ScanLog.scan_date.desc()).first()

    return {
        "total_jobs": total_jobs,
        "new_jobs": new_jobs,
        "saved_jobs": saved_jobs,
        "total_applications": total_apps,
        "interviews": interviews,
        "offers": offers,
        "rejected": rejected,
        "response_rate": response_rate,
        "offer_rate": offer_rate,
        "last_scan": last_scan.scan_date.strftime("%Y-%m-%d %H:%M") if last_scan else "Never",
        "last_scan_status": last_scan.status if last_scan else "—",
    }


def upcoming_followups(limit: int = 10) -> list:
    """Return applications with follow-up dates due within the next 7 days."""
    now = datetime.utcnow()
    due = now + timedelta(days=7)
    apps = (
        Application.query
        .filter(
            Application.next_follow_up >= now,
            Application.next_follow_up <= due,
            Application.status.in_(["submitted", "phone_screen"]),
        )
        .order_by(Application.next_follow_up)
        .limit(limit)
        .all()
    )
    return apps


def overdue_followups() -> list:
    """Return applications where the follow-up date has passed."""
    now = datetime.utcnow()
    return (
        Application.query
        .filter(
            Application.next_follow_up < now,
            Application.status.in_(["submitted", "phone_screen"]),
        )
        .order_by(Application.next_follow_up)
        .all()
    )
