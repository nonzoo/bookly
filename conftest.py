"""Pytest bootstrap.

Test-only defaults that real environments (CI, production) override:
Celery tasks run eagerly in tests so the whole suite runs without Redis.
Settings read these via os.environ before any Django import, which is why
this lives at the repo root next to pytest.ini.
"""
import os

os.environ.setdefault("CELERY_TASK_ALWAYS_EAGER", "1")
