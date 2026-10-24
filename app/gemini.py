"""Thin async client for the Gemini image-generation REST API."""
import base64
import os
from dataclasses import dataclass

import httpx

API_BASE = os.environ.get("GEMINI_API_BASE", "https://generativelanguage.googleapis.com/v1beta")

# Friendly aliases exposed to MCP clients -> Gemini model IDs.
MODELS = {
    "flash-lite": "gemini-3.1-flash-lite-image",
    "flash": "gemini-3.1-flash-image",
    "pro": "gemini-3-pro-image",
    "legacy": "gemini-2.5-flash-image",
}

ASPECT_RATIOS = {
    "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9",
}
RESOLUTIONS = {"512", "1K", "2K", "4K"}


class GeminiError(Exception):
    """Raised for any failure the MCP client should see as a tool error."""


@dataclass
class GeneratedImage:
    data: bytes
    mime_type: str
    text: str


def _new_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=httpx.Timeout(120.0))


def resolve_model(model: str) -> str:
    if model in MODELS:
        return MODELS[model]
    if model in MODELS.values():
        return model
    raise GeminiError(f"Unknown model '{model}'. Use one of: {', '.join(MODELS)}")


async def generate(
    api_key: str,
    prompt: str,
    *,
    model: str = "flash",
    aspect_ratio: str | None = None,
    resolution: str | None = None,
    reference_images: list[tuple[bytes, str]] | None = None,
    client: httpx.AsyncClient | None = None,
) -> GeneratedImage:
    if aspect_ratio and aspect_ratio not in ASPECT_RATIOS:
        raise GeminiError(f"Invalid aspect_ratio '{aspect_ratio}'. Valid: {', '.join(sorted(ASPECT_RATIOS))}")
    if resolution and resolution not in RESOLUTIONS:
        raise GeminiError(f"Invalid resolution '{resolution}'. Valid: {', '.join(sorted(RESOLUTIONS))}")

    model_id = resolve_model(model)
    parts: list[dict] = [{"text": prompt}]
    for data, mime in reference_images or []:
        parts.append({"inline_data": {"mime_type": mime, "data": base64.b64encode(data).decode()}})

    image_config = {}
    if aspect_ratio:
        image_config["aspectRatio"] = aspect_ratio
    if resolution:
        image_config["imageSize"] = resolution
    generation_config: dict = {"responseModalities": ["TEXT", "IMAGE"]}
    if image_config:
        generation_config["imageConfig"] = image_config

    body = {"contents": [{"parts": parts}], "generationConfig": generation_config}
    url = f"{API_BASE}/models/{model_id}:generateContent"
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}

    owns_client = client is None
    client = client or _new_client()
    try:
        resp = await client.post(url, headers=headers, json=body)
    except httpx.HTTPError as e:
        raise GeminiError(f"Request to Gemini failed: {type(e).__name__}") from e
    finally:
        if owns_client:
            await client.aclose()

    if resp.status_code != 200:
        # Gemini error bodies don't echo the key; still truncate defensively.
        raise GeminiError(f"Gemini returned HTTP {resp.status_code}: {resp.text[:500]}")

    payload = resp.json()
    candidates = payload.get("candidates") or []
    if not candidates:
        feedback = payload.get("promptFeedback", {})
        raise GeminiError(f"Gemini returned no candidates (promptFeedback={feedback})")

    text_chunks: list[str] = []
    image: tuple[bytes, str] | None = None
    for part in candidates[0].get("content", {}).get("parts", []):
        if part.get("thought"):
            continue
        inline = part.get("inlineData") or part.get("inline_data")
        if inline and inline.get("data"):
            image = (base64.b64decode(inline["data"]), inline.get("mimeType") or inline.get("mime_type") or "image/png")
        elif part.get("text"):
            text_chunks.append(part["text"])

    text = "\n".join(text_chunks)
    if image is None:
        raise GeminiError(f"Model returned no image. Model said: {text or candidates[0].get('finishReason', 'unknown')}")
    return GeneratedImage(data=image[0], mime_type=image[1], text=text)
