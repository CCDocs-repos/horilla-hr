"""
Public late/out notice form: /attendance-notice/<link-token>/

No sign-in. It never logs anyone in and never reads the X-Auth-Request-*
headers (middleware.PublicNoticeMiddleware removes them before the Google-gate
login can see them). Wrong token -> 404; token not configured -> 503; CSRF on;
honeypot; per-IP limit (keyed on CF-Connecting-IP). There is no daily ceiling
for everyone: one person with the link must not be able to close the form for
every agent. A busy day only raises an alarm (NOTICE_ALARM_PER_DAY, logged here
and reported by GET day/ as notice_form.alarm) and the form keeps saving.
"""

import hmac
import logging
import time as time_module

from django.core.cache import cache
from django.db import DatabaseError
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.utils import timezone
from django.views.decorators.cache import never_cache

from horilla.ccdocs_attendance import common
from horilla.ccdocs_attendance.forms import NoticeForm
from horilla.ccdocs_attendance.models import AttendanceNotice

logger = logging.getLogger(__name__)

PING_KEYWORD = "attendance-notice-ok"
PER_IP_PER_HOUR = 20
CACHE_PREFIX = "ccdocs_attendance:notice"


def _method_not_allowed():
    # A plain 405: Horilla's MethodNotAllowedMiddleware would swap an
    # HttpResponseNotAllowed for its signed-in 405 page.
    return HttpResponse("Method not allowed", status=405, content_type="text/plain")


def _message(request, status, title, text):
    return render(
        request,
        "ccdocs_attendance/notice_message.html",
        {"title": title, "text": text},
        status=status,
    )


def _check_token(request, token):
    """None when the token is right, else the response to send."""
    expected = common.link_token()
    if not expected:
        return _message(
            request,
            503,
            "This form is not turned on yet",
            "Please tell your team lead you could not send the form.",
        )
    if not hmac.compare_digest(token.encode("utf-8"), expected.encode("utf-8")):
        return _message(request, 404, "Page not found", "Check the link and try again.")
    return None


def client_ip(request) -> str:
    """CF-Connecting-IP, else the last X-Forwarded-For hop, else REMOTE_ADDR."""
    ip = (request.META.get("HTTP_CF_CONNECTING_IP") or "").strip()
    if not ip:
        hops = [
            h.strip()
            for h in (request.META.get("HTTP_X_FORWARDED_FOR") or "").split(",")
        ]
        hops = [h for h in hops if h]
        ip = hops[-1] if hops else ""
    if not ip:
        ip = (request.META.get("REMOTE_ADDR") or "").strip()
    return (ip or "unknown")[:64]


def _count(key, timeout):
    cache.add(key, 0, timeout)
    try:
        return cache.incr(key)
    except ValueError:  # expired between add() and incr()
        cache.set(key, 1, timeout)
        return 1


def _ip_bucket_full(ip_hash) -> bool:
    hour = int(time_module.time() // 3600)
    return _count(f"{CACHE_PREFIX}:ip:{ip_hash}:{hour}", 3600) > PER_IP_PER_HOUR


def _choices():
    employees = list(common.floor_employees())
    labels = common.labels_for(employees)
    return sorted(
        ((e.id, labels[e.id]) for e in employees), key=lambda c: (c[1].lower(), c[0])
    )


def _ping_down(body):
    return HttpResponse(body, status=503, content_type="text/plain")


@never_cache
def ping(request):
    """
    The keyword only when the form really works: its link token is set and
    the database answers. Anything else is a 503 with a different body, so
    the keyword monitor goes red.
    """
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    if not common.link_token():
        return _ping_down("attendance-notice-not-configured")
    try:
        AttendanceNotice.objects.exists()
    except DatabaseError:
        logger.exception("attendance notice ping: the database check failed")
        return _ping_down("attendance-notice-db-error")
    return HttpResponse(PING_KEYWORD, content_type="text/plain")


def _raise_alarm_if_busy(ip_hash):
    filed_today = common.notices_filed_on(common.today_et())
    if filed_today >= common.NOTICE_ALARM_PER_DAY:
        # One line per busy day; GET day/ reports it to the engine every run.
        if cache.add(f"{CACHE_PREFIX}:alarm:{common.today_et()}", 1, 2 * 24 * 3600):
            logger.error(
                "attendance notice form: %s notices filed today (alarm at %s); "
                "latest from ip_hash %s",
                filed_today,
                common.NOTICE_ALARM_PER_DAY,
                ip_hash[:12],
            )


@never_cache
def notice_form(request, token):
    if request.method not in ("GET", "HEAD", "POST"):
        return _method_not_allowed()
    refused = _check_token(request, token)
    if refused is not None:
        return refused

    today = common.today_et()
    choices = _choices()
    if request.method == "POST":
        ip_hash = common.hash_ip(client_ip(request))
        if _ip_bucket_full(ip_hash):
            return _message(
                request,
                429,
                "Too many forms from here",
                "Please wait an hour and try again, or tell your team lead.",
            )
        form = NoticeForm(request.POST, employee_choices=choices, today=today)
        if form.is_bot():
            return redirect("ccdocs-attendance-notice-thanks", token=token)
        if form.is_valid():
            data = form.cleaned_data
            notice = AttendanceNotice.objects.create(
                employee_id=data["employee"],
                kind=data["kind"],
                from_date=data["from_date"],
                to_date=data["to_date"],
                expected_arrival=data["expected_arrival"],
                reason=data["reason"],
                filed_by_self=not data["filing_for_someone_else"],
                filed_by_name=data["filed_by_name"],
                filed_at=timezone.now(),
                ip_hash=ip_hash,
                status="requested",
            )
            logger.info("attendance notice %s filed (%s)", notice.id, notice.kind)
            _raise_alarm_if_busy(ip_hash)
            return redirect("ccdocs-attendance-notice-thanks", token=token)
    else:
        form = NoticeForm(employee_choices=choices, today=today)

    return render(
        request,
        "ccdocs_attendance/notice_form.html",
        {"form": form, "today": today},
    )


@never_cache
def notice_thanks(request, token):
    if request.method not in ("GET", "HEAD"):
        return _method_not_allowed()
    refused = _check_token(request, token)
    if refused is not None:
        return refused
    return render(request, "ccdocs_attendance/notice_thanks.html", {"token": token})
