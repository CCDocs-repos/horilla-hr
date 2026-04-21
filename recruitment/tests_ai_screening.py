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
