#!/usr/bin/env bash
# Deploy the Nano Banana MCP runtime for one environment.
#   1. sam deploy  -> ECR, secret, bucket, Cognito (runtime is skipped while ImageTag is empty)
#   2. store the Gemini key, build + push the linux/arm64 image
#   3. sam deploy  -> same stack with ImageTag set, which creates/updates the AgentCore Runtime
#
# Usage: GEMINI_API_KEY=... ./scripts/deploy.sh [dev|prd]
# Prereqs: AWS credentials + region, sam CLI, docker with buildx.
set -euo pipefail
cd "$(dirname "$0")/.."

ENV="${1:-dev}"
TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%s)}"

cfg() { python3 - "$ENV" "$1" <<'PY'
import sys, tomllib
env, key = sys.argv[1:]
print(tomllib.load(open("samconfig.toml", "rb"))[env]["deploy"]["parameters"][key])
PY
}
STACK="$(cfg stack_name)"
OVERRIDES="$(cfg parameter_overrides)"
REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-$(aws configure get region)}}"
export AWS_DEFAULT_REGION="$REGION"

out() { aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }
deploy() { sam deploy --config-env "$ENV" --no-confirm-changeset --no-fail-on-empty-changeset "$@"; }

echo "==> sam build + deploy (infrastructure)"
sam build
deploy

if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  echo "==> Storing Gemini API key in Secrets Manager"
  aws secretsmanager put-secret-value --secret-id "$(out GeminiSecretArn)" --secret-string "$GEMINI_API_KEY" >/dev/null
else
  echo "!! GEMINI_API_KEY not set; secret left as-is. Set it with:"
  echo "   aws secretsmanager put-secret-value --secret-id $(out GeminiSecretArn) --secret-string <key>"
fi

REPO_URI="$(out RepositoryUri)"
echo "==> Building and pushing $REPO_URI:$TAG (linux/arm64)"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO_URI%%/*}"
docker buildx build --platform linux/arm64 -t "$REPO_URI:$TAG" --push containers/nanobanana

echo "==> sam deploy (runtime, ImageTag=$TAG)"
# Passing --parameter-overrides replaces the samconfig.toml value, so the config's overrides are re-supplied here.
deploy --parameter-overrides "$OVERRIDES ImageTag=\"$TAG\""

RUNTIME_ARN="$(out RuntimeArn)"
ENCODED_ARN="$(python3 -c 'import sys,urllib.parse as u; print(u.quote(sys.argv[1], safe=""))' "$RUNTIME_ARN")"
CLIENT_ID="$(out GatewayClientId)"; POOL_ID="$(out UserPoolId)"

cat <<MSG

Runtime: $RUNTIME_ARN

Gateway target settings (Target type: MCP server, outbound auth: OAuth client credentials):
  Endpoint:       https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/$ENCODED_ARN/invocations?qualifier=DEFAULT
  Client ID:      $CLIENT_ID
  Client secret:  aws cognito-idp describe-user-pool-client --user-pool-id $POOL_ID --client-id $CLIENT_ID --query UserPoolClient.ClientSecret --output text
  Token URL:      $(out TokenUrl)
  Scope:          $(out GatewayScope)
MSG
