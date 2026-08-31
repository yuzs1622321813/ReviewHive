#!/usr/bin/env bash
# 启动 ReviewHive 依赖的本地模型服务（llama.cpp）
# 用法: ./scripts/start_models.sh [start|stop|status]
set -euo pipefail

LLAMA_SERVER="${LLAMA_SERVER:-$HOME/llama.cpp/build/bin/llama-server}"
MODELS_DIR="${MODELS_DIR:-$HOME/.cache/modelscope/hub/models}"
RUN_DIR="$(cd "$(dirname "$0")/.." && pwd)/.run"
mkdir -p "$RUN_DIR"

MAIN_GGUF="$MODELS_DIR/Abiray/Qwen3.6-35B-A3B-Q4_K_M-GGUF/Qwen3.6-35B-A3B-Q4_K_M.gguf"
VL_GGUF="$MODELS_DIR/Qwen/Qwen3-VL-8B-Instruct-GGUF/Qwen3VL-8B-Instruct-Q4_K_M.gguf"
VL_MMPROJ="$MODELS_DIR/Qwen/Qwen3-VL-8B-Instruct-GGUF/mmproj-Qwen3VL-8B-Instruct-Q8_0.gguf"

start_one() {
  local name="$1" port="$2"; shift 2
  if [[ -f "$RUN_DIR/$name.pid" ]] && kill -0 "$(cat "$RUN_DIR/$name.pid")" 2>/dev/null; then
    echo "[$name] 已在运行 (pid $(cat "$RUN_DIR/$name.pid"))"
    return
  fi
  echo "[$name] 启动中 -> 127.0.0.1:$port"
  nohup "$@" >"$RUN_DIR/$name.log" 2>&1 &
  echo $! > "$RUN_DIR/$name.pid"
}

wait_healthy() {
  local name="$1" url="$2"
  for _ in $(seq 1 60); do
    if curl -fs "$url" >/dev/null 2>&1; then
      echo "[$name] 就绪"
      return 0
    fi
    sleep 2
  done
  echo "[$name] 启动超时，查看日志: $RUN_DIR/$name.log" >&2
  return 1
}

start() {
  [[ -x "$LLAMA_SERVER" ]] || { echo "未找到 llama-server: $LLAMA_SERVER" >&2; exit 1; }

  start_one main 8080 "$LLAMA_SERVER" \
    --host 0.0.0.0 --port 8080 \
    -m "$MAIN_GGUF" \
    -ngl 999 -c 16384 -t 4 --reasoning off

  if [[ -f "$VL_GGUF" && -f "$VL_MMPROJ" ]]; then
    start_one vision 8082 "$LLAMA_SERVER" \
      --host 0.0.0.0 --port 8082 \
      -m "$VL_GGUF" --mmproj "$VL_MMPROJ" \
      -ngl 999 -c 8192 -t 4
  else
    echo "[vision] 未找到 Qwen3-VL GGUF，跳过多模态服务（可在配置中关闭 vision）"
  fi

  wait_healthy main http://127.0.0.1:8080/health || true
  [[ -f "$RUN_DIR/vision.pid" ]] && wait_healthy vision http://127.0.0.1:8082/health || true

  echo
  echo "说明: 嵌入与重排模型（bge-m3 / bge-reranker-v2-m3）由 Python 进程内加载，无需单独启动。"
  echo "如需换用 Qwen3-Embedding-8B，可另起嵌入服务："
  echo "  $LLAMA_SERVER --host 0.0.0.0 --port 8081 -m <qwen3-embedding.gguf> --embedding --pooling last -c 8192 -ngl 999"
}

stop() {
  for pidfile in "$RUN_DIR"/*.pid; do
    [[ -f "$pidfile" ]] || continue
    pid=$(cat "$pidfile")
    if kill -0 "$pid" 2>/dev/null; then
      kill "$pid" && echo "已停止 $(basename "$pidfile" .pid) (pid $pid)"
    fi
    rm -f "$pidfile"
  done
}

status() {
  for name in main vision; do
    if [[ -f "$RUN_DIR/$name.pid" ]] && kill -0 "$(cat "$RUN_DIR/$name.pid")" 2>/dev/null; then
      echo "[$name] 运行中 (pid $(cat "$RUN_DIR/$name.pid"))"
    else
      echo "[$name] 未运行"
    fi
  done
}

case "${1:-start}" in
  start) start ;;
  stop) stop ;;
  status) status ;;
  *) echo "用法: $0 [start|stop|status]" >&2; exit 1 ;;
esac
