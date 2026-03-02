"""
config.py — Application configuration loaded from environment / .env
"""
import os
import sys
from dotenv import load_dotenv

# When running as a frozen executable (PyInstaller / Nuitka installer) the
# .env file lives next to the .exe, not in sys._MEIPASS (which is the
# read-only extraction directory for bundled resources).
if getattr(sys, "frozen", False):
    _env_path = os.path.join(os.path.dirname(sys.executable), ".env")
    load_dotenv(_env_path)
else:
    load_dotenv()


class Config:
    # Flask
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")
    DEBUG = os.getenv("DEBUG", "false").lower() == "true"

    # Database
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL", "sqlite:///job_tracker.db")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Applicant profile
    APPLICANT_NAME = os.getenv("APPLICANT_NAME", "Your Name")
    APPLICANT_EMAIL = os.getenv("APPLICANT_EMAIL", "")
    APPLICANT_PHONE = os.getenv("APPLICANT_PHONE", "")
    APPLICANT_LOCATION = os.getenv("APPLICANT_LOCATION", "Remote")
    YEARS_EXPERIENCE = os.getenv("YEARS_EXPERIENCE", "5")
    APPLICANT_LINKEDIN = os.getenv("APPLICANT_LINKEDIN", "")
    APPLICANT_GITHUB = os.getenv("APPLICANT_GITHUB", "")
    RESUME_PATH = os.getenv("RESUME_PATH", "")

    # Email / SMTP
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.gmail.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 587))
    SMTP_USER = os.getenv("SMTP_USER", "")
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
    FROM_EMAIL = os.getenv("FROM_EMAIL", "")
    NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")

    # Job filtering
    MIN_MATCH_SCORE = int(os.getenv("MIN_MATCH_SCORE", 25))
    REQUIRED_SKILLS = [s.strip() for s in os.getenv(
        "REQUIRED_SKILLS", "C#,.NET,SQL Server,Oracle,Azure,Microsoft"
    ).split(",") if s.strip()]
    PREFERRED_SKILLS = [s.strip() for s in os.getenv(
        "PREFERRED_SKILLS",
        "ASP.NET,Entity Framework,WPF,LINQ,PL/SQL,Power BI,SSRS,SSIS,Blazor",
    ).split(",") if s.strip()]

    # Scheduling
    SCAN_TIME_MORNING = os.getenv("SCAN_TIME_MORNING", "08:00")
    SCAN_TIME_EVENING = os.getenv("SCAN_TIME_EVENING", "20:00")
    TIMEZONE = os.getenv("TIMEZONE", "America/New_York")
    FOLLOW_UP_DAYS = int(os.getenv("FOLLOW_UP_DAYS", 7))

    # OpenAI (optional)
    OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
    OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
