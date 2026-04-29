import io
import os
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase

from horilla import infisical_boot

from recruitment.ai_screening import cv_text

_SimpleTestCase = SimpleTestCase


class InfisicalBootTests(SimpleTestCase):
    def test_load_secrets_hydrates_env_from_sdk(self):
        fake_secret = MagicMock(secretKey="DEEPSEEK_API_KEY", secretValue="sk-test-123")
        fake_response = MagicMock(secrets=[fake_secret])

        fake_client = MagicMock()
        fake_client.secrets.list_secrets.return_value = fake_response

        env = {
            "INFISICAL_SITE_URL": "https://secrets.example.com",
            "INFISICAL_CLIENT_ID": "id",
            "INFISICAL_CLIENT_SECRET": "secret",
            "INFISICAL_PROJECT_ID": "proj",
            "INFISICAL_ENVIRONMENT": "prod",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(infisical_boot, "InfisicalSDKClient", return_value=fake_client):
            os.environ.pop("DEEPSEEK_API_KEY", None)
            infisical_boot.load_secrets()
            self.assertEqual(os.environ.get("DEEPSEEK_API_KEY"), "sk-test-123")

    def test_load_secrets_swallows_errors(self):
        env = {
            "INFISICAL_SITE_URL": "https://secrets.example.com",
            "INFISICAL_CLIENT_ID": "id",
            "INFISICAL_CLIENT_SECRET": "secret",
            "INFISICAL_PROJECT_ID": "proj",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(infisical_boot, "InfisicalSDKClient", side_effect=RuntimeError("boom")) as ctor:
            # Must not raise — Horilla must boot even if Infisical throws.
            infisical_boot.load_secrets()
            # Confirm we actually exercised the error path.
            ctor.assert_called_once()

    def test_load_secrets_does_not_overwrite_existing_env(self):
        fake_secret = MagicMock(secretKey="DEEPSEEK_API_KEY", secretValue="sk-from-infisical")
        fake_response = MagicMock(secrets=[fake_secret])
        fake_client = MagicMock()
        fake_client.secrets.list_secrets.return_value = fake_response

        env = {
            "INFISICAL_SITE_URL": "https://secrets.example.com",
            "INFISICAL_CLIENT_ID": "id",
            "INFISICAL_CLIENT_SECRET": "secret",
            "INFISICAL_PROJECT_ID": "proj",
            "DEEPSEEK_API_KEY": "sk-already-set",
        }
        with patch.dict(os.environ, env, clear=False), \
             patch.object(infisical_boot, "InfisicalSDKClient", return_value=fake_client):
            infisical_boot.load_secrets()
            self.assertEqual(os.environ["DEEPSEEK_API_KEY"], "sk-already-set")


class CvTextTests(_SimpleTestCase):
    def test_unknown_extension_returns_placeholder(self):
        f = SimpleUploadedFile("resume.xyz", b"nothing", content_type="application/octet-stream")
        text = cv_text.extract_cv_text(f)
        self.assertIn("Unsupported", text)

    def test_corrupt_pdf_returns_placeholder(self):
        f = SimpleUploadedFile("resume.pdf", b"not a real pdf", content_type="application/pdf")
        text = cv_text.extract_cv_text(f)
        self.assertIn("could not be parsed", text)

    def test_pdf_happy_path_uses_pdfplumber(self):
        f = SimpleUploadedFile("resume.pdf", b"%PDF-1.4 fake", content_type="application/pdf")
        fake_page = type("P", (), {"extract_text": lambda self: "Jane Doe\nPython, Django"})()
        fake_pdf = type("PDF", (), {
            "pages": [fake_page],
            "__enter__": lambda self: self,
            "__exit__": lambda self, *a: False,
        })()
        with patch.object(cv_text, "pdfplumber") as plumber:
            plumber.open.return_value = fake_pdf
            text = cv_text.extract_cv_text(f)
        self.assertIn("Jane Doe", text)
        self.assertIn("Django", text)

    def test_docx_happy_path(self):
        f = SimpleUploadedFile("resume.docx", b"fake docx", content_type="application/msword")
        fake_para = type("P", (), {"text": "Experienced SDR"})()
        fake_doc = type("D", (), {"paragraphs": [fake_para]})()
        with patch.object(cv_text, "docx") as dcx:
            dcx.Document.return_value = fake_doc
            text = cv_text.extract_cv_text(f)
        self.assertIn("Experienced SDR", text)

    def test_truncates_to_8000_chars(self):
        big = "a" * 12000
        result = cv_text._truncate(big)
        self.assertEqual(len(result), 8000)


from recruitment.ai_screening import prompt as prompt_mod


class BuildPromptTests(_SimpleTestCase):
    def _make_candidate(self, **overrides):
        c = MagicMock()
        c.name = overrides.get("name", "Jane Doe")
        c.email = overrides.get("email", "jane@example.com")
        c.portfolio = overrides.get("portfolio", "")
        c.schedule_date = overrides.get("schedule_date", None)
        return c

    def _make_recruitment(self, title="Call Center Agent", description="Handle inbound calls.", skills=("English", "Sales")):
        r = MagicMock()
        r.title = title
        r.description = description
        skills_qs = MagicMock()
        skills_qs.values_list.return_value = list(skills)
        r.skills = MagicMock()
        r.skills.all.return_value = [MagicMock(**{"__str__.return_value": s}) for s in skills]
        return r

    def test_prompt_includes_core_sections(self):
        c = self._make_candidate()
        r = self._make_recruitment()
        text = prompt_mod.build_prompt(c, "Five years of SaaS sales.", r)
        self.assertIn("Call Center Agent", text)
        self.assertIn("Handle inbound calls.", text)
        self.assertIn("Jane Doe", text)
        self.assertIn("jane@example.com", text)
        self.assertIn("Five years of SaaS sales.", text)
        self.assertIn("1. English", text)
        self.assertIn("2. Sales", text)

    def test_prompt_handles_missing_description_and_skills(self):
        c = self._make_candidate()
        r = self._make_recruitment(description=None, skills=())
        r.skills.all.return_value = []
        text = prompt_mod.build_prompt(c, "cv", r)
        self.assertIn("Call Center Agent", text)
        # Should not crash; requirements section should still render (possibly empty).
        self.assertIn("Key requirements", text)

    def test_prompt_truncates_cv_at_8000(self):
        c = self._make_candidate()
        r = self._make_recruitment()
        sentinel = "\u00a7"  # § — absent from the template and fixture data
        cv = sentinel * 20000
        text = prompt_mod.build_prompt(c, cv, r)
        self.assertEqual(text.count(sentinel), 8000)

    def test_prompt_requests_strict_json_output(self):
        c = self._make_candidate()
        r = self._make_recruitment()
        text = prompt_mod.build_prompt(c, "cv", r)
        self.assertIn('"score"', text)
        self.assertIn('"greenFlags"', text)
        self.assertIn('"redFlags"', text)
        self.assertIn("Return ONLY a valid JSON object", text)


from recruitment.ai_screening import llm as llm_mod


class LlmClientTests(_SimpleTestCase):
    def test_parse_report_happy_path(self):
        raw = '{"score": 8, "summary": "Good fit.", "greenFlags": ["a"], "redFlags": []}'
        report = llm_mod.parse_report(raw)
        self.assertEqual(report["score"], 8)
        self.assertEqual(report["summary"], "Good fit.")

    def test_parse_report_extracts_wrapped_json(self):
        raw = 'Here is the analysis: {"score": 5, "summary": "", "greenFlags": [], "redFlags": []} thanks'
        report = llm_mod.parse_report(raw)
        self.assertEqual(report["score"], 5)

    def test_parse_report_fallback_on_garbage(self):
        report = llm_mod.parse_report("not json at all")
        self.assertEqual(report["score"], 0)
        self.assertIn("manual review", report["summary"].lower())
        self.assertTrue(report["redFlags"])

    def test_call_llm_uses_deepseek_config(self):
        fake_message = MagicMock(content='{"score": 7, "summary": "ok", "greenFlags": [], "redFlags": []}')
        fake_choice = MagicMock(message=fake_message)
        fake_response = MagicMock(choices=[fake_choice])
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}, clear=False), \
             patch.object(llm_mod, "OpenAI", return_value=fake_client) as ctor:
            result = llm_mod.call_llm("prompt goes here")

        ctor.assert_called_once()
        kwargs = ctor.call_args.kwargs
        self.assertEqual(kwargs["api_key"], "sk-test")
        self.assertEqual(kwargs["base_url"], "https://api.deepseek.com")
        create_kwargs = fake_client.chat.completions.create.call_args.kwargs
        self.assertEqual(create_kwargs["model"], "deepseek-reasoner")
        self.assertEqual(create_kwargs["max_tokens"], 4096)
        self.assertIn("prompt goes here", create_kwargs["messages"][0]["content"])
        self.assertIn('"score": 7', result)

    def test_call_llm_raises_if_api_key_missing(self):
        env = dict(os.environ)
        env.pop("DEEPSEEK_API_KEY", None)
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(RuntimeError):
                llm_mod.call_llm("p")

    def test_call_llm_uses_reasoning_content_if_content_empty(self):
        fake_message = MagicMock(content=None, reasoning_content='{"score": 4, "summary": "", "greenFlags": [], "redFlags": []}')
        fake_choice = MagicMock(message=fake_message)
        fake_response = MagicMock(choices=[fake_choice])
        fake_client = MagicMock()
        fake_client.chat.completions.create.return_value = fake_response

        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "sk-test"}, clear=False), \
             patch.object(llm_mod, "OpenAI", return_value=fake_client):
            result = llm_mod.call_llm("p")
        self.assertIn('"score": 4', result)


