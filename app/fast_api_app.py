"""FastAPI server for Pip the Study Buddy.

Serves the cute kid UI (voice + whiteboard) and a small JSON API around the
ADK ``Runner``:

* ``POST /api/chat``        - send text (and optionally a whiteboard drawing / photo).
* ``POST /api/confirm``     - a grown-up approves/rejects a paused HITL action.
* ``POST /api/transcribe``  - server-side speech-to-text fallback (Gemini audio).
* ``GET  /api/calendar``    - upcoming events for the side panel.
* ``GET  /api/profile``     - what Pip remembers (parent view).
* ``GET  /healthz``         - liveness probe.
"""

from __future__ import annotations

import base64
import os
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from google.adk.runners import Runner
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from app.agent import app as adk_app
from app.config.settings import PROJECT_ROOT, get_settings
from app.memory.async_memory_worker import memory_manager
from app.memory.session_store import build_session_service, get_or_create_profile
from app.observability.logger import get_logger
from app.observability.pii_redactor import get_pii_scrubber
from app.observability.tracing import traced_span
from app.tools.calendar_tools import get_calendar_backend
from app.tools.whiteboard_tools import WHITEBOARD_STATE_KEY

logger = get_logger(__name__)
settings = get_settings()
FRONTEND_DIR = PROJECT_ROOT / "frontend"
CONFIRMATION_FN = "adk_request_confirmation"
INTERNAL_AUTHORS = {"curriculum_researcher_agent", "socratic_tutor_agent", "pedagogy_critic_agent", "user"}

session_service = build_session_service()
runner = Runner(app=adk_app, session_service=session_service)


@asynccontextmanager
async def lifespan(_: FastAPI):
    logger.info("server_started", extra={"fast_model": settings.fast_model, "reasoning_model": settings.reasoning_model})
    yield
    await memory_manager.drain()


api = FastAPI(title="Pip the Study Buddy", lifespan=lifespan)
if (FRONTEND_DIR / "static").exists():
    api.mount("/static", StaticFiles(directory=FRONTEND_DIR / "static"), name="static")


# --------------------------------------------------------------------------- #
# Request / response schemas
# --------------------------------------------------------------------------- #
class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str | None = Field(None, max_length=64)
    text: str = Field("", max_length=1000)
    image_base64: str | None = Field(None, description="PNG/JPEG data URL or base64 of a drawing/photo.")


class ConfirmRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session_id: str = Field(..., max_length=64)
    confirmation_call_id: str = Field(..., max_length=128)
    confirmed: bool


class TranscribeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    audio_base64: str
    mime_type: str = Field("audio/webm", pattern=r"^audio/[a-z0-9.+-]+$")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
async def _ensure_session(session_id: str | None) -> str:
    if session_id:
        existing = await session_service.get_session(
            app_name=adk_app.name, user_id=settings.kid_id, session_id=session_id
        )
        if existing:
            return session_id
    sid = session_id or uuid.uuid4().hex
    await session_service.create_session(app_name=adk_app.name, user_id=settings.kid_id, session_id=sid)
    return sid


def _decode_image(data: str) -> types.Part:
    mime = "image/png"
    if data.startswith("data:"):
        header, data = data.split(",", 1)
        mime = header[5:].split(";")[0]
    if mime not in {"image/png", "image/jpeg", "image/webp"}:
        raise HTTPException(400, "Only PNG, JPEG or WEBP images are supported.")
    raw = base64.b64decode(data)
    if len(raw) > 4 * 1024 * 1024:
        raise HTTPException(413, "Image too large (max 4 MB).")
    return types.Part(inline_data=types.Blob(mime_type=mime, data=raw))


