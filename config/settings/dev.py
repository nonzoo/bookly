"""Local development settings: base plus a local DATABASE_URL fallback."""
import dj_database_url

from .base import *

DATABASES = {
    "default": dj_database_url.config(
        # .env (loaded by base.py) sets DATABASE_URL with your real
        # password; this default only exists so a fresh clone boots
        # before anyone has copied .env.example.
        default="postgres://postgres:postgres@localhost:5432/book",
        conn_max_age=600,        # reuse connections for 10 minutes
    )
}
