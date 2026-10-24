#!/usr/bin/env bash
# Build the arm64 image, push to ECR, and create/update the AgentCore Runtime (MCP protocol).
# Prereqs: aws CLI v2 (recent enough for `bedrock-agentcore-control`), docker with buildx.
# Usage:   GEMINI_API_KEY=... AWS_REGION=us-east-1 ./scripts/deploy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"
PREFIX="${PREFIX:-nanobanana}"
STACK="${PREFIX}-foundation"
RUNTIME_NAME="${PREFIX}_mcp"
TAG="${IMAGE_TAG:-$(git rev-parse --short HEAD 2>/dev/null || date +%s)}"
export AWS_DEFAULT_REGION="$REGION"

out() { aws cloudformation describe-stacks --stack-name "$STACK" \
  --query "Stacks[0].Outputs[?OutputKey=='$1'].OutputValue" --output text; }

echo "==> Deploying foundation stack"
aws cloudformation deploy --stack-name "$STACK" --template-file infra/foundation.yaml \
  --capabilities CAPABILITY_IAM --parameter-overrides Prefix="$PREFIX"

REPO_URI=$(out RepositoryUri); ROLE_ARN=$(out RuntimeRoleArn); SECRET_ARN=$(out SecretArn)
BUCKET=$(out ImageBucket); DISCOVERY=$(out DiscoveryUrl); CLIENT_ID=$(out GatewayClientId)

if [[ -n "${GEMINI_API_KEY:-}" ]]; then
  echo "==> Storing Gemini API key in Secrets Manager"
  aws secretsmanager put-secret-value --secret-id "$SECRET_ARN" --secret-string "$GEMINI_API_KEY" >/dev/null
else
  echo "!! GEMINI_API_KEY not set; leaving secret as-is. Set it later with:"
  echo "   aws secretsmanager put-secret-value --secret-id $SECRET_ARN --secret-string <key>"
fi

echo "==> Building and pushing $REPO_URI:$TAG (linux/arm64)"
aws ecr get-login-password | docker login --username AWS --password-stdin "${REPO_URI%%/*}"
docker buildx build --platform linux/arm64 -t "$REPO_URI:$TAG" --push .

ENV_VARS="GEMINI_SECRET_ARN=$SECRET_ARN,IMAGE_BUCKET=$BUCKET"
AUTH="customJWTAuthorizer={discoveryUrl=$DISCOVERY,allowedClients=[$CLIENT_ID]}"
ARTIFACT="containerConfiguration={containerUri=$REPO_URI:$TAG}"

EXISTING=$(aws bedrock-agentcore-control list-agent-runtimes \
  --query "agentRuntimes[?agentRuntimeName=='$RUNTIME_NAME'].agentRuntimeId | [0]" --output text)

if [[ "$EXISTING" == "None" || -z "$EXISTING" ]]; then
  echo "==> Creating runtime $RUNTIME_NAME"
  RUNTIME_ID=$(aws bedrock-agentcore-control create-agent-runtime \
    --agent-runtime-name "$RUNTIME_NAME" --agent-runtime-artifact "$ARTIFACT" --role-arn "$ROLE_ARN" \
    --network-configuration networkMode=PUBLIC --protocol-configuration serverProtocol=MCP \
    --authorizer-configuration "$AUTH" --environment-variables "$ENV_VARS" \
    --query agentRuntimeId --output text)
else
  echo "==> Updating runtime $EXISTING"
  RUNTIME_ID="$EXISTING"
  aws bedrock-agentcore-control update-agent-runtime --agent-runtime-id "$RUNTIME_ID" \
    --agent-runtime-artifact "$ARTIFACT" --role-arn "$ROLE_ARN" \
    --network-configuration networkMode=PUBLIC --protocol-configuration serverProtocol=MCP \
    --authorizer-configuration "$AUTH" --environment-variables "$ENV_VARS" >/dev/null
fi

echo "==> Waiting for READY"
for _ in $(seq 60); do
  STATUS=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" --query status --output text)
  [[ "$STATUS" == "READY" ]] && break
  [[ "$STATUS" == *FAILED* ]] && { echo "Runtime status: $STATUS"; exit 1; }
  sleep 10
done
[[ "$STATUS" == "READY" ]] || { echo "Timed out; status=$STATUS"; exit 1; }

RUNTIME_ARN=$(aws bedrock-agentcore-control get-agent-runtime --agent-runtime-id "$RUNTIME_ID" --query agentRuntimeArn --output text)
ENCODED_ARN=$(python3 -c 'import sys,urllib.parse as u; print(u.quote(sys.argv[1], safe=""))' "$RUNTIME_ARN")

cat <<MSG

Runtime READY: $RUNTIME_ARN

Gateway target settings (Target type: MCP server):
  Endpoint:        https://bedrock-agentcore.$REGION.amazonaws.com/runtimes/$ENCODED_ARN/invocations?qualifier=DEFAULT
  Outbound auth:   OAuth client credentials
  Client ID:       $CLIENT_ID
  Client secret:   (Cognito console > $(out UserPoolId) > App clients > agentcore-gateway)
  Token URL:       $(out TokenUrl)
  Scope:           nanobanana/invoke
MSG