async def _run_turn(session_id: str, message: types.Content) -> dict[str, Any]:
    texts: list[tuple[str, str]] = []
    confirmations: list[dict[str, Any]] = []
    critic_ran = False
    with traced_span("kid_turn", session_id=session_id):
        async for event in runner.run_async(user_id=settings.kid_id, session_id=session_id, new_message=message):
            if event.author == "pedagogy_critic_agent":
                critic_ran = True
            for call in event.get_function_calls() or []:
                if call.name == CONFIRMATION_FN:
                    tool_conf = (call.args or {}).get("toolConfirmation", {}) or {}
                    original = (call.args or {}).get("originalFunctionCall", {}) or {}
                    confirmations.append(
                        {
                            "confirmation_call_id": call.id,
                            "tool": original.get("name"),
                            "hint": tool_conf.get("hint", "A grown-up needs to approve this."),
                            "payload": tool_conf.get("payload"),
                        }
                    )
            if event.content and event.content.parts and not event.partial:
                text = "".join(p.text for p in event.content.parts if getattr(p, "text", None) and not p.thought)
                if text.strip():
                    texts.append((event.author, text.strip()))

    session = await session_service.get_session(app_name=adk_app.name, user_id=settings.kid_id, session_id=session_id)
    state = session.state if session else {}
    reply = ""
    if critic_ran and isinstance(state.get("pedagogy_review"), dict):
        reply = state["pedagogy_review"].get("final_reply_to_kid", "")
    if not reply:
        visible = [t for a, t in texts if a not in INTERNAL_AUTHORS]
        tutor = [t for a, t in texts if a == "socratic_tutor_agent"]
        reply = (visible or tutor or [""])[-1]
    if not reply and confirmations:
        reply = "I've asked a grown-up to check that for us! 🐰"
    return {
        "session_id": session_id,
        "reply": reply or "Hmm, my bunny ears missed that. Can you say it again? 🐰",
        "whiteboard": state.get(WHITEBOARD_STATE_KEY),
        "pending_confirmations": confirmations,
        "agents": sorted({a for a, _ in texts}),
    }


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@api.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND_DIR / "index.html")


@api.get("/healthz")
async def healthz() -> dict:
    return {"status": "ok", "fast_model": settings.fast_model, "reasoning_model": settings.reasoning_model}


@api.post("/api/chat")
async def chat(req: ChatRequest) -> JSONResponse:
    if not req.text.strip() and not req.image_base64:
        raise HTTPException(400, "Say or draw something first!")
    session_id = await _ensure_session(req.session_id)
    parts: list[types.Part] = []
    if req.text.strip():
        parts.append(types.Part(text=req.text.strip()))
    if req.image_base64:
        if not req.text.strip():
            parts.append(types.Part(text="Here is my drawing / homework picture. Can you help me with it?"))
        parts.append(_decode_image(req.image_base64))
    try:
        result = await _run_turn(session_id, types.Content(role="user", parts=parts))
    except Exception as exc:  # noqa: BLE001
        logger.exception("chat_turn_failed")
        return JSONResponse(
            status_code=200,
            content={
                "session_id": session_id,
                "reply": "Oops, my bunny brain got tangled! 🐰 Can we try that again?",
                "error": type(exc).__name__,
                "whiteboard": None,
                "pending_confirmations": [],
            },
        )
    return JSONResponse(result)


@api.post("/api/confirm")
async def confirm(req: ConfirmRequest) -> JSONResponse:
    message = types.Content(
        role="user",
        parts=[
            types.Part(
                function_response=types.FunctionResponse(
                    id=req.confirmation_call_id, name=CONFIRMATION_FN, response={"confirmed": req.confirmed}
                )
            )
        ],
    )
    logger.info("grown_up_decision", extra={"confirmed": req.confirmed, "call_id": req.confirmation_call_id})
    return JSONResponse(await _run_turn(req.session_id, message))


@api.post("/api/transcribe")
async def transcribe(req: TranscribeRequest) -> dict:
    """Server-side STT fallback for browsers without the Web Speech API."""
    from google import genai

    audio = base64.b64decode(req.audio_base64.split(",", 1)[-1])
    if len(audio) > 8 * 1024 * 1024:
        raise HTTPException(413, "Recording too long.")
    client = genai.Client(vertexai=True, project=settings.project_id, location=settings.location)
    resp = await client.aio.models.generate_content(
        model=settings.fast_model,
        contents=[
            types.Part(text="Transcribe exactly what this child says. Return only the words."),
            types.Part(inline_data=types.Blob(mime_type=req.mime_type, data=audio)),
        ],
    )
    return {"text": get_pii_scrubber().redact_text((resp.text or "").strip())}


@api.get("/api/calendar")
async def calendar(days: int = 7) -> dict:
    days = max(1, min(days, 14))
    today = datetime.now(ZoneInfo(settings.kid_timezone)).replace(tzinfo=None)
    events = get_calendar_backend().list_events(
        today.strftime("%Y-%m-%dT00:00"), (today + timedelta(days=days)).strftime("%Y-%m-%dT23:59")
    )
    return {"events": [e.model_dump(mode="json") for e in events]}


@api.get("/api/profile")
async def profile() -> dict:
    p = get_or_create_profile(settings.kid_id)
    data = p.model_dump(mode="json")
    data["strengths"], data["focus_areas"] = p.strengths(), p.focus_areas()
    return data


app = api  # uvicorn app.fast_api_app:app

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.fast_api_app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8080")))
