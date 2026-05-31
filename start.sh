#!/usr/bin/env bash
# HITFM Local - 一键启动脚本
#
# 启动流程：
#   1. 检查 ollama 服务（不在跑就 brew services start）
#   2. 后台启动 IndexTTS API server（带 --warmup-ref-audio 指向当前 host 的参考音色）
#   3. 等 IndexTTS /health 返回 ok
#   4. 启动 demo_llm_runtime.py
#
# Ctrl+C 时自动清理 IndexTTS server 进程。
#
# 用法:
#   ./start.sh                                  # 用 config.yaml 配置启动
#   ./start.sh --no-indextts-warmup             # 不做 warmup（更快进 ready，但首次合成慢）

set -euo pipefail

# uv 默认装在 ~/.local/bin。start.sh 通过 nohup 在后台子 shell 里调 uv 起 IndexTTS，
# 那个环境不一定继承交互式 shell 的 PATH（典型报错：nohup: uv: No such file or directory）。
# 这里显式把 uv 的安装目录加进 PATH，保证后台子进程也能找到 uv。
export PATH="$HOME/.local/bin:$PATH"

HITFM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEXTTS_DIR="$HITFM_DIR/external/index-tts"
INDEXTTS_LOG="$HITFM_DIR/.indextts_server.log"
INDEXTTS_PID_FILE="$HITFM_DIR/.indextts_server.pid"

DO_WARMUP=true
EXTRA_PY_ARGS=()

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[start]${NC} $*"; }
ok()      { echo -e "${GREEN}[start]${NC} ✓ $*"; }
warn()    { echo -e "${YELLOW}[start]${NC} ⚠ $*"; }
err()     { echo -e "${RED}[start]${NC} ✗ $*" >&2; }

while [[ $# -gt 0 ]]; do
    case "$1" in
        --no-indextts-warmup)
            DO_WARMUP=false
            shift
            ;;
        --)
            shift
            EXTRA_PY_ARGS=("$@")
            break
            ;;
        -h|--help)
            sed -n '2,18p' "$0"
            echo
            echo "示例:"
            echo "  ./start.sh                                # 默认启动"
            echo "  ./start.sh -- --max-segments 50           # 传参给 demo_llm_runtime.py"
            exit 0
            ;;
        *)
            err "未知参数: $1（如果想传给 python 程序，用 '-- --xxx'）"
            exit 1
            ;;
    esac
done

# ========== 系统检查 ==========
if [[ "$(uname)" != "Darwin" ]]; then
    err "HITFM 当前只支持 macOS"
    exit 1
fi

# ========== 1. ollama ==========
info "1. 检查 ollama 服务 ..."
if curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
    ok "ollama 服务已在跑"
else
    info "启动 ollama 后台服务 ..."
    if ! command -v ollama >/dev/null 2>&1; then
        err "ollama 未安装。请先跑 ./install.sh"
        exit 1
    fi
    brew services start ollama
    for _ in $(seq 1 10); do
        sleep 1
        if curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
            break
        fi
    done
    if ! curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
        err "ollama 启动超时"
        exit 1
    fi
    ok "ollama 服务启动完成"
fi

# ========== 2. 解析当前 host 的 voice_ref ==========
info "2. 读取 config.yaml 当前 host ..."
if [[ ! -f "$HITFM_DIR/config.yaml" ]]; then
    err "找不到 config.yaml"
    exit 1
fi

# 简单解析 YAML 顶层的 host 字段——避免引入额外依赖
HOST_ID=$(awk '/^host:/ {print $2; exit}' "$HITFM_DIR/config.yaml" | tr -d '"' | tr -d "'")
if [[ -z "$HOST_ID" ]]; then
    err "config.yaml 里没找到 host 字段"
    exit 1
fi

HOST_DIR="$HITFM_DIR/hosts/$HOST_ID"
if [[ ! -d "$HOST_DIR" ]]; then
    err "找不到主持人目录: $HOST_DIR"
    exit 1
fi

# 找 voice_ref.*
VOICE_REF=""
for ext in wav mp3 flac m4a; do
    candidate="$HOST_DIR/voice_ref.$ext"
    if [[ -f "$candidate" ]]; then
        VOICE_REF="$candidate"
        break
    fi
