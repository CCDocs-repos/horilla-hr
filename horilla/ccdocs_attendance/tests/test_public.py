"""The public late/out form: token, CSRF, honeypot, limits, rules, no sign-in."""

import hashlib
import os
from datetime import timedelta
from unittest import mock

from django.conf import settings
from django.contrib.auth.models import User
from django.test import Client
from django.urls import resolve

from horilla.ccdocs_attendance import common, views_public
from horilla.ccdocs_attendance.models import AttendanceNotice
from horilla.ccdocs_attendance.tests.base import LINK_TOKEN, AttendanceTestCase

FORM = f"/attendance-notice/{LINK_TOKEN}/"


class PublicFormTests(AttendanceTestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.make_employee(
            "Maria", "Test", "maria@example.com", position=27
        )
        self.today = common.today_et()

    def post(self, client=None, ip="198.51.100.7", **overrides):
        data = {
            "employee": str(self.agent.id),
            "kind": "late",
            "from_date": self.today.isoformat(),
            "to_date": "",
            "expected_arrival": "12:30",
            "reason": "Bus was late",
            "filed_by_name": "",
            "website": "",
        }
        data.update(overrides)
        data = {k: v for k, v in data.items() if v is not None}
        return (client or self.client).post(FORM, data, HTTP_CF_CONNECTING_IP=ip)

    # -- routing, token -------------------------------------------------- #

    def test_urls_resolve_to_this_app(self):
        self.assertEqual(resolve("/attendance-notice/ping/").func, views_public.ping)
        self.assertEqual(resolve(FORM).func.__name__, "notice_form")
        self.assertEqual(resolve(FORM + "thanks/").func.__name__, "notice_thanks")

    def test_ping_needs_no_token_and_returns_the_keyword(self):
        response = self.client.get("/attendance-notice/ping/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"attendance-notice-ok")

    def test_wrong_token_is_404(self):
        response = self.client.get("/attendance-notice/not-the-token/")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            self.post_to("/attendance-notice/not-the-token/").status_code, 404
        )
        self.assertEqual(AttendanceNotice.objects.count(), 0)

    def post_to(self, url):
        return self.client.post(url, {"employee": str(self.agent.id)})

    def test_form_is_503_when_the_link_token_is_not_set(self):
        with mock.patch.dict(os.environ, {"ATTENDANCE_NOTICE_LINK_TOKEN": ""}):
            self.assertEqual(self.client.get(FORM).status_code, 503)
            self.assertEqual(self.post().status_code, 503)
        self.assertEqual(AttendanceNotice.objects.count(), 0)

    # -- the list of names ------------------------------------------------ #

    def test_form_lists_only_active_floor_positions(self):
        self.make_employee("Lead", "Person", "lead@example.com", position=28)
        self.make_employee("Office", "Person", "office@example.com", position=90)
        self.make_employee(
            "Gone", "Person", "gone@example.com", position=27, active=False
        )
        response = self.client.get(FORM)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Maria T.")
        for hidden in ("Lead P.", "Office P.", "Gone P."):
            self.assertNotContains(response, hidden)

    def test_form_has_no_duplicate_labels(self):
        self.make_employee("Maria", "Tran", "maria.t@example.com", position=26)
        response = self.client.get(FORM)
        labels = [
            label for _, label in response.context["form"].fields["employee"].choices
        ][1:]
        self.assertEqual(len(labels), len(set(labels)))
        self.assertEqual(len(labels), 2)

    # -- saving ----------------------------------------------------------- #

    def test_late_notice_is_saved_and_redirects_to_thanks(self):
        response = self.post()
        self.assertRedirects(response, FORM + "thanks/", fetch_redirect_response=False)
        notice = AttendanceNotice.objects.get()
        self.assertEqual(notice.employee_id, self.agent.id)
        self.assertEqual(notice.kind, "late")
        self.assertEqual(notice.from_date, self.today)
        self.assertEqual(notice.to_date, self.today)
        self.assertEqual(notice.expected_arrival.strftime("%H:%M"), "12:30")
        self.assertTrue(notice.filed_by_self)
        self.assertEqual(notice.filed_by_name, "")
        self.assertEqual(notice.status, "requested")
        expected = hashlib.sha256(
            f"{settings.SECRET_KEY}:198.51.100.7".encode()
        ).hexdigest()
        self.assertEqual(notice.ip_hash, expected)
        thanks = self.client.get(FORM + "thanks/")
        self.assertContains(thanks, "We got it")

    def test_out_notice_for_several_days(self):
        start = self.today + timedelta(days=1)
        self.post(
            kind="out",
            from_date=start.isoformat(),
            to_date=(start + timedelta(days=13)).isoformat(),
            expected_arrival="",
        )
        notice = AttendanceNotice.objects.get()
        self.assertEqual(
            (notice.kind, notice.to_date), ("out", start + timedelta(days=13))
        )
        self.assertIsNone(notice.expected_arrival)

    def test_filing_for_someone_else_needs_your_name(self):
        response = self.post(filing_for_someone_else="on")
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Write your own name.")
        self.assertEqual(AttendanceNotice.objects.count(), 0)
        self.post(filing_for_someone_else="on", filed_by_name="Coworker Pat")
        notice = AttendanceNotice.objects.get()
        self.assertFalse(notice.filed_by_self)
        self.assertEqual(notice.filed_by_name, "Coworker Pat")

    def test_rules_are_enforced(self):
        cases = [
            ({"expected_arrival": ""}, "Tell us what time you will get here."),
            (
                {"from_date": (self.today - timedelta(days=1)).isoformat()},
                "Pick today or a day after today.",
            ),
            (
                {"from_date": (self.today + timedelta(days=15)).isoformat()},
                "Pick a day in the next 14 days.",
            ),
            (
                {
                    "kind": "out",
                    "to_date": (self.today + timedelta(days=14)).isoformat(),
                },
                "One form can cover at most 14 days.",
            ),
            (
                {"to_date": (self.today - timedelta(days=1)).isoformat()},
                "The last day cannot be before the first day.",
            ),
            ({"reason": "x" * 301}, "Keep the reason under 300 characters."),
            ({"reason": "   "}, "Write a short reason."),
            ({"employee": "999999"}, "Pick a name from the list."),
            ({"kind": "sick"}, "Pick late or out."),
        ]
        for overrides, message in cases:
            with self.subTest(overrides=overrides):
                response = self.post(**overrides)
                self.assertEqual(response.status_code, 200)
                self.assertContains(response, message)
        self.assertEqual(AttendanceNotice.objects.count(), 0)

    def test_an_inactive_person_cannot_be_picked(self):
        gone = self.make_employee("Gone", "Person", "gone@example.com", active=False)
        response = self.post(employee=str(gone.id))
        self.assertContains(response, "Pick a name from the list.")
        self.assertEqual(AttendanceNotice.objects.count(), 0)

    # -- abuse ------------------------------------------------------------ #

    def test_csrf_is_enforced(self):
        client = Client(enforce_csrf_checks=True)
        response = self.post(client=client)
        self.assertEqual(response.status_code, 403)
        self.assertEqual(AttendanceNotice.objects.count(), 0)
        page = client.get(FORM)
        token = page.cookies["csrftoken"].value
        response = self.post(client=client, csrfmiddlewaretoken=token)
        self.assertEqual(response.status_code, 302)
        self.assertEqual(AttendanceNotice.objects.count(), 1)

    def test_honeypot_saves_nothing_but_looks_like_success(self):
        response = self.post(website="http://spam.example")
        self.assertRedirects(response, FORM + "thanks/", fetch_redirect_response=False)
        self.assertEqual(AttendanceNotice.objects.count(), 0)

    def test_per_ip_limit_is_per_cf_connecting_ip(self):
        for _ in range(views_public.PER_IP_PER_HOUR):
            self.assertEqual(
                self.post(ip="203.0.113.1", website="bot").status_code, 302
            )
        blocked = self.post(ip="203.0.113.1")
        self.assertEqual(blocked.status_code, 429)
        self.assertEqual(AttendanceNotice.objects.count(), 0)
        # A different CF-Connecting-IP has its own bucket.
        self.assertEqual(self.post(ip="203.0.113.2").status_code, 302)
        self.assertEqual(AttendanceNotice.objects.count(), 1)

    def test_ip_falls_back_to_the_last_forwarded_hop_then_remote_addr(self):
        request = mock.Mock(
            META={
                "HTTP_X_FORWARDED_FOR": "10.0.0.1, 192.0.2.44",
                "REMOTE_ADDR": "127.0.0.1",
            }
        )
        self.assertEqual(views_public.client_ip(request), "192.0.2.44")
        request = mock.Mock(META={"REMOTE_ADDR": "127.0.0.1"})
        self.assertEqual(views_public.client_ip(request), "127.0.0.1")
        request = mock.Mock(
            META={
                "HTTP_CF_CONNECTING_IP": "192.0.2.9",
                "HTTP_X_FORWARDED_FOR": "192.0.2.44",
            }
        )
        self.assertEqual(views_public.client_ip(request), "192.0.2.9")

    def test_global_daily_ceiling(self):
        with mock.patch.object(views_public, "DAILY_CEILING", 2):
            self.assertEqual(self.post(ip="192.0.2.1").status_code, 302)
            self.assertEqual(self.post(ip="192.0.2.2").status_code, 302)
            response = self.post(ip="192.0.2.3")
        self.assertEqual(response.status_code, 429)
        self.assertEqual(AttendanceNotice.objects.count(), 2)

    def test_a_forged_identity_header_stays_anonymous(self):
        boss = User.objects.create_superuser(
            "boss", "boss@example.com", "not-used-pw-123"
        )
        headers = {
            "HTTP_X_AUTH_REQUEST_EMAIL": boss.email,
            "HTTP_X_AUTH_REQUEST_USER": "boss",
        }
        page = self.client.get(FORM, **headers)
        self.assertEqual(page.status_code, 200)
        self.assertTrue(page.wsgi_request.user.is_anonymous)
        response = self.client.post(
            FORM,
            {
                "employee": str(self.agent.id),
                "kind": "out",
                "from_date": self.today.isoformat(),
                "reason": "sick",
            },
            HTTP_CF_CONNECTING_IP="198.51.100.9",
            **headers,
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.wsgi_request.user.is_anonymous)
        self.assertNotIn("_auth_user_id", self.client.session)
        notice = AttendanceNotice.objects.get()
        self.assertTrue(notice.filed_by_self)
        self.assertEqual(notice.filed_by_name, "")

    def test_wrong_method_is_a_plain_405(self):
        self.assertEqual(self.client.put(FORM).status_code, 405)
        self.assertEqual(self.client.post("/attendance-notice/ping/").status_code, 405)

    def test_the_page_uses_no_static_files(self):
        response = self.client.get(FORM)
        self.assertNotContains(response, "/static/")
        self.assertContains(response, 'name="viewport"')
