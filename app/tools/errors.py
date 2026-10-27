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

"""Guided error helpers.

Tools never raise into the LLM. They return a structured payload with an
``error_code``, a human-readable ``message`` and concrete
``recovery_instructions`` the model can follow to self-correct.
"""

from __future__ import annotations

from pydantic import ValidationError

from app.observability.logger import get_logger
from app.schemas.calendar_schemas import ToolErrorResult

logger = get_logger(__name__)


def guided_error(error_code: str, message: str, recovery_instructions: str) -> dict:
    logger.warning("tool_guided_error", extra={"error_code": error_code, "error_message": message})
    return ToolErrorResult(
        error_code=error_code, message=message, recovery_instructions=recovery_instructions
    ).model_dump(mode="json")


def validation_error(exc: ValidationError, tool_name: str) -> dict:
    problems = "; ".join(
        f"{'.'.join(str(p) for p in err['loc']) or 'input'}: {err['msg']}" for err in exc.errors()
    )
    return guided_error(
        "INVALID_ARGUMENTS",
        f"{tool_name} received invalid arguments: {problems}",
        f"Fix the listed fields and call {tool_name} again. If a value is missing or unclear "
        "(e.g. the exact date or time), ask the kid a short clarifying question first.",
    )
