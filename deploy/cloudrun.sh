#!/usr/bin/env bash
# Deploy CargoMind to Google Cloud Run (source deploy: Cloud Build builds the repo's Dockerfile).
#
# Usage, from anywhere inside the repo (Cloud Shell, macOS, Linux, or Git Bash / WSL on Windows):
#   ./deploy/cloudrun.sh               # API keys from .env -> Secret Manager (recommended)
#   ./deploy/cloudrun.sh --plain-env   # API keys as plain env vars (quicker, visible in the console)
#   ./deploy/cloudrun.sh --no-keys     # zero-key deploy: offline rule-engine + free/offline APIs
#
# Optional environment overrides:
#   PROJECT_ID       Google Cloud project   (default: `gcloud config get-value project`)
#   REGION           Cloud Run region       (default: asia-south1, Mumbai)
#   SERVICE          Cloud Run service name (default: cargomind)
#   ENV_FILE         where keys are read    (default: .env); exported env vars win over the file
#   SERVICE_ACCOUNT  runtime service account (default: the Compute Engine default service account)
set -euo pipefail

MODE="secrets"
for arg in "$@"; do
  case "$arg" in
    --plain-env) MODE="plain" ;;
    --no-keys) MODE="none" ;;
    -h | --help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg (try --help)" >&2; exit 2 ;;
  esac
done

cd "$(dirname "$0")/.."

if ! command -v gcloud >/dev/null 2>&1; then
  echo "gcloud CLI not found. Install it: https://cloud.google.com/sdk/docs/install" >&2
  exit 1
fi

PROJECT_ID="${PROJECT_ID:-$(gcloud config get-value project 2>/dev/null || true)}"
if [ -z "$PROJECT_ID" ] || [ "$PROJECT_ID" = "(unset)" ]; then
  echo "No Google Cloud project selected. Run: gcloud config set project YOUR_PROJECT_ID" >&2
  exit 1
fi
REGION="${REGION:-asia-south1}"
SERVICE="${SERVICE:-cargomind}"
ENV_FILE="${ENV_FILE:-.env}"
KEYS="GOOGLE_API_KEY GOOGLE_MAPS_API_KEY ANTHROPIC_API_KEY OPENAI_API_KEY"

# Reads KEY=VALUE from $ENV_FILE without executing the file. Exported env vars take precedence.
# Blank values and placeholders (PASTE_..., YOUR_..., <...>) count as "not set", like config.py.
read_key() {
  local name="$1" value=""
  if [ -f "$ENV_FILE" ]; then
    value="$(tr -d '\r' <"$ENV_FILE" | grep -E "^[[:space:]]*${name}[[:space:]]*=" | tail -n 1 | cut -d= -f2- || true)"
    value="$(printf '%s' "$value" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/^"\(.*\)"$/\1/' -e "s/^'\(.*\)'$/\1/")"
  fi
  if [ -n "${!name:-}" ]; then
    value="${!name}"
  fi
  case "$(printf '%s' "$value" | tr '[:lower:]' '[:upper:]')" in
    "" | PASTE_* | YOUR_* | "<"*) value="" ;;
  esac
  printf '%s' "$value"
}

echo "==> project=$PROJECT_ID region=$REGION service=$SERVICE keys=$MODE"
echo "==> enabling Cloud Run, Cloud Build and Artifact Registry APIs"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  --project "$PROJECT_ID"

ENV_VARS="CARGOMIND_CHAOS=0,CARGOMIND_MCP_LOCAL=0,PYTHONUTF8=1"
EXTRA_FLAGS=()

if [ "$MODE" = "secrets" ]; then
  echo "==> storing API keys in Secret Manager"
  gcloud services enable secretmanager.googleapis.com --project "$PROJECT_ID"
  PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
  RUNTIME_SA="${SERVICE_ACCOUNT:-${PROJECT_NUMBER}-compute@developer.gserviceaccount.com}"
  SECRETS=""
  for key in $KEYS; do
    value="$(read_key "$key")"
    if [ -z "$value" ]; then
      echo "    - $key not set (that feature uses its fallback)"
      continue
    fi
    secret="cargomind-$(printf '%s' "$key" | tr '[:upper:]_' '[:lower:]-')"
    if ! gcloud secrets describe "$secret" --project "$PROJECT_ID" >/dev/null 2>&1; then
      gcloud secrets create "$secret" --replication-policy=automatic --project "$PROJECT_ID" >/dev/null
    fi
    printf '%s' "$value" | gcloud secrets versions add "$secret" --data-file=- --project "$PROJECT_ID" >/dev/null
    gcloud secrets add-iam-policy-binding "$secret" --project "$PROJECT_ID" \
      --member="serviceAccount:${RUNTIME_SA}" --role="roles/secretmanager.secretAccessor" >/dev/null
    SECRETS="${SECRETS:+$SECRETS,}${key}=${secret}:latest"
    echo "    + $key -> secret $secret (readable by $RUNTIME_SA)"
  done
  if [ -n "$SECRETS" ]; then
    EXTRA_FLAGS+=(--set-secrets "$SECRETS")
  fi
elif [ "$MODE" = "plain" ]; then
  echo "==> passing API keys as plain environment variables"
  for key in $KEYS; do
    value="$(read_key "$key")"
    if [ -n "$value" ]; then
      ENV_VARS="$ENV_VARS,$key=$value"
      echo "    + $key"
    else
      echo "    - $key not set (that feature uses its fallback)"
    fi
  done
else
  echo "==> zero-key deploy: offline rule-engine + free/offline API fallbacks"
fi

if [ -n "${SERVICE_ACCOUNT:-}" ]; then
  EXTRA_FLAGS+=(--service-account "$SERVICE_ACCOUNT")
fi

# --max-instances 1 + --session-affinity: chat sessions, the healing journal ring and the
# SQLite demo DB live in one instance, so every browser tab sees the same state.
# --timeout 3600: the dashboard keeps long-lived SSE streams open.
echo "==> building and deploying (takes a few minutes the first time)"
gcloud run deploy "$SERVICE" \
  --source . \
  --project "$PROJECT_ID" \
  --region "$REGION" \
  --allow-unauthenticated \
  --port 8080 \
  --memory 1Gi \
  --cpu 1 \
  --timeout 3600 \
  --max-instances 1 \
  --session-affinity \
  --set-env-vars "$ENV_VARS" \
  ${EXTRA_FLAGS[@]+"${EXTRA_FLAGS[@]}"} \
  --quiet

URL="$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')"
echo
echo "==> CargoMind is live: $URL"
echo "    health check: $URL/api/health"
echo "    API docs:     $URL/docs"
