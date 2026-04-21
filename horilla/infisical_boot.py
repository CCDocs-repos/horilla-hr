"""Boot-time secret hydration from Infisical.

Mirrors /opt/workspace/job-applicant-platform/src/lib/infisical.ts — uses a
Universal Auth machine identity to list secrets at a given project/environment
and populate os.environ with any value that isn't already set. Runs once at
Django startup from RecruitmentConfig.ready().

Any failure (network, auth, SDK change) is logged and swallowed so that
Horilla still boots when Infisical is unreachable. Callers must gracefully
handle the case where an expected secret (e.g. DEEPSEEK_API_KEY) is missing.
"""

import logging
import os

from infisical_sdk import InfisicalSDKClient

logger = logging.getLogger(__name__)


def load_secrets() -> None:
    try:
        site_url = os.environ.get("INFISICAL_SITE_URL", "https://app.infisical.com")
        client_id = os.environ.get("INFISICAL_CLIENT_ID")
        client_secret = os.environ.get("INFISICAL_CLIENT_SECRET")
        project_id = os.environ.get("INFISICAL_PROJECT_ID")
        environment = os.environ.get("INFISICAL_ENVIRONMENT", "prod")
        secret_path = os.environ.get("INFISICAL_SECRET_PATH", "/")

        if not (client_id and client_secret and project_id):
            logger.warning(
                "Infisical env vars not fully configured; skipping secret hydration."
            )
            return

        client = InfisicalSDKClient(host=site_url)
        client.auth.universal_auth.login(
            client_id=client_id,
            client_secret=client_secret,
        )
        response = client.secrets.list_secrets(
            project_id=project_id,
            environment_slug=environment,
            secret_path=secret_path,
            view_secret_value=True,
            expand_secret_references=True,
            include_imports=True,
            recursive=False,
        )
        secrets = getattr(response, "secrets", None) or []
        hydrated = 0
        for secret in secrets:
            # Infisical SDK uses camelCase (secretKey / secretValue).
            key = getattr(secret, "secretKey", None) or getattr(secret, "secret_key", None)
            value = getattr(secret, "secretValue", None) or getattr(secret, "secret_value", None)
            if key and value is not None and key not in os.environ:
                os.environ[key] = value
                hydrated += 1
        logger.info("Infisical: hydrated %d secret(s) into os.environ", hydrated)
    except Exception:
        logger.exception("Failed to load secrets from Infisical; continuing without.")
