#!/usr/bin/env bash
# Host entry point; Python and all validation inputs stay in deployed containers.
set -uo pipefail
umask 077
ROOT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
cd "$ROOT" || exit 2
STATE="${TMPDIR:-/tmp}/voice-validation-${UID}-$(printf '%s' "$ROOT" | cksum | cut -d ' ' -f 1)"
mkdir -p "$STATE" || exit 2
COMPOSE=(docker compose --env-file .env -f deploy/compose.production.yaml)
VOICE_CONTAINER=${VOICE_CONTAINER:-nemotron-labs-voicechat}
fail() { printf 'collection=failed stage=%s details=local_only\n' "$1"; exit 2; }
API_CONTAINER=$("${COMPOSE[@]}" ps -q api 2>"$STATE/setup.log") || fail discover_api
[[ -n "$API_CONTAINER" && "$API_CONTAINER" != *$'\n'* ]] || fail api_container_count
now() { docker exec "$API_CONTAINER" python -c 'from datetime import datetime, timezone; print(datetime.now(timezone.utc).isoformat())' 2>>"$STATE/setup.log"; }
verify_file() {
  local container=$1 source=$2 target=$3 expected actual
  expected=$(sha256sum "$source" 2>>"$STATE/setup.log") || return 1
  expected=${expected%% *}
  actual=$(docker exec "$container" sha256sum "$target" 2>>"$STATE/setup.log") || actual=missing
  actual=${actual%% *}
  if [[ "$actual" != "$expected" ]]; then
    printf 'version=failed file=%s expected=%s actual=%s\n' "$target" "$expected" "$actual"
    return 1
  fi
}
check() {
  local file errors=0
  for file in run_native_cuekb_validation.py validate_native_cuekb.py probe_voicechat.py verify_deployment.py inspect_native_answer.py summarize_voice_timeline.py collect_validation_summary.py; do
    verify_file "$API_CONTAINER" "scripts/$file" "/app/scripts/$file" || errors=1
  done
  for file in apps/api/app/voice/relay.py apps/api/app/voice/provider.py apps/api/app/sessions/coordinator.py apps/api/app/agent_runtime/native_input.py apps/api/app/agent_runtime/native_cuekb.py config/voice-qa-prompt.txt; do
    verify_file "$API_CONTAINER" "$file" "/app/$file" || errors=1
  done
  for file in cases.json hello.wav tcp_udp.wav black_pig_crisp_heart.wav golden_yolk.wav; do
    verify_file "$API_CONTAINER" "artifacts/native-cuekb/$file" "/app/validation/native-cuekb/$file" || errors=1
  done
  for file in s2s/audio_server.py s2s/prompt_template.jinja opt/tritonserver/backends/nemotron-voicechat/model.py opt/tritonserver/backends/nemotron-voicechat/data_types.py opt/tritonserver/backends/nemotron-voicechat/runtime_setup.py opt/tritonserver/backends/nemotron-voicechat/checkpoint_utils/load_utils.py; do
    verify_file "$VOICE_CONTAINER" "docs/voicechat/$file" "/$file" || errors=1
  done
  [[ "$errors" == 0 ]] || fail files_not_current
  docker exec "$API_CONTAINER" python -c 'import urllib.request; urllib.request.urlopen("http://127.0.0.1:8000/health/ready", timeout=5).close()' >"$STATE/health.log" 2>&1 || fail api_health
  docker exec "$VOICE_CONTAINER" python -c 'import os; assert os.getenv("VOICECHAT_DIAGNOSTICS") == "1"; assert os.getenv("USE_JINJA_TEMPLATE_PROMPT") == "1"' >"$STATE/voice-env.log" 2>&1 || fail voice_diagnostics_config
  printf 'preparation=passed fixtures=matched voicechat_files=matched helpers=matched api_health=ready\n'
  docker inspect --format 'runtime container={{.Name}} image={{.Image}} started={{.State.StartedAt}}' "$API_CONTAINER" "$VOICE_CONTAINER" 2>>"$STATE/setup.log" || fail runtime_metadata
}
collect() {
  local start=$1 end=$2 dir=$3 cid=${4:-} code=0
  docker logs --timestamps --since "$start" --until "$end" "$API_CONTAINER" >"$dir/api.log" 2>&1 || { printf 'collection=failed stage=api_logs\n'; return 1; }
  docker logs --timestamps --since "$start" --until "$end" "$VOICE_CONTAINER" >"$dir/voicechat.log" 2>&1 || { printf 'collection=failed stage=voicechat_logs\n'; return 1; }
  local args=()
  [[ -z "$cid" ]] || args=(--conversation-id "$cid")
  cat "$dir/api.log" "$dir/voicechat.log" | docker exec -i "$API_CONTAINER" \
    python /app/scripts/collect_validation_summary.py --window-start "$start" --window-end "$end" "${args[@]}" >"$dir/summary.txt" 2>"$dir/collector-error.log" || code=1
  cat "$dir/summary.txt"
  [[ "$code" == 0 ]] || printf 'collection=failed stage=summary\n'
  printf 'local_artifacts=%s raw_logs=not_printed\n' "$dir"
  return "$code"
}
auto_one() {
  local case_id=$1 dir report code=0 meta start end cid
  dir=$(mktemp -d "$STATE/auto-${case_id}-XXXXXX") || return 2
  report="/tmp/native-cuekb/$(basename "$dir")/report.json"
  printf 'operation=auto case=%s\n' "$case_id"
  docker exec "$API_CONTAINER" python /app/scripts/run_native_cuekb_validation.py \
    --case-id "$case_id" --case-timeout 120 --output "$report" \
    --audio-dir "${report%/*}/replies" >"$dir/validation.txt" 2>"$dir/validation-error.log" || code=$?
  # The runner already emits an allowlisted summary; retain that instead of a traceback.
  sed -n '/^preflight=/p; /^window_start=/p; /^case=/p; /^answer_check=/p; /^validation=/p' "$dir/validation.txt"
  printf 'host_exit_code=%s\n' "$code"
  meta=$(docker exec "$API_CONTAINER" python -c 'import json,sys; r=json.load(open(sys.argv[1])); print(r["window_start"], r["window_end"], r["conversation_id"])' "$report" 2>"$dir/report-error.log") || { printf 'collection=unavailable reason=NO_CURRENT_REPORT local_artifacts=%s\n' "$dir"; return 1; }
  read -r start end cid <<< "$meta"
  [[ -n "$start" && -n "$end" && -n "$cid" && "$cid" != None ]] || { printf 'collection=unavailable reason=NO_SESSION\n'; return 1; }
  printf 'window_start=%s window_end=%s conversation_id=%s\n' "$start" "$end" "$cid"
  collect "$start" "$end" "$dir" "$cid" || code=1
  return "$code"
}
case "${1:-check}" in
  check) check ;;
  auto)
    [[ ! -e "$STATE/web-pending" ]] || fail web_capture_pending
    check
    result=0
    case "${2:-golden_yolk}" in
      all) cases=(greeting general_tech black_pig_crisp_heart golden_yolk) ;;
      greeting|general_tech|black_pig_crisp_heart|golden_yolk) cases=("${2:-golden_yolk}") ;;
      *) fail unknown_case ;;
    esac
    for case_id in "${cases[@]}"; do auto_one "$case_id" || result=1; done
    exit "$result"
    ;;
  web-start)
    case "${2:-golden_yolk}" in greeting|general_tech|black_pig_crisp_heart|golden_yolk) ;; *) fail unknown_case ;; esac
    check
    mkdir "$STATE/web-pending" 2>/dev/null || fail web_capture_already_started
    start=$(now) || { rmdir "$STATE/web-pending"; fail clock; }
    printf '%s\n' "$start" >"$STATE/web-pending/start"
    printf '%s\n' "${2:-golden_yolk}" >"$STATE/web-pending/case"
    printf 'web_capture=started case=%s window_start=%s\n' "${2:-golden_yolk}" "$start"
    ;;
  web-end)
    [[ -f "$STATE/web-pending/start" ]] || fail web_capture_not_started
    read -r start <"$STATE/web-pending/start"
    read -r case_id <"$STATE/web-pending/case"
    end=$(now) || fail clock
    dir=$(mktemp -d "$STATE/web-${case_id}-XXXXXX") || fail output_directory
    printf 'operation=web case=%s window_start=%s window_end=%s\n' "$case_id" "$start" "$end"
    result=0
    collect "$start" "$end" "$dir" || result=1
    mv "$STATE/web-pending" "$dir/marker" || fail close_capture
    exit "$result"
    ;;
  *) fail unknown_operation ;;
esac
