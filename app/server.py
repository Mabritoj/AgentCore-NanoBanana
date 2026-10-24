"""MCP server exposing Google Gemini ("Nano Banana") image generation.

Runs as an AgentCore Runtime container: streamable-HTTP MCP on 0.0.0.0:8000/mcp, stateless.
"""
import base64
import logging
import os
import time
import uuid
from functools import lru_cache
from typing import Annotated, Literal

import boto3
from mcp.server.fastmcp import FastMCP, Image
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import Field
from starlette.requests import Request
from starlette.responses import PlainTextResponse

from . import gemini

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
log = logging.getLogger("nanobanana")

IMAGE_BUCKET = os.environ.get("IMAGE_BUCKET", "")
URL_TTL_SECONDS = int(os.environ.get("IMAGE_URL_TTL_SECONDS", "3600"))
DEFAULT_DELIVERY = os.environ.get("DEFAULT_DELIVERY", "inline" if not IMAGE_BUCKET else "both")
MAX_REFERENCE_IMAGES = 10
MAX_REFERENCE_BYTES = 7 * 1024 * 1024

mcp = FastMCP(
    "nanobanana",
    host="0.0.0.0",
    port=8000,
    stateless_http=True,
    json_response=True,
)

Delivery = Literal["inline", "url", "both"]
ModelName = Literal["flash", "flash-lite", "pro", "legacy"]
AspectRatio = Literal[
    "1:1", "1:4", "1:8", "2:3", "3:2", "3:4", "4:1", "4:3", "4:5", "5:4", "8:1", "9:16", "16:9", "21:9"
]
Resolution = Literal["512", "1K", "2K", "4K"]


@lru_cache(maxsize=1)
def _api_key() -> str:
    """Key comes from GEMINI_API_KEY (local dev) or Secrets Manager (deployed)."""
    if key := os.environ.get("GEMINI_API_KEY"):
        return key
    arn = os.environ.get("GEMINI_SECRET_ARN")
    if not arn:
        raise ToolError("Server misconfigured: set GEMINI_SECRET_ARN or GEMINI_API_KEY")
    sm = boto3.client("secretsmanager")
    return sm.get_secret_value(SecretId=arn)["SecretString"].strip()


def _s3_url(data: bytes, mime: str) -> str:
    ext = "jpg" if "jpeg" in mime else mime.split("/")[-1]
    key = f"images/{time.strftime('%Y/%m/%d')}/{uuid.uuid4().hex}.{ext}"
    s3 = _s3()
    s3.put_object(Bucket=IMAGE_BUCKET, Key=key, Body=data, ContentType=mime)
    return s3.generate_presigned_url(
        "get_object", Params={"Bucket": IMAGE_BUCKET, "Key": key}, ExpiresIn=URL_TTL_SECONDS
    )


@lru_cache(maxsize=1)
def _s3():
    return boto3.client("s3")


async def _run(
    prompt: str,
    model: str,
    aspect_ratio: str | None,
    resolution: str | None,
    delivery: str,
    refs: list[tuple[bytes, str]] | None = None,
) -> list:
    if delivery in ("url", "both") and not IMAGE_BUCKET:
        raise ToolError("delivery='url' requires the server to be deployed with an image bucket; use 'inline'")
    try:
        result = await gemini.generate(
            _api_key(), prompt, model=model, aspect_ratio=aspect_ratio, resolution=resolution, reference_images=refs
        )
    except gemini.GeminiError as e:
        raise ToolError(str(e)) from e

    log.info("generated image model=%s bytes=%d", model, len(result.data))
    out: list = []
    if delivery in ("url", "both"):
        out.append(f"Image URL (expires in {URL_TTL_SECONDS}s): {_s3_url(result.data, result.mime_type)}")
    if delivery in ("inline", "both"):
        out.append(Image(data=result.data, format=result.mime_type.split("/")[-1]))
    if result.text:
        out.append(result.text)
    return out


@mcp.tool()
async def generate_image(
    prompt: Annotated[str, Field(description="Detailed description of the image to create", min_length=1, max_length=8000)],
    model: Annotated[ModelName, Field(description="flash (default, general), pro (best text rendering/adherence), flash-lite (fast/cheap, 1K only), legacy")] = "flash",
    aspect_ratio: Annotated[AspectRatio | None, Field(description="e.g. 16:9, 1:1, 9:16")] = None,
    resolution: Annotated[Resolution | None, Field(description="512, 1K, 2K or 4K (availability varies by model)")] = None,
    delivery: Annotated[Delivery | None, Field(description="inline = image bytes in the MCP result; url = presigned S3 link; both")] = None,
) -> list:
    """Generate an image from a text prompt with Google Nano Banana (Gemini image models)."""
    return await _run(prompt, model, aspect_ratio, resolution, delivery or DEFAULT_DELIVERY)


@mcp.tool()
async def edit_image(
    prompt: Annotated[str, Field(description="Instruction for the edit, e.g. 'make the shirt navy blue'", min_length=1, max_length=8000)],
    images_base64: Annotated[list[str], Field(description="One or more base64-encoded reference/source images (PNG/JPEG/WEBP)", min_length=1, max_length=MAX_REFERENCE_IMAGES)],
    model: ModelName = "flash",
    aspect_ratio: AspectRatio | None = None,
    resolution: Resolution | None = None,
    delivery: Delivery | None = None,
) -> list:
    """Edit or combine existing images using a text instruction (Nano Banana)."""
    refs: list[tuple[bytes, str]] = []
    for i, b64 in enumerate(images_base64):
        try:
            raw = base64.b64decode(b64.split(",", 1)[-1], validate=True)
        except Exception:
            raise ToolError(f"images_base64[{i}] is not valid base64")
        if len(raw) > MAX_REFERENCE_BYTES:
            raise ToolError(f"images_base64[{i}] exceeds {MAX_REFERENCE_BYTES // (1024 * 1024)} MB")
        refs.append((raw, _sniff_mime(raw)))
    return await _run(prompt, model, aspect_ratio, resolution, delivery or DEFAULT_DELIVERY, refs)


def _sniff_mime(raw: bytes) -> str:
    if raw.startswith(b"\x89PNG"):
        return "image/png"
    if raw.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp"
    raise ToolError("Unsupported image format; use PNG, JPEG or WEBP")


@mcp.custom_route("/ping", methods=["GET"])
async def ping(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok")


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
