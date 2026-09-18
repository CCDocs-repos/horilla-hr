"""
Tests for searching a candidate by email address or phone number.

Recruiters used to be able to search the candidate list by name only: an email
address returned nothing, and a phone number returned nothing unless it was
typed with exactly the punctuation it happened to be stored with.
"""

from unittest.mock import patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase

from base.models import Company, Department, JobPosition
from recruitment.filters import CandidateFilter
from recruitment.models import Candidate, Recruitment, Stage


class CandidateSearchTests(TestCase):
    """The one search box has to accept a name, an email or a phone number."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Candidate creation fires the AI screening signal; keep it off the wire.
        cls._thread_patch = patch("threading.Thread")
        cls._thread_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._thread_patch.stop()
        super().tearDownClass()

    def setUp(self):
        self.company = Company.objects.create(company="Acme")
        # Bypass Department.save() bug (passes save-kwargs to clean) by using
        # bulk_create, which skips save() entirely.
        Department.objects.bulk_create([Department(department="Ops")])
        self.department = Department.objects.get(department="Ops")
        self.job_position = JobPosition.objects.create(
            job_position="Agent", department_id=self.department
        )
        self.recruitment = Recruitment.objects.create(
            title="Call Center Agent",
            description="Handle inbound calls.",
            company_id=self.company,
            job_position_id=self.job_position,
            vacancy=5,
        )
        self.stage = Stage.objects.filter(recruitment_id=self.recruitment).first()
        # The four ways a real phone number turns up in this column.
        self.spaced = self.make_candidate("Ann Spaced", "ann@example.com", "876 742-0692")
        self.plussed = self.make_candidate("Bob Plussed", "bob@example.com", "+1876 562 1861")
        self.bare = self.make_candidate("Cara Bare", "cara@example.com", "4436856925")
        self.foreign = self.make_candidate("Dan Foreign", "dan@example.com", "+543234450093")

    def make_candidate(self, name, email, mobile):
        """Create one candidate on the shared recruitment."""
        return Candidate.objects.create(
            name=name,
            email=email,
            mobile=mobile,
            resume=SimpleUploadedFile(
                "resume.pdf", b"%PDF-1.4 placeholder", content_type="application/pdf"
            ),
            recruitment_id=self.recruitment,
            job_position_id=self.job_position,
            stage_id=self.stage,
        )

    def search(self, term, field="name"):
        """Run the candidate search box and return the candidates it found."""
        return list(
            CandidateFilter({field: term}, queryset=Candidate.objects.all()).qs
        )

    # -- name still works ----------------------------------------------------

    def test_name_still_matches(self):
        self.assertEqual(self.search("Ann"), [self.spaced])

    def test_partial_name_still_matches(self):
        self.assertEqual(self.search("Bare"), [self.bare])

    # -- email ---------------------------------------------------------------

    def test_full_email_matches(self):
        self.assertEqual(self.search("cara@example.com"), [self.bare])

    def test_partial_email_matches(self):
        self.assertEqual(self.search("bob@"), [self.plussed])

    def test_email_match_is_case_insensitive(self):
        self.assertEqual(self.search("ANN@EXAMPLE.COM"), [self.spaced])

    # -- phone number --------------------------------------------------------

    def test_digits_match_a_number_stored_with_spaces_and_a_dash(self):
        self.assertEqual(self.search("8767420692"), [self.spaced])

    def test_digits_match_a_number_stored_with_a_country_code(self):
        self.assertEqual(self.search("8765621861"), [self.plussed])

    def test_number_pasted_with_its_punctuation_matches(self):
        self.assertEqual(self.search("+1 (876) 562-1861"), [self.plussed])

    def test_number_pasted_with_a_country_code_matches_a_bare_stored_number(self):
        self.assertEqual(self.search("+1 443-685-6925"), [self.bare])

    def test_international_number_matches(self):
        self.assertEqual(self.search("543234450093"), [self.foreign])

    def test_partial_number_matches(self):
        self.assertEqual(self.search("4436856"), [self.bare])

    def test_unknown_number_matches_nobody(self):
        self.assertEqual(self.search("9999999999"), [])

    def test_short_digit_string_does_not_match_every_candidate(self):
        # "86" appears inside several stored numbers; two digits must not turn
        # the search box into a list-everything button.
        self.assertEqual(self.search("86"), [])

    # -- the advanced filter panel's own Email and Mobile boxes --------------

    def test_advanced_email_filter_accepts_a_partial_value(self):
        self.assertEqual(self.search("dan@", field="email"), [self.foreign])

    def test_advanced_mobile_filter_ignores_punctuation(self):
        self.assertEqual(self.search("8767420692", field="mobile"), [self.spaced])

    def test_advanced_mobile_filter_matches_nobody_without_digits(self):
        self.assertEqual(self.search("not a number", field="mobile"), [])

    # -- the pipeline search box uses a different filter ---------------------

    def test_pipeline_search_matches_email(self):
        self.assertEqual(self.search("cara@example.com", field="candidate_name"), [self.bare])

    def test_pipeline_search_matches_phone_number(self):
        self.assertEqual(self.search("8765621861", field="candidate_name"), [self.plussed])

    def test_pipeline_search_still_matches_name(self):
        self.assertEqual(self.search("Dan", field="candidate_name"), [self.foreign])
