from django.apps import AppConfig


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
            path("attendance-notice/", include("horilla.ccdocs_attendance.urls_public")),
        )
        urlpatterns.append(
            path("ccdocs-attendance/", include("horilla.ccdocs_attendance.urls")),
        )
        super().ready()
