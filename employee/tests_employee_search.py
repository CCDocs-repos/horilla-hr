"""
Tests for the Employees page search: who shows up, the active/inactive marker
beside each name, and the "Search <Field> for:" dropdown.

Before this change a typed search only ever looked at active people, so an
ex-employee could not be found without first setting "Is Active? = No" (which
then hid everyone active). Seven of the eight dropdown choices matched nobody,
because they looked up short names like `department` that do not exist on an
employee, and every choice broke on a capital letter.
"""

import re
from pathlib import Path

from django.core.exceptions import FieldDoesNotExist
from django.db import connection
from django.template.loader import render_to_string
from django.test import TestCase
from django.urls import reverse

from base.models import Company, Department, JobPosition
from employee.filters import EmployeeFilter
from employee.models import Employee, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals


class EmployeeSearchTests(TestCase):
    """The Employees search box, its marker, and its field dropdown."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Horilla adds is_new_employee to auth.User via add_to_class without a
        # migration (production got the column by hand), so a fresh test
        # database cannot create any user until the column exists.
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE auth_user ADD COLUMN IF NOT EXISTS "
                "is_new_employee boolean NOT NULL DEFAULT false"
            )

    def tearDown(self):
        # Horilla's ThreadLocalMiddleware keeps the last request around; clear
        # it so the next test's Employee.save() does not see a stale request.
        if hasattr(_thread_locals, "request"):
            del _thread_locals.request

    def setUp(self):
        self.company = Company.objects.create(company="Acme")
        # Bypass Department.save() bug (passes save-kwargs to clean) by using
        # bulk_create, which skips save() entirely.
        Department.objects.bulk_create([Department(department="Sales")])
        self.sales = Department.objects.get(department="Sales")
        self.position = JobPosition.objects.create(
            job_position="Closer", department_id=self.sales
        )

        self.boss = self.make_employee("Jason", "Boss", "boss@example.com")
        self.ann = self.make_employee("Ann", "Active", "ann@example.com")
        self.bob = self.make_employee("Bob", "Gone", "bob@example.com")
        for emp in (self.ann, self.bob):
            EmployeeWorkInformation.objects.filter(employee_id=emp).update(
                department_id=self.sales,
                job_position_id=self.position,
                reporting_manager_id=self.boss,
            )
        # update(), not save(): Employee.save() flips an archived employee back
        # to active when it runs inside a request.
        Employee.objects.filter(pk=self.bob.pk).update(is_active=False)
        self.bob.refresh_from_db()

        user = self.boss.employee_user_id
        user.is_staff = True
        user.is_superuser = True
        # New users are sent to change-password before any other page.
        user.is_new_employee = False
        user.save()
        self.client.force_login(user)
        # A browser session always has a company choice by the time the search
        # box is used; without one the view shows only company-less people.
        session = self.client.session
        session["selected_company"] = "all"
        session.save()

    def make_employee(self, first, last, email):
        """Create one employee in the shared company."""
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

    def search(self, **params):
        """Call the view the search box calls, the way htmx calls it."""
        params.setdefault("view", "list")
        response = self.client.get(
            reverse("employee-filter-view"), params, HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    def names(self, **params):
        """The people shown as result rows, by name -- not every name on the
        page (a row also shows its reporting manager's name)."""
        page = self.search(**params)
        return {
            name.strip()
            for name in re.findall(r'oh-profile__name oh-text--dark">([^<]+)', page)
        }

    # -- who shows up -------------------------------------------------------

    def test_nothing_typed_lists_active_people_only(self):
        people = self.names()
        self.assertIn("Ann Active", people)
        self.assertNotIn("Bob Gone", people)

    def test_typed_search_also_finds_inactive_people(self):
        self.assertIn("Bob Gone", self.names(search="bob"))

    def test_typed_search_still_narrows_to_the_match(self):
        people = self.names(search="ann")
        self.assertIn("Ann Active", people)
        self.assertNotIn("Bob Gone", people)

    def test_is_active_yes_hides_inactive_people_while_searching(self):
        people = self.names(search="example", is_active="True")
        self.assertIn("Ann Active", people)
        self.assertNotIn("Bob Gone", people)

    def test_is_active_no_shows_only_inactive_people_while_searching(self):
        people = self.names(search="example", is_active="False")
        self.assertIn("Bob Gone", people)
        self.assertNotIn("Ann Active", people)

    # -- the marker beside each name ---------------------------------------

    def test_marker_is_green_for_active_and_red_inactive_for_inactive(self):
        active = render_to_string(
            "employee_personal_info/active_marker.html", {"emp": self.ann}
        )
        inactive = render_to_string(
            "employee_personal_info/active_marker.html", {"emp": self.bob}
        )
        self.assertIn("oh-dot--success", active)
        self.assertNotIn("Inactive", active)
        self.assertIn("oh-dot--danger", inactive)
        self.assertIn("Inactive", inactive)

    def test_marker_shows_in_list_card_and_group_by_views(self):
        for extra in ({"view": "list"}, {"view": "card"}, {"field": "employee_work_info__department_id"}):
            with self.subTest(**extra):
                page = self.search(search="example", **extra)
                self.assertIn("oh-dot--success", page)
                self.assertIn("oh-dot--danger", page)

    # -- the "Search <Field> for:" dropdown --------------------------------

    def test_every_dropdown_choice_is_a_real_employee_field(self):
        nav = Path(__file__).parent / "templates" / "employee_nav.html"
        choices = re.findall(r"\[name=search_field\]'\)\.val\('([a-z_]+)'\)", nav.read_text())
        self.assertEqual(len(choices), 8)
        for choice in choices:
            with self.subTest(choice=choice):
                model = Employee
                for part in choice.split("__"):
                    try:
                        field = model._meta.get_field(part)
                    except FieldDoesNotExist:
                        self.fail(f"dropdown choice {choice!r}: no field {part!r}")
                    model = field.related_model or model

    def dropdown(self, search, field):
        """Run the dropdown search on its own, as the filter applies it."""
        data = {"search": search, "search_field": field}
        # filter(), like the view: Employee.objects.all() silently hides
        # inactive people outside a request.
        return set(EmployeeFilter(data, queryset=Employee.objects.filter()).qs)

    def test_dropdown_department_finds_people_and_ignores_capitals(self):
        self.assertEqual(
            self.dropdown("SALES", "employee_work_info__department_id"),
            {self.ann, self.bob},
        )

    def test_dropdown_reporting_manager_finds_their_team(self):
        self.assertEqual(
            self.dropdown("Boss", "employee_work_info__reporting_manager_id"),
            {self.ann, self.bob},
        )

    def test_dropdown_email_ignores_capitals(self):
        self.assertEqual(self.dropdown("ANN@Example", "email"), {self.ann})

    def test_dropdown_search_through_the_view(self):
        people = self.names(
            search="Sales", search_field="employee_work_info__department_id"
        )
        self.assertIn("Ann Active", people)
        self.assertIn("Bob Gone", people)
        self.assertNotIn("Jason Boss", people)
