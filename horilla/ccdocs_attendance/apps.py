from django.apps import AppConfig
from django.core import checks


def public_path_guard_check(app_configs=None, **kwargs):
    """
    Refuse to start (manage.py check / migrate / test fail) unless the public
    path guard is in MIDDLEWARE and runs before the Google-gate login.
    """
    from django.conf import settings

    from horilla.ccdocs_attendance.middleware import DOTTED_PATH, GATE_NAME

    middleware = list(settings.MIDDLEWARE)
    if DOTTED_PATH not in middleware:
        return [
            checks.Error(
                f"{DOTTED_PATH} is not in MIDDLEWARE.",
                hint="Add the insert(0, ...) line next to the ccdocs_attendance "
                "INSTALLED_APPS line in horilla/horilla_apps.py.",
                id="ccdocs_attendance.E001",
            )
        ]
    gates = [i for i, name in enumerate(middleware) if name.endswith(GATE_NAME)]
    if gates and middleware.index(DOTTED_PATH) > min(gates):
        return [
            checks.Error(
                f"{DOTTED_PATH} must come before {GATE_NAME} in MIDDLEWARE.",
                id="ccdocs_attendance.E002",
            )
        ]
    return []


class CcdocsAttendanceConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "horilla.ccdocs_attendance"
    label = "ccdocs_attendance"
    verbose_name = "CCDocs Floor Attendance"

    def ready(self):
        from django.urls import include, path

        from horilla.urls import urlpatterns

        # Same pattern as leave/apps.py: the app mounts itself, so horilla/urls.py
        # needs no edit. Trailing slashes everywhere (APPEND_SLASH would 301 the
        # bare paths, and a keyword monitor must not follow a redirect).
        urlpatterns.append(
            path(
                "attendance-notice/", include("horilla.ccdocs_attendance.urls_public")
            ),
        )
        urlpatterns.append(
            path("ccdocs-attendance/", include("horilla.ccdocs_attendance.urls")),
        )
        checks.register(public_path_guard_check, checks.Tags.security)
        super().ready()