from django.test import TestCase
from unittest.mock import patch

from recruitment.models import Candidate, Recruitment, Stage
from base.models import JobPosition, Department, Company
from recruitment.ai_screening import service as svc


class ScreenCandidateTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # Patch threading.Thread globally so candidate-create fixtures don't
        # spawn real screening threads. Works whether or not recruitment.signals
        # has imported threading yet (Task 9 adds that import).
        cls._thread_patch = patch("threading.Thread")
        cls._thread_patch.start()

    @classmethod
    def tearDownClass(cls):
        cls._thread_patch.stop()
        super().tearDownClass()

    def setUp(self):
        self.company = Company.objects.create(company="Acme")
        # Bypass Department.save() bug (passes save-kwargs to clean) by
        # using bulk_create, which skips save() entirely. Project constraint:
        # do not modify existing production models.
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
        self.resume = SimpleUploadedFile(
            "resume.pdf", b"%PDF-1.4 placeholder", content_type="application/pdf"
        )
        self.candidate = Candidate.objects.create(
            name="Jane Doe",
            email="jane@example.com",
            mobile="1234567890",
            resume=self.resume,
            recruitment_id=self.recruitment,
            job_position_id=self.job_position,
            stage_id=self.stage,
        )

    def test_screen_candidate_persists_score_and_report(self):
        report_json = '{"score": 7, "summary": "Good fit.", "greenFlags": ["x"], "redFlags": []}'
        with patch.object(svc, "extract_cv_text", return_value="cv text"), \
             patch.object(svc, "call_llm", return_value=report_json):
            svc.screen_candidate(self.candidate.id)

        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.ai_score, 7)
        self.assertEqual(self.candidate.ai_report["summary"], "Good fit.")
        self.assertIsNotNone(self.candidate.ai_screened_at)

    def test_screen_candidate_swallows_llm_errors(self):
        with patch.object(svc, "extract_cv_text", return_value="cv"), \
             patch.object(svc, "call_llm", side_effect=RuntimeError("deepseek 500")):
            svc.screen_candidate(self.candidate.id)  # must not raise

        self.candidate.refresh_from_db()
        self.assertIsNone(self.candidate.ai_score)
        self.assertIsNone(self.candidate.ai_screened_at)

    def test_screen_candidate_noop_for_missing_candidate(self):
        # Must not raise.
        svc.screen_candidate(999999)

    def test_screen_candidate_update_avoids_post_save_signal(self):
        # Uses queryset.update() rather than instance.save() to avoid
        # re-triggering the AI signal on persist.
        report_json = '{"score": 5, "summary": "", "greenFlags": [], "redFlags": []}'
        save_calls = []
        original_save = Candidate.save

        def tracking_save(self, *a, **kw):
            save_calls.append(self.pk)
            return original_save(self, *a, **kw)

        with patch.object(svc, "extract_cv_text", return_value="cv"), \
             patch.object(svc, "call_llm", return_value=report_json), \
             patch.object(Candidate, "save", tracking_save):
            svc.screen_candidate(self.candidate.id)

        # Only the setUp create should have called save(); screen_candidate should not.
        self.assertNotIn(self.candidate.id, save_calls)


