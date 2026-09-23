"""Labels, reset windows and New York time (settings.TIME_ZONE is unset in tests)."""

from datetime import date, datetime, time, timezone as dt_timezone

from django.conf import settings

from horilla.ccdocs_attendance import common
from horilla.ccdocs_attendance.tests.base import AttendanceTestCase


class CommonTests(AttendanceTestCase):
    def test_tests_really_run_without_new_york_as_the_django_time_zone(self):
        # The point of these tests: nothing may lean on settings.TIME_ZONE.
        self.assertNotEqual(settings.TIME_ZONE, "America/New_York")

    def test_new_york_offsets_follow_daylight_saving(self):
        self.assertEqual(
            common.at_et(date(2026, 10, 30), time(12, 0)).isoformat(), "2026-10-30T12:00:00-04:00"
        )
        # DST ends 2026-11-01; the next Monday is EST.
        self.assertEqual(
            common.at_et(date(2026, 11, 2), time(12, 0)).isoformat(), "2026-11-02T12:00:00-05:00"
        )
        utc = datetime(2026, 11, 2, 17, 7, 31, tzinfo=dt_timezone.utc)
        self.assertEqual(common.iso_et(utc), "2026-11-02T12:07:31-05:00")

    def test_reset_windows_are_jan_1_and_jun_1(self):
        self.assertEqual(common.reset_window(date(2026, 5, 31)), (date(2026, 1, 1), date(2026, 5, 31)))
        self.assertEqual(common.reset_window(date(2026, 6, 1)), (date(2026, 6, 1), date(2026, 12, 31)))
        self.assertEqual(common.reset_window(date(2026, 12, 31)), (date(2026, 6, 1), date(2026, 12, 31)))

    def test_label_is_first_name_and_first_letter_of_first_last_name_word(self):
        emp = self.make_employee("Quinn", "Del Rey", "quinn@example.com")
        self.assertEqual(common.labels_for([emp])[emp.id], "Quinn D.")

    def test_same_label_gets_the_join_month_then_the_join_day(self):
        a = self.make_employee("Leo", "Alpha", "leo.a@example.com", joined=date(2026, 9, 2))
        b = self.make_employee("Leo", "Adams", "leo.b@example.com", joined=date(2026, 10, 5))
        c = self.make_employee("Leo", "Avery", "leo.c@example.com", joined=date(2026, 10, 9))
        d = self.make_employee("Leo", "Arden", "leo.d@example.com", joined=date(2026, 10, 9))
        labels = common.labels_for([a, b, c, d])
        self.assertEqual(labels[a.id], "Leo A. (joined Sep)")
        self.assertEqual(labels[b.id], "Leo A. (joined Oct 5)")
        self.assertEqual(labels[c.id], "Leo A. (joined Oct 9) #1")
        self.assertEqual(labels[d.id], "Leo A. (joined Oct 9) #2")
        self.assertEqual(len(set(labels.values())), 4)

    def test_labels_are_unique_across_the_whole_floor_even_for_a_subset(self):
        a = self.make_employee("Ana", "Bell", "ana.b@example.com", position=27, joined=date(2026, 7, 1))
        self.make_employee("Ana", "Brown", "ana.c@example.com", position=28, joined=date(2026, 8, 1))
        self.assertEqual(common.labels_for([a])[a.id], "Ana B. (joined Jul)")
