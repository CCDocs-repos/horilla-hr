"""
Shared helpers: floor positions, names of the shifts and groups, Eastern-time
date maths, reset windows and the short name ("label") people see.

Every business date and time is worked out in America/New_York explicitly.
settings.TIME_ZONE is never used for that (tests run with it unset, which
makes Django fall back to Asia/Kolkata).
"""

import hashlib
import os
from collections import Counter, defaultdict
from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

ET = ZoneInfo("America/New_York")

# Horilla job positions. 26/27 = floor agents; 28 = team lead (listed, not scored).
FLOOR_POSITION_IDS = (26, 27)
ROSTER_POSITION_IDS = (26, 27, 28)

FLOOR_SHIFT_NAME = "Floor 12-8 ET"
LATE_SHIFT_NAME = "Floor 11-8 ET"

FIXERS_GROUP = "Attendance Fixers"
VIEWERS_GROUP = "Attendance Viewers"

WEEKDAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)

DEFAULT_PUBLIC_BASE = "https://hr.ccdocs.com"


def now_et():
    return timezone.now().astimezone(ET)


def today_et():
    return now_et().date()


def at_et(day: date, clock: time) -> datetime:
    """An aware datetime for a wall-clock time on a day in New York (DST-aware)."""
    return datetime.combine(day, clock.replace(tzinfo=None), tzinfo=ET)


def iso_et(value):
    """ISO-8601 with the New York offset, or None."""
    if value is None:
        return None
    return value.astimezone(ET).isoformat(timespec="seconds")


def hhmm(value):
    return value.strftime("%H:%M") if value is not None else None


def reset_window(day: date):
    """Points reset Jan 1 and Jun 1: Jan 1..May 31 or Jun 1..Dec 31."""
    if day.month < 6:
        return date(day.year, 1, 1), date(day.year, 5, 31)
    return date(day.year, 6, 1), date(day.year, 12, 31)


def weekday_name(day: date) -> str:
    return WEEKDAY_NAMES[day.weekday()]


def public_base() -> str:
    base = (
        os.environ.get("ATTENDANCE_PUBLIC_BASE")
        or getattr(settings, "ATTENDANCE_PUBLIC_BASE", "")
        or DEFAULT_PUBLIC_BASE
    )
    return base.strip().rstrip("/")


def link_token() -> str:
    return (os.environ.get("ATTENDANCE_NOTICE_LINK_TOKEN") or "").strip()


def form_url():
    token = link_token()
    if not token:
        return None
    return f"{public_base()}/attendance-notice/{token}/"


def manage_url() -> str:
    return f"{public_base()}/ccdocs-attendance/manage/"


def hash_ip(ip: str) -> str:
    salt = str(settings.SECRET_KEY)
    return hashlib.sha256(f"{salt}:{ip}".encode("utf-8")).hexdigest()


def employee_email(employee) -> str:
    """Work email when one is set, else the personal email."""
    work_info = getattr(employee, "employee_work_info", None)
    work_email = (getattr(work_info, "email", None) or "").strip()
    return work_email or (employee.email or "").strip()


def position_id(employee):
    work_info = getattr(employee, "employee_work_info", None)
    return getattr(work_info, "job_position_id_id", None)


def date_joining(employee):
    work_info = getattr(employee, "employee_work_info", None)
    return getattr(work_info, "date_joining", None)


def _employees():
    # entire(): never the per-session company filter Horilla's manager applies.
    from employee.models import Employee

    return Employee.objects.entire().select_related(
        "employee_work_info", "employee_work_info__shift_id"
    )


def floor_employees():
    """Active employees in the floor positions (the people on the form)."""
    return _employees().filter(
        is_active=True, employee_work_info__job_position_id__in=FLOOR_POSITION_IDS
    )


def label_base_employees():
    """The set labels are made unique across: active 26/27/28 + open links."""
    from horilla.ccdocs_attendance.models import AgentLink

    open_ids = AgentLink.objects.filter(valid_to__isnull=True).values("employee_id")
    return _employees().filter(
        Q(is_active=True, employee_work_info__job_position_id__in=ROSTER_POSITION_IDS)
        | Q(id__in=open_ids)
    )


def base_label(employee) -> str:
    first = (employee.employee_first_name or "").strip()
    last_words = (employee.employee_last_name or "").split()
    initial = f"{last_words[0][0].upper()}." if last_words else ""
    return f"{first} {initial}".strip()


def _joined(employee, with_day=False) -> str:
    joined = date_joining(employee)
    if joined is None:
        return "(start date unknown)"
    if with_day:
        return f"(joined {joined:%b} {joined.day})"
    return f"(joined {joined:%b})"


def build_labels(employees) -> dict:
    """
    {employee_id: label}. Label = first name + first letter of the first word of
    the last name + "." ("Quinn D."). Two people with the same label get
    " (joined Mon)"; if that still clashes, " (joined Mon D)"; if that still
    clashes, " #n" in id order. Labels come out unique.
    """
    employees = sorted({e.id: e for e in employees}.values(), key=lambda e: e.id)
    by_base = defaultdict(list)
    for emp in employees:
        by_base[base_label(emp)].append(emp)

    labels = {}
    for base, members in by_base.items():
        if len(members) == 1:
            labels[members[0].id] = base
            continue
        step1 = {m.id: f"{base} {_joined(m)}" for m in members}
        clashes = Counter(step1.values())
        for member in members:
            label = step1[member.id]
            if clashes[label] > 1:
                label = f"{base} {_joined(member, with_day=True)}"
            labels[member.id] = label

    seen = Counter()
    totals = Counter(labels.values())
    for emp in employees:
        label = labels[emp.id]
        if totals[label] > 1:
            seen[label] += 1
            labels[emp.id] = f"{label} #{seen[label]}"
    return labels


def labels_for(employees) -> dict:
    """Labels for `employees`, made unique across the label base set as well."""
    employees = list(employees)
    universe = {e.id: e for e in label_base_employees()}
    for emp in employees:
        universe.setdefault(emp.id, emp)
    all_labels = build_labels(universe.values())
    return {emp.id: all_labels[emp.id] for emp in employees}


def user_tag(user) -> str:
    return (getattr(user, "email", "") or "").strip() or user.get_username()
