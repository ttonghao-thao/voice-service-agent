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
  echo "Copy .env.example to .env and replace every placeholder first." >&2
  exit 1
fi
environment_file=$(CDPATH= cd -- "$(dirname -- "$environment_file")" && pwd)/$(basename -- "$environment_file")
export DEPLOY_ENV_FILE=$environment_file
if ! grep -qx 'AUTH_MODE=local' "$environment_file"; then
  echo "AUTH_MODE=local is required for deployment." >&2
  exit 1
fi
require_values \
  API_IMAGE WEB_IMAGE POSTGRES_PASSWORD REDIS_PASSWORD AUTH_COOKIE_SECRET LOCAL_USERS_JSON TENANT_ID KNOWLEDGE_BASE_IDS \
  API_BIND_ADDRESS WEB_TLS_CERT_FILE WEB_TLS_KEY_FILE \
  AGENT_PROVIDER AGENT_MODEL OPENAI_API_KEY ENABLED_TOOLS

api_bind_address=$(environment_value API_BIND_ADDRESS)
if ! printf '%s\n' "$api_bind_address" | awk -F. '
  NF != 4 {exit 1}
  {
    for (i = 1; i <= 4; i++) if ($i !~ /^[0-9]+$/ || $i > 255) exit 1
    if ($1 == 10 || ($1 == 172 && $2 >= 16 && $2 <= 31) || ($1 == 192 && $2 == 168)) exit 0
    exit 1
  }
'; then
  echo "API_BIND_ADDRESS must be a host RFC 1918 private IPv4 address." >&2
  exit 1
fi
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
if ! grep -Eq '^PUBLIC_ORIGIN=https://[^/?#@[:space:]]+$' "$environment_file"; then
  echo "PUBLIC_ORIGIN must be an HTTPS origin without a path." >&2
  exit 1
fi

compose() {
  docker compose --env-file "$environment_file" -f "$compose_file" "$@"
}

api_image=$(environment_value API_IMAGE)
web_image=$(environment_value WEB_IMAGE)
export API_IMAGE="$api_image" WEB_IMAGE="$web_image"
export PUBLIC_ORIGIN="$(environment_value PUBLIC_ORIGIN)"
export API_BIND_ADDRESS="$api_bind_address"
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
