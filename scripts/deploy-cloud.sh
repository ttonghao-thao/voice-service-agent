#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
environment_file=${1:-"$project_dir/.env"}
compose_file="$project_dir/deploy/compose.production.yaml"

environment_value() {
  value=$(sed -n "s/^$1=//p" "$environment_file" | tail -n 1)
  printf '%s\n' "${value:-${2:-}}"
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

tool_enabled() {
  case ",$enabled_tools," in
    *,"$1",*) return 0 ;;
    *) return 1 ;;
  esac
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
  IMAGE_TAG POSTGRES_PASSWORD REDIS_PASSWORD WEB_TLS_CERT_FILE WEB_TLS_KEY_FILE \
  PUBLIC_ORIGIN WEB_SEARCH_API_KEY

enabled_tools=$(environment_value ENABLED_TOOLS search_knowledge,weather,web_search |
  sed -E 's/^[[:space:]]+|[[:space:]]+$//g; s/[[:space:]]*,[[:space:]]*/,/g')
if ! tool_enabled web_search; then
  echo "Deployment requires ENABLED_TOOLS to include web_search." >&2
  exit 1
fi
if tool_enabled search_knowledge; then
  require_values CUEKB_BASE_URL CUEKB_API_KEY KNOWLEDGE_BASE_IDS
fi
if tool_enabled weather; then
  require_values WEATHER_BASE_URL WEATHER_API_KEY
  if [ "$(environment_value WEATHER_PROVIDER qweather)" != "qweather" ] || \
     [ "$(environment_value WEATHER_MODE real)" != "real" ]; then
    echo "Weather requires WEATHER_PROVIDER=qweather and WEATHER_MODE=real." >&2
    exit 1
  fi
fi

voice_provider=$(environment_value VOICE_PROVIDER)
case "$voice_provider" in
  sophon_live)
    require_values SOPHON_LIVE_WS_URL LIVEKIT_URL LIVEKIT_API_KEY LIVEKIT_API_SECRET LIVEKIT_NODE_IP \
      DELEGATION_ROUTER_BASE_URL DELEGATION_ROUTER_MODEL DELEGATION_ROUTER_API_KEY
    if [ "$(environment_value DELEGATION_ROUTER_PROVIDER openai_compatible)" != "openai_compatible" ]; then
      echo "Sophon deployment requires DELEGATION_ROUTER_PROVIDER=openai_compatible for native task parsing." >&2
      exit 1
    fi
    if ! grep -Eq '^LIVEKIT_URL=wss://[A-Za-z0-9.-]+(:[0-9]+)?/?$' "$environment_file"; then
      echo "LIVEKIT_URL must be a WSS origin routed to this Compose Web port 7880." >&2
      exit 1
    fi
    livekit_secret=$(environment_value LIVEKIT_API_SECRET)
    if [ "${#livekit_secret}" -lt 32 ]; then
      echo "LIVEKIT_API_SECRET must contain at least 32 characters." >&2
      exit 1
    fi
    if ! environment_value LIVEKIT_NODE_IP | awk -F. '
      NF != 4 { exit 1 }
      { for (i=1; i<=4; i++) if ($i !~ /^[0-9]+$/ || $i > 255) exit 1 }
    '; then
      echo "LIVEKIT_NODE_IP must be the deployment host IPv4 reachable by clients and the API." >&2
      exit 1
    fi
    export COMPOSE_PROFILES=sophon
    secret_keys="REDIS_PASSWORD LIVEKIT_API_KEY LIVEKIT_API_SECRET"
    ;;
  nvidia)
    if ! tool_enabled search_knowledge; then
      echo "NVIDIA requires ENABLED_TOOLS to include search_knowledge." >&2
      exit 1
    fi
    require_values VOICECHAT_WS_URL VOICECHAT_API_KEY
    export COMPOSE_PROFILES=
    secret_keys=REDIS_PASSWORD
    ;;
  *) echo "VOICE_PROVIDER must select exactly one backend: sophon_live or nvidia." >&2; exit 1 ;;
esac

for key in $secret_keys; do
  case "$(environment_value "$key")" in
    *[!A-Za-z0-9._~-]*) echo "$key must use URL-safe letters, digits, dot, underscore, tilde or hyphen." >&2; exit 1 ;;
  esac
done
# The configured voice model chooses direct replies or tool calls.
# Reject an old strict .env before starting or migrating any container.
if [ "$(environment_value QA_EXECUTION_MODE dual_tools)" != "dual_tools" ] || \
   [ "$(environment_value QA_ANSWER_POLICY general_qa)" != "general_qa" ]; then
  echo "Autonomous voice replies require QA_EXECUTION_MODE=dual_tools and QA_ANSWER_POLICY=general_qa. Create a new conversation after deployment." >&2
  exit 1
fi

case "$(environment_value AGENT_PROVIDER disabled)" in
  disabled) ;;
  *) echo "Native voice routing requires AGENT_PROVIDER=disabled." >&2; exit 1 ;;
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
export VOICE_PROVIDER="$voice_provider"
export WEB_TLS_CERT_FILE="$(environment_value WEB_TLS_CERT_FILE)"
export WEB_TLS_KEY_FILE="$(environment_value WEB_TLS_KEY_FILE)"
export POSTGRES_PASSWORD="$(environment_value POSTGRES_PASSWORD)"
export REDIS_PASSWORD="$(environment_value REDIS_PASSWORD)"
# Let Compose parse these settings from --env-file without inherited overrides.
unset AGENT_PROVIDER AGENT_DEADLINE_MS QA_EXECUTION_MODE QA_ANSWER_POLICY QA_EXTERNAL_FALLBACK_ENABLED \
  QA_MAX_RETRIEVAL_CALLS QA_PROVIDER_ANSWER_TIMEOUT_MS KNOWLEDGE_BASE_IDS WEATHER_PROVIDER WEATHER_MODE DELEGATION_ROUTER_PROVIDER
export ENABLED_TOOLS="$enabled_tools"
for key in LIVEKIT_URL LIVEKIT_NODE_IP LIVEKIT_API_KEY LIVEKIT_API_SECRET; do
  export "$key=$(environment_value "$key")"
done
compose config --quiet
for image in "$api_image" "$web_image"; do
  if ! docker image inspect "$image" >/dev/null 2>&1; then
    echo "Required application image is not available locally: $image" >&2
    echo "Build with docker build or load/pull the exact image before deploying." >&2
    exit 1
  fi
done
if [ "$voice_provider" = sophon_live ]; then
  compose up -d --no-build --wait --wait-timeout 180 postgres redis livekit
else
  # Switching this deployment to NVIDIA also stops its previous Sophon SFU.
  compose --profile sophon stop livekit
  compose up -d --no-build --wait --wait-timeout 180 postgres redis
fi
compose run --rm --no-deps migrate
compose up -d --no-build --wait --wait-timeout 180 --no-deps api
compose exec -T api python /app/scripts/verify_deployment.py --base-url http://127.0.0.1:8000 --expect-autonomous-voice
compose up -d --no-build --wait --wait-timeout 60 --no-deps --force-recreate web
compose ps
