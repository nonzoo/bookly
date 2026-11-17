# The Celery app must load with Django itself so manage.py, the web
# process and workers all share one app instance.
from .celery import app as celery_app

__all__ = ("celery_app",)
