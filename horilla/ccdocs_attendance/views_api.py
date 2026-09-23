"""
Token API for the attendance engine (outside Horilla).

Base: /ccdocs-attendance/api/v1/  -- JSON only, csrf_exempt, bearer token
HORILLA_ATTENDANCE_TOKEN checked the same way ccdocs_automation/ingest.py checks
its token: unset -> 503, missing/wrong -> 401, hmac.compare_digest. Never reads
the session or any X-Auth-Request-* header.
"""

import hmac
import json
import logging
import os
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

from django.db import IntegrityError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt

from base.models import EmployeeShift, EmployeeShiftSchedule
from employee.models import Employee, EmployeeWorkInformation
from horilla.ccdocs_attendance import common
from horilla.ccdocs_attendance.models import (
    AgentLink,
    AttendanceNotice,
    DayResult,
    Delivery,
    PointEntry,
)

logger = logging.getLogger(__name__)

TOKEN_ENV = "HORILLA_ATTENDANCE_TOKEN"
RESULT_STATUSES = {code for code, _ in DayResult.STATUS_CHOICES}
RULE_KEYS = {"late", "out_notice", "out_no_notice"}
DELIVERY_KINDS = {code for code, _ in Delivery.KIND_CHOICES}
NOTICE_LEAD = timedelta(minutes=60)
MAX_HISTORY_DAYS = 366


class ApiError(Exception):
    def __init__(self, status, code, detail=""):
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


def _error(status, code, detail=""):
    return JsonResponse({"error": code, "detail": detail}, status=status)


def api_view(method):
    """Token check + method check + JSON errors for every API view."""

    def decorator(view):
        @csrf_exempt
        @wraps(view)
        def wrapped(request, *args, **kwargs):
            expected = (os.environ.get(TOKEN_ENV) or "").strip()
            if not expected:
                logger.error("%s not set; refusing attendance API calls", TOKEN_ENV)
                return JsonResponse({"error": "not_configured"}, status=503)
            auth = request.META.get("HTTP_AUTHORIZATION", "")
            if not auth.lower().startswith("bearer "):
                return JsonResponse({"error": "unauthorized"}, status=401)
            presented = auth.split(" ", 1)[1].strip()
            if not hmac.compare_digest(
                presented.encode("utf-8"), expected.encode("utf-8")
            ):
                return JsonResponse({"error": "unauthorized"}, status=401)
            if request.method != method:
                return _error(405, "method_not_allowed", f"use {method}")
            try:
                return view(request, *args, **kwargs)
            except ApiError as exc:
                return _error(exc.status, exc.code, exc.detail)

        return wrapped

    return decorator


# --------------------------------------------------------------------------- #
# parsing helpers
# --------------------------------------------------------------------------- #


def _body(request):
    try:
        body = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ApiError(400, "bad_json", "body is not valid JSON")
    if not isinstance(body, dict):
        raise ApiError(400, "bad_json", "body must be a JSON object")
    return body


def _date(value, field):
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{field} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "bad_request", f"{field} must be YYYY-MM-DD")


