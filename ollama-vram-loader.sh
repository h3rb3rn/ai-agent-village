#!/usr/bin/env bash
set -Eeuo pipefail

HOST="${OLLAMA_HOST:-192.168.155.222}"
PORT=''; MODEL=''; CONTEXT=''; UNLOAD_PORT=''; KEEP_ALIVE='10m'
TIMEOUT=900; CONNECT_TIMEOUT=10; JSON_OUTPUT=false

usage() {
  cat <<'EOF'
Usage:
  ollama-vram-loader.sh --port PORT --model MODEL --ctx TOKENS [options]
  ollama-vram-loader.sh --unload-port PORT [options]

Load one model with an explicit num_ctx, or unload every model on one port.
Options: --host HOST --keep-alive VALUE --timeout SEC --connect-timeout SEC --json
Examples:
  ./ollama-vram-loader.sh --port 11434 --model qwen3.5:4b --ctx 65536
  ./ollama-vram-loader.sh --unload-port 11434
EOF
}
die() { printf 'error: %s\n' "$*" >&2; exit 2; }
need() { command -v "$1" >/dev/null 2>&1 || die "missing command: $1"; }
port_ok() { [[ "$1" =~ ^[0-9]+$ ]] && ((1 <= 10#$1 && 10#$1 <= 65535)); }
positive() { [[ "$1" =~ ^[1-9][0-9]*$ ]]; }

while (($#)); do
  case "$1" in
    --host) (($# > 1)) || die '--host requires a value'; HOST=$2; shift 2 ;;
    --port) (($# > 1)) || die '--port requires a value'; PORT=$2; shift 2 ;;
    --model) (($# > 1)) || die '--model requires a value'; MODEL=$2; shift 2 ;;
    --ctx|--context) (($# > 1)) || die '--ctx requires a value'; CONTEXT=$2; shift 2 ;;
    --unload-port) (($# > 1)) || die '--unload-port requires a value'; UNLOAD_PORT=$2; shift 2 ;;
    --keep-alive) (($# > 1)) || die '--keep-alive requires a value'; KEEP_ALIVE=$2; shift 2 ;;
    --timeout) (($# > 1)) || die '--timeout requires a value'; TIMEOUT=$2; shift 2 ;;
    --connect-timeout) (($# > 1)) || die '--connect-timeout requires a value'; CONNECT_TIMEOUT=$2; shift 2 ;;
    --json) JSON_OUTPUT=true; shift ;;
    -h|--help) usage; exit 0 ;;
    *) die "unknown argument: $1" ;;
  esac
done

need curl; need jq
positive "$TIMEOUT" || die '--timeout must be a positive integer'
positive "$CONNECT_TIMEOUT" || die '--connect-timeout must be a positive integer'

if [[ -n "$UNLOAD_PORT" ]]; then
  [[ -z "$PORT" && -z "$MODEL" && -z "$CONTEXT" ]] || die '--unload-port cannot be combined with load options'
  port_ok "$UNLOAD_PORT" || die "invalid port: $UNLOAD_PORT"
  ACTION=unload; PORT=$UNLOAD_PORT
else
  [[ -n "$PORT" && -n "$MODEL" && -n "$CONTEXT" ]] || die 'loading requires --port, --model and --ctx'
  port_ok "$PORT" || die "invalid port: $PORT"
  positive "$CONTEXT" || die '--ctx must be a positive integer'
  ACTION=load
fi
BASE="http://${HOST}:${PORT}"
request() { curl --fail-with-body --silent --show-error --connect-timeout "$CONNECT_TIMEOUT" --max-time "$TIMEOUT" "$@"; }
ps_json() { request "$BASE/api/ps"; }

unload_all() {
  local ps models model payload response remaining
  ps=$(ps_json) || die "cannot query $BASE/api/ps"
  models=$(jq -r '.models[]?.name' <<<"$ps")
  [[ -n "$models" ]] || { printf 'No loaded models on %s\n' "$BASE"; return 0; }
  while IFS= read -r model; do
    [[ -n "$model" ]] || continue
    payload=$(jq -nc --arg model "$model" '{model:$model,keep_alive:0}')
    if response=$(request -H 'Content-Type: application/json' -X POST --data "$payload" "$BASE/api/generate"); then
      if [[ -n "$(jq -r '.error // empty' <<<"$response")" ]]; then
        printf 'unload rejected: %s: %s\n' "$model" "$(jq -r '.error' <<<"$response")" >&2
      else
        printf 'unload requested: %s\n' "$model"
      fi
    else
      printf 'unload request failed: %s\n' "$model" >&2
    fi
  done <<<"$models"
  sleep 1
  remaining=$(ps_json | jq -r '.models[]?.name') || die "cannot verify $BASE/api/ps"
  [[ -z "$remaining" ]] || { printf 'Models still resident on %s:\n%s\n' "$BASE" "$remaining" >&2; return 1; }
  printf 'All models unloaded on %s\n' "$BASE"
}

load_model() {
  local payload response entry
  payload=$(jq -nc --arg model "$MODEL" --argjson context "$CONTEXT" --arg keep_alive "$KEEP_ALIVE" \
    '{model:$model,stream:false,keep_alive:$keep_alive,messages:[{role:"user",content:"Reply exactly: OK"}],options:{num_ctx:$context,num_predict:1}}')
  response=$(request -H 'Content-Type: application/json' -X POST --data "$payload" "$BASE/api/chat") || { printf '%s\n' "$response" >&2; die 'model request failed'; }
  [[ -z "$(jq -r '.error // empty' <<<"$response")" ]] || die "Ollama rejected load: $(jq -r '.error' <<<"$response")"
  entry=$(ps_json | jq -c --arg model "$MODEL" --argjson context "$CONTEXT" \
    '.models[]? | select(.name == $model) | {model:.name,context:$context,total_bytes:.size,vram_bytes:.size_vram,gpu_only:(.size == .size_vram),expires_at}')
  [[ -n "$entry" ]] || die 'model loaded but was not found in /api/ps'
  if [[ "$JSON_OUTPUT" == true ]]; then printf '%s\n' "$entry"; else
    jq -r '"model=" + .model + " context=" + (.context|tostring) + " total_bytes=" + (.total_bytes|tostring) + " vram_bytes=" + (.vram_bytes|tostring) + " gpu_only=" + (.gpu_only|tostring)' <<<"$entry"
  fi
}

if [[ "$ACTION" == unload ]]; then unload_all; else load_model; fi
