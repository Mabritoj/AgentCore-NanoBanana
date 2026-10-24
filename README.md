# AgentCore-NanoBanana

An MCP server, packaged as a container for **Amazon Bedrock AgentCore Runtime**, that exposes Google's
Nano Banana (Gemini image) models. Register it as an **MCP server target** on an AgentCore Gateway and any
MCP client (Claude, Cursor, ...) can generate and edit images through the gateway.

```
Claude / Cursor ──MCP──▶ AgentCore Gateway ──MCP (OAuth)──▶ AgentCore Runtime (this container) ──▶ Gemini API
```

## Tools

| Tool | Purpose |
|---|---|
| `generate_image` | Text → image. Args: `prompt`, `model` (`flash` default, `pro`, `flash-lite`, `legacy`), `aspect_ratio`, `resolution` (`512`/`1K`/`2K`/`4K`), `delivery` |
| `edit_image` | Edit/combine images. Same args plus `images_base64` (PNG/JPEG/WEBP, ≤10, ≤7 MB each) |

`delivery`: `inline` (MCP image content), `url` (presigned S3 link), or `both`. Default is `both` when the S3
bucket is configured (as deployed), otherwise `inline`.

Model aliases map to `gemini-3.1-flash-image`, `gemini-3.1-flash-lite-image`, `gemini-3-pro-image`,
`gemini-2.5-flash-image` (see `app/gemini.py`). Verify these IDs against your AI Studio account; edit `MODELS` if they differ.

## Deploy

Prereqs: AWS CLI v2 with `bedrock-agentcore-control`, Docker with buildx, a Google AI Studio API key.

```bash
GEMINI_API_KEY=... AWS_REGION=us-east-1 ./scripts/deploy.sh
```

This deploys `infra/foundation.yaml` (ECR repo, Secrets Manager secret, S3 bucket with 7-day expiry, runtime
execution role, Cognito client-credentials auth), builds and pushes the **linux/arm64** image, and creates or
updates the runtime with `serverProtocol=MCP` and a JWT authorizer. It prints the values needed for the gateway target.

## Add to the Gateway

Create a target of type **MCP server** with the endpoint, Cognito client ID/secret, token URL and scope printed by
the script (outbound auth: OAuth client credentials). Clients then see the tools as `<target-name>___generate_image`, etc.

## Local development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                       # mocked Gemini, real MCP client over HTTP
GEMINI_API_KEY=... .venv/bin/python -m app.server # serves http://localhost:8000/mcp
```

## Notes

- Runtime contract: stateless streamable HTTP on `0.0.0.0:8000/mcp`, arm64.
- Presigned URLs are signed with the runtime role's temporary credentials, so they may expire earlier than
  `IMAGE_URL_TTL_SECONDS` (credential lifetime caps them).
- Large inline images inflate MCP responses; prefer `delivery=url` for 2K/4K output.
- Generated images carry Google's SynthID watermark.
