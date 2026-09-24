"""Public URLs, mounted at /attendance-notice/ (Caddy exposes only this prefix)."""

from django.urls import path

from horilla.ccdocs_attendance import views_public

urlpatterns = [
    # ping/ must come first so "ping" is never read as a link token.
    path("ping/", views_public.ping, name="ccdocs-attendance-notice-ping"),
    path("<str:token>/", views_public.notice_form, name="ccdocs-attendance-notice"),
    path(
        "<str:token>/thanks/",
        views_public.notice_thanks,
        name="ccdocs-attendance-notice-thanks",
    ),
]
