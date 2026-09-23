"""Engine API + manager page, mounted at /ccdocs-attendance/ (behind the Google gate)."""

from django.urls import path

from horilla.ccdocs_attendance import views_api, views_manage

urlpatterns = [
    path("api/v1/day/", views_api.day_view, name="ccdocs-attendance-api-day"),
    path(
        "api/v1/day-results/",
        views_api.day_results_view,
        name="ccdocs-attendance-api-day-results",
    ),
    path(
        "api/v1/points/withdraw/",
        views_api.points_withdraw_view,
        name="ccdocs-attendance-api-points-withdraw",
    ),
    path("api/v1/links/", views_api.links_view, name="ccdocs-attendance-api-links"),
    path(
        "api/v1/shifts/default/",
        views_api.shifts_default_view,
        name="ccdocs-attendance-api-shifts-default",
    ),
    path(
        "api/v1/deliveries/",
        views_api.deliveries_list_view,
        name="ccdocs-attendance-api-deliveries",
    ),
    path(
        "api/v1/deliveries/claim/",
        views_api.deliveries_claim_view,
        name="ccdocs-attendance-api-deliveries-claim",
    ),
    path(
        "api/v1/deliveries/done/",
        views_api.deliveries_done_view,
        name="ccdocs-attendance-api-deliveries-done",
    ),
    path("manage/", views_manage.manage, name="ccdocs-attendance-manage"),
    path(
        "manage/points/<int:point_id>/void/",
        views_manage.void_point,
        name="ccdocs-attendance-void-point",
    ),
    path(
        "manage/notices/<int:notice_id>/excuse/",
        views_manage.excuse_notice,
        name="ccdocs-attendance-excuse-notice",
    ),
    path(
        "manage/excuse-dates/",
        views_manage.excuse_dates,
        name="ccdocs-attendance-excuse-dates",
    ),
]
