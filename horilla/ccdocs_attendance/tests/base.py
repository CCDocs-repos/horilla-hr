"""
Shared test fixtures. Made-up names and emails only (this repo is public).

Every test runs with the Gmail backend patched to raise and ends by checking
mail.outbox is empty: this app must never send email.
"""

import os
from datetime import date, time
from unittest import mock

from django.contrib.auth.models import Group
from django.core import mail
from django.core.cache import cache
from django.db import connection
from django.test import TestCase

from base.models import (
    Company,
    Department,
    EmployeeShift,
    EmployeeShiftDay,
    EmployeeShiftSchedule,
    JobPosition,
)
from employee.models import Employee, EmployeeWorkInformation
from horilla.ccdocs_attendance import common
from horilla.horilla_middlewares import _thread_locals

API_TOKEN = "test-api-token-0123456789abcdef"
LINK_TOKEN = "test-link-token-fedcba9876543210"


def _refuse_to_send(*_args, **_kwargs):
    raise AssertionError("ccdocs_attendance tried to send an email")


class AttendanceTestCase(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE auth_user ADD COLUMN IF NOT EXISTS "
                "is_new_employee boolean NOT NULL DEFAULT false"
            )

    def setUp(self):
        super().setUp()
        cache.clear()
        env = mock.patch.dict(
            os.environ,
            {
                "HORILLA_ATTENDANCE_TOKEN": API_TOKEN,
                "ATTENDANCE_NOTICE_LINK_TOKEN": LINK_TOKEN,
                "ATTENDANCE_PUBLIC_BASE": "https://hr.example.test",
            },
        )
        env.start()
        self.addCleanup(env.stop)
        gmail = mock.patch(
            "horilla.gmail_dwd_backend.GmailDWDBackend.send_messages",
            side_effect=_refuse_to_send,
        )
        self.gmail_send = gmail.start()
        self.addCleanup(gmail.stop)

        self.company = Company.objects.create(
            company="Test Co",
            address="1 Test St",
            country="US",
            state="NY",
            city="NYC",
            zip="10001",
        )
        # Department/EmployeeShift.save() pass save kwargs to clean(), so
        # objects.create() (force_insert=True) fails on them: save() plainly.
        self.department = Department(department="Floor")
        self.department.save()
        self.positions = {}
        for pos_id, title in (
            (26, "Agent"),
            (27, "Senior Agent"),
            (28, "Team Lead"),
            (90, "Office"),
        ):
            self.positions[pos_id] = JobPosition.objects.create(
                id=pos_id, job_position=title, department_id=self.department
            )
        for name in common.WEEKDAY_NAMES:
            EmployeeShiftDay.objects.get_or_create(day=name)

    def tearDown(self):
        self.assertEqual(mail.outbox, [], "an email reached the outbox")
        self.gmail_send.assert_not_called()
        if hasattr(_thread_locals, "request"):
            del _thread_locals.request
        super().tearDown()

    # ------------------------------------------------------------------ #

    def make_employee(
        self,
        first,
        last,
        email,
        position=27,
        active=True,
        joined=date(2026, 6, 2),
        shift=None,
        work_email=None,
    ):
        emp = Employee(
            employee_first_name=first,
            employee_last_name=last,
            email=email,
            phone="5550100",
        )
        emp.save()
        EmployeeWorkInformation.objects.entire().filter(employee_id=emp).update(
            company_id=self.company,
            job_position_id=self.positions.get(position),
            date_joining=joined,
            shift_id=shift,
            email=work_email,
        )
        if not active:
            Employee.objects.entire().filter(pk=emp.pk).update(is_active=False)
        user = emp.employee_user_id
        user.is_new_employee = False
        user.save()
        return (
            Employee.objects.entire()
            .select_related("employee_work_info")
            .get(pk=emp.pk)
        )

    def make_shift(
        self, name, start=time(12, 0), end=time(20, 0), days=common.WEEKDAY_NAMES[:5]
    ):
        shift = EmployeeShift(employee_shift=name)
        shift.save()
        for day_name in days:
            EmployeeShiftSchedule.objects.create(
                shift_id=shift,
                day=EmployeeShiftDay.objects.get(day=day_name),
                start_time=start,
                end_time=end,
            )
        return shift

    def add_to_group(self, user, name):
        group, _ = Group.objects.get_or_create(name=name)
        group.user_set.add(user)
        return group

    def api_headers(self, token=API_TOKEN):
        return {
            "HTTP_AUTHORIZATION": f"Bearer {token}",
            "HTTP_X_FORWARDED_PROTO": "https",
        }
