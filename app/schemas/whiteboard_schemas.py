"""Strict JSON schemas for the interactive kid whiteboard.

The agent never sends raw pixels or coordinates. It describes *semantic*
visual elements (a fraction pizza, a number line, counting stickers...) and
the frontend auto-lays them out on a cute SVG whiteboard. This keeps the LLM's
job simple and makes every drawing validated, safe and kid-friendly.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

PASTEL_COLORS = ("pink", "purple", "mint", "sky", "peach", "lemon", "lilac", "coral")
PastelColor = Literal["pink", "purple", "mint", "sky", "peach", "lemon", "lilac", "coral"]

#: Child-safe emoji stickers the board may render.
ALLOWED_STICKERS = (
    "🐰", "🦄", "🌈", "⭐", "🌸", "🦋", "🐛", "🥚", "🍕", "🍎", "🍓", "🧁",
    "🐱", "🐶", "🐟", "🐦", "🐝", "🌻", "☀️", "💧", "🌱", "🍃", "❤️", "✨", "🎈", "🍪",
)


class _Element(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    label: str = Field("", max_length=40, description="Optional caption shown under the element.")
    color: PastelColor = "pink"


class TextElement(_Element):
    type: Literal["text"]
    text: str = Field(..., min_length=1, max_length=120)
    size: Literal["small", "medium", "big"] = "medium"


class FractionShapeElement(_Element):
    """A circle ('pizza') or bar cut into equal parts with some parts shaded."""

    type: Literal["fraction_shape"]
    shape: Literal["pizza", "bar"] = "pizza"
    parts: int = Field(..., ge=1, le=12)
    shaded: int = Field(..., ge=0, le=12)

    @model_validator(mode="after")
    def _shaded_le_parts(self) -> "FractionShapeElement":
        if self.shaded > self.parts:
            raise ValueError("shaded cannot be more than parts")
        return self


class NumberLineElement(_Element):
    type: Literal["number_line"]
    start: int = Field(..., ge=-20, le=1000)
    end: int = Field(..., ge=-19, le=1000)
    step: int = Field(1, ge=1, le=100)
    highlight: list[int] = Field(default_factory=list, max_length=10)
    jumps: list[tuple[int, int]] = Field(default_factory=list, max_length=8, description="Hops as [from, to].")
    fraction_denominator: int | None = Field(None, ge=2, le=12, description="If set, show ticks as fractions of 0..1.")

    @model_validator(mode="after")
    def _range(self) -> "NumberLineElement":
        if self.end <= self.start:
            raise ValueError("end must be greater than start")
        if (self.end - self.start) / self.step > 25:
            raise ValueError("too many ticks; use a bigger step (max 25 ticks)")
        return self


class CountingObjectsElement(_Element):
    type: Literal["counting_objects"]
    sticker: str = Field("⭐", description="One emoji from the allowed sticker list.")
    count: int = Field(..., ge=1, le=30)
    groups: int = Field(1, ge=1, le=6, description="Split the objects into equal groups (multiplication/division).")
    crossed_out: int = Field(0, ge=0, le=30, description="How many to cross out (subtraction).")

    @field_validator("sticker")
    @classmethod
    def _safe_sticker(cls, v: str) -> str:
        if v not in ALLOWED_STICKERS:
            raise ValueError(f"sticker must be one of {' '.join(ALLOWED_STICKERS)}")
        return v


class BaseTenBlocksElement(_Element):
    type: Literal["base_ten_blocks"]
    tens: int = Field(..., ge=0, le=9)
    ones: int = Field(..., ge=0, le=19, description="Allow >9 to show a regrouping moment.")


class StepsElement(_Element):
    """Numbered steps or a cycle (e.g. butterfly life cycle)."""

    type: Literal["steps"]
    items: list[str] = Field(..., min_length=2, max_length=6)
    stickers: list[str] = Field(default_factory=list, max_length=6)
    cycle: bool = False

    @field_validator("items")
    @classmethod
    def _short_items(cls, v: list[str]) -> list[str]:
        if any(len(i) > 40 for i in v):
            raise ValueError("each step must be 40 characters or fewer")
        return v

    @field_validator("stickers")
    @classmethod
    def _safe_stickers(cls, v: list[str]) -> list[str]:
        bad = [s for s in v if s not in ALLOWED_STICKERS]
        if bad:
            raise ValueError(f"stickers must be from {' '.join(ALLOWED_STICKERS)}")
        return v


class StickerElement(_Element):
    type: Literal["sticker"]
    sticker: str
    size: Literal["small", "medium", "big"] = "big"

    @field_validator("sticker")
    @classmethod
    def _safe_sticker(cls, v: str) -> str:
        if v not in ALLOWED_STICKERS:
            raise ValueError(f"sticker must be one of {' '.join(ALLOWED_STICKERS)}")
        return v


class EquationElement(_Element):
    """A big friendly equation; use '?' for the part the kid should solve."""

    type: Literal["equation"]
    expression: str = Field(..., min_length=1, max_length=40, pattern=r"^[0-9+\-x×÷=?/ ().]+$")


WhiteboardElement = Annotated[
    Union[
        TextElement,
        FractionShapeElement,
        NumberLineElement,
        CountingObjectsElement,
        BaseTenBlocksElement,
        StepsElement,
        StickerElement,
        EquationElement,
    ],
    Field(discriminator="type"),
]

ELEMENT_LIST_ADAPTER: TypeAdapter[list[WhiteboardElement]] = TypeAdapter(list[WhiteboardElement])


class WhiteboardScene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(..., min_length=1, max_length=60)
    elements: list[WhiteboardElement] = Field(..., min_length=1, max_length=8)
    version: int = 1


class DrawOnWhiteboardOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["success"] = "success"
    scene_version: int
    element_count: int
    message: str
