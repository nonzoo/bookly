"""Local development settings: base plus a local DATABASE_URL fallback."""
from .base import *

DATABASES = {
    "default": dj_database_url.config(
        # .env (loaded in base.py) can override DATABASE_URL; this default
        # only exists so a fresh clone boots before reading .env.example.
        default="postgres://postgres:postgres@localhost:5432/book",
        conn_max_age=600,        # reuse connections for 10 minutes
        conn_health_checks=True,
    )
}
