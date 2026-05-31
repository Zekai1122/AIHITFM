#!/usr/bin/env bash
# HITFM Local - 一键安装脚本
# 仅支持 macOS。其他系统请等待后续支持。
#
# 用法:
#   ./install.sh                          # 默认装 qwen2.5:7b-instruct
#   ./install.sh --model qwen2.5:14b      # 改用更大的模型
#   ./install.sh --model llama3.2:3b      # 用更小的模型（性能较弱的机器）
#   ./install.sh --skip-indextts          # 已经装过 IndexTTS 跳过
#   ./install.sh --skip-ollama-pull       # 已经拉过模型跳过
#   ./install.sh --indextts-mirror modelscope  # 国内用户用 ModelScope 镜像
#
# 脚本是 idempotent 的——每步都先检查是否已完成，已完成就跳过。

set -euo pipefail

# ========== 默认参数 ==========
LLM_MODEL="qwen2.5:7b-instruct"
SKIP_INDEXTTS=false
SKIP_OLLAMA_PULL=false
INDEXTTS_MIRROR="huggingface"   # huggingface | modelscope
HITFM_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INDEXTTS_DIR="$HITFM_DIR/external/index-tts"

# ========== 颜色输出 ==========
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
BOLD='\033[1m'
NC='\033[0m'

info()    { echo -e "${BLUE}[install]${NC} $*"; }
ok()      { echo -e "${GREEN}[install]${NC} ✓ $*"; }
warn()    { echo -e "${YELLOW}[install]${NC} ⚠ $*"; }
err()     { echo -e "${RED}[install]${NC} ✗ $*" >&2; }
section() { echo; echo -e "${BOLD}━━━ $* ━━━${NC}"; }

# ========== 解析参数 ==========
while [[ $# -gt 0 ]]; do
    case "$1" in
        --model)
            LLM_MODEL="$2"
            shift 2
            ;;
        --skip-indextts)
            SKIP_INDEXTTS=true
            shift
            ;;
        --skip-ollama-pull)
            SKIP_OLLAMA_PULL=true
            shift
            ;;
        --indextts-mirror)
            INDEXTTS_MIRROR="$2"
            shift 2
            ;;
        -h|--help)
            sed -n '2,20p' "$0"
            exit 0
            ;;
        *)
            err "未知参数: $1"
            exit 1
            ;;
    esac
done

# ========== 1. 系统检查 ==========
section "1. 系统检查"

if [[ "$(uname)" != "Darwin" ]]; then
    err "HITFM 当前只支持 macOS。检测到: $(uname)"
    err "Linux/Windows 支持在后续版本规划中。"
    exit 1
fi
ok "macOS 检测通过"

# ========== 2. Homebrew ==========
section "2. Homebrew"

if ! command -v brew >/dev/null 2>&1; then
    err "未检测到 Homebrew。请先安装 Homebrew："
    echo
    echo '    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"'
    echo
    err "安装完成后请重新跑 ./install.sh"
    exit 1
fi
ok "Homebrew 已安装"

# ========== 3. 基础工具：git / git-lfs / ffmpeg ==========
section "3. 基础工具"

install_brew_if_missing() {
    local pkg="$1"
    local cmd="${2:-$1}"
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$pkg 已安装"
    else
        info "用 brew 安装 $pkg ..."
        brew install "$pkg"
        ok "$pkg 安装完成"
    fi
}

install_brew_if_missing git
install_brew_if_missing git-lfs
# git-lfs 需要 init 一次
if ! git lfs version >/dev/null 2>&1; then
    info "初始化 git-lfs ..."
    git lfs install
fi
install_brew_if_missing ffmpeg

# ========== 4. uv（Python 包管理器，IndexTTS 用） ==========
section "4. uv"

if command -v uv >/dev/null 2>&1; then
    ok "uv 已安装：$(uv --version)"
else
    info "通过官方脚本安装 uv ..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # 把 uv 加进当前 shell 的 PATH（官方脚本装到 ~/.local/bin）
    export PATH="$HOME/.local/bin:$PATH"
    if ! command -v uv >/dev/null 2>&1; then
        err "uv 安装完成但 PATH 里找不到。重启 shell 后再跑一次本脚本。"
        exit 1
    fi
    ok "uv 安装完成：$(uv --version)"
fi

# ========== 5. Ollama + LLM 模型 ==========
section "5. Ollama"

install_brew_if_missing ollama

# 确保 ollama 服务在跑
if pgrep -q ollama; then
    ok "ollama 服务已在跑"
