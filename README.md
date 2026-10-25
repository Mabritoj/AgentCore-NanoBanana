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
`gemini-2.5-flash-image` (see `containers/nanobanana/app/gemini.py`). Verify these IDs against your AI Studio account; edit `MODELS` if they differ.

## Layout (follows the AWS SAM conventions)

```
template.yaml            # root stack: SAM transform + AWS::CloudFormation::Stack declarations only
samconfig.toml           # [dev] / [prd] sections: stack name, parameter_overrides, BACKUP tag
infrastructure/
  ecr.yaml               # EcrStack      - image repository
  storage.yaml           # StorageStack  - Gemini API key secret + image bucket
  auth.yaml              # AuthStack     - Cognito client-credentials auth for the gateway
  runtime.yaml           # RuntimeStack  - runtime execution role + AWS::BedrockAgentCore::Runtime
containers/nanobanana/   # the MCP server container: Dockerfile, app/, tests/, requirements*.txt (Python)
scripts/deploy.sh        # sam build/deploy, image build + push, runtime rollout
```

Values reach nested stacks only through the root (`Parameters:` on each `AWS::CloudFormation::Stack`; sibling
outputs are read with `!GetAtt` in the root and forwarded). All resources have explicit names, stateful ones
(secret, bucket, user pool) are `Retain`, and the only tag is `BACKUP=<stack name>` from `samconfig.toml`.
There is no `Globals` block because the project has no `AWS::Serverless::Function`; compute is the AgentCore
Runtime container.

## Deploy

Prereqs: AWS credentials and region, SAM CLI, Docker with buildx, a Google AI Studio API key.

```bash
GEMINI_API_KEY=... ./scripts/deploy.sh dev     # or prd
```

The script runs `sam build` + `sam deploy --config-env <env>` twice: first without `ImageTag` (creates ECR,
secret, bucket, Cognito; the `RuntimeStack` is skipped by the `HasImage` condition), then, after building and
pushing the **linux/arm64** image, with `ImageTag` set so the runtime (`serverProtocol=MCP`, JWT authorizer) is
created or updated. It prints the gateway target settings at the end.

## Add to the Gateway

Create a target of type **MCP server** with the endpoint, Cognito client ID/secret, token URL and scope printed by
the script (outbound auth: OAuth client credentials). Clients then see the tools as `<target-name>___generate_image`, etc.

## Teardown

`sam delete --config-env <env>` removes everything except the `Retain` resources (secret, image bucket, user
pool). Delete those manually, or redeploys with the same names will conflict.

## Local development

```bash
cd containers/nanobanana
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                        # mocked Gemini, real MCP client over HTTP
GEMINI_API_KEY=... .venv/bin/python -m app.server # serves http://localhost:8000/mcp
```

## Notes

- Runtime contract: stateless streamable HTTP on `0.0.0.0:8000/mcp`, arm64.
- Presigned URLs are signed with the runtime role's temporary credentials, so they may expire earlier than
  `IMAGE_URL_TTL_SECONDS` (credential lifetime caps them).
- Large inline images inflate MCP responses; prefer `delivery=url` for 2K/4K output.
- Generated images carry Google's SynthID watermark.
