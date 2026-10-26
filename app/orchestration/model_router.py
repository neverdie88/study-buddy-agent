"""Strategic model routing (Flash for fast tasks, Pro for deep reasoning).

Static routing: each agent is assigned a default model tier by role
(coordinator / calendar / researcher / critic -> fast; Socratic tutor -> reasoning).

Dynamic routing: :func:`route_model_by_complexity` is a ``before_model_callback``
on the tutor that inspects the actual request and downgrades simple asks
(e.g. spelling a word) to Flash, while multi-step word problems, images of
homework, or repeated struggles go to Pro.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

from google.adk.agents.callback_context import CallbackContext
from google.adk.models import LlmRequest

from app.config.settings import get_settings
from app.observability.logger import get_logger

logger = get_logger(__name__)


class ModelTier(str, Enum):
    FAST = "fast"
    REASONING = "reasoning"


#: Static role -> tier table.
ROLE_TIERS: dict[str, ModelTier] = {
    "study_buddy_coordinator": ModelTier.FAST,
    "schedule_manager_agent": ModelTier.FAST,
    "curriculum_researcher_agent": ModelTier.FAST,
    "pedagogy_critic_agent": ModelTier.FAST,
    "progress_coach_agent": ModelTier.FAST,
    "socratic_tutor_agent": ModelTier.REASONING,
}

_COMPLEX_SIGNALS = re.compile(
    r"(word problem|how many|altogether|left over|share|groups of|divide|"
    r"don't get|dont get|confused|stuck|still wrong|again)",
    re.IGNORECASE,
)
_SIMPLE_SIGNALS = re.compile(r"^(how do you spell|spell|what is|what's|is it|yes|no|ok|okay|thanks)\b", re.IGNORECASE)


@dataclass(frozen=True)
class RoutingDecision:
    tier: ModelTier
    model: str
    reason: str


def model_for_role(role: str) -> str:
    settings = get_settings()
    tier = ROLE_TIERS.get(role, ModelTier.FAST)
    return settings.reasoning_model if tier is ModelTier.REASONING else settings.fast_model


def classify_request(text: str, has_image: bool) -> RoutingDecision:
    """Pure routing policy - easy to unit test."""
    settings = get_settings()
    text = (text or "").strip()
    if has_image:
        return RoutingDecision(ModelTier.REASONING, settings.reasoning_model, "image_of_homework")
    if _COMPLEX_SIGNALS.search(text):
        return RoutingDecision(ModelTier.REASONING, settings.reasoning_model, "multi_step_reasoning")
    if _SIMPLE_SIGNALS.search(text) or len(text.split()) <= 12:
        return RoutingDecision(ModelTier.FAST, settings.fast_model, "simple_short_request")
    return RoutingDecision(ModelTier.FAST, settings.fast_model, "default_fast_for_low_latency_voice")


def _latest_user_parts(llm_request: LlmRequest) -> tuple[str, bool]:
    for content in reversed(llm_request.contents or []):
        if content.role != "user" or not content.parts:
            continue
        texts = [p.text for p in content.parts if getattr(p, "text", None)]
        has_image = any(getattr(p, "inline_data", None) for p in content.parts)
        if texts or has_image:
            return " ".join(texts), has_image
    return "", False


def route_model_by_complexity(callback_context: CallbackContext, llm_request: LlmRequest) -> None:
    """``before_model_callback``: pick Flash or Pro for this specific request."""
    text, has_image = _latest_user_parts(llm_request)
    decision = classify_request(text, has_image)
    llm_request.model = decision.model
    callback_context.state["temp:model_route"] = {"tier": decision.tier.value, "reason": decision.reason}
    logger.info(
        "model_routed",
        extra={"model": decision.model, "tier": decision.tier.value, "reason": decision.reason},
    )
    return None