else
    info "启动 ollama 后台服务 ..."
    brew services start ollama
    # 等服务真正起来
    for _ in $(seq 1 10); do
        sleep 1
        if curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
            break
        fi
    done
    if ! curl -sf http://localhost:11434/api/version >/dev/null 2>&1; then
        err "ollama 服务启动失败，请手动跑：brew services start ollama"
        exit 1
    fi
    ok "ollama 服务已启动"
fi

if [[ "$SKIP_OLLAMA_PULL" == "true" ]]; then
    warn "跳过 LLM 模型拉取（--skip-ollama-pull）"
else
    # 已经有了就跳过
    if ollama list 2>/dev/null | awk 'NR>1 {print $1}' | grep -Fxq "$LLM_MODEL"; then
        ok "LLM 模型 $LLM_MODEL 已存在"
    else
        info "拉取 LLM 模型 $LLM_MODEL（首次较慢，几 GB 大小）..."
        ollama pull "$LLM_MODEL"
        ok "LLM 模型拉取完成"
    fi
fi

# ========== 6. IndexTTS ==========
section "6. IndexTTS"

if [[ "$SKIP_INDEXTTS" == "true" ]]; then
    warn "跳过 IndexTTS 安装（--skip-indextts）"
else
    # 6.1 clone 仓库
    if [[ -d "$INDEXTTS_DIR/.git" ]]; then
        ok "IndexTTS 仓库已 clone（$INDEXTTS_DIR）"
    else
        info "clone IndexTTS 仓库到 external/index-tts ..."
        mkdir -p "$HITFM_DIR/external"
        git clone https://github.com/index-tts/index-tts.git "$INDEXTTS_DIR"
        ok "IndexTTS 仓库 clone 完成"
    fi
    
    # 6.2 同步依赖
    info "用 uv 同步 IndexTTS 依赖（首次较慢）..."
    (cd "$INDEXTTS_DIR" && uv sync --all-extras)
    ok "IndexTTS 依赖安装完成"
    
    # 6.3 下载模型权重
    INDEXTTS_CKPT_DIR="$INDEXTTS_DIR/checkpoints"
    # 简单存在性判断：config.yaml 在 checkpoints 下就视为下载过了
    if [[ -f "$INDEXTTS_CKPT_DIR/config.yaml" ]]; then
        ok "IndexTTS-2 模型权重已存在（$INDEXTTS_CKPT_DIR）"
    else
        info "下载 IndexTTS-2 模型权重到 $INDEXTTS_CKPT_DIR（约 5-7 GB）..."
        if [[ "$INDEXTTS_MIRROR" == "modelscope" ]]; then
            # 用 ModelScope，适合国内
            uv tool install "modelscope" --quiet 2>/dev/null || uv tool install "modelscope"
            (cd "$INDEXTTS_DIR" && uv tool run modelscope download \
                --model IndexTeam/IndexTTS-2 --local_dir checkpoints)
        else
            # 用 HuggingFace（默认）
            uv tool install "huggingface-hub[cli,hf_xet]" --quiet 2>/dev/null || \
                uv tool install "huggingface-hub[cli,hf_xet]"
            (cd "$INDEXTTS_DIR" && uv tool run hf download \
                IndexTeam/IndexTTS-2 --local-dir=checkpoints)
        fi
        ok "IndexTTS-2 模型权重下载完成"
    fi
    
    # 6.4 复制 HITFM 自己的 api_server.py 到 IndexTTS 目录
    if [[ -f "$HITFM_DIR/external/api_server.py" ]]; then
        cp "$HITFM_DIR/external/api_server.py" "$INDEXTTS_DIR/api_server.py"
        ok "已复制 api_server.py 到 IndexTTS 目录"
    else
        warn "找不到 external/api_server.py，跳过复制"
    fi
fi

# ========== 7. HITFM 自身的 Python 依赖 ==========
section "7. HITFM Python 依赖"

# 用系统 python3 + pip 装（HITFM 自己只依赖 openai 和 pyyaml）
# 不用 uv 是因为 HITFM 本身没有 pyproject.toml；保持现有 requirements.txt 风格
if [[ -f "$HITFM_DIR/requirements.txt" ]]; then
    info "安装 HITFM Python 依赖（pip3）..."
    python3 -m pip install --user --upgrade -r "$HITFM_DIR/requirements.txt"
    ok "HITFM Python 依赖安装完成"
else
    warn "找不到 requirements.txt，跳过"
fi

# ========== 完成 ==========
section "✓ 全部安装完成"
echo
echo "下一步："
echo "  1. 确认 hosts/ 下有你想用的主持人（默认 hosts/default/）"
echo "  2. 检查 config.yaml 的设置（特别是 host 和 llm.ollama.model）"
echo "  3. 跑 ./start.sh 启动电台"
echo
echo "如遇问题，先看看 README 的「故障排查」一节，再 issue 报到 GitHub。"