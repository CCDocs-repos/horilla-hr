"""
Test settings that run against a throwaway PostgreSQL instead of SQLite.

horilla/test_settings.py uses SQLite, but recruitment migration 0004 widens
Candidate.mobile with a PostgreSQL DO block, so SQLite now fails the migration
step with `near "DO": syntax error` before any test runs. Point this at a
scratch PostgreSQL container instead:

    docker run -d --name horilla-test-db --network container:horilla-server \
        -e POSTGRES_PASSWORD=<pw> -e POSTGRES_DB=horillatest \
        pgvector/pgvector:pg16 -p 5439
    docker exec -e HORILLA_TEST_DB_PASSWORD=<pw> horilla-server \
        python manage.py test recruitment.tests_candidate_search \
        --settings=horilla.test_settings_postgres

Never point this at the production database -- Django creates and DROPS a
`test_<name>` database on whichever server it is given.
"""

import os

from horilla.settings import *  # noqa: F401, F403

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": os.environ.get("HORILLA_TEST_DB_NAME", "horillatest"),
        "USER": os.environ.get("HORILLA_TEST_DB_USER", "postgres"),
        "PASSWORD": os.environ["HORILLA_TEST_DB_PASSWORD"],
        "HOST": os.environ.get("HORILLA_TEST_DB_HOST", "127.0.0.1"),
        "PORT": os.environ.get("HORILLA_TEST_DB_PORT", "5439"),
    }
}

# Disable HTTPS redirect so the Django test client (HTTP) reaches views directly.
SECURE_SSL_REDIRECT = False
SESSION_COOKIE_SECURE = False
CSRF_COOKIE_SECURE = False

# Strip the SSO gate; tests authenticate directly.
MIDDLEWARE = [m for m in MIDDLEWARE if "GsuiteGateAuthMiddleware" not in m]
