"""Engine API: auth, the day read, idempotent writes, links, shifts, deliveries."""

import json
import os
from datetime import date, datetime, time, timedelta
from datetime import timezone as dt_timezone
from decimal import Decimal
from unittest import mock

from django.utils import timezone

from employee.models import EmployeeWorkInformation
from horilla.ccdocs_attendance import common
from horilla.ccdocs_attendance.models import (
    AgentLink,
    AttendanceNotice,
    DayResult,
    Delivery,
    PointEntry,
)
from horilla.ccdocs_attendance.tests.base import (
    API_TOKEN,
    LINK_TOKEN,
    AttendanceTestCase,
)

API = "/ccdocs-attendance/api/v1/"
FRIDAY = date(2026, 9, 25)


class ApiTestCase(AttendanceTestCase):
    def get(self, path, **params):
        return self.client.get(API + path, params, **self.api_headers())

    def post_json(self, path, body, token=API_TOKEN):
        return self.client.post(
            API + path,
            json.dumps(body),
            content_type="application/json",
            **self.api_headers(token),
        )

    def notice(
        self, employee, first, last=None, kind="out", filed_at=None, status="requested"
    ):
        return AttendanceNotice.objects.create(
            employee=employee,
            kind=kind,
            from_date=first,
            to_date=last or first,
            expected_arrival=time(12, 30) if kind == "late" else None,
            reason="test",
            filed_at=filed_at or timezone.now(),
            status=status,
        )


class AuthTests(ApiTestCase):
    def test_503_when_the_token_is_not_configured(self):
        with mock.patch.dict(os.environ, {"HORILLA_ATTENDANCE_TOKEN": ""}):
            response = self.get("day/", date="2026-09-25")
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json(), {"error": "not_configured"})

    def test_401_without_or_with_a_wrong_token(self):
        response = self.client.get(API + "day/", {"date": "2026-09-25"})
        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json(), {"error": "unauthorized"})
        response = self.client.get(
            API + "day/", {"date": "2026-09-25"}, **self.api_headers("wrong-token")
        )
        self.assertEqual(response.status_code, 401)
        response = self.post_json(
            "deliveries/claim/", {"key": "k", "kind": "late_email"}, token="nope"
        )
        self.assertEqual(response.status_code, 401)
        self.assertEqual(Delivery.objects.count(), 0)

    def test_200_with_the_token(self):
        response = self.get("day/", date="2026-09-25")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["date"], "2026-09-25")

    def test_the_session_and_identity_headers_do_not_count(self):
        from django.contrib.auth.models import User

        boss = User.objects.create_superuser(
            "boss", "boss@example.com", "not-used-pw-123"
        )
        self.client.force_login(boss)
        response = self.client.get(
            API + "day/", {"date": "2026-09-25"}, HTTP_X_AUTH_REQUEST_EMAIL=boss.email
        )
        self.assertEqual(response.status_code, 401)

    def test_wrong_method_and_bad_input_are_json_errors(self):
        self.assertEqual(self.post_json("day/", {}).status_code, 405)
        response = self.get("day/", date="25/09/2026")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "bad_request")
        response = self.client.post(
            API + "day-results/",
            "not json",
            content_type="application/json",
            **self.api_headers(),
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["error"], "bad_json")


class DayReadTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.floor_shift = self.make_shift(common.FLOOR_SHIFT_NAME)
        self.agent = self.make_employee(
            "Quinn",
            "Del Rey",
            "quinn@example.com",
            position=27,
            shift=self.floor_shift,
            work_email="quinn.work@example.com",
        )
        self.lead = self.make_employee("Lee", "Lead", "lee@example.com", position=28)
        self.office = self.make_employee(
            "Olga", "Office", "olga@example.com", position=90
        )
        self.linked_office = self.make_employee(
            "Lin", "Linked", "lin@example.com", position=90
        )
        self.gone = self.make_employee(
            "Gus", "Gone", "gus@example.com", position=27, active=False
        )
        AgentLink.objects.create(
            employee=self.agent, dialer_user="9001", valid_from=date(2026, 6, 2)
        )
        AgentLink.objects.create(
            employee=self.agent,
            dialer_user="1111",
            valid_from=date(2026, 1, 1),
            valid_to=date(2026, 5, 31),
        )
        AgentLink.objects.create(
            employee=self.linked_office, dialer_user="2001", valid_from=date(2026, 9, 1)
        )

    def test_roster_shift_links_and_urls(self):
        body = self.get(
            "day/", date=FRIDAY.isoformat(), include=str(self.office.id)
        ).json()
        self.assertEqual(
            body["form_url"], f"https://hr.example.test/attendance-notice/{LINK_TOKEN}/"
        )
        self.assertEqual(
            body["manage_url"], "https://hr.example.test/ccdocs-attendance/manage/"
        )
        by_id = {e["id"]: e for e in body["employees"]}
        self.assertEqual(
            set(by_id),
            {self.agent.id, self.lead.id, self.office.id, self.linked_office.id},
        )
        agent = by_id[self.agent.id]
        self.assertEqual(agent["label"], "Quinn D.")
        self.assertEqual(agent["email"], "quinn.work@example.com")
        self.assertEqual(agent["position_id"], 27)
        self.assertEqual(agent["date_joining"], "2026-06-02")
        self.assertEqual(
            agent["shift"],
            {
                "id": self.floor_shift.id,
                "name": "Floor 12-8 ET",
                "start": "12:00",
                "end": "20:00",
            },
        )
        self.assertEqual([link["dialer_user"] for link in agent["links"]], ["9001"])
        self.assertIsNone(by_id[self.lead.id]["shift"])
        self.assertEqual(by_id[self.lead.id]["email"], "lee@example.com")

    def test_no_shift_on_a_weekend(self):
        body = self.get("day/", date="2026-09-26").json()
        agent = next(e for e in body["employees"] if e["id"] == self.agent.id)
        self.assertIsNone(agent["shift"])

    def test_past_link_is_returned_for_a_past_date(self):
        body = self.get("day/", date="2026-05-15").json()
        agent = next(e for e in body["employees"] if e["id"] == self.agent.id)
        self.assertEqual([link["dialer_user"] for link in agent["links"]], ["1111"])

    def test_unknown_include_is_404(self):
        self.assertEqual(
            self.get("day/", date="2026-09-25", include="999999").status_code, 404
        )

    def test_form_url_is_null_when_the_link_token_is_not_set(self):
        with mock.patch.dict(os.environ, {"ATTENDANCE_NOTICE_LINK_TOKEN": ""}):
            self.assertIsNone(self.get("day/", date="2026-09-25").json()["form_url"])

    def test_notices_results_history_and_points(self):
        covering = self.notice(
            self.agent, FRIDAY, FRIDAY + timedelta(days=3), kind="out"
        )
        self.notice(self.agent, FRIDAY, status="void")
        self.notice(self.agent, FRIDAY + timedelta(days=5))
        DayResult.objects.create(
            employee=self.agent,
            day=FRIDAY,
            status="late",
            scheduled_start=time(12, 0),
            first_login_at=datetime(2026, 9, 25, 16, 7, 31, tzinfo=dt_timezone.utc),
            minutes_late=7,
        )
        thursday = FRIDAY - timedelta(days=1)
        early = common.at_et(thursday, time(10, 59, 59))
        self.notice(self.agent, thursday, filed_at=early)
        DayResult.objects.create(
            employee=self.agent,
            day=thursday,
            status="out",
            scheduled_start=time(12, 0),
            closed=True,
        )
        wednesday = FRIDAY - timedelta(days=2)
        DayResult.objects.create(
            employee=self.agent,
            day=wednesday,
            status="out",
            scheduled_start=time(12, 0),
            closed=True,
        )
        PointEntry.objects.create(
            employee=self.agent,
            day=wednesday,
            rule_key="out_no_notice",
            points=Decimal("2"),
            idem_key=f"{wednesday}:{self.agent.id}:out_no_notice",
        )
        PointEntry.objects.create(
            employee=self.agent,
            day=thursday,
            rule_key="out_notice",
            points=Decimal("1"),
            idem_key=f"{thursday}:{self.agent.id}:out_notice",
            voided_at=timezone.now(),
            voided_by="someone",
        )
        PointEntry.objects.create(
            employee=self.agent,
            day=date(2026, 5, 29),
            rule_key="late",
            points=Decimal("1"),
            idem_key=f"2026-05-29:{self.agent.id}:late",
        )

        body = self.get("day/", date=FRIDAY.isoformat()).json()
        self.assertEqual([n["id"] for n in body["notices"]], [covering.id])
        self.assertEqual(body["notices"][0]["to_date"], "2026-09-28")
        self.assertEqual(
            body["day_results"],
            [
                {
                    "employee_id": self.agent.id,
                    "day": "2026-09-25",
                    "status": "late",
                    "scheduled_start": "12:00",
                    "first_login_at": "2026-09-25T12:07:31-04:00",
                    "minutes_late": 7,
                    "notice_id": None,
                    "closed": False,
                }
            ],
        )
        history = {h["day"]: h for h in body["history"]}
        self.assertEqual(set(history), {"2026-09-23", "2026-09-24"})
        self.assertTrue(history["2026-09-24"]["called_in"])
        self.assertTrue(history["2026-09-24"]["any_notice"])
        self.assertEqual(history["2026-09-24"]["points"], 0.0)
        self.assertFalse(history["2026-09-23"]["called_in"])
        self.assertFalse(history["2026-09-23"]["any_notice"])
        self.assertEqual(history["2026-09-23"]["points"], 2.0)
        points = body["points"][str(self.agent.id)]
        self.assertEqual(
            (points["window_start"], points["window_end"]), ("2026-06-01", "2026-12-31")
        )
        self.assertEqual(points["total"], 2.0)
        self.assertEqual(len(points["entries"]), 2)
        self.assertEqual(
            {e["idem_key"]: e["voided"] for e in points["entries"]},
            {
                f"{wednesday}:{self.agent.id}:out_no_notice": False,
                f"{thursday}:{self.agent.id}:out_notice": True,
            },
        )
        self.assertEqual(body["points"][str(self.lead.id)]["total"], 0.0)

    def test_called_in_uses_new_york_time_after_dst_ends(self):
        # 2026-11-02 is EST (UTC-5). Start 12:00 -> must file by 11:00 EST = 16:00 UTC.
        day = date(2026, 11, 2)
        for employee, filed in (
            (self.agent, datetime(2026, 11, 2, 16, 0, 0, tzinfo=dt_timezone.utc)),
            (self.lead, datetime(2026, 11, 2, 16, 0, 1, tzinfo=dt_timezone.utc)),
        ):
            self.notice(employee, day, filed_at=filed)
            DayResult.objects.create(
                employee=employee, day=day, status="out", scheduled_start=time(12, 0)
            )
        body = self.get("day/", date="2026-11-03").json()
        history = {h["employee_id"]: h for h in body["history"]}
        self.assertTrue(history[self.agent.id]["called_in"])
        self.assertFalse(history[self.lead.id]["called_in"])
        self.assertEqual(body["notices"], [])


class DayResultsWriteTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.agent = self.make_employee("Ann", "Agent", "ann@example.com")
        self.key = f"{FRIDAY}:{self.agent.id}:late"

    def body(self, status="late", points=1.0, **extra):
        result = {
            "employee_id": self.agent.id,
            "status": status,
            "scheduled_start": "12:00",
            "first_login_at": "2026-09-25T12:07:31-04:00",
            "minutes_late": 7,
            "notice_id": None,
            "closed": False,
            "detail": {"source": "dialer", "login": "9001"},
            "points": [{"idem_key": self.key, "rule_key": "late", "points": points}]
            if points is not None
            else [],
        }
        result.update(extra)
        return {
            "date": FRIDAY.isoformat(),
            "run_id": "watch-2026-09-25T12:15",
            "results": [result],
        }

    def bad_rule_body(self):
        body = self.body()
        body["results"][0]["points"] = [
            {
                "idem_key": f"{FRIDAY}:{self.agent.id}:tardy",
                "rule_key": "tardy",
                "points": 1,
            }
        ]
        return body

    def test_first_post_creates_and_the_repeat_is_a_no_op(self):
        first = self.post_json("day-results/", self.body())
        self.assertEqual(first.status_code, 200)
        self.assertEqual(
            first.json(),
            {
                "upserted": 1,
                "points_created": 1,
                "points_skipped_voided": 0,
                "skipped_excused": 0,
                "conflicts": [],
            },
        )
        second = self.post_json("day-results/", self.body())
        self.assertEqual(second.json()["points_created"], 0)
        self.assertEqual(second.json()["conflicts"], [])
        self.assertEqual(DayResult.objects.count(), 1)
        self.assertEqual(PointEntry.objects.count(), 1)
        result = DayResult.objects.get()
        self.assertEqual(result.minutes_late, 7)
        self.assertEqual(
            common.iso_et(result.first_login_at), "2026-09-25T12:07:31-04:00"
        )
        entry = PointEntry.objects.get()
        self.assertEqual(
            (entry.points, entry.source, entry.day_result_id),
            (Decimal("1.00"), "engine", result.id),
        )

    def test_a_voided_point_is_never_revived(self):
        self.post_json("day-results/", self.body())
        PointEntry.objects.update(
            voided_at=timezone.now(),
            voided_by="sarthak@example.com",
            void_reason="wrong",
        )
        response = self.post_json("day-results/", self.body())
        self.assertEqual(response.json()["points_skipped_voided"], 1)
        entry = PointEntry.objects.get()
        self.assertTrue(entry.voided)
        self.assertEqual(entry.voided_by, "sarthak@example.com")

    def test_a_changed_value_is_a_conflict_and_nothing_changes(self):
        self.post_json("day-results/", self.body())
        response = self.post_json("day-results/", self.body(points=2.0))
        self.assertEqual(
            response.json()["conflicts"],
            [{"idem_key": self.key, "existing": 1.0, "incoming": 2.0}],
        )
        self.assertEqual(PointEntry.objects.get().points, Decimal("1.00"))

    def test_a_day_a_person_excused_is_never_overwritten(self):
        DayResult.objects.create(
            employee=self.agent,
            day=FRIDAY,
            status="excused",
            excused_by="fixer@example.com",
        )
        response = self.post_json("day-results/", self.body())
        self.assertEqual(response.json()["skipped_excused"], 1)
        self.assertEqual(DayResult.objects.get().status, "excused")
        self.assertEqual(PointEntry.objects.count(), 0)

    def test_the_whole_post_is_one_transaction(self):
        good = self.body()["results"][0]
        other = self.make_employee("Bob", "Agent", "bob@example.com")
        bad = dict(
            good,
            employee_id=other.id,
            points=[{"idem_key": "wrong", "rule_key": "late", "points": 1}],
        )
        response = self.post_json(
            "day-results/", {"date": FRIDAY.isoformat(), "results": [good, bad]}
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(DayResult.objects.count(), 0)
        self.assertEqual(PointEntry.objects.count(), 0)

    def test_input_is_checked(self):
        cases = [
            (self.body(status="missing"), 400),
            (self.body(first_login_at="2026-09-25T12:07:31"), 400),
            (self.body(employee_id=999999, points=None), 404),
            (self.bad_rule_body(), 400),
            (self.body(notice_id=999999), 404),
            ({"date": "2026-09-25", "results": "nope"}, 400),
        ]
        for body, status in cases:
            with self.subTest(body=body):
                self.assertEqual(
                    self.post_json("day-results/", body).status_code, status
                )
        self.assertEqual(DayResult.objects.count(), 0)

    def test_first_login_keeps_the_est_offset_after_dst(self):
        body = self.body(first_login_at="2026-11-02T12:07:31-05:00")
        body["date"] = "2026-11-02"
        body["results"][0]["points"] = [
            {
                "idem_key": f"2026-11-02:{self.agent.id}:late",
                "rule_key": "late",
                "points": 1,
            }
        ]
        self.assertEqual(self.post_json("day-results/", body).status_code, 200)
        read = self.get("day/", date="2026-11-02", include=str(self.agent.id)).json()
        self.assertEqual(
            read["day_results"][0]["first_login_at"], "2026-11-02T12:07:31-05:00"
        )

    def test_withdraw_voids_as_engine_and_lists_missing(self):
        self.post_json("day-results/", self.body())
        response = self.post_json(
            "points/withdraw/",
            {
                "idem_keys": [self.key, "2026-09-25:1:late"],
                "reason": "outage hold 2026-09-25",
                "run_id": "r1",
            },
        )
        self.assertEqual(
            response.json(), {"voided": 1, "missing": ["2026-09-25:1:late"]}
        )
        entry = PointEntry.objects.get()
        self.assertEqual(
            (entry.voided_by, entry.void_reason), ("engine", "outage hold 2026-09-25")
        )
        again = self.post_json(
            "points/withdraw/", {"idem_keys": [self.key], "reason": "again"}
        )
        self.assertEqual(again.json(), {"voided": 0, "missing": []})
        self.assertEqual(PointEntry.objects.get().void_reason, "outage hold 2026-09-25")
        self.assertEqual(
            self.post_json("points/withdraw/", {"idem_keys": [self.key]}).status_code,
            400,
        )


class LinksTests(ApiTestCase):
    def setUp(self):
        super().setUp()
        self.ann = self.make_employee("Ann", "Agent", "ann@example.com")
        self.bob = self.make_employee("Bob", "Agent", "bob@example.com")

    def open(self, employee, login, valid_from="2026-09-01"):
        return {
            "employee_id": employee.id,
            "dialer_user": login,
            "webwork_email": None,
            "valid_from": valid_from,
        }

    def test_one_open_holder_per_login(self):
        first = self.post_json("links/", {"open": [self.open(self.ann, "9001")]}).json()
        self.assertEqual(len(first["opened"]), 1)
        again = self.post_json("links/", {"open": [self.open(self.ann, "9001")]}).json()
        self.assertEqual(again, {"opened": [], "closed": [], "conflicts": []})
        clash = self.post_json("links/", {"open": [self.open(self.bob, "9001")]}).json()
        self.assertEqual(
            clash["conflicts"], [{"dialer_user": "9001", "held_by": self.ann.id}]
        )
        self.assertEqual(AgentLink.objects.count(), 1)

    def test_close_then_reopen_for_someone_else_in_one_call(self):
        link_id = self.post_json(
            "links/", {"open": [self.open(self.ann, "9001")]}
        ).json()["opened"][0]
        response = self.post_json(
            "links/",
            {
                "close": [
                    {
                        "link_id": link_id,
                        "valid_to": "2026-09-24",
                        "reason": "dialer name changed",
                    }
                ],
                "open": [self.open(self.bob, "9001", valid_from="2026-09-25")],
            },
        ).json()
        self.assertEqual(response["closed"], [link_id])
        self.assertEqual(len(response["opened"]), 1)
        closed = AgentLink.objects.get(pk=link_id)
        self.assertEqual(
            (closed.valid_to, closed.close_reason),
            (date(2026, 9, 24), "dialer name changed"),
        )

    def test_bad_links_are_refused(self):
        self.assertEqual(
            self.post_json(
                "links/",
                {"open": [{"employee_id": self.ann.id, "valid_from": "2026-09-01"}]},
            ).status_code,
            400,
        )
        self.assertEqual(
            self.post_json(
                "links/", {"close": [{"link_id": 999999, "valid_to": "2026-09-24"}]}
            ).status_code,
            404,
        )


class ShiftsDefaultTests(ApiTestCase):
    def test_only_floor_people_with_no_shift_get_the_default(self):
        self.assertEqual(
            self.post_json("shifts/default/", {"employee_ids": []}).status_code, 409
        )
        floor_shift = self.make_shift(common.FLOOR_SHIFT_NAME)
        other_shift = self.make_shift("Day Shift", start=time(9), end=time(18))
        no_shift = self.make_employee("Nia", "None", "nia@example.com", position=26)
        has_shift = self.make_employee(
            "Hal", "Has", "hal@example.com", position=27, shift=other_shift
        )
        lead = self.make_employee("Lou", "Lead", "lou@example.com", position=28)
        gone = self.make_employee(
            "Gil", "Gone", "gil@example.com", position=27, active=False
        )
        response = self.post_json(
            "shifts/default/",
            {"employee_ids": [no_shift.id, has_shift.id, lead.id, gone.id, 999999]},
        ).json()
        self.assertEqual(response["assigned"], [no_shift.id])
        self.assertEqual(
            {s["id"]: s["reason"] for s in response["skipped"]},
            {
                has_shift.id: "has_shift",
                lead.id: "not_floor_position",
                gone.id: "inactive",
                999999: "not_found",
            },
        )
        shifts = dict(
            EmployeeWorkInformation.objects.entire().values_list(
                "employee_id", "shift_id"
            )
        )
        self.assertEqual(shifts[no_shift.id], floor_shift.id)
        self.assertEqual(shifts[has_shift.id], other_shift.id)
        self.assertIsNone(shifts[lead.id])


class DeliveriesTests(ApiTestCase):
    def test_claim_once_then_409(self):
        claim = {
            "key": "late:2026-09-25:900",
            "kind": "late_email",
            "detail": {"to": "agent"},
        }
        first = self.post_json("deliveries/claim/", claim)
        self.assertEqual(first.status_code, 201)
        self.assertEqual(
            first.json(), {"key": "late:2026-09-25:900", "status": "attempting"}
        )
        second = self.post_json("deliveries/claim/", claim)
        self.assertEqual(second.status_code, 409)
        self.assertEqual(second.json()["status"], "attempting")
        self.assertIn("updated_at", second.json())
        self.assertEqual(Delivery.objects.count(), 1)

    def test_done_and_stale_list(self):
        self.post_json("deliveries/claim/", {"key": "a", "kind": "late_email"})
        self.post_json("deliveries/claim/", {"key": "b", "kind": "slack_notin"})
        Delivery.objects.filter(key="a").update(
            updated_at=timezone.now() - timedelta(minutes=30)
        )
        stale = self.get(
            "deliveries/", status="attempting", older_than_minutes="15"
        ).json()
        self.assertEqual([d["key"] for d in stale["deliveries"]], ["a"])
        done = self.post_json(
            "deliveries/done/",
            {"key": "a", "status": "sent", "detail": {"message_id": "m1"}},
        )
        self.assertEqual(done.status_code, 200)
        self.assertEqual(Delivery.objects.get(key="a").status, "sent")
        self.assertEqual(Delivery.objects.get(key="a").detail, {"message_id": "m1"})
        self.assertEqual(
            self.post_json(
                "deliveries/done/", {"key": "a", "status": "sent"}
            ).status_code,
            200,
        )
        self.assertEqual(
            self.post_json(
                "deliveries/done/", {"key": "a", "status": "failed"}
            ).status_code,
            409,
        )
        self.assertEqual(
            self.post_json(
                "deliveries/done/", {"key": "zzz", "status": "sent"}
            ).status_code,
            404,
        )
        self.assertEqual(
            self.post_json(
                "deliveries/claim/", {"key": "c", "kind": "fax"}
            ).status_code,
            400,
        )
