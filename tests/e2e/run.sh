#!/usr/bin/env bash

set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$repo_root"

keep_chats=0
selected_tests=()
while (($#)); do
  case "$1" in
    --keep-chats)
      keep_chats=1
      shift
      ;;
    --test)
      if (($# < 2)) || [[ ! "$2" =~ ^test_[A-Za-z0-9_]+$ ]]; then
        echo "--test requires one LiveThreadModelTests method name." >&2
        exit 2
      fi
      selected_tests+=("$2")
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--keep-chats] [--test test_method ...]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if ! docker compose version >/dev/null 2>&1; then
  echo "Docker Compose is required." >&2
  exit 1
fi
if [[ ! -f .env ]]; then
  echo "Missing .env; copy .env.example and add the test app credentials." >&2
  exit 1
fi
for setting in FEISHU_APP_ID FEISHU_APP_SECRET; do
  if ! grep -Eq "^[[:space:]]*${setting}=.+$" .env; then
    echo "Missing ${setting} in .env." >&2
    exit 1
  fi
done
if [[ ! -s .hermes-secrets/user-access-token ]]; then
  echo "Missing user access token; run acquire_user_access_token.py first." >&2
  exit 1
fi

mkdir -p .hermes-validation .hermes-secrets
chmod 700 .hermes-validation .hermes-secrets

lock_dir="$repo_root/.hermes-validation/e2e.lock"
lock_pid_file="$lock_dir/pid"
if ! mkdir "$lock_dir" 2>/dev/null; then
  prior_pid=""
  if [[ -f "$lock_pid_file" ]]; then
    prior_pid="$(tr -cd '0-9' < "$lock_pid_file")"
  fi
  if [[ -n "$prior_pid" ]] && kill -0 "$prior_pid" 2>/dev/null; then
    echo "Another E2E run is active for this checkout (PID ${prior_pid})." >&2
    exit 1
  fi
  rm -f "$lock_pid_file"
  if ! rmdir "$lock_dir" 2>/dev/null || ! mkdir "$lock_dir"; then
    echo "Cannot acquire the E2E run lock at ${lock_dir}." >&2
    exit 1
  fi
fi
printf '%s\n' "$$" > "$lock_pid_file"

project_name="hermes-lark-e2e-$$"
compose=(docker compose -p "$project_name" -f compose.validation.yaml)
restart_checkpoint="$repo_root/.hermes-validation/hermes-lark-e2e-gateway-restart.json"
rm -f "$restart_checkpoint"
up_started=0

cleanup() {
  status=$?
  trap - EXIT HUP INT TERM
  if ((up_started)); then
    if ((status != 0)); then
      echo "E2E diagnostics (credentials redacted):" >&2
      "${compose[@]}" ps >&2 || true
      "${compose[@]}" logs --no-color --tail 200 gateway model-stub 2>&1 |
        sed -E -f "$repo_root/tests/e2e/redact_diagnostics.sed" >&2 || true
    fi
    "${compose[@]}" down --remove-orphans --timeout 30 >/dev/null 2>&1 || true
  fi
  rm -f "$lock_pid_file"
  rmdir "$lock_dir" 2>/dev/null || true
  exit "$status"
}
trap cleanup EXIT HUP INT TERM

wait_for_gateway() {
  local since="${1:-}"
  local gateway_container=""
  local gateway_logs=""
  local -a logs_command

  for ((attempt = 0; attempt < 90; attempt++)); do
    gateway_container="$("${compose[@]}" ps -q gateway)"
    if [[ -z "$gateway_container" ]] || \
      [[ "$(docker inspect -f '{{.State.Running}}' "$gateway_container" 2>/dev/null || true)" != "true" ]]; then
      return 1
    fi
    logs_command=("${compose[@]}" logs --no-color)
    if [[ -n "$since" ]]; then
      logs_command+=(--since "$since")
    fi
    logs_command+=(gateway)
    gateway_logs="$("${logs_command[@]}" 2>&1 || true)"
    if grep -Fq "connected to wss://" <<< "$gateway_logs"; then
      return 0
    fi
    sleep 1
  done
  return 1
}

run_live_module() {
  local module="$1"
  shift
  "${compose[@]}" run --rm --no-deps \
    "${runner_environment[@]}" \
    "$@" \
    --user "$(id -u):$(id -g)" \
    --workdir /opt/hermes-lark \
    --entrypoint /opt/hermes/.venv/bin/python gateway \
    -m unittest -c -v "$module"
}

run_live_tests() {
  "${compose[@]}" run --rm --no-deps \
    "${runner_environment[@]}" \
    --user "$(id -u):$(id -g)" \
    --workdir /opt/hermes-lark \
    --entrypoint /opt/hermes/.venv/bin/python gateway \
    -m unittest -c -v "$@"
}

"${compose[@]}" build gateway
"${compose[@]}" run --rm --no-deps \
  --user "$(id -u):$(id -g)" \
  --entrypoint /opt/hermes/.venv/bin/python gateway \
  /opt/hermes-lark/tests/e2e/configure_gateway.py

up_started=1
"${compose[@]}" up -d --wait --wait-timeout 60 --force-recreate gateway

if ! wait_for_gateway ""; then
  echo "Timed out waiting for the Feishu WebSocket connection." >&2
  exit 1
fi

runner_environment=(
  -e FEISHU_E2E=1
  -e "FEISHU_E2E_KEEP_CHATS=${keep_chats}"
)

if ((${#selected_tests[@]})); then
  test_ids=()
  for test_name in "${selected_tests[@]}"; do
    test_ids+=(
      "tests.e2e.test_live_thread_model.LiveThreadModelTests.${test_name}"
    )
  done
  run_live_tests "${test_ids[@]}"
  exit 0
fi

run_live_module \
  tests.e2e.test_live_gateway_restart \
  -e FEISHU_E2E_RESTART_PHASE=prepare

"${compose[@]}" stop --timeout 30 gateway
"${compose[@]}" start gateway
gateway_container="$("${compose[@]}" ps -q gateway)"
restart_started="$(
  docker inspect -f '{{.State.StartedAt}}' "$gateway_container"
)"
if ! wait_for_gateway "$restart_started"; then
  echo "Timed out waiting for Feishu after the gateway restart." >&2
  exit 1
fi

run_live_module \
  tests.e2e.test_live_gateway_restart \
  -e FEISHU_E2E_RESTART_PHASE=verify

run_live_module tests.e2e.test_live_thread_model
