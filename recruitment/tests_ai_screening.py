import os
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase

from horilla import infisical_boot


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
