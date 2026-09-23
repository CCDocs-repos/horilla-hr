"""manage.py ccdocs_attendance_setup: dry run, apply, apply twice, revert."""

import json
import os
import shutil
import tempfile
from datetime import time, timedelta
from io import StringIO

from django.contrib.auth.models import Group
from django.core.management import CommandError, call_command

from attendance.models import WorkRecords
from base.models import EmployeeShift, EmployeeShiftSchedule
from employee.models import EmployeeWorkInformation
from horilla.ccdocs_attendance import common
from horilla.ccdocs_attendance.tests.base import AttendanceTestCase
from payroll.models.models import Contract


class SetupCommandTests(AttendanceTestCase):
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.mkdtemp(prefix="att-setup-")
        self.addCleanup(shutil.rmtree, self.tmp)
        self.wrong_shift = self.make_shift("Day Shift (9-6)", start=time(9), end=time(18))
        self.a = self.make_employee("Ava", "Floor", "ava@example.com", position=27)
        self.b = self.make_employee("Ben", "Floor", "ben@example.com", position=26)
        self.early = self.make_employee("Eli", "Early", "eli@example.com", position=27)
        self.wrong = self.make_employee(
            "Wes", "Wrong", "wes@example.com", position=27, shift=self.wrong_shift
        )
        self.lead = self.make_employee("Lia", "Lead", "lia@example.com", position=28)
        self.gone = self.make_employee("Gil", "Gone", "gil@example.com", position=27, active=False)
        self.fixer = self.make_employee("Fay", "Fixer", "fay@example.test", position=90)
        self.viewer = self.make_employee(
            "Vic", "Viewer", "vic.personal@example.test", position=90, work_email="vic@example.test"
        )
        self.viewer.employee_user_id.email = ""
        self.viewer.employee_user_id.save()

    def run_setup(self, *extra, apply=False, snapshot=None):
        out = StringIO()
        args = [
            "ccdocs_attendance_setup",
            f"--company-id={self.company.id}",
            f"--late-shift-ids={self.early.id}",
            f"--reassign-to-floor={self.wrong.id}",
            "--fixers=fay@example.test",
            "--viewers=vic@example.test",
            *extra,
        ]
        if apply:
            args += ["--apply", f"--snapshot={snapshot}"]
        call_command(*args, stdout=out)
        return out.getvalue()

    def shifts(self):
        return dict(EmployeeWorkInformation.objects.entire().values_list("employee_id", "shift_id"))

    def state(self):
        return (
            self.shifts(),
            EmployeeShift.objects.count(),
            EmployeeShiftSchedule.objects.count(),
            sorted(Group.objects.values_list("name", flat=True)),
            sorted(Group.objects.filter(user__isnull=False).values_list("name", "user__id")),
        )

    def test_dry_run_writes_nothing_and_says_what_it_would_do(self):
        before = self.state()
        out = self.run_setup()
        self.assertEqual(self.state(), before)
        self.assertIn("DRY RUN", out)
        self.assertIn("create shift 'Floor 12-8 ET'", out)
        self.assertIn("create shift 'Floor 11-8 ET'", out)
        self.assertIn(f"employee {self.early.id}: shift none -> 'Floor 11-8 ET'", out)
        self.assertIn(f"employee {self.wrong.id}: shift {self.wrong_shift.id} -> 'Floor 12-8 ET'", out)
        self.assertIn("20 change(s) would be made", out)

    def test_apply_sets_everything_up_and_a_second_apply_changes_nothing(self):
        contracts = Contract.objects.entire().count()
        snapshot = os.path.join(self.tmp, "setup-1.json")
        out = self.run_setup(apply=True, snapshot=snapshot)
        self.assertIn("20 change(s) written", out)

        floor = EmployeeShift.objects.get(employee_shift=common.FLOOR_SHIFT_NAME)
        late = EmployeeShift.objects.get(employee_shift=common.LATE_SHIFT_NAME)
        self.assertEqual(list(floor.company_id.all()), [self.company])
        schedules = {
            (s.shift_id_id, s.day.day): (s.start_time, s.end_time)
            for s in EmployeeShiftSchedule.objects.select_related("day")
        }
        for day_name in ("monday", "tuesday", "wednesday", "thursday", "friday"):
            self.assertEqual(schedules[(floor.id, day_name)], (time(12), time(20)))
            self.assertEqual(schedules[(late.id, day_name)], (time(11), time(20)))
        self.assertNotIn((floor.id, "saturday"), schedules)

        shifts = self.shifts()
        self.assertEqual(shifts[self.a.id], floor.id)
        self.assertEqual(shifts[self.b.id], floor.id)
        self.assertEqual(shifts[self.early.id], late.id)
        self.assertEqual(shifts[self.wrong.id], floor.id)
        self.assertIsNone(shifts[self.lead.id])
        self.assertIsNone(shifts[self.gone.id])
        self.assertIsNone(shifts[self.fixer.id])
        self.assertTrue(
            Group.objects.get(name=common.FIXERS_GROUP).user_set.filter(pk=self.fixer.employee_user_id.pk).exists()
        )
        self.assertTrue(
            Group.objects.get(name=common.VIEWERS_GROUP).user_set.filter(pk=self.viewer.employee_user_id.pk).exists()
        )
        self.assertEqual(Contract.objects.entire().count(), contracts, "setup must not create payroll contracts")

        with open(snapshot, encoding="utf-8") as handle:
            data = json.load(handle)
        self.assertEqual(len(data["created"]["shifts"]), 2)
        self.assertEqual(len(data["created"]["schedules"]), 10)
        self.assertEqual(len(data["work_info_changes"]), 4)
        change = next(c for c in data["work_info_changes"] if c["employee_id"] == self.wrong.id)
        self.assertEqual(change["previous_shift_id"], self.wrong_shift.id)

        before = self.state()
        out = self.run_setup(apply=True, snapshot=os.path.join(self.tmp, "setup-2.json"))
        self.assertIn("0 change(s) written", out)
        self.assertEqual(self.state(), before)

    def test_apply_needs_a_new_snapshot_path(self):
        with self.assertRaises(CommandError):
            call_command("ccdocs_attendance_setup", "--apply", stdout=StringIO())
        existing = os.path.join(self.tmp, "exists.json")
        open(existing, "w").close()
        with self.assertRaises(CommandError):
            self.run_setup(apply=True, snapshot=existing)
        self.assertEqual(EmployeeShift.objects.count(), 1)

    def test_a_bad_id_or_unknown_email_stops_the_apply(self):
        snapshot = os.path.join(self.tmp, "bad.json")
        with self.assertRaises(CommandError):
            self.run_setup(f"--late-shift-ids={self.lead.id}", apply=True, snapshot=snapshot)
        with self.assertRaises(CommandError):
            self.run_setup("--fixers=nobody@example.test", apply=True, snapshot=snapshot)
        self.assertFalse(os.path.exists(snapshot))
        self.assertEqual(EmployeeShift.objects.count(), 1)

    def test_revert_restores_and_removes_what_it_made(self):
        before_shifts = self.shifts()
        snapshot = os.path.join(self.tmp, "setup.json")
        self.run_setup(apply=True, snapshot=snapshot)
        floor = EmployeeShift.objects.get(employee_shift=common.FLOOR_SHIFT_NAME)
        today = common.today_et()
        # What Horilla's scheduler writes once schedules exist (drafts, no timestamps).
        made_after = WorkRecords.objects.create(
            employee_id=self.a, date=today, work_record_type="DFT", shift_id=floor, note=""
        )
        older = WorkRecords.objects.create(
            employee_id=self.a, date=today - timedelta(days=3), work_record_type="DFT", shift_id=floor, note=""
        )
        unrelated = WorkRecords.objects.create(
            employee_id=self.lead, date=today, work_record_type="DFT", note=""
        )

        dry = StringIO()
        call_command("ccdocs_attendance_setup", f"--revert={snapshot}", stdout=dry)
        self.assertIn("DRY RUN", dry.getvalue())
        self.assertEqual(EmployeeShift.objects.count(), 3)
        self.assertTrue(WorkRecords.objects.filter(pk=made_after.pk).exists())

        out = StringIO()
        call_command("ccdocs_attendance_setup", f"--revert={snapshot}", "--apply", stdout=out)
        self.assertIn("Reverted: 4 shift(s) restored.", out.getvalue())
        self.assertEqual(self.shifts(), before_shifts)
        self.assertEqual(list(EmployeeShift.objects.values_list("id", flat=True)), [self.wrong_shift.id])
        self.assertEqual(EmployeeShiftSchedule.objects.filter(shift_id=self.wrong_shift).count(), 5)
        self.assertEqual(EmployeeShiftSchedule.objects.count(), 5)
        self.assertFalse(Group.objects.filter(name__in=[common.FIXERS_GROUP, common.VIEWERS_GROUP]).exists())
        self.assertFalse(WorkRecords.objects.filter(pk=made_after.pk).exists())
        self.assertTrue(WorkRecords.objects.filter(pk=unrelated.pk).exists())
        self.assertTrue(WorkRecords.objects.filter(pk=older.pk).exists(), "dated before the snapshot: kept")

    def test_revert_leaves_a_later_human_change_alone(self):
        snapshot = os.path.join(self.tmp, "setup.json")
        self.run_setup(apply=True, snapshot=snapshot)
        late = EmployeeShift.objects.get(employee_shift=common.LATE_SHIFT_NAME)
        EmployeeWorkInformation.objects.entire().filter(employee_id=self.a).update(shift_id=late)
        with self.assertRaises(CommandError):
            # Ava is on a shift this snapshot created but no longer the one it set.
            call_command("ccdocs_attendance_setup", f"--revert={snapshot}", "--apply", stdout=StringIO())
        self.assertEqual(self.shifts()[self.a.id], late.id)
