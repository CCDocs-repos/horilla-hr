"""
Tests for the Bank Information card on an employee's profile.

People are paid either through Wise or into a bank account, and the card used to
show every field for everyone: a Wise person's card read "Bank Name: WISE (see
additional_info)" with Branch, Bank Address, Account Number, both Bank Codes and
Country all "None". The card now shows the Wise details for Wise people, the
bank details for everyone else, and skips whatever is blank.
"""

from django.db import connection
from django.test import TestCase
from django.urls import reverse

from base.models import Company
from employee.models import Employee, EmployeeBankDetails, EmployeeWorkInformation
from horilla.horilla_middlewares import _thread_locals


class BankInformationCardTests(TestCase):
    """Wise people see Wise fields; bank people see bank fields."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Horilla adds is_new_employee to auth.User via add_to_class without a
        # migration, so a fresh test database cannot create any user until the
        # column exists.
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
        self.viewer = self.make_employee("Vera", "Viewer", "vera@example.com")
        user = self.viewer.employee_user_id
        user.is_staff = True
        user.is_superuser = True
        # New users are sent to change-password before any other page.
        user.is_new_employee = False
        user.save()
        self.client.force_login(user)
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

    def card(self, employee):
        """The profile's About tab, the way the page asks for it."""
        response = self.client.get(
            reverse("about-tab", args=[employee.id]), HTTP_HX_REQUEST="true"
        )
        self.assertEqual(response.status_code, 200)
        return response.content.decode()

    # -- paid through Wise ---------------------------------------------------

    def test_wise_person_shows_only_wise_details(self):
        emp = self.make_employee("Andy", "Wise", "andy@example.com")
        EmployeeBankDetails.objects.create(
            employee_id=emp,
            bank_name="WISE (see additional_info)",
            additional_info={
                "payout_rail": "wise",
                "wise_email": "andy.wise@example.com",
                "wise_account_name": "Andy Wise Valverde",
                "wise_currency": "COP",
            },
        )
        page = self.card(emp)
        self.assertIn("andy.wise@example.com", page)
        self.assertIn("Andy Wise Valverde", page)
        self.assertIn("COP", page)
        for hidden in ("Account Number", "Bank Code", "Branch", "Bank Address"):
            self.assertNotIn(hidden, page)
        self.assertNotIn("see additional_info", page)

    def test_wise_person_keeps_the_fields_that_exist(self):
        """Some Wise people have only an email and a currency saved."""
        emp = self.make_employee("Fel", "Partial", "fel@example.com")
        EmployeeBankDetails.objects.create(
            employee_id=emp,
            bank_name="WISE (see additional_info)",
            additional_info={
                "payout_rail": "wise",
                "wise_email": "fel@example.com",
                "currency": "USD",
            },
        )
        page = self.card(emp)
        self.assertIn("fel@example.com", page)
        self.assertIn("USD", page)
        self.assertNotIn("Account Name", page)

    def test_a_wise_issued_bank_account_keeps_its_bank_fields(self):
        """A bank named "Wise" is NOT an email payout: Wise issues real accounts
        (Divyansh Kumar holds one). Only payout_rail decides, or we would hide the
        account number, SWIFT and routing of someone who must be wired money."""
        emp = self.make_employee("Div", "WiseBank", "div@example.com")
        EmployeeBankDetails.objects.create(
            employee_id=emp,
            bank_name="Wise",
            account_number="206545725380306",
            any_other_code1="TRWIBEB1XXX",
            additional_info={"payout_rail": "bank", "currency": "USD"},
        )
        page = self.card(emp)
        self.assertIn("206545725380306", page)
        self.assertIn("TRWIBEB1XXX", page)
        self.assertNotIn("Wise Email", page)

    def test_the_template_has_no_multiline_hash_comment(self):
        """A {# #} comment is single-line only; a multi-line one renders its 2nd
        line onward as visible text on the profile page, which is exactly what
        happened on 2026-09-17."""
        from pathlib import Path as _Path

        tpl = _Path(__file__).parent / "templates" / "tabs" / "personal_tab.html"
        for n, line in enumerate(tpl.read_text().splitlines(), 1):
            if "{#" in line:
                self.assertIn("#}", line, f"{tpl.name}:{n}: {{# comment never closes on its line")

    # -- paid into a bank account -------------------------------------------

    def test_bank_person_shows_bank_details_and_hides_blanks(self):
        emp = self.make_employee("Bo", "Banker", "bo@example.com")
        EmployeeBankDetails.objects.create(
            employee_id=emp,
            bank_name="Lead Bank",
            account_number="210861060851",
            address="1801 Main Street, Kansas City, MO 64108",
            country="United States",
            any_other_code1="101019644",
            branch="",
            any_other_code2="",
            additional_info={},
        )
        page = self.card(emp)
        self.assertIn("Lead Bank", page)
        self.assertIn("210861060851", page)
        self.assertIn("United States", page)
        self.assertIn("101019644", page)
        self.assertNotIn("Wise Email", page)
        self.assertNotIn("Branch", page)
        self.assertNotIn("Bank Code #2", page)
        self.assertIn("Bank Code #1", page)
        self.assertEqual(page.count("<span>Currency</span>"), 0)

    def test_bank_person_with_a_saved_currency_sees_it(self):
        """The onboarding form saves a currency for bank people too."""
        emp = self.make_employee("Mia", "Peso", "mia@example.com")
        EmployeeBankDetails.objects.create(
            employee_id=emp,
            bank_name="BBVA",
            account_number="012345678901234567",
            additional_info={"payout_rail": "bank", "currency": "MXN"},
        )
        page = self.card(emp)
        self.assertIn("BBVA", page)
        # exactly one Currency row, and it sits inside the bank card
        self.assertEqual(page.count("<span>Currency</span>"), 1)
        self.assertGreater(page.index("<span>Currency</span>"), page.index("Bank Information"))
        self.assertIn("MXN", page)
        self.assertNotIn("Wise Email", page)
