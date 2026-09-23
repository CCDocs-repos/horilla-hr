"""
Manager page: /ccdocs-attendance/manage/ (Horilla sign-in; the Google gate sits
in front in production).

View: members of "Attendance Viewers" or "Attendance Fixers".
Void a point / mark a notice excused / excuse dates: "Attendance Fixers" ONLY.
Group membership is checked explicitly -- a superuser outside the group is
refused like anyone else. Every action is POST + CSRF. Nothing here sends email.
"""

from collections import defaultdict
from datetime import date, timedelta
from decimal import Decimal
from urllib.parse import urlencode

from django.contrib import messages
from django.db import transaction
from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.utils import timezone

from employee.models import Employee
from horilla.ccdocs_attendance import common
from horilla.ccdocs_attendance.models import AttendanceNotice, DayResult, PointEntry
from horilla.decorators import login_required

MAX_EXCUSE_SPAN_DAYS = 62
LIST_LIMIT = 200
LEVELS = (
    (6, "6+: job review"),
    (5, "5: meeting"),
    (4, "4: written warning"),
    (3, "3: verbal warning"),
)


def _in_group(user, name):
    return bool(
        user and user.is_authenticated and user.groups.filter(name=name).exists()
    )


def can_fix(user):
    return _in_group(user, common.FIXERS_GROUP)


def can_view(user):
    return can_fix(user) or _in_group(user, common.VIEWERS_GROUP)


def _refuse(request, text):
    return render(
        request,
        "ccdocs_attendance/notice_message.html",
        {"title": "Not allowed", "text": text},
        status=403,
    )


def _plain_405():
    return HttpResponse("Method not allowed", status=405, content_type="text/plain")


def _back(request):
    url = reverse("ccdocs-attendance-manage")
    employee = (request.POST.get("back_employee") or "").strip()
    if employee.isdigit():
        url += "?" + urlencode({"employee": employee})
    return redirect(url)


def _level(total):
    for threshold, text in LEVELS:
        if total >= threshold:
            return text
    return ""


def _void_points(points_qs, user, reason):
    now = timezone.now()
    count = 0
    for entry in points_qs.select_for_update().filter(voided_at__isnull=True):
        entry.voided_at = now
        entry.voided_by = common.user_tag(user)
        entry.void_reason = reason[:300]
        entry.save(update_fields=["voided_at", "voided_by", "void_reason"])
        count += 1
    return count


def _excuse_days(employee_id, first, last, user, reason):
    now = timezone.now()
    return DayResult.objects.filter(
        employee_id=employee_id, day__gte=first, day__lte=last
    ).update(
        status="excused",
        excused_by=common.user_tag(user),
        excused_at=now,
        excuse_reason=reason[:300],
        updated_at=now,
    )


@login_required
def manage(request):
    if request.method not in ("GET", "HEAD"):
        return _plain_405()
    if not can_view(request.user):
        return _refuse(request, "Only the attendance managers can see this page.")

    today = common.today_et()
    window_start, window_end = common.reset_window(today)
    only = request.GET.get("employee", "").strip()
    only_id = int(only) if only.isdigit() else None

    window_points = PointEntry.objects.filter(
        day__gte=window_start, day__lte=window_end
    )
    totals = defaultdict(Decimal)
    for entry in window_points.filter(voided_at__isnull=True):
        totals[entry.employee_id] += entry.points
    with_points = set(window_points.values_list("employee_id", flat=True))

    people = list(common.floor_employees()) + list(
        common._employees().filter(id__in=with_points)
    )
    people = list({p.id: p for p in people}.values())
    labels = common.labels_for(people)
    rows = []
    for person in people:
        work_info = getattr(person, "employee_work_info", None)
        shift = getattr(work_info, "shift_id", None) if work_info else None
        total = totals.get(person.id, Decimal("0"))
        rows.append(
            {
                "id": person.id,
                "label": labels[person.id],
                "name": person.get_full_name(),
                "active": person.is_active,
                "position": (
                    getattr(work_info, "job_position_id", None) if work_info else None
                ),
                "shift": shift.employee_shift if shift else "",
                "total": total,
                "level": _level(total),
            }
        )
    rows.sort(key=lambda r: (-r["total"], r["label"].lower()))
    names = {r["id"]: r["label"] for r in rows}

    entries = window_points.order_by("-day", "-id")
    notices = AttendanceNotice.objects.filter(to_date__gte=today - timedelta(days=30))
    day_results = DayResult.objects.filter(day__gte=today - timedelta(days=14))
    if only_id is not None:
        entries = entries.filter(employee_id=only_id)
        notices = notices.filter(employee_id=only_id)
        day_results = day_results.filter(employee_id=only_id)
    entries = list(entries[:LIST_LIMIT])
    notices = list(notices.order_by("-from_date", "-id")[:LIST_LIMIT])
    day_results = list(day_results.order_by("-day", "employee_id")[:LIST_LIMIT])
    shown = {
        obj.employee_id for group in (entries, notices, day_results) for obj in group
    }
    missing = shown - set(names)
    if missing:
        names.update(common.labels_for(common._employees().filter(id__in=missing)))
    for group in (entries, notices, day_results):
        for obj in group:
            obj.person = names.get(obj.employee_id, f"employee {obj.employee_id}")

    return render(
        request,
        "ccdocs_attendance/manage.html",
        {
            "today": today,
            "window_start": window_start,
            "window_end": window_end,
            "rows": rows,
            "entries": entries,
            "notices": notices,
            "day_results": day_results,
            "only_id": only_id,
            "only_label": names.get(only_id, "") if only_id is not None else "",
            "can_fix": can_fix(request.user),
            "floor_choices": sorted(
                ((r["id"], r["label"]) for r in rows), key=lambda c: c[1].lower()
            ),
            "list_limit": LIST_LIMIT,
        },
    )


