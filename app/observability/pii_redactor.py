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

"""Active PII scrubbing for logs and long-term memory.

Children's data deserves extra care. Before anything is written to logs,
traces, or the persistent learner-memory store, text passes through
:class:`PiiRedactionScrubber`, which combines:

* **Google Cloud DLP API** (``google-cloud-dlp``) de-identification when
  ``BUDDY_ENABLE_CLOUD_DLP=true`` and a project is configured.
* A **deterministic regex fallback** that always runs (offline / CI safe),
  covering emails, phone numbers, street addresses, credit cards, dates of
  birth, government IDs, and school student IDs.
"""

from __future__ import annotations

import re
from functools import lru_cache
from typing import Any

from app.config.settings import get_settings

# Ordered: more specific patterns first.
_REGEX_RULES: list[tuple[str, re.Pattern[str]]] = [
    ("EMAIL_ADDRESS", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("CREDIT_CARD_NUMBER", re.compile(r"\b(?:\d[ -]?){13,16}\b")),
    ("STUDENT_ID", re.compile(r"\b(?:student[ _-]?id|sid)[:#\s]*[A-Za-z0-9-]{4,}\b", re.IGNORECASE)),
    ("DATE_OF_BIRTH", re.compile(r"\b(?:dob|date of birth|born on)[:\s]*\d{1,4}[/-]\d{1,2}[/-]\d{1,4}\b", re.IGNORECASE)),
    ("PHONE_NUMBER", re.compile(r"(?<!\w)(?:\+?\d{1,3}[ .-]?)?(?:\(?\d{2,4}\)?[ .-]?)\d{3,4}[ .-]?\d{3,4}(?!\w)")),
    ("STREET_ADDRESS", re.compile(
        r"\b\d{1,5}\s+(?:[A-Z][a-z]+\s){1,3}(?:Street|St|Road|Rd|Avenue|Ave|Lane|Ln|Drive|Dr|Court|Ct|Place|Pl)\b"
    )),
    ("GOVERNMENT_ID", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
]

_DLP_INFO_TYPES = [
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "CREDIT_CARD_NUMBER",
    "STREET_ADDRESS",
    "DATE_OF_BIRTH",
    "PERSON_NAME",
    "US_SOCIAL_SECURITY_NUMBER",
    "AUSTRALIA_TAX_FILE_NUMBER",
]


class PiiRedactionScrubber:
    """Redact sensitive data from free text and nested structures."""

    def __init__(self, use_cloud_dlp: bool | None = None, project_id: str | None = None) -> None:
        settings = get_settings()
        self._project_id = project_id or settings.project_id
        self._use_cloud_dlp = (settings.enable_cloud_dlp if use_cloud_dlp is None else use_cloud_dlp) and bool(
            self._project_id
        )
        self._dlp_client = None

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #
    def redact_text(self, text: str) -> str:
        """Return ``text`` with PII replaced by ``[REDACTED_<INFO_TYPE>]`` tokens."""
        if not text:
            return text
        scrubbed = self._redact_with_cloud_dlp(text) if self._use_cloud_dlp else text
        return self._redact_with_regex(scrubbed)

    def redact(self, value: Any) -> Any:
        """Recursively redact strings inside dicts, lists, and tuples."""
        if isinstance(value, str):
            return self.redact_text(value)
        if isinstance(value, dict):
            return {k: self.redact(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return type(value)(self.redact(v) for v in value)
        return value

    # ------------------------------------------------------------------ #
    # Backends
    # ------------------------------------------------------------------ #
    @staticmethod
    def _redact_with_regex(text: str) -> str:
        for info_type, pattern in _REGEX_RULES:
            text = pattern.sub(f"[REDACTED_{info_type}]", text)
        return text

    def _redact_with_cloud_dlp(self, text: str) -> str:
        """De-identify using the Cloud DLP ``deidentify_content`` API.

        Falls back to the original text (regex still runs afterwards) on any
        error so redaction never blocks the child's conversation.
        """
        try:
            if self._dlp_client is None:
                from google.cloud import dlp_v2

                self._dlp_client = dlp_v2.DlpServiceClient()
            response = self._dlp_client.deidentify_content(
                request={
                    "parent": f"projects/{self._project_id}/locations/global",
                    "inspect_config": {"info_types": [{"name": t} for t in _DLP_INFO_TYPES]},
                    "deidentify_config": {
                        "info_type_transformations": {
                            "transformations": [
                                {"primitive_transformation": {"replace_with_info_type_config": {}}}
                            ]
                        }
                    },
                    "item": {"value": text},
                }
            )
            return response.item.value
        except Exception:  # noqa: BLE001 - never block on DLP availability
            return text


@lru_cache(maxsize=1)
def get_pii_scrubber() -> PiiRedactionScrubber:
    return PiiRedactionScrubber()
