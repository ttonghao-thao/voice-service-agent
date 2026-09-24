#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
environment_file=${1:-"$project_dir/.env"}
compose_file="$project_dir/deploy/compose.production.yaml"

environment_value() {
  sed -n "s/^$1=//p" "$environment_file" | tail -n 1
}

require_values() {
  for key in "$@"; do
    value=$(environment_value "$key")
    case "$value" in
      ''|*REPLACE_*|*example.com*)
        echo "Validation deployment requires a real value for $key." >&2
        exit 1
        ;;
    esac
  done
}

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Engine with Docker Compose v2 is required." >&2
  exit 1
fi
if [ ! -f "$environment_file" ]; then
  echo "Validation environment file not found: $environment_file" >&2
  echo "Copy .env.example to .env and replace every placeholder first." >&2
  exit 1
fi
environment_file=$(CDPATH= cd -- "$(dirname -- "$environment_file")" && pwd)/$(basename -- "$environment_file")
export DEPLOY_ENV_FILE=$environment_file
require_values \
  IMAGE_TAG POSTGRES_PASSWORD REDIS_PASSWORD TENANT_ID KNOWLEDGE_BASE_IDS \
  WEB_TLS_CERT_FILE WEB_TLS_KEY_FILE \
  PUBLIC_ORIGIN AGENT_PROVIDER AGENT_MODEL OPENAI_API_KEY \
  CUEKB_BASE_URL CUEKB_API_KEY VOICECHAT_WS_URL VOICECHAT_API_KEY

case "$(environment_value AGENT_PROVIDER)" in
  openai) ;;
  compatible) require_values AGENT_BASE_URL ;;
  *) echo "AGENT_PROVIDER must be openai or compatible." >&2; exit 1 ;;
esac
for key in WEB_TLS_CERT_FILE WEB_TLS_KEY_FILE; do
  path=$(environment_value "$key")
  case "$path" in
    /*) ;;
    *) echo "$key must be an absolute host path." >&2; exit 1 ;;
  esac
  if [ ! -f "$path" ] || [ ! -r "$path" ]; then
    echo "$key must point to a readable file: $path" >&2
    exit 1
  fi
done

if ! grep -Eq '^PUBLIC_ORIGIN=https://[^/?#@[:space:]]+$' "$environment_file"; then
  echo "PUBLIC_ORIGIN must be an HTTPS origin without a path." >&2
  exit 1
fi

compose() {
  docker compose --env-file "$environment_file" -f "$compose_file" "$@"
}

image_tag=$(environment_value IMAGE_TAG)
api_image="voice-service-agent-api:$image_tag"
web_image="voice-service-agent-web:$image_tag"
export IMAGE_TAG="$image_tag"
export PUBLIC_ORIGIN="$(environment_value PUBLIC_ORIGIN)"
export WEB_TLS_CERT_FILE="$(environment_value WEB_TLS_CERT_FILE)"
export WEB_TLS_KEY_FILE="$(environment_value WEB_TLS_KEY_FILE)"
export POSTGRES_PASSWORD="$(environment_value POSTGRES_PASSWORD)"
export REDIS_PASSWORD="$(environment_value REDIS_PASSWORD)"
compose config --quiet
for image in "$api_image" "$web_image"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "Required application image is not available locally: $image" >&2
    echo "Build with docker build or load/pull the exact image before deploying." >&2
    exit 1
  fi
done
compose up -d --no-build --wait --wait-timeout 180 postgres redis
compose run --rm --no-deps migrate
compose up -d --no-build --wait --wait-timeout 180 --no-deps api
compose exec -T api python /app/scripts/verify_deployment.py --base-url http://127.0.0.1:8000
compose up -d --no-build --wait --wait-timeout 60 --no-deps web
compose ps
