"""Test settings override — uses SQLite so tests run without the Docker DB."""
from horilla.settings import *  # noqa: F401, F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": ":memory:",
    }
}

# Disable HTTPS redirect so Django test client (HTTP) reaches views directly.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Strip middlewares that depend on WIP code not yet on disk.
MIDDLEWARE = [m for m in MIDDLEWARE if "GsuiteGateAuthMiddleware" not in m]
