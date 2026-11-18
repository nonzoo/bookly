import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[2]  # repo root (this file sits in config/settings/)


def _load_dotenv(path=BASE_DIR / ".env"):
    """Load KEY=VALUE pairs from .env into the environment.

    Uses setdefault: a real environment variable always wins over the file,
    which is what production and CI need. Values may be single- or
    double-quoted; everything after the first "=" is kept verbatim, so a
    password containing "#" is not mistaken for a comment. No dependency:
    the format this project needs is one key per line.
    """
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export "):]
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv()


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
        "payment_verify": "30/min",  # return-page polling, backed by a provider call
    },
}

# Paystack. The secret key never gets committed; the webhook flow reads it.
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "")
# New timestamped `Paystack-Signature` scheme (whsec_...). Optional: when
# absent, the legacy x-paystack-signature scheme (secret key) is used.
PAYSTACK_WEBHOOK_SECRET = os.environ.get("PAYSTACK_WEBHOOK_SECRET", "")
# Where Paystack sends the customer after payment (the frontend's return
# page). Paystack appends ?trxref=...&reference=... to it. Empty = let
# Paystack show its own page; the webhook still settles the booking.
PAYSTACK_CALLBACK_URL = os.environ.get("PAYSTACK_CALLBACK_URL", "")

# Celery (config/celery.py reads settings with the CELERY_ prefix).
CELERY_BROKER_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
# Eager mode runs tasks inline without a broker — how the test suite runs.
CELERY_TASK_ALWAYS_EAGER = os.environ.get("CELERY_TASK_ALWAYS_EAGER", "") == "1"

# Beat schedule. The per-resource sweep inside create_hold only clears the
# slot being booked; this tick clears lapsed holds everywhere.
CELERY_BEAT_SCHEDULE = {
    "expire-overdue-holds": {
        "task": "bookings.tasks.expire_overdue_holds",
        "schedule": 60.0,  # seconds
    },
    # Catch charges Paystack confirmed while the webhook was delayed or
    # dropped. Runs before the 15-minute hold deadline so a paid customer
    # keeps the slot instead of being refunded after it lapses.
    "reconcile-pending-payments": {
        "task": "bookings.tasks.reconcile_pending_payments",
        "schedule": 300.0,
    },
    # A refund whose on-commit send failed stays INITIATED forever
    # otherwise. create_refund is idempotent by reference.
    "retry-stuck-refunds": {
        "task": "bookings.tasks.retry_stuck_refunds",
        "schedule": 900.0,
    },
}
