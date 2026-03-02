"""
extensions.py — Shared Flask extensions (SQLAlchemy, APScheduler)
"""
from flask_sqlalchemy import SQLAlchemy
from apscheduler.schedulers.background import BackgroundScheduler

db = SQLAlchemy()
scheduler = BackgroundScheduler()