import threading
from unittest.mock import patch

from recruitment import signals as rec_signals


class CandidateAiSignalTests(TestCase):
    def setUp(self):
        self.company = Company.objects.create(company="Acme2")
        # Bypass Department.save() bug via bulk_create.
        Department.objects.bulk_create([Department(department="Ops2")])
        self.department = Department.objects.get(department="Ops2")
        self.job_position = JobPosition.objects.create(
            job_position="Agent", department_id=self.department
        )
        self.recruitment = Recruitment.objects.create(
            title="Agent",
            description="",
            company_id=self.company,
            job_position_id=self.job_position,
            vacancy=1,
        )
        self.stage = Stage.objects.filter(recruitment_id=self.recruitment).first()

    def _make_candidate(self):
        resume = SimpleUploadedFile("r.pdf", b"%PDF-1.4", content_type="application/pdf")
        return Candidate.objects.create(
            name="X",
            email="x@e.com",
            mobile="1",
            resume=resume,
            recruitment_id=self.recruitment,
            job_position_id=self.job_position,
            stage_id=self.stage,
        )

    def test_signal_enqueues_screening_for_new_candidate(self):
        called = threading.Event()

        def fake_screen(cid):
            called.set()

        with patch.object(rec_signals, "screen_candidate", side_effect=fake_screen), \
             patch.object(rec_signals.threading, "Thread") as ThreadCls:
            # Run the target synchronously instead of spawning a thread, so
            # assertion is deterministic.
            def fake_thread(target, daemon):
                class _T:
                    def start(self_inner):
                        target()
                return _T()
            ThreadCls.side_effect = fake_thread
            self._make_candidate()
            self.assertTrue(called.is_set())

    def test_signal_does_not_fire_on_update(self):
        c = self._make_candidate()
        with patch.object(rec_signals, "screen_candidate") as mock_screen:
            c.name = "Updated"
            c.save()
            mock_screen.assert_not_called()

    def test_signal_swallows_screening_errors(self):
        with patch.object(rec_signals, "screen_candidate", side_effect=RuntimeError("kaboom")):
            # Must not raise — candidate creation path must survive an AI failure.
            c = self._make_candidate()
            self.assertIsNotNone(c.id)

    def test_signal_skips_candidate_without_resume(self):
        with patch.object(rec_signals, "screen_candidate") as mock_screen, \
             patch.object(rec_signals.threading, "Thread") as ThreadCls:
            ThreadCls.side_effect = lambda target, daemon: type("T", (), {"start": lambda s: target()})()
            # Candidate without resume file.
            Candidate.objects.create(
                name="NoResume",
                email="no@e.com",
                mobile="1",
                recruitment_id=self.recruitment,
                job_position_id=self.job_position,
                stage_id=self.stage,
            )
            mock_screen.assert_not_called()


