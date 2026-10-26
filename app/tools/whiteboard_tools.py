"""Whiteboard tool: lets Buddy *show* ideas visually while it talks.

The validated scene is written to session state under ``whiteboard_scene``;
the web UI renders it as an animated pastel SVG whiteboard.
"""

from __future__ import annotations

from typing import Any

from google.adk.tools import ToolContext
from pydantic import ValidationError

from app.observability.tracing import traced_span
from app.schemas.whiteboard_schemas import ALLOWED_STICKERS, DrawOnWhiteboardOutput, WhiteboardScene
from app.tools.errors import guided_error, validation_error

WHITEBOARD_STATE_KEY = "whiteboard_scene"

ELEMENT_GUIDE = (
    "Allowed element types (each is a JSON object with a 'type' field): "
    "text{text,size}; equation{expression using digits + - x ÷ = ? /}; "
    "fraction_shape{shape:'pizza'|'bar',parts,shaded}; "
    "number_line{start,end,step,highlight[],jumps[[from,to]],fraction_denominator}; "
    "counting_objects{sticker,count,groups,crossed_out}; base_ten_blocks{tens,ones}; "
    "steps{items[],stickers[],cycle}; sticker{sticker,size}. "
    "Every element may also have label and color (pink|purple|mint|sky|peach|lemon|lilac|coral). "
    f"Stickers must be one of: {' '.join(ALLOWED_STICKERS)}"
)


def draw_on_whiteboard(title: str, elements: list[dict[str, Any]], tool_context: ToolContext) -> dict:
    """Draw a kid-friendly picture on the shared whiteboard to explain an idea visually.

    Use it whenever a picture helps an 8-year-old understand: fractions as
    pizzas or bars, number lines with hops, counting stickers for adding or
    taking away, base-ten blocks for regrouping, numbered steps or a life
    cycle, or a big equation with a "?" for the part she should solve.
    NEVER put the final homework answer on the board - show the method, and
    use "?" where she should think.

    Args:
        title: Short heading for the board, e.g. "Let's cut the pizza into quarters!".
        elements: 1 to 8 element objects. Supported shapes:
            {"type":"text","text":"...","size":"small|medium|big"}
            {"type":"equation","expression":"27 + 15 = ?"}
            {"type":"fraction_shape","shape":"pizza|bar","parts":4,"shaded":1}
            {"type":"number_line","start":0,"end":10,"step":1,"highlight":[3],"jumps":[[3,7]]}
            {"type":"counting_objects","sticker":"🍓","count":8,"groups":2,"crossed_out":3}
            {"type":"base_ten_blocks","tens":2,"ones":7}
            {"type":"steps","items":["Egg","Caterpillar"],"stickers":["🥚","🐛"],"cycle":true}
            {"type":"sticker","sticker":"🦄","size":"big"}
            Any element may include "label" and a pastel "color".
        tool_context: Injected by ADK; used to persist the scene in session state.

    Returns:
        dict: ``{"status":"success","scene_version":int,"element_count":int,"message":str}``
        or a guided error with ``recovery_instructions`` explaining how to fix the elements.
    """
    previous = tool_context.state.get(WHITEBOARD_STATE_KEY) or {}
    version = int(previous.get("version", 0)) + 1

    with traced_span("whiteboard.draw", element_count=len(elements or []), scene_version=version):
        try:
            scene = WhiteboardScene(title=title, elements=elements, version=version)
        except ValidationError as exc:
            result = validation_error(exc, "draw_on_whiteboard")
            result["recovery_instructions"] += " " + ELEMENT_GUIDE
            return result
        except Exception as exc:  # noqa: BLE001
            return guided_error(
                "WHITEBOARD_UNAVAILABLE",
                f"The whiteboard could not be updated ({type(exc).__name__}).",
                "Explain the idea with words and a simple emoji picture in your reply instead.",
            )

    tool_context.state[WHITEBOARD_STATE_KEY] = scene.model_dump(mode="json")
    return DrawOnWhiteboardOutput(
        scene_version=version,
        element_count=len(scene.elements),
        message="The whiteboard now shows your drawing. Refer to it in your reply (e.g. 'Look at the pizza!').",
    ).model_dump(mode="json")


def clear_whiteboard(tool_context: ToolContext) -> dict:
    """Wipe the shared whiteboard clean, e.g. when starting a brand-new topic.

    Args:
        tool_context: Injected by ADK; used to reset the scene in session state.

    Returns:
        dict: ``{"status": "success", "message": str}``.
    """
    previous = tool_context.state.get(WHITEBOARD_STATE_KEY) or {}
    tool_context.state[WHITEBOARD_STATE_KEY] = {
        "title": "",
        "elements": [],
        "version": int(previous.get("version", 0)) + 1,
    }
    return {"status": "success", "message": "Whiteboard cleared."}
