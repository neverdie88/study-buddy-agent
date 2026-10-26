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