from django.contrib.auth import get_user_model
from django.db import connection
from django.urls import reverse

from employee.models import Employee
from horilla.horilla_middlewares import _thread_locals


class RerunAiScreeningViewTests(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._thread_patch = patch("threading.Thread")
        cls._thread_patch.start()
        # Horilla adds is_new_employee to auth.User via add_to_class without a
        # migration, so the test DB's auth_user table is missing the column.
        # Add it here if absent so create_user() doesn't fail.
        with connection.cursor() as cur:
            cols = [row[1] for row in cur.execute("PRAGMA table_info(auth_user)").fetchall()]
            if "is_new_employee" not in cols:
                cur.execute(
                    "ALTER TABLE auth_user ADD COLUMN is_new_employee bool NOT NULL DEFAULT 0"
                )

    @classmethod
    def tearDownClass(cls):
        cls._thread_patch.stop()
        super().tearDownClass()

    def tearDown(self):
        # Horilla's ThreadLocalMiddleware stores request.user in _thread_locals.
        # Clear it after each HTTP test so later TestCase classes that create
        # model objects don't inherit a stale user reference (which would cause
        # FK integrity errors on teardown once that user is rolled back).
        if hasattr(_thread_locals, "request"):
            del _thread_locals.request

    def setUp(self):
        self.company = Company.objects.create(company="Acme3")
        Department.objects.bulk_create([Department(department="Ops3")])
        self.department = Department.objects.get(department="Ops3")
        self.job_position = JobPosition.objects.create(
            job_position="Agent", department_id=self.department
        )
        self.recruitment = Recruitment.objects.create(
            title="Agent",
            description="desc",
            company_id=self.company,
            job_position_id=self.job_position,
            vacancy=1,
        )
        self.stage = Stage.objects.filter(recruitment_id=self.recruitment).first()
        resume = SimpleUploadedFile("r.pdf", b"%PDF-1.4", content_type="application/pdf")
        self.candidate = Candidate.objects.create(
            name="Y",
            email="y@e.com",
            mobile="1",
            resume=resume,
            recruitment_id=self.recruitment,
            job_position_id=self.job_position,
            stage_id=self.stage,
        )
        User = get_user_model()
        self.user = User.objects.create_user(
            username="admin", password="pw", is_staff=True, is_superuser=True
        )
        # Horilla's CompanyMiddleware calls logout() when the authenticated user
        # has no linked Employee.  Create a minimal Employee to avoid that.
        Employee.objects.create(
            employee_user_id=self.user,
            employee_first_name="Admin",
            email="admin@test.com",
            phone="0",
        )
        self.client.force_login(self.user)

    def test_rerun_view_invokes_screen_candidate_inline(self):
        with patch("recruitment.views.views.screen_candidate") as mock_screen:
            url = reverse("candidate-ai-rescreen", kwargs={"candidate_id": self.candidate.id})
            response = self.client.post(url)
        self.assertEqual(response.status_code, 200)
        mock_screen.assert_called_once_with(self.candidate.id)

    def test_rerun_view_returns_404_for_missing_candidate(self):
        url = reverse("candidate-ai-rescreen", kwargs={"candidate_id": 999999})
        with patch("recruitment.views.views.screen_candidate"):
            response = self.client.post(url)
        self.assertEqual(response.status_code, 404)

    def test_rerun_view_rejects_anonymous(self):
        self.client.logout()
        url = reverse("candidate-ai-rescreen", kwargs={"candidate_id": self.candidate.id})
        response = self.client.post(url)
        self.assertIn(response.status_code, (302, 401, 403))
