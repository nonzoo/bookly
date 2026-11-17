"""Settings package selector.

base.py holds everything shared; dev.py and prod.py hold the
environment differences. The active module is picked by DJANGO_ENV
(default: dev), so manage.py, wsgi.py and pytest.ini can all keep
pointing at DJANGO_SETTINGS_MODULE=config.settings.
"""
import os

_env = os.environ.get("DJANGO_ENV", "dev").lower()

if _env == "prod":
    from .prod import *  # noqa: F401,F403
else:
    from .dev import *  # noqa: F401,F403