@login_required
def void_point(request, point_id):
    if request.method != "POST":
        return _plain_405()
    if not can_fix(request.user):
        return _refuse(request, "Only Attendance Fixers can void a point.")
    reason = (request.POST.get("reason") or "").strip()
    if not reason:
        messages.error(request, "Write a reason to void a point.")
        return _back(request)
    with transaction.atomic():
        entry = PointEntry.objects.select_for_update().filter(id=point_id).first()
        if entry is None:
            messages.error(request, "That point does not exist.")
            return _back(request)
        if entry.voided:
            messages.info(request, "That point was already voided.")
            return _back(request)
        entry.voided_at = timezone.now()
        entry.voided_by = common.user_tag(request.user)
        entry.void_reason = reason[:300]
        entry.save(update_fields=["voided_at", "voided_by", "void_reason"])
    messages.success(request, f"Point voided ({entry.day}, {entry.rule_key}).")
    return _back(request)


@login_required
def excuse_notice(request, notice_id):
    if request.method != "POST":
        return _plain_405()
    if not can_fix(request.user):
        return _refuse(request, "Only Attendance Fixers can excuse a notice.")
    note = (request.POST.get("note") or "").strip()[:300]
    with transaction.atomic():
        notice = (
            AttendanceNotice.objects.select_for_update().filter(id=notice_id).first()
        )
        if notice is None:
            messages.error(request, "That form does not exist.")
            return _back(request)
        if notice.status != "requested":
            messages.info(request, f"That form is already {notice.status}.")
            return _back(request)
        notice.status = "excused"
        notice.status_changed_at = timezone.now()
        notice.status_changed_by = common.user_tag(request.user)
        notice.status_note = note
        notice.save(
            update_fields=[
                "status",
                "status_changed_at",
                "status_changed_by",
                "status_note",
            ]
        )
        reason = f"excused (form {notice.id})" + (f": {note}" if note else "")
        voided = _void_points(
            PointEntry.objects.filter(
                employee_id=notice.employee_id,
                day__gte=notice.from_date,
                day__lte=notice.to_date,
            ),
            request.user,
            reason,
        )
        days = _excuse_days(
            notice.employee_id, notice.from_date, notice.to_date, request.user, reason
        )
    messages.success(
        request,
        f"Form marked excused. {voided} point(s) voided, {days} day(s) marked excused.",
    )
    return _back(request)


@login_required
def excuse_dates(request):
    if request.method != "POST":
        return _plain_405()
    if not can_fix(request.user):
        return _refuse(request, "Only Attendance Fixers can excuse dates.")
    raw_employee = (request.POST.get("employee_id") or "").strip()
    reason = (request.POST.get("reason") or "").strip()
    try:
        first = date.fromisoformat((request.POST.get("from_date") or "").strip())
        last = date.fromisoformat(
            (request.POST.get("to_date") or "").strip() or first.isoformat()
        )
    except ValueError:
        messages.error(request, "Pick real dates.")
        return _back(request)
    if not raw_employee.isdigit():
        messages.error(request, "Pick a person.")
        return _back(request)
    employee = Employee.objects.entire().filter(id=int(raw_employee)).first()
    if employee is None:
        messages.error(request, "That person does not exist.")
        return _back(request)
    if not reason:
        messages.error(request, "Write a reason to excuse dates.")
        return _back(request)
    if last < first:
        messages.error(request, "The last day cannot be before the first day.")
        return _back(request)
    if (last - first).days + 1 > MAX_EXCUSE_SPAN_DAYS:
        messages.error(
            request, f"Excuse at most {MAX_EXCUSE_SPAN_DAYS} days at a time."
        )
        return _back(request)

    tag = common.user_tag(request.user)
    with transaction.atomic():
        voided = _void_points(
            PointEntry.objects.filter(employee=employee, day__gte=first, day__lte=last),
            request.user,
            f"excused dates: {reason}",
        )
        days = _excuse_days(
            employee.id, first, last, request.user, f"excused dates: {reason}"
        )
        # Days not scored yet (today, later this week) come out excused too:
        # the engine sees an excused notice and gives no points.
        now = timezone.now()
        AttendanceNotice.objects.create(
            employee=employee,
            kind="out",
            from_date=first,
            to_date=last,
            reason=reason[:300],
            filed_by_self=False,
            filed_by_name=f"Manager page: {tag}"[:120],
            filed_at=now,
            status="excused",
            status_changed_at=now,
            status_changed_by=tag,
            status_note="excused dates on the manager page",
        )
    messages.success(
        request,
        f"Excused {first} to {last}: {voided} point(s) voided, {days} day(s) marked excused.",
    )
    return _back(request)
