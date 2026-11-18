import os
from pathlib import Path
import dj_database_url


BASE_DIR = Path(__file__).resolve().parents[2]  # repo root (this file sits in config/settings/)



SECRET_KEY = os.environ.get("DJANGO_SECRET_KEY", "dev-only-insecure-key")



# 1 in dev; CI and production set DJANGO_DEBUG=0
DEBUG = os.environ.get("DJANGO_DEBUG", "1") == "1"

ALLOWED_HOSTS = os.environ.get("DJANGO_ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # third-party
    "rest_framework",
    "django.contrib.postgres",  # range fields + exclusion constraints
    # local
    "bookings",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

# Store UTC in the database; each Business carries its own timezone for
# presentation and slot generation. All boundary math (hold expiry,
# cancellation windows) runs on aware datetimes, so one UTC clock is safe.
TIME_ZONE = "UTC"
USE_TZ = True

LANGUAGE_CODE = "en-us"
STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": ("rest_framework.permissions.IsAuthenticated",),
    # Rates are centralized here; the throttle *classes* are attached
    # per-view, because a global class would throttle the booking endpoint
    # and the webhook endpoint identically, and they must not be.
    "DEFAULT_THROTTLE_RATES": {
        "booking_create": "10/min",  # a customer hammering the hold endpoint
        "webhook": "300/min",        # loose IP cap on the unauthenticated route
    },
}

# Paystack. The secret key never gets committed; the webhook flow reads it.
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")

# Celery (config/celery.py reads settings with the CELERY_ prefix).
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# Eager mode runs tasks inline without a broker — how the test suite runs.
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "") == "1"
