# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Secure secret resolution via Google Cloud Secret Manager.

No API keys or OAuth credentials are ever hardcoded in this repository.
Secrets are fetched at runtime with Application Default Credentials (ADC),
so access is governed entirely by IAM (`roles/secretmanager.secretAccessor`).
"""

from __future__ import annotations

import os
from functools import lru_cache

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


class SecretNotAvailableError(RuntimeError):
    """Raised when a secret cannot be resolved from any secure source."""


class SecretManagerResolver:
    """Resolve secrets from Google Cloud Secret Manager with an env-var override.

    Resolution order:
      1. An environment variable injected by the platform (e.g. Cloud Run
         ``--set-secrets`` mounts Secret Manager values as env vars).
      2. Google Cloud Secret Manager ``projects/<project>/secrets/<name>/versions/latest``.
    """

    def __init__(self, project_id: str | None = None) -> None:
        self._project_id = project_id or get_settings().project_id
        self._client = None

    def _get_client(self):
        if self._client is None:
            from google.cloud import secretmanager  # lazy import keeps local dev light

            self._client = secretmanager.SecretManagerServiceClient()
        return self._client

    @staticmethod
    def _env_name(secret_name: str) -> str:
        return secret_name.upper().replace("-", "_")

    def get_secret(self, secret_name: str, version: str = "latest") -> str:
        """Return the secret payload as a UTF-8 string.

        Args:
            secret_name: The Secret Manager secret id (e.g. ``study-buddy-calendar-credentials``).
            version: Secret version, defaults to ``latest``.

        Raises:
            SecretNotAvailableError: If the secret is not injected and cannot be
                read from Secret Manager.
        """
        injected = os.getenv(self._env_name(secret_name))
        if injected:
            logger.info("secret_resolved", extra={"secret_name": secret_name, "source": "platform_injection"})
            return injected

        if not self._project_id:
            raise SecretNotAvailableError(
                f"Secret '{secret_name}' not injected and GOOGLE_CLOUD_PROJECT is not set."
            )

        try:
            name = f"projects/{self._project_id}/secrets/{secret_name}/versions/{version}"
            response = self._get_client().access_secret_version(request={"name": name})
            logger.info("secret_resolved", extra={"secret_name": secret_name, "source": "secret_manager"})
            return response.payload.data.decode("utf-8")
        except Exception as exc:  # noqa: BLE001 - surface a typed, non-leaky error
            logger.warning(
                "secret_resolution_failed",
                extra={"secret_name": secret_name, "error_type": type(exc).__name__},
            )
            raise SecretNotAvailableError(f"Unable to access secret '{secret_name}'.") from exc


@lru_cache(maxsize=1)
def get_secret_resolver() -> SecretManagerResolver:
    return SecretManagerResolver()
