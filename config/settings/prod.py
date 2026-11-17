# Production settings.
# DATABASE_URL MUST be provided by the environment — there is no fallback
# here on purpose: a prod boot with a missing database URL must fail loudly.
from .base import *

DATABASES = {
    "default": dj_database_url.config(
        conn_max_age=600,        # reuse connections for 10 minutes
        conn_health_checks=True,
    )
}
