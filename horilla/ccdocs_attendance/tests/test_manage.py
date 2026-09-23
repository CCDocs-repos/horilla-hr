"""Manager page: who can see it, who can change points (group, not superuser)."""

import json
from datetime import timedelta
from decimal import Decimal

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.test import Client
from django.urls import reverse
from django.utils import timezone

from horilla.ccdocs_attendance import common
from horilla.ccdocs_attendance.models import (
    AgentLink,
    AttendanceNotice,
    DayResult,
    Delivery,
    PointEntry,
)
from horilla.ccdocs_attendance.tests.base import AttendanceTestCase

MANAGE = "/ccdocs-attendance/manage/"


class ManagePageTests(AttendanceTestCase):
    def setUp(self):
        super().setUp()
        self.today = common.today_et()
        self.agent = self.make_employee("Ann", "Agent", "ann@example.com", position=27)
        self.point = PointEntry.objects.create(
            employee=self.agent,
            day=self.today,
            rule_key="late",
            points=Decimal("1"),
            idem_key=f"{self.today}:{self.agent.id}:late",
        )
        self.fixer = self.make_employee("Fay", "Fixer", "fay@example.test", position=90)
        self.viewer = self.make_employee(
            "Vic", "Viewer", "vic@example.test", position=90
        )
        self.boss = self.make_employee("Sue", "Super", "sue@example.test", position=90)
        self.nobody = self.make_employee(
            "Ned", "Nobody", "ned@example.test", position=90
        )
        self.add_to_group(self.fixer.employee_user_id, common.FIXERS_GROUP)
        self.add_to_group(self.viewer.employee_user_id, common.VIEWERS_GROUP)
        boss_user = self.boss.employee_user_id
        boss_user.is_superuser = True
        boss_user.is_staff = True
        boss_user.save()

    def login(self, employee):
        client = Client()
        client.force_login(employee.employee_user_id)
        session = client.session
        session["selected_company"] = "all"
        session.save()
        return client

    def void(self, client, reason="wrong login"):
        return client.post(
            f"{MANAGE}points/{self.point.id}/void/",
            {"reason": reason, "back_employee": ""},
        )

    def test_anonymous_is_sent_to_sign_in(self):
        response = Client().get(MANAGE)
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response["Location"])
        response = Client().post(
            f"{MANAGE}points/{self.point.id}/void/", {"reason": "x"}
        )
        self.assertEqual(response.status_code, 302)
        self.point.refresh_from_db()
        self.assertFalse(self.point.voided)

    def test_people_outside_both_groups_get_403_even_a_superuser(self):
        for employee in (self.nobody, self.boss):
            with self.subTest(user=employee.email):
                self.assertEqual(self.login(employee).get(MANAGE).status_code, 403)

    def test_viewer_sees_the_page_without_buttons_and_cannot_change_anything(self):
        client = self.login(self.viewer)
        page = client.get(MANAGE)
        self.assertEqual(page.status_code, 200)
        self.assertContains(page, "Ann A.")
        self.assertNotContains(page, "/void/")
        self.assertNotContains(page, "Excuse these dates")
        self.assertEqual(self.void(client).status_code, 403)
        notice = AttendanceNotice.objects.create(
            employee=self.agent,
            kind="out",
            from_date=self.today,
            to_date=self.today,
            reason="x",
            filed_at=timezone.now(),
        )
        self.assertEqual(
            client.post(f"{MANAGE}notices/{notice.id}/excuse/").status_code, 403
        )
        self.assertEqual(
            client.post(
                f"{MANAGE}excuse-dates/",
                {
                    "employee_id": self.agent.id,
                    "from_date": self.today.isoformat(),
                    "reason": "x",
                },
            ).status_code,
            403,
        )
        self.point.refresh_from_db()
        notice.refresh_from_db()
        self.assertFalse(self.point.voided)
        self.assertEqual(notice.status, "requested")

    def test_superuser_not_in_fixers_cannot_void(self):
        self.add_to_group(self.boss.employee_user_id, common.VIEWERS_GROUP)
        client = self.login(self.boss)
        self.assertEqual(client.get(MANAGE).status_code, 200)
        self.assertEqual(self.void(client).status_code, 403)
        self.point.refresh_from_db()
        self.assertFalse(self.point.voided)

    def test_fixer_can_void_with_a_reason(self):
        client = self.login(self.fixer)
        page = client.get(MANAGE)
        self.assertContains(page, f"points/{self.point.id}/void/")
        self.assertEqual(
            client.get(f"{MANAGE}points/{self.point.id}/void/").status_code, 405
        )
        self.void(client, reason="  ")
        self.point.refresh_from_db()
        self.assertFalse(self.point.voided)
        response = self.void(client)
        self.assertEqual(response.status_code, 302)
        self.point.refresh_from_db()
        self.assertTrue(self.point.voided)
        self.assertEqual(self.point.voided_by, "fay@example.test")
        self.assertEqual(self.point.void_reason, "wrong login")
        page = client.get(MANAGE)
        self.assertContains(page, "Voided by fay@example.test")

    def test_actions_need_csrf(self):
        client = Client(enforce_csrf_checks=True)
        client.force_login(self.fixer.employee_user_id)
        response = self.void(client)
        self.assertEqual(response.status_code, 403)
        self.point.refresh_from_db()
        self.assertFalse(self.point.voided)

    def test_mark_notice_excused_voids_its_points_and_days(self):
        notice = AttendanceNotice.objects.create(
            employee=self.agent,
            kind="out",
            from_date=self.today,
            to_date=self.today,
            reason="doctor",
            filed_at=timezone.now(),
        )
        DayResult.objects.create(employee=self.agent, day=self.today, status="late")
        client = self.login(self.fixer)
        client.post(f"{MANAGE}notices/{notice.id}/excuse/", {"note": "doctor's note"})
        notice.refresh_from_db()
        self.point.refresh_from_db()
        day = DayResult.objects.get()
        self.assertEqual(
            (notice.status, notice.status_changed_by), ("excused", "fay@example.test")
        )
        self.assertTrue(self.point.voided)
        self.assertEqual((day.status, day.excused_by), ("excused", "fay@example.test"))

    def test_excuse_dates_covers_past_and_future_days_and_the_engine_keeps_it(self):
        yesterday = self.today - timedelta(days=1)
        old = PointEntry.objects.create(
            employee=self.agent,
            day=yesterday,
            rule_key="out_no_notice",
            points=Decimal("2"),
            idem_key=f"{yesterday}:{self.agent.id}:out_no_notice",
        )
        outside = PointEntry.objects.create(
            employee=self.agent,
            day=self.today - timedelta(days=9),
            rule_key="late",
            points=Decimal("1"),
            idem_key=f"{self.today - timedelta(days=9)}:{self.agent.id}:late",
        )
        DayResult.objects.create(
            employee=self.agent, day=yesterday, status="out", closed=True
        )
        client = self.login(self.fixer)
        response = client.post(
            f"{MANAGE}excuse-dates/",
            {
                "employee_id": self.agent.id,
                "from_date": yesterday.isoformat(),
                "to_date": (self.today + timedelta(days=3)).isoformat(),
                "reason": "jury duty",
            },
        )
        self.assertEqual(response.status_code, 302)
        old.refresh_from_db()
        outside.refresh_from_db()
        self.point.refresh_from_db()
        self.assertTrue(old.voided)
        self.assertTrue(self.point.voided)
        self.assertFalse(outside.voided)
        self.assertEqual(DayResult.objects.get(day=yesterday).status, "excused")
        excuse = AttendanceNotice.objects.get(status="excused")
        self.assertEqual(
            (excuse.from_date, excuse.to_date),
            (yesterday, self.today + timedelta(days=3)),
        )

        # The engine re-posting that day changes nothing.
        from horilla.ccdocs_attendance.tests.base import API_TOKEN

        body = {
            "date": yesterday.isoformat(),
            "results": [
                {
                    "employee_id": self.agent.id,
                    "status": "out",
                    "scheduled_start": "12:00",
                    "first_login_at": None,
                    "minutes_late": None,
                    "notice_id": None,
                    "closed": True,
                    "points": [
                        {
                            "idem_key": f"{yesterday}:{self.agent.id}:out_no_notice",
                            "rule_key": "out_no_notice",
                            "points": 2,
                        }
                    ],
                }
            ],
        }
        api = Client().post(
            "/ccdocs-attendance/api/v1/day-results/",
            json.dumps(body),
            content_type="application/json",
            HTTP_AUTHORIZATION=f"Bearer {API_TOKEN}",
        )
        self.assertEqual(api.json()["skipped_excused"], 1)
        self.assertEqual(DayResult.objects.get(day=yesterday).status, "excused")
        old.refresh_from_db()
        self.assertTrue(old.voided)

    def test_excuse_dates_input_is_checked(self):
        client = self.login(self.fixer)
        for data in (
            {
                "employee_id": self.agent.id,
                "from_date": self.today.isoformat(),
                "reason": "",
            },
            {"employee_id": self.agent.id, "from_date": "nope", "reason": "x"},
            {"employee_id": 999999, "from_date": self.today.isoformat(), "reason": "x"},
            {
                "employee_id": self.agent.id,
                "from_date": self.today.isoformat(),
                "to_date": (self.today + timedelta(days=62)).isoformat(),
                "reason": "x",
            },
        ):
            with self.subTest(data=data):
                client.post(f"{MANAGE}excuse-dates/", data)
        self.point.refresh_from_db()
        self.assertFalse(self.point.voided)
        self.assertEqual(AttendanceNotice.objects.count(), 0)


