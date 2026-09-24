"""
manage.py ccdocs_attendance_setup -- one-time floor setup for the attendance app.

Dry run by default: prints every change and runs full_clean on every row it
would write. Nothing is written without --apply.

  --apply --snapshot <path>   write the changes in ONE transaction and save a JSON
                              snapshot (every row created or changed, with its
                              previous values). The snapshot file must not exist.
  --revert <snapshot>         undo a snapshot (dry run unless --apply as well).

What it sets up:
  * shifts "Floor 12-8 ET" (12:00-20:00) and "Floor 11-8 ET" (11:00-20:00) with
    Monday-Friday schedules, linked to --company-id (default 2);
  * "Floor 12-8 ET" for every active employee in the floor positions with no shift;
  * "Floor 11-8 ET" for the ids in --late-shift-ids;
  * "Floor 12-8 ET" for the ids in --reassign-to-floor (people with a wrong shift);
  * groups "Attendance Fixers" / "Attendance Viewers", with the users whose email
    is given in --fixers / --viewers.

Shift changes use QuerySet.update(), not save(): save() would also create a
payroll Contract for anyone without one (payroll/signals.py). No email is sent.
No names, emails or ids live in this file: they are passed on the command line.
"""

import json
import os
from datetime import datetime, time
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import ProtectedError, Q

from attendance.models import WorkRecords
from base.models import Company, EmployeeShift, EmployeeShiftDay, EmployeeShiftSchedule
from employee.models import Employee, EmployeeWorkInformation
from horilla.ccdocs_attendance import common

SNAPSHOT_VERSION = 1
WORK_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday")
SHIFT_SPECS = (
    {
        "name": common.FLOOR_SHIFT_NAME,
        "start": time(12, 0),
        "end": time(20, 0),
        "minimum": "08:00",
        "weekly": "40:00",
    },
    {
        "name": common.LATE_SHIFT_NAME,
        "start": time(11, 0),
        "end": time(20, 0),
        "minimum": "09:00",
        "weekly": "45:00",
    },
)


def _ids(raw, flag):
    ids = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        if not part.isdigit():
            raise CommandError(f"{flag}: {part!r} is not an employee id")
        ids.append(int(part))
    return ids


def _emails(raw):
    return [e.strip().lower() for e in (raw or "").split(",") if e.strip()]


def _errors(exc):
    if hasattr(exc, "message_dict"):
        return "; ".join(f"{k}: {', '.join(v)}" for k, v in exc.message_dict.items())
    return "; ".join(exc.messages)


