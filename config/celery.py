"""Celery bootstrap.

Standard Django+Celery wiring: read settings with the CELERY_ prefix,
autodiscover tasks in installed apps. The Celery *beat* schedule for the
expiry sweep is added together with the task it schedules (bookings/tasks,
lifecycle step) — a beat schedule referencing a task that doesn't exist
yet would crash the beat process on startup, so a schedule never ships
ahead of its task.
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings.dev")

app = Celery("book")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