class NeverDeletedTests(AttendanceTestCase):
    """Void or excuse only: no row here can be deleted, not even by a superuser."""

    def setUp(self):
        super().setUp()
        today = common.today_et()
        agent = self.make_employee("Ann", "Agent", "ann@example.com", position=27)
        notice = AttendanceNotice.objects.create(
            employee=agent,
            kind="out",
            from_date=today,
            to_date=today,
            filed_at=timezone.now(),
        )
        day = DayResult.objects.create(
            employee=agent, day=today, status="out", notice=notice
        )
        self.rows = [
            notice,
            day,
            PointEntry.objects.create(
                employee=agent,
                day=today,
                rule_key="out_no_notice",
                points=Decimal("2"),
                idem_key=f"{today}:{agent.id}:out_no_notice",
                day_result=day,
            ),
            AgentLink.objects.create(
                employee=agent, dialer_user="9002", valid_from=today
            ),
            Delivery.objects.create(key=f"out:{today}:{agent.id}", kind="out_email"),
        ]
        self.boss = self.make_employee("Sue", "Super", "sue@example.test", position=90)
        boss_user = self.boss.employee_user_id
        boss_user.is_superuser = True
        boss_user.is_staff = True
        boss_user.save()

    def assert_all_rows_still_there(self):
        for row in self.rows:
            self.assertTrue(
                type(row).objects.filter(pk=row.pk).exists(), type(row).__name__
            )

    def test_horilla_generic_delete_cannot_remove_a_row_even_for_a_superuser(self):
        client = Client()
        client.force_login(self.boss.employee_user_id)
        session = client.session
        session["selected_company"] = "all"
        session.save()
        for row in self.rows:
            with self.subTest(model=type(row).__name__):
                client.post(
                    f"{reverse('generic-delete')}"
                    f"?model=ccdocs_attendance.{type(row).__name__}&pk={row.pk}"
                )
        self.assert_all_rows_still_there()

    def test_delete_and_bulk_delete_are_refused(self):
        for row in self.rows:
            with self.subTest(model=type(row).__name__):
                with self.assertRaises(PermissionDenied):
                    row.delete()
                with self.assertRaises(PermissionDenied), transaction.atomic():
                    type(row).objects.filter(pk=row.pk).delete()
        self.assert_all_rows_still_there()
