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
