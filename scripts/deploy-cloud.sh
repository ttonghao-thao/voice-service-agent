#!/bin/sh
set -eu

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
project_dir=$(CDPATH= cd -- "$script_dir/.." && pwd)
environment_file=${1:-"$project_dir/.env.production"}
compose_file="$project_dir/deploy/compose.production.yaml"
export PRODUCTION_ENV_FILE=$environment_file

if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "Docker Engine with Docker Compose v2 is required." >&2
  exit 1
fi
if [ ! -f "$environment_file" ]; then
  echo "Production environment file not found: $environment_file" >&2
  echo "Copy .env.production.example and replace every placeholder first." >&2
  exit 1
fi
if grep -Eq '(^|=)(REPLACE_|https?://[^/]*example\.com|wss://[^/]*example\.com)' "$environment_file"; then
  echo "Production environment still contains example values or placeholders." >&2
  exit 1
fi
if ! grep -qx 'APP_ENV=production' "$environment_file"; then
  echo "APP_ENV=production is required for cloud deployment." >&2
  exit 1
fi
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
compose up -d --wait --wait-timeout 60 --no-deps web
compose ps
