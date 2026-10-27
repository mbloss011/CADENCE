"""Image understanding: OpenAI-style `image_url` content parts for vision-capable models."""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

from .llm import LLM

MIME = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".webp": "image/webp", ".gif": "image/gif"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024

DESCRIBE_SYSTEM = (
    "You are the eyes of a coding agent. Describe the image precisely and factually: visible text (verbatim), "
    "UI elements and their state, errors, layout, diagrams and data. Answer the question if one is given. No speculation."
)


class ImageError(ValueError):
    """The file is not a usable image."""


def data_uri(path: Path) -> str:
    path = Path(path)
    mime = MIME.get(path.suffix.lower())
    if mime is None:
        raise ImageError(f"unsupported image type '{path.suffix}' (use {', '.join(sorted(MIME))})")
    if not path.is_file():
        raise ImageError(f"not a file: {path}")
    raw = path.read_bytes()
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageError(f"image is {len(raw) / 1e6:.1f} MB; limit is {MAX_IMAGE_BYTES / 1e6:.0f} MB")
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def user_content(text: str, images: list[Path]) -> str | list[dict[str, Any]]:
    """A user message's content: plain text, or text + image parts when images are attached."""
    if not images:
        return text
    parts: list[dict[str, Any]] = [{"type": "text", "text": text or "Describe this image."}]
    parts += [{"type": "image_url", "image_url": {"url": data_uri(p)}} for p in images]
    return parts


async def describe(llm: LLM, path: Path, question: str = "", max_tokens: int = 900) -> str:
    content = user_content(question or "Describe this image in detail.", [path])
    comp = await llm.complete([{"role": "system", "content": DESCRIBE_SYSTEM}, {"role": "user", "content": content}],
                              max_tokens=max_tokens, temperature=0.1)
    return comp.content.strip() or "(the vision model returned nothing)"