def _clock(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{field} must be HH:MM")
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time()
        except ValueError:
            continue
    raise ApiError(400, "bad_request", f"{field} must be HH:MM")


def _aware(value, field):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{field} must be ISO-8601 with an offset")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ApiError(400, "bad_request", f"{field} must be ISO-8601 with an offset")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ApiError(400, "bad_request", f"{field} must carry a UTC offset")
    return parsed


def _int(value, field, allow_none=False):
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ApiError(400, "bad_request", f"{field} must be an integer")
    return value


def _str(value, field, max_len, allow_blank=False, allow_none=False):
    if value is None and allow_none:
        return None
    if not isinstance(value, str):
        raise ApiError(400, "bad_request", f"{field} must be a string")
    value = value.strip()
    if not value and not allow_blank:
        raise ApiError(400, "bad_request", f"{field} is required")
    if len(value) > max_len:
        raise ApiError(400, "bad_request", f"{field} is longer than {max_len}")
    return value


def _list(value, field):
    if not isinstance(value, list):
        raise ApiError(400, "bad_request", f"{field} must be a list")
    return value


def _dict(value, field):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ApiError(400, "bad_request", f"{field} must be an object")
    return value


def _points_value(value, field):
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ApiError(400, "bad_request", f"{field} must be a number")
    try:
        number = Decimal(str(value))
    except InvalidOperation:
        raise ApiError(400, "bad_request", f"{field} must be a number")
    if not number.is_finite() or number < 0 or number > 10:
        raise ApiError(400, "bad_request", f"{field} must be between 0 and 10")
    return number


def _employees_by_id(ids):
    found = {e.id: e for e in Employee.objects.entire().filter(id__in=set(ids))}
    missing = sorted(set(ids) - set(found))
    if missing:
        raise ApiError(404, "not_found", f"no employee with id {missing}")
    return found


# --------------------------------------------------------------------------- #
# serializers
# --------------------------------------------------------------------------- #


def _link_json(link):
    return {
        "id": link.id,
        "dialer_user": link.dialer_user,
        "webwork_email": link.webwork_email,
        "valid_from": link.valid_from.isoformat(),
        "valid_to": link.valid_to.isoformat() if link.valid_to else None,
    }


def _notice_json(notice):
    return {
        "id": notice.id,
        "employee_id": notice.employee_id,
        "kind": notice.kind,
        "from_date": notice.from_date.isoformat(),
        "to_date": notice.to_date.isoformat(),
        "expected_arrival": common.hhmm(notice.expected_arrival),
        "filed_at": common.iso_et(notice.filed_at),
        "filed_by_self": notice.filed_by_self,
        "filed_by_name": notice.filed_by_name,
        "status": notice.status,
    }


def _result_json(result):
    return {
        "employee_id": result.employee_id,
        "day": result.day.isoformat(),
        "status": result.status,
        "scheduled_start": common.hhmm(result.scheduled_start),
        "first_login_at": common.iso_et(result.first_login_at),
        "minutes_late": result.minutes_late,
        "notice_id": result.notice_id,
        "closed": result.closed,
    }


def _entry_json(entry):
    return {
        "idem_key": entry.idem_key,
        "day": entry.day.isoformat(),
        "rule_key": entry.rule_key,
        "points": float(entry.points),
        "voided": entry.voided,
        "source": entry.source,
    }


def _delivery_json(delivery):
    return {
        "key": delivery.key,
        "kind": delivery.kind,
        "status": delivery.status,
        "detail": delivery.detail,
        "created_at": common.iso_et(delivery.created_at),
        "updated_at": common.iso_et(delivery.updated_at),
    }


def _called_in(notices, day, scheduled_start):
    """A non-void notice covering the day, filed >= 60 min before the start."""
    start = common.at_et(day, scheduled_start or time(0, 0))
    return any(
        n.from_date <= day <= n.to_date and n.filed_at <= start - NOTICE_LEAD
        for n in notices
    )


# --------------------------------------------------------------------------- #
# GET day/
# --------------------------------------------------------------------------- #


@api_view("GET")
def day_view(request):
    day = _date(request.GET.get("date"), "date")
    include = []
    raw_include = (request.GET.get("include") or "").strip()
    if raw_include:
        try:
            include = [int(part) for part in raw_include.split(",") if part.strip()]
        except ValueError:
            raise ApiError(
                400, "bad_request", "include must be employee ids, comma separated"
            )
        _employees_by_id(include)
    try:
        history_days = int(request.GET.get("history_days", 14))
    except ValueError:
        raise ApiError(400, "bad_request", "history_days must be an integer")
    if not 0 <= history_days <= MAX_HISTORY_DAYS:
        raise ApiError(
            400, "bad_request", f"history_days must be 0..{MAX_HISTORY_DAYS}"
        )

    open_holders = AgentLink.objects.filter(valid_to__isnull=True).values("employee_id")
    employees = list(
        common._employees()
        .filter(
            Q(
                is_active=True,
                employee_work_info__job_position_id__in=common.ROSTER_POSITION_IDS,
            )
            | Q(id__in=open_holders)
            | Q(id__in=include)
        )
        .order_by("id")
    )
    employee_ids = [e.id for e in employees]
    labels = common.labels_for(employees)

    shift_ids = {
        e.employee_work_info.shift_id_id
        for e in employees
        if getattr(e, "employee_work_info", None) and e.employee_work_info.shift_id_id
    }
    schedules = {
        s.shift_id_id: s
        for s in EmployeeShiftSchedule.objects.entire().filter(
            shift_id__in=shift_ids, day__day=common.weekday_name(day)
        )
    }

    links = defaultdict(list)
    for link in AgentLink.objects.filter(
        employee_id__in=employee_ids, valid_from__lte=day
    ).filter(Q(valid_to__isnull=True) | Q(valid_to__gte=day)):
        links[link.employee_id].append(_link_json(link))

    employees_json = []
    for emp in employees:
        work_info = getattr(emp, "employee_work_info", None)
        shift = work_info.shift_id if work_info else None
        schedule = schedules.get(shift.id) if shift else None
        shift_json = None
        if schedule and schedule.start_time and schedule.end_time:
            shift_json = {
                "id": shift.id,
                "name": shift.employee_shift,
                "start": common.hhmm(schedule.start_time),
                "end": common.hhmm(schedule.end_time),
            }
        joined = common.date_joining(emp)
        employees_json.append(
            {
                "id": emp.id,
                "first_name": emp.employee_first_name,
                "last_name": emp.employee_last_name or "",
                "label": labels[emp.id],
                "email": common.employee_email(emp),
                "position_id": common.position_id(emp),
                "is_active": emp.is_active,
                "date_joining": joined.isoformat() if joined else None,
                "shift": shift_json,
                "links": links.get(emp.id, []),
            }
        )

    notices = AttendanceNotice.objects.filter(
        from_date__lte=day, to_date__gte=day
    ).exclude(status="void")

    history_start = day - timedelta(days=history_days)
    history_results = list(
        DayResult.objects.filter(day__gte=history_start, day__lt=day).order_by(
            "employee_id", "day"
        )
    )
    history_notices = defaultdict(list)
    for notice in AttendanceNotice.objects.filter(
        employee_id__in={r.employee_id for r in history_results},
        from_date__lt=day,
        to_date__gte=history_start,
    ).exclude(status="void"):
        history_notices[notice.employee_id].append(notice)
    day_points = defaultdict(Decimal)
    for entry in PointEntry.objects.filter(
        day__gte=history_start, day__lt=day, voided_at__isnull=True
    ):
        day_points[(entry.employee_id, entry.day)] += entry.points
    history = []
    for result in history_results:
        emp_notices = history_notices.get(result.employee_id, [])
        history.append(
            {
                "employee_id": result.employee_id,
                "day": result.day.isoformat(),
                "status": result.status,
                "called_in": _called_in(
                    emp_notices, result.day, result.scheduled_start
                ),
                "any_notice": any(
                    n.from_date <= result.day <= n.to_date for n in emp_notices
                ),
                "closed": result.closed,
                "points": float(day_points.get((result.employee_id, result.day), 0)),
            }
        )

    filed_on_day = common.notices_filed_on(day)
    window_start, window_end = common.reset_window(day)
    points = {
        str(emp_id): {
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "total": 0.0,
            "entries": [],
        }
        for emp_id in employee_ids
    }
    for entry in PointEntry.objects.filter(
        day__gte=window_start, day__lte=window_end
    ).order_by("day", "id"):
        bucket = points.setdefault(
            str(entry.employee_id),
            {
                "window_start": window_start.isoformat(),
                "window_end": window_end.isoformat(),
                "total": 0.0,
                "entries": [],
            },
        )
        bucket["entries"].append(_entry_json(entry))
        if not entry.voided:
            bucket["total"] = float(Decimal(str(bucket["total"])) + entry.points)

    return JsonResponse(
        {
            "date": day.isoformat(),
            "form_url": common.form_url(),
            "manage_url": common.manage_url(),
            "employees": employees_json,
            "notices": [_notice_json(n) for n in notices],
            "day_results": [
                _result_json(r)
                for r in DayResult.objects.filter(day=day).order_by("employee_id")
            ],
            "history": history,
            "points": points,
            "notice_form": {
                "filed_on_day": filed_on_day,
                "alarm_at": common.NOTICE_ALARM_PER_DAY,
                "alarm": filed_on_day >= common.NOTICE_ALARM_PER_DAY,
            },
        }
    )


# --------------------------------------------------------------------------- #
# POST day-results/
# --------------------------------------------------------------------------- #


def _parse_result(item, day, index):
    where = f"results[{index}]"
    item = _dict(item, where)
    employee_id = _int(item.get("employee_id"), f"{where}.employee_id")
    status = item.get("status")
    if status not in RESULT_STATUSES:
        raise ApiError(
            400,
            "bad_request",
            f"{where}.status must be one of {sorted(RESULT_STATUSES)}",
        )
    closed = item.get("closed", False)
    if not isinstance(closed, bool):
        raise ApiError(400, "bad_request", f"{where}.closed must be true or false")
    points = []
    for p_index, point in enumerate(_list(item.get("points", []), f"{where}.points")):
        p_where = f"{where}.points[{p_index}]"
        point = _dict(point, p_where)
        rule_key = point.get("rule_key")
        if rule_key not in RULE_KEYS:
            raise ApiError(
                400,
                "bad_request",
                f"{p_where}.rule_key must be one of {sorted(RULE_KEYS)}",
            )
        idem_key = _str(point.get("idem_key"), f"{p_where}.idem_key", 120)
        expected_key = f"{day.isoformat()}:{employee_id}:{rule_key}"
        if idem_key != expected_key:
            raise ApiError(
                400, "bad_request", f"{p_where}.idem_key must be {expected_key!r}"
            )
        points.append(
            {
                "idem_key": idem_key,
                "rule_key": rule_key,
                "points": _points_value(point.get("points"), f"{p_where}.points"),
            }
        )
    return {
        "employee_id": employee_id,
        "status": status,
        "scheduled_start": _clock(
            item.get("scheduled_start"), f"{where}.scheduled_start"
        ),
        "first_login_at": _aware(item.get("first_login_at"), f"{where}.first_login_at"),
        "minutes_late": _int(
            item.get("minutes_late"), f"{where}.minutes_late", allow_none=True
        ),
        "notice_id": _int(item.get("notice_id"), f"{where}.notice_id", allow_none=True),
        "closed": closed,
        "detail": _dict(item.get("detail"), f"{where}.detail"),
        "points": points,
    }


@api_view("POST")
def day_results_view(request):
    body = _body(request)
    day = _date(body.get("date"), "date")
    run_id = _str(body.get("run_id", ""), "run_id", 120, allow_blank=True)
    results = [
        _parse_result(item, day, i)
        for i, item in enumerate(_list(body.get("results"), "results"))
    ]
    seen = set()
    for result in results:
        if result["employee_id"] in seen:
            raise ApiError(
                400, "bad_request", f"employee {result['employee_id']} is listed twice"
            )
        seen.add(result["employee_id"])
    _employees_by_id([r["employee_id"] for r in results])
    notice_ids = {r["notice_id"] for r in results if r["notice_id"] is not None}
    notices = AttendanceNotice.objects.in_bulk(notice_ids)
    if set(notices) != notice_ids:
        raise ApiError(
            404, "not_found", f"no notice with id {sorted(notice_ids - set(notices))}"
        )
    for result in results:
        notice = notices.get(result["notice_id"])
        if notice is not None and notice.employee_id != result["employee_id"]:
            raise ApiError(
                400, "bad_request", f"notice {notice.id} belongs to another employee"
            )

    counts = {
        "upserted": 0,
        "points_created": 0,
        "points_skipped_voided": 0,
        "skipped_excused": 0,
    }
    conflicts = []
    try:
        with transaction.atomic():
            for result in results:
                existing = (
                    DayResult.objects.select_for_update()
                    .filter(employee_id=result["employee_id"], day=day)
                    .first()
                )
                if existing is not None and existing.excused_by:
                    # A person excused this day on the manager page: the engine
                    # never overwrites it and never adds points to it.
                    counts["skipped_excused"] += 1
                    continue
                day_result = existing or DayResult(
                    employee_id=result["employee_id"], day=day
                )
                day_result.status = result["status"]
                day_result.scheduled_start = result["scheduled_start"]
                day_result.first_login_at = result["first_login_at"]
                day_result.minutes_late = result["minutes_late"]
                day_result.notice_id = result["notice_id"]
                day_result.closed = result["closed"]
                day_result.detail = result["detail"]
                day_result.run_id = run_id
                day_result.save()
                counts["upserted"] += 1

                for point in result["points"]:
                    entry = (
                        PointEntry.objects.select_for_update()
                        .filter(idem_key=point["idem_key"])
                        .first()
                    )
                    if entry is None:
                        PointEntry.objects.create(
                            employee_id=result["employee_id"],
                            day=day,
                            rule_key=point["rule_key"],
                            points=point["points"],
                            idem_key=point["idem_key"],
                            source="engine",
                            day_result=day_result,
                            run_id=run_id,
                        )
                        counts["points_created"] += 1
                    elif entry.voided:
                        counts["points_skipped_voided"] += 1
                    elif entry.points != point["points"]:
                        conflicts.append(
                            {
                                "idem_key": entry.idem_key,
                                "existing": float(entry.points),
                                "incoming": float(point["points"]),
                            }
                        )
    except IntegrityError as exc:
        logger.warning("attendance day-results write raced: %s", exc)
        raise ApiError(
            409, "conflict", "a concurrent write touched the same rows; retry"
        )

    return JsonResponse({**counts, "conflicts": conflicts})


# --------------------------------------------------------------------------- #
# POST points/withdraw/
# --------------------------------------------------------------------------- #


@api_view("POST")
def points_withdraw_view(request):
    body = _body(request)
    keys = _list(body.get("idem_keys"), "idem_keys")
    if not keys or not all(isinstance(k, str) and k.strip() for k in keys):
        raise ApiError(
            400, "bad_request", "idem_keys must be a non-empty list of strings"
        )
    reason = _str(body.get("reason"), "reason", 300)
    _str(body.get("run_id", ""), "run_id", 120, allow_blank=True)
    keys = [k.strip() for k in keys]

    voided = 0
    with transaction.atomic():
        entries = {
            e.idem_key: e
            for e in PointEntry.objects.select_for_update().filter(idem_key__in=keys)
        }
        now = timezone.now()
        for key in keys:
            entry = entries.get(key)
            if entry is None or entry.voided:
                continue
            entry.voided_at = now
            entry.voided_by = "engine"
            entry.void_reason = reason
            entry.save(update_fields=["voided_at", "voided_by", "void_reason"])
            voided += 1
    missing = [k for k in keys if k not in entries]
    return JsonResponse({"voided": voided, "missing": missing})


# --------------------------------------------------------------------------- #
# POST links/
# --------------------------------------------------------------------------- #


@api_view("POST")
def links_view(request):
    body = _body(request)
    opens = []
    for i, item in enumerate(_list(body.get("open", []), "open")):
        where = f"open[{i}]"
        item = _dict(item, where)
        dialer_user = _str(
            item.get("dialer_user"), f"{where}.dialer_user", 40, allow_none=True
        )
        webwork_email = _str(
            item.get("webwork_email"), f"{where}.webwork_email", 254, allow_none=True
        )
        if not dialer_user and not webwork_email:
            raise ApiError(
                400, "bad_request", f"{where} needs a dialer_user or a webwork_email"
            )
        opens.append(
            {
                "employee_id": _int(item.get("employee_id"), f"{where}.employee_id"),
                "dialer_user": dialer_user or None,
                "webwork_email": (webwork_email or "").lower() or None,
                "valid_from": _date(item.get("valid_from"), f"{where}.valid_from"),
            }
        )
    closes = []
    for i, item in enumerate(_list(body.get("close", []), "close")):
        where = f"close[{i}]"
        item = _dict(item, where)
        closes.append(
            {
                "link_id": _int(item.get("link_id"), f"{where}.link_id"),
                "valid_to": _date(item.get("valid_to"), f"{where}.valid_to"),
                "reason": _str(
                    item.get("reason", ""), f"{where}.reason", 200, allow_blank=True
                ),
            }
        )
    _employees_by_id([o["employee_id"] for o in opens])

    opened, closed, conflicts = [], [], []
    try:
        with transaction.atomic():
            links = AgentLink.objects.select_for_update().in_bulk(
                [c["link_id"] for c in closes]
            )
            missing = sorted({c["link_id"] for c in closes} - set(links))
            if missing:
                raise ApiError(404, "not_found", f"no link with id {missing}")
            for close in closes:
                link = links[close["link_id"]]
                if link.valid_to is not None:
                    continue
                if close["valid_to"] < link.valid_from:
                    raise ApiError(
                        400,
                        "bad_request",
                        f"link {link.id}: valid_to is before valid_from",
                    )
                link.valid_to = close["valid_to"]
                link.close_reason = close["reason"]
                link.save(update_fields=["valid_to", "close_reason", "updated_at"])
                closed.append(link.id)

            for item in opens:
                open_links = AgentLink.objects.select_for_update().filter(
                    valid_to__isnull=True
                )
                if item["dialer_user"]:
                    holder = open_links.filter(dialer_user=item["dialer_user"]).first()
                    if holder is not None:
                        if holder.employee_id != item["employee_id"]:
                            conflicts.append(
                                {
                                    "dialer_user": item["dialer_user"],
                                    "held_by": holder.employee_id,
                                }
                            )
                        # Same person already holds it open: identical, no-op.
                        continue
                else:
                    if open_links.filter(
                        employee_id=item["employee_id"],
                        dialer_user__isnull=True,
                        webwork_email=item["webwork_email"],
                    ).exists():
                        continue
                link = AgentLink.objects.create(**item)
                opened.append(link.id)
    except IntegrityError as exc:
        logger.warning("attendance links write raced: %s", exc)
        raise ApiError(
            409, "conflict", "a concurrent write touched the same login; retry"
        )

    return JsonResponse({"opened": opened, "closed": closed, "conflicts": conflicts})


# --------------------------------------------------------------------------- #
# POST shifts/default/
# --------------------------------------------------------------------------- #


@api_view("POST")
def shifts_default_view(request):
    body = _body(request)
    ids = _list(body.get("employee_ids"), "employee_ids")
    if not all(isinstance(i, int) and not isinstance(i, bool) for i in ids):
        raise ApiError(400, "bad_request", "employee_ids must be a list of integers")

    shifts = list(
        EmployeeShift.objects.entire().filter(employee_shift=common.FLOOR_SHIFT_NAME)
    )
    if len(shifts) != 1:
        raise ApiError(
            409,
            "not_configured",
            f"expected exactly one shift named {common.FLOOR_SHIFT_NAME!r}, found "
            f"{len(shifts)}; run manage.py ccdocs_attendance_setup",
        )
    shift = shifts[0]

    assigned, skipped = [], []
    employees = {e.id: e for e in common._employees().filter(id__in=set(ids))}
    with transaction.atomic():
        for emp_id in dict.fromkeys(ids):
            emp = employees.get(emp_id)
            work_info = getattr(emp, "employee_work_info", None) if emp else None
            if emp is None:
                skipped.append({"id": emp_id, "reason": "not_found"})
            elif not emp.is_active:
                skipped.append({"id": emp_id, "reason": "inactive"})
            elif work_info is None:
                skipped.append({"id": emp_id, "reason": "no_work_info"})
            elif work_info.job_position_id_id not in common.FLOOR_POSITION_IDS:
                skipped.append({"id": emp_id, "reason": "not_floor_position"})
            else:
                # update() (not save()): save() would also create a payroll
                # Contract for anyone without one (payroll/signals.py pre_save).
                changed = (
                    EmployeeWorkInformation.objects.entire()
                    .filter(pk=work_info.pk, shift_id__isnull=True)
                    .update(shift_id=shift)
                )
                if changed:
                    assigned.append(emp_id)
                else:
                    skipped.append({"id": emp_id, "reason": "has_shift"})
    return JsonResponse({"assigned": assigned, "skipped": skipped})


# --------------------------------------------------------------------------- #
# deliveries
# --------------------------------------------------------------------------- #


@api_view("POST")
def deliveries_claim_view(request):
    body = _body(request)
    key = _str(body.get("key"), "key", 200)
    kind = body.get("kind")
    if kind not in DELIVERY_KINDS:
        raise ApiError(
            400, "bad_request", f"kind must be one of {sorted(DELIVERY_KINDS)}"
        )
    detail = _dict(body.get("detail"), "detail")
    try:
        with transaction.atomic():
            delivery = Delivery.objects.create(key=key, kind=kind, detail=detail)
    except IntegrityError:
        existing = Delivery.objects.get(key=key)
        return JsonResponse(
            {
                "key": existing.key,
                "status": existing.status,
                "updated_at": common.iso_et(existing.updated_at),
            },
            status=409,
        )
    return JsonResponse({"key": delivery.key, "status": delivery.status}, status=201)


@api_view("POST")
def deliveries_done_view(request):
    body = _body(request)
    key = _str(body.get("key"), "key", 200)
    status = body.get("status")
    if status not in ("sent", "failed"):
        raise ApiError(400, "bad_request", "status must be sent or failed")
    detail = _dict(body.get("detail"), "detail")
    with transaction.atomic():
        delivery = Delivery.objects.select_for_update().filter(key=key).first()
        if delivery is None:
            raise ApiError(404, "not_found", f"no delivery with key {key!r}")
        if delivery.status == status:
            return JsonResponse(_delivery_json(delivery))
        if delivery.status != "attempting":
            raise ApiError(
                409,
                "conflict",
                f"delivery is already {delivery.status}; it cannot become {status}",
            )
        delivery.status = status
        delivery.detail = {**(delivery.detail or {}), **detail}
        delivery.save(update_fields=["status", "detail", "updated_at"])
    return JsonResponse(_delivery_json(delivery))


@api_view("GET")
def deliveries_list_view(request):
    deliveries = Delivery.objects.all()
    status = request.GET.get("status")
    if status:
        if status not in {code for code, _ in Delivery.STATUS_CHOICES}:
            raise ApiError(
                400, "bad_request", "status must be attempting, sent or failed"
            )
        deliveries = deliveries.filter(status=status)
    kind = request.GET.get("kind")
    if kind:
        if kind not in DELIVERY_KINDS:
            raise ApiError(
                400, "bad_request", f"kind must be one of {sorted(DELIVERY_KINDS)}"
            )
        deliveries = deliveries.filter(kind=kind)
    older = request.GET.get("older_than_minutes")
    if older not in (None, ""):
        try:
            minutes = int(older)
        except ValueError:
            raise ApiError(400, "bad_request", "older_than_minutes must be an integer")
        if minutes < 0:
            raise ApiError(400, "bad_request", "older_than_minutes must be 0 or more")
        deliveries = deliveries.filter(
            updated_at__lte=timezone.now() - timedelta(minutes=minutes)
        )
    return JsonResponse(
        {"deliveries": [_delivery_json(d) for d in deliveries.order_by("created_at")]}
    )