class Command(BaseCommand):
    help = "Set up shifts and groups for floor attendance (dry run unless --apply)."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="write the changes")
        parser.add_argument(
            "--snapshot", help="where to save the JSON snapshot (with --apply)"
        )
        parser.add_argument("--revert", metavar="SNAPSHOT", help="undo a snapshot")
        parser.add_argument(
            "--late-shift-ids", default="", help="ids that get Floor 11-8 ET"
        )
        parser.add_argument(
            "--reassign-to-floor",
            default="",
            help="ids moved to Floor 12-8 ET from another shift",
        )
        parser.add_argument(
            "--fixers", default="", help="emails added to Attendance Fixers"
        )
        parser.add_argument(
            "--viewers", default="", help="emails added to Attendance Viewers"
        )
        parser.add_argument("--company-id", type=int, default=2)

    # ------------------------------------------------------------------ #

    def say(self, line):
        self.stdout.write(line)

    def handle(self, *args, **options):
        if options["revert"]:
            return self._revert(options["revert"], apply=options["apply"])

        apply = options["apply"]
        snapshot_path = options["snapshot"]
        if apply and not snapshot_path:
            raise CommandError("--apply needs --snapshot <path>")
        if snapshot_path and os.path.exists(snapshot_path):
            raise CommandError(
                f"snapshot {snapshot_path} already exists; pick a new path"
            )

        company = Company.objects.filter(id=options["company_id"]).first()
        if company is None:
            raise CommandError(f"no company with id {options['company_id']}")
        late_ids = _ids(options["late_shift_ids"], "--late-shift-ids")
        reassign_ids = _ids(options["reassign_to_floor"], "--reassign-to-floor")
        both = sorted(set(late_ids) & set(reassign_ids))
        if both:
            raise CommandError(
                f"ids in both --late-shift-ids and --reassign-to-floor: {both}"
            )

        self.say(
            f"ccdocs_attendance_setup: {'APPLY' if apply else 'DRY RUN (nothing is written)'}"
        )
        problems = []
        plan = {
            "shifts": self._plan_shifts(company, problems),
            "groups": self._plan_groups(
                {
                    common.FIXERS_GROUP: _emails(options["fixers"]),
                    common.VIEWERS_GROUP: _emails(options["viewers"]),
                },
                problems,
            ),
        }
        plan["work_info"] = self._plan_work_info(
            plan["shifts"], late_ids, reassign_ids, problems
        )

        if problems:
            for problem in problems:
                self.say(f"  PROBLEM: {problem}")
            if apply:
                raise CommandError(f"{len(problems)} problem(s); nothing was written")
            self.say(
                f"{len(problems)} problem(s): --apply would refuse until they are fixed."
            )
            return

        changes = self._count(plan)
        if not apply:
            self.say(
                f"{changes} change(s) would be made. Re-run with --apply --snapshot <path>."
            )
            return

        with transaction.atomic():
            snapshot = self._apply(plan, company)
            snapshot_file = Path(snapshot_path)
            snapshot_file.parent.mkdir(parents=True, exist_ok=True)
            with open(snapshot_file, "x", encoding="utf-8") as handle:
                json.dump(snapshot, handle, indent=2, sort_keys=True)
        self.say(f"{changes} change(s) written. Snapshot: {snapshot_path}")

    # ------------------------------------------------------------------ #
    # planning (reads only)
    # ------------------------------------------------------------------ #

    def _plan_shifts(self, company, problems):
        days = {d.day: d for d in EmployeeShiftDay.objects.filter(day__in=WORK_DAYS)}
        missing_days = [d for d in WORK_DAYS if d not in days]
        if missing_days:
            problems.append(f"shift days missing from Horilla: {missing_days}")
        planned = []
        for spec in SHIFT_SPECS:
            found = list(
                EmployeeShift.objects.entire().filter(employee_shift=spec["name"])
            )
            if len(found) > 1:
                problems.append(
                    f"{len(found)} shifts are named {spec['name']!r}; expected one"
                )
                continue
            shift = found[0] if found else None
            item = {"spec": spec, "shift": shift, "new_schedules": []}
            if shift is None:
                shift = EmployeeShift(
                    employee_shift=spec["name"], weekly_full_time=spec["weekly"]
                )
                self._clean(shift, f"new shift {spec['name']!r}", problems)
                self.say(f"  create shift {spec['name']!r} (company {company.id})")
            existing = (
                {
                    s.day.day: s
                    for s in EmployeeShiftSchedule.objects.entire().filter(
                        shift_id=shift
                    )
                }
                if shift.pk
                else {}
            )
            for day_name in WORK_DAYS:
                current = existing.get(day_name)
                if current is not None:
                    if (current.start_time, current.end_time) != (
                        spec["start"],
                        spec["end"],
                    ):
                        problems.append(
                            f"shift {spec['name']!r} {day_name} is "
                            f"{current.start_time}-{current.end_time}, expected "
                            f"{spec['start']}-{spec['end']}; fix it in Horilla by hand"
                        )
                    continue
                if day_name not in days:
                    continue
                schedule = EmployeeShiftSchedule(
                    day=days[day_name],
                    start_time=spec["start"],
                    end_time=spec["end"],
                    minimum_working_hour=spec["minimum"],
                )
                if shift.pk:
                    schedule.shift_id = shift
                self._clean(
                    schedule,
                    f"new schedule {spec['name']!r} {day_name}",
                    problems,
                    ["shift_id"],
                )
                item["new_schedules"].append(schedule)
                self.say(
                    f"  create schedule {spec['name']!r} {day_name} "
                    f"{spec['start']:%H:%M}-{spec['end']:%H:%M}"
                )
            planned.append(item)
        return planned

    def _plan_groups(self, wanted, problems):
        planned = []
        for group_name, emails in wanted.items():
            group = Group.objects.filter(name=group_name).first()
            if group is None:
                self.say(f"  create group {group_name!r}")
            members = []
            for email in emails:
                user = self._user_for(email)
                if user is None:
                    problems.append(f"{group_name}: no active Horilla user for {email}")
                    continue
                if group is not None and group.user_set.filter(pk=user.pk).exists():
                    continue
                members.append(user)
                self.say(f"  add user {user.pk} ({email}) to {group_name!r}")
            planned.append({"name": group_name, "group": group, "add": members})
        return planned

    @staticmethod
    def _user_for(email):
        # Same pick as the Google gate: superuser, then staff, then lowest id.
        user = (
            User.objects.filter(email__iexact=email, is_active=True)
            .order_by("-is_superuser", "-is_staff", "id")
            .first()
        )
        if user is not None:
            return user
        employee = (
            Employee.objects.entire()
            .filter(Q(email__iexact=email) | Q(employee_work_info__email__iexact=email))
            .exclude(employee_user_id__isnull=True)
            .order_by("id")
            .first()
        )
        if employee is not None and employee.employee_user_id.is_active:
            return employee.employee_user_id
        return None

    def _plan_work_info(self, shifts, late_ids, reassign_ids, problems):
        by_name = {item["spec"]["name"]: item for item in shifts}
        explicit = {emp_id: common.LATE_SHIFT_NAME for emp_id in late_ids}
        explicit.update({emp_id: common.FLOOR_SHIFT_NAME for emp_id in reassign_ids})

        targets = {}
        people = {e.id: e for e in common._employees().filter(id__in=list(explicit))}
        for emp_id, shift_name in explicit.items():
            emp = people.get(emp_id)
            work_info = getattr(emp, "employee_work_info", None) if emp else None
            if emp is None:
                problems.append(f"employee {emp_id}: not found")
            elif not emp.is_active:
                problems.append(f"employee {emp_id}: not active")
            elif work_info is None:
                problems.append(f"employee {emp_id}: has no work information")
            elif work_info.job_position_id_id not in common.FLOOR_POSITION_IDS:
                problems.append(f"employee {emp_id}: not in a floor position")
            else:
                targets[emp_id] = (emp, shift_name)
        for emp in common.floor_employees().filter(
            employee_work_info__shift_id__isnull=True
        ):
            targets.setdefault(emp.id, (emp, common.FLOOR_SHIFT_NAME))

        planned = []
        for emp_id in sorted(targets):
            emp, shift_name = targets[emp_id]
            work_info = emp.employee_work_info
            shift = by_name.get(shift_name, {}).get("shift")
            if shift is not None and work_info.shift_id_id == shift.pk:
                continue
            previous = work_info.shift_id_id
            if shift is not None:
                work_info.shift_id = shift
                self._clean(work_info, f"employee {emp_id} work information", problems)
            else:
                self._clean(
                    work_info,
                    f"employee {emp_id} work information",
                    problems,
                    ["shift_id"],
                )
            planned.append(
                {
                    "employee_id": emp_id,
                    "work_info_id": work_info.pk,
                    "previous": previous,
                    "shift_name": shift_name,
                }
            )
            self.say(
                f"  employee {emp_id}: shift {previous if previous is not None else 'none'}"
                f" -> {shift_name!r}"
            )
        return planned

    def _clean(self, obj, what, problems, exclude=None):
        try:
            obj.full_clean(exclude=exclude)
        except ValidationError as exc:
            problems.append(f"{what} fails Horilla's own checks: {_errors(exc)}")

    @staticmethod
    def _count(plan):
        count = 0
        for item in plan["shifts"]:
            count += (0 if item["shift"] else 1) + len(item["new_schedules"])
        for item in plan["groups"]:
            count += (0 if item["group"] else 1) + len(item["add"])
        return count + len(plan["work_info"])

    # ------------------------------------------------------------------ #
    # applying (inside the caller's transaction)
    # ------------------------------------------------------------------ #

    def _apply(self, plan, company):
        now = common.now_et()
        snapshot = {
            "version": SNAPSHOT_VERSION,
            "created_at": now.isoformat(timespec="seconds"),
            "created_on_et": now.date().isoformat(),
            "company_id": company.id,
            "created": {"shifts": [], "schedules": [], "groups": [], "memberships": []},
            "work_info_changes": [],
        }
        shift_ids = {}
        for item in plan["shifts"]:
            spec = item["spec"]
            shift = item["shift"]
            if shift is None:
                shift = EmployeeShift(
                    employee_shift=spec["name"], weekly_full_time=spec["weekly"]
                )
                shift.save()
                shift.company_id.add(company)
                snapshot["created"]["shifts"].append(
                    {"id": shift.pk, "name": spec["name"]}
                )
            shift_ids[spec["name"]] = shift.pk
            for schedule in item["new_schedules"]:
                schedule.shift_id = shift
                schedule.save()
                schedule.company_id.add(company)
                snapshot["created"]["schedules"].append(
                    {"id": schedule.pk, "shift_id": shift.pk, "day": schedule.day.day}
                )

        for change in plan["work_info"]:
            new_shift_id = shift_ids[change["shift_name"]]
            updated = (
                EmployeeWorkInformation.objects.entire()
                .filter(pk=change["work_info_id"], shift_id=change["previous"])
                .update(shift_id=new_shift_id)
            )
            if updated != 1:
                raise CommandError(
                    f"employee {change['employee_id']}: shift changed while the setup ran; nothing was written"
                )
            snapshot["work_info_changes"].append(
                {
                    "employee_id": change["employee_id"],
                    "work_info_id": change["work_info_id"],
                    "previous_shift_id": change["previous"],
                    "new_shift_id": new_shift_id,
                }
            )

        for item in plan["groups"]:
            group = item["group"]
            if group is None:
                group = Group.objects.create(name=item["name"])
                snapshot["created"]["groups"].append(
                    {"id": group.pk, "name": item["name"]}
                )
            for user in item["add"]:
                group.user_set.add(user)
                snapshot["created"]["memberships"].append(
                    {"group_id": group.pk, "user_id": user.pk}
                )
        return snapshot

    # ------------------------------------------------------------------ #
    # revert
    # ------------------------------------------------------------------ #

    def _revert(self, path, apply):
        try:
            with open(path, encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            raise CommandError(f"cannot read snapshot {path}: {exc}")
        if snapshot.get("version") != SNAPSHOT_VERSION:
            raise CommandError(
                f"snapshot {path} is not a version {SNAPSHOT_VERSION} snapshot"
            )
        created = snapshot["created"]
        since = datetime.fromisoformat(snapshot["created_on_et"]).date()
        self.say(
            f"ccdocs_attendance_setup --revert: {'APPLY' if apply else 'DRY RUN (nothing is written)'}"
        )

        with transaction.atomic():
            restored = 0
            for change in snapshot["work_info_changes"]:
                current = (
                    EmployeeWorkInformation.objects.entire()
                    .filter(pk=change["work_info_id"])
                    .values_list("shift_id", flat=True)
                    .first()
                )
                if current != change["new_shift_id"]:
                    self.say(
                        f"  employee {change['employee_id']}: shift is now {current}, not "
                        f"{change['new_shift_id']}; left alone"
                    )
                    continue
                self.say(
                    f"  employee {change['employee_id']}: shift {current} -> {change['previous_shift_id']}"
                )
                EmployeeWorkInformation.objects.entire().filter(
                    pk=change["work_info_id"]
                ).update(shift_id=change["previous_shift_id"])
                restored += 1

            # WorkRecords has no creation time (Horilla bulk_creates the daily
            # drafts), so "created after the snapshot" = dated on/after the
            # snapshot day AND carrying a shift this snapshot assigned.
            drafts = WorkRecords.objects.entire().filter(
                employee_id__in=[
                    c["employee_id"] for c in snapshot["work_info_changes"]
                ],
                shift_id__in={c["new_shift_id"] for c in snapshot["work_info_changes"]},
                work_record_type="DFT",
                is_attendance_record=False,
                is_leave_record=False,
                date__gte=since,
            )
            draft_count = drafts.count()
            self.say(
                f"  delete {draft_count} draft (DFT) work record(s) dated {since} or later"
            )
            drafts.delete()

            for membership in created["memberships"]:
                group = Group.objects.filter(pk=membership["group_id"]).first()
                if group is not None:
                    group.user_set.remove(membership["user_id"])
                    self.say(
                        f"  remove user {membership['user_id']} from {group.name!r}"
                    )
            for item in created["groups"]:
                deleted, _ = Group.objects.filter(pk=item["id"]).delete()
                if deleted:
                    self.say(f"  delete group {item['name']!r}")
            schedule_ids = [s["id"] for s in created["schedules"]]
            deleted, _ = (
                EmployeeShiftSchedule.objects.entire()
                .filter(pk__in=schedule_ids)
                .delete()
            )
            self.say(f"  delete {deleted} schedule(s)")
            for item in created["shifts"]:
                still_on = list(
                    EmployeeWorkInformation.objects.entire()
                    .filter(shift_id=item["id"])
                    .values_list("employee_id", flat=True)
                )
                if still_on:
                    raise CommandError(
                        f"shift {item['name']!r} is still set for employees {still_on}; "
                        "move them to another shift first. Nothing was written."
                    )
                try:
                    EmployeeShift.objects.entire().filter(pk=item["id"]).delete()
                except ProtectedError as exc:
                    raise CommandError(
                        f"shift {item['name']!r} is still used elsewhere ({exc}); nothing was written"
                    )
                self.say(f"  delete shift {item['name']!r}")

            if not apply:
                transaction.set_rollback(True)
                self.say(
                    f"DRY RUN: {restored} shift(s) would be restored. Re-run with --apply."
                )
                return
        self.say(f"Reverted: {restored} shift(s) restored.")
