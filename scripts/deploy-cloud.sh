#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
environment_file=${1:-"$project_dir/.env.production"}
compose_file="$project_dir/deploy/compose.production.yaml"
export PRODUCTION_ENV_FILE=$environment_file

environment_value() {
  sed -n "s/^$1=//p" "$environment_file" | tail -n 1
}

require_values() {
  for key in "$@"; do
    value=$(environment_value "$key")
    case "$value" in
      ''|*REPLACE_*|*example.com*)
        echo "Production environment requires a real value for $key." >&2
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
  echo "Production environment file not found: $environment_file" >&2
  echo "Copy .env.production.example and replace every placeholder first." >&2
  exit 1
fi
if ! grep -qx 'APP_ENV=production' "$environment_file"; then
  echo "APP_ENV=production is required for cloud deployment." >&2
  exit 1
fi
require_values \
  POSTGRES_PASSWORD REDIS_PASSWORD AUTH_COOKIE_SECRET TENANT_ID KNOWLEDGE_BASE_IDS \
  OIDC_ISSUER OIDC_AUDIENCE OIDC_JWKS_URL AGENT_PROVIDER AGENT_MODEL OPENAI_API_KEY ENABLED_TOOLS

enabled_tools=$(environment_value ENABLED_TOOLS)
case ",$enabled_tools," in
  *,search_knowledge,*)
    require_values CUEKB_MODE CUEKB_BASE_URL CUEKB_API_KEY CUEKB_API_REVISION
    ;;
esac
case ",$enabled_tools," in
  *,weather,*)
    require_values WEATHER_MODE WEATHER_BASE_URL WEATHER_API_KEY
    ;;
esac
case "$(environment_value VOICE_PROVIDER)" in
  disabled) ;;
  nvidia)
    require_values VOICECHAT_WS_URL VOICECHAT_HEALTH_URL VOICECHAT_API_KEY VOICECHAT_API_VERSION VOICECHAT_IMAGE_DIGEST
    ;;
  *)
    echo "VOICE_PROVIDER must be disabled or nvidia in production." >&2
    exit 1
    ;;
esac
if ! grep -Eq '^PUBLIC_ORIGIN=https://[^[:space:]]+$' "$environment_file"; then
  echo "PUBLIC_ORIGIN must be an HTTPS origin." >&2
  exit 1
fi

compose() {
  docker compose --env-file "$environment_file" -f "$compose_file" "$@"
}

compose config --quiet
compose build
compose up -d --wait --wait-timeout 180 postgres redis
compose run --rm --no-deps migrate
compose up -d --wait --wait-timeout 180 --no-deps api
compose exec -T api python /app/scripts/verify_deployment.py --base-url http://127.0.0.1:8000
compose up -d --wait --wait-timeout 60 --no-deps web
compose ps
