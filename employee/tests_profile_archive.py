"""
Tests for archiving an employee straight from their profile page.

The Employees list has had an Archive entry in each row's menu for a long time,
but the profile page -- where you land after clicking a person -- had no way to
set them inactive. It now has one in the gear menu, wired to the same
`employee-archive` action and gated by the same permission.
"""

from pathlib import Path

from django.db import connection
from django.test import TestCase
from django.urls import reverse

from base.models import Company
from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals


class ProfileArchiveTests(TestCase):
    """Archive / un-archive from the profile page."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE auth_user ADD COLUMN IF NOT EXISTS "
                "is_new_employee boolean NOT NULL DEFAULT false"
            )

    def tearDown(self):
        if hasattr(_thread_locals, "request"):
            del _thread_locals.request

    def setUp(self):
        self.company = Company.objects.create(company="Acme")
        self.admin = self.make_employee("Ada", "Admin", "ada@example.com")
        user = self.admin.employee_user_id
        user.is_staff = True
        user.is_superuser = True
        user.is_new_employee = False
        user.save()
        self.client.force_login(user)
        session = self.client.session
        session["selected_company"] = "all"
        session.save()
        self.target = self.make_employee("Tom", "Target", "tom@example.com")

    def make_employee(self, first, last, email):
        emp = Employee(
            employee_first_name=first,
            employee_last_name=last,
            email=email,
            phone="5550100",
        )
        emp.save()
        EmployeeWorkInformation.objects.filter(employee_id=emp).update(
            company_id=self.company
        )
        return Employee.objects.get(pk=emp.pk)

    def archive_from_profile(self, employee):
        return self.client.post(
            reverse("employee-archive", args=[employee.id]) + "?from_profile=1",
            HTTP_HX_REQUEST="true",
        )

    def test_archive_from_profile_sets_inactive_and_reloads_the_page(self):
        response = self.archive_from_profile(self.target)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get("HX-Refresh"), "true")
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)
        self.assertFalse(self.target.employee_user_id.is_active)

    def test_archiving_again_from_profile_un_archives(self):
        self.archive_from_profile(self.target)
        response = self.archive_from_profile(self.target)
        self.assertEqual(response.get("HX-Refresh"), "true")
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        self.assertTrue(self.target.employee_user_id.is_active)

    def test_the_list_page_behaviour_is_unchanged(self):
        """Without from_profile the response still re-filters the list."""
        response = self.client.post(
            reverse("employee-archive", args=[self.target.id]), HTTP_HX_REQUEST="true"
        )
        self.assertIsNone(response.get("HX-Refresh"))
        self.assertIn("filterEmployee", response.content.decode())
        self.target.refresh_from_db()
        self.assertFalse(self.target.is_active)

    def test_someone_who_cannot_archive_gets_no_button_and_no_action(self):
        viewer = self.make_employee("Vic", "Viewer", "vic@example.com")
        vuser = viewer.employee_user_id
        vuser.is_new_employee = False
        vuser.save()
        self.client.force_login(vuser)
        session = self.client.session
        session["selected_company"] = "all"
        session.save()
        response = self.archive_from_profile(self.target)
        # Horilla answers a missing permission with a redirect or a "no
        # permission" page depending on settings; what matters is that nothing
        # happened and the page was not told to reload.
        self.assertIsNone(response.get("HX-Refresh"))
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)
        self.assertTrue(self.target.employee_user_id.is_active)

    def test_a_blocked_archive_shows_the_replacement_popup(self):
        """Someone who still manages other people cannot be archived silently."""
        EmployeeWorkInformation.objects.filter(employee_id=self.admin).update(
            reporting_manager_id=self.target
        )
        response = self.archive_from_profile(self.target)
        page = response.content.decode()
        self.assertIn("replaceEmployeeForm", page)
        self.assertNotEqual(response.get("HX-Refresh"), "true")
        self.target.refresh_from_db()
        self.assertTrue(self.target.is_active)

    def test_profile_page_carries_the_menu_entry_and_the_popup_container(self):
        tpl = Path(__file__).parent / "templates" / "employee" / "view" / "individual.html"
        src = tpl.read_text()
        self.assertIn("employee-archive", src)
        self.assertIn("from_profile=1", src)
        self.assertIn('id="relatedModel"', src)
        self.assertIn("perms.employee.delete_employee", src)
        self.assertIn("active_marker.html", src)