done
if [[ -z "$VOICE_REF" ]]; then
    err "找不到主持人参考音频: $HOST_DIR/voice_ref.{wav,mp3,flac,m4a}"
    exit 1
fi
ok "当前 host: $HOST_ID (参考音色: $(basename "$VOICE_REF"))"

# ========== 3. IndexTTS API server ==========
info "3. 启动 IndexTTS API server ..."

if [[ ! -d "$INDEXTTS_DIR" ]]; then
    err "找不到 IndexTTS 目录 ${INDEXTTS_DIR}。请先跑 ./install.sh"
    exit 1
fi

# 已经在跑就跳过
if curl -sf http://127.0.0.1:9881/health >/dev/null 2>&1; then
    ok "IndexTTS API server 已在跑（http://127.0.0.1:9881）"
else
    # 启动后台 server
    WARMUP_ARGS=""
    if [[ "$DO_WARMUP" == "true" ]]; then
        WARMUP_ARGS="--warmup-ref-audio $VOICE_REF"
    fi
    
    info "在后台启动 IndexTTS server（日志: .indextts_server.log）..."
    (
        cd "$INDEXTTS_DIR" && \
        nohup uv run python api_server.py $WARMUP_ARGS \
            > "$INDEXTTS_LOG" 2>&1 &
        echo $! > "$INDEXTTS_PID_FILE"
    )
    
    # 等 server 起来。
    # IndexTTS-2 首次冷启动要把几个 GB 权重读进来 + 初始化 GPT/s2mel/BigVGAN/Qwen 情感模型，
    # 在 Mac（CPU/MPS）上可能要 3-10 分钟，所以超时给到 600s，并每 10s 报一次进度。
    INDEXTTS_STARTUP_TIMEOUT=600
    info "等 IndexTTS server 进入 ready（首次冷启动较慢，最多等 ${INDEXTTS_STARTUP_TIMEOUT}s）..."
    for i in $(seq 1 "$INDEXTTS_STARTUP_TIMEOUT"); do
        if curl -sf http://127.0.0.1:9881/health >/dev/null 2>&1; then
            ok "IndexTTS API server 已就绪"
            break
        fi
        # 每 10s 报一次进度，让用户知道没卡死
        if (( i % 10 == 0 )); then
            info "  仍在加载模型... 已等 ${i}s（日志: tail -f $INDEXTTS_LOG）"
        fi
        if [[ $i -eq "$INDEXTTS_STARTUP_TIMEOUT" ]]; then
            err "IndexTTS server 启动超时（${INDEXTTS_STARTUP_TIMEOUT}s），查日志: tail -50 $INDEXTTS_LOG"
            exit 1
        fi
        sleep 1
    done
fi

# ========== 4. 清理函数 ==========
cleanup_indextts() {
    if [[ -f "$INDEXTTS_PID_FILE" ]]; then
        local pid
        pid=$(cat "$INDEXTTS_PID_FILE")
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            info "清理 IndexTTS server (pid=$pid)..."
            kill -TERM "$pid" 2>/dev/null || true
            sleep 1
            kill -KILL "$pid" 2>/dev/null || true
        fi
        rm -f "$INDEXTTS_PID_FILE"
    fi
    # 顺手杀掉 IndexTTS 用 uv 起的 python 子进程（防御性兜底）
    pkill -f "$INDEXTTS_DIR.*api_server.py" 2>/dev/null || true
}

trap cleanup_indextts EXIT INT TERM

# ========== 5. 启动 HITFM 主程序 ==========
info "5. 启动电台主循环 ..."
echo
cd "$HITFM_DIR"

# 用 install.sh 创建的 .venv 里的 python（依赖 openai / pyyaml 都装在那里）。
# 系统自带的 python3 没有这些依赖，直接跑会 ModuleNotFoundError。
PYTHON_BIN="$HITFM_DIR/.venv/bin/python"
if [[ ! -x "$PYTHON_BIN" ]]; then
    warn "找不到 .venv（$PYTHON_BIN），回退到系统 python3——如果缺依赖请重跑 ./install.sh"
    PYTHON_BIN="python3"
fi
"$PYTHON_BIN" demo_llm_runtime.py "${EXTRA_PY_ARGS[@]+"${EXTRA_PY_ARGS[@]}"}" || true

# trap 会在退出时自动清理 IndexTTS