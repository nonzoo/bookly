"""Celery bootstrap.

Standard Django+Celery wiring: read settings with the CELERY_ prefix,
autodiscover tasks in installed apps. The beat schedule lives next to the
settings it uses (CELERY_BEAT_SCHEDULE in config/settings/base.py) and
points at bookings.tasks.expire_overdue_holds.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("book")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
