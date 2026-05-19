#!/usr/bin/env bash
# install.sh — LiMem Agent Plugin one-shot installer
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --api-key sk-xxx
#   bash install.sh --target codex --ref v0.2.0 --no-bootstrap
#   bash install.sh --update --target both
#
# 平台：macOS / Linux / WSL；Windows 请用 WSL。

set -euo pipefail

# ============================================================
# 全局变量
# ============================================================

REPO_OWNER="gaooooosh"
REPO_NAME="limem-agent-plugin"
REPO_URL="https://github.com/${REPO_OWNER}/${REPO_NAME}"

REF="main"
API_KEY="${LIMEM_API_KEY:-}"
API_KEY_ARG_SET=0
ACTION="install"
INSTALL_TARGETS="auto"
DO_INIT=1
DO_BOOTSTRAP=1
BOOTSTRAP_SET=0
VERBOSE=0

OS=""
ARCH=""
PYTHON=""
PIPX=""
WORKDIR=""
SRC_DIR=""

# ============================================================
# 输出 / 颜色（仅 stderr 是 tty 时上色）
# ============================================================

if [[ -t 2 ]]; then
  C_RED=$'\033[31m'; C_GREEN=$'\033[32m'; C_YELLOW=$'\033[33m'
  C_BLUE=$'\033[34m'; C_DIM=$'\033[2m';   C_RESET=$'\033[0m'
else
  C_RED=""; C_GREEN=""; C_YELLOW=""; C_BLUE=""; C_DIM=""; C_RESET=""
fi

step() { printf "%s==>%s %s\n" "$C_BLUE"   "$C_RESET" "$*" >&2; }
ok()   { printf "%s ✓ %s %s\n" "$C_GREEN"  "$C_RESET" "$*" >&2; }
warn() { printf "%s ! %s %s\n" "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf "%s ✗ %s %s\n" "$C_RED"    "$C_RESET" "$*" >&2; exit "${2:-1}"; }
dbg()  { (( VERBOSE )) && printf "%s[debug]%s %s\n" "$C_DIM" "$C_RESET" "$*" >&2 || true; }

# ============================================================
# parse_args
# ============================================================

print_help() {
  cat >&2 <<'EOF'
LiMem Agent Plugin 安装器

用法：
  curl -fsSL <url> | bash                            # 默认 main，交互式 bootstrap
  curl -fsSL <url> | bash -s -- --api-key sk-xxx     # 一行装完
  bash install.sh --target codex --no-bootstrap      # 只接入 Codex
  bash install.sh --target claude-code               # 只接入 Claude Code
  bash install.sh --target both                      # 同时接入 Claude Code + Codex
  bash install.sh --update --target both             # 更新 CLI 并刷新两边配置

选项：
  --api-key TOKEN     LiMem API key（也可通过 $LIMEM_API_KEY）
  --ref REF           git 分支或 tag（默认：main）
  --target TARGET      安装目标：auto / claude-code / codex / both（默认：auto）
  --targets TARGETS    同 --target；也接受 claude-code,codex
  --update             更新已安装的 limem-cli，并刷新 hooks / skills（默认不重新 bootstrap）
  --no-init           跳过 limem init
  --bootstrap          即使 --update 也运行 limem bootstrap
  --no-bootstrap      跳过 limem bootstrap
  --verbose, -v       打印调试信息
  --help, -h          显示本说明
EOF
}

normalize_targets() {
  local raw="${1:-auto}"
  raw="${raw// /}"
  raw="$(printf '%s' "$raw" | tr '[:upper:]' '[:lower:]')"

  case "$raw" in
    ""|auto) echo "auto" ;;
    claude|claude-code|claudecode) echo "claude-code" ;;
    codex) echo "codex" ;;
    both|all|claude-code,codex|codex,claude-code|claude,codex|codex,claude)
      echo "claude-code,codex"
      ;;
    *)
      die "未知安装目标：$1（可选：auto / claude-code / codex / both）" 2
      ;;
  esac
}

parse_args() {
  while (( $# )); do
    case "$1" in
      --api-key)        API_KEY="${2:?--api-key 需要一个值}"; API_KEY_ARG_SET=1; shift 2 ;;
      --api-key=*)      API_KEY="${1#*=}"; API_KEY_ARG_SET=1; shift ;;
      --ref)            REF="${2:?--ref 需要一个值}"; shift 2 ;;
      --ref=*)          REF="${1#*=}"; shift ;;
      --target|--targets)
                         INSTALL_TARGETS="$(normalize_targets "${2:?--target 需要一个值}")"; shift 2 ;;
      --target=*|--targets=*)
                         INSTALL_TARGETS="$(normalize_targets "${1#*=}")"; shift ;;
      --update)         ACTION="update"; shift ;;
      --no-init)        DO_INIT=0; shift ;;
      --bootstrap)      DO_BOOTSTRAP=1; BOOTSTRAP_SET=1; shift ;;
      --no-bootstrap)   DO_BOOTSTRAP=0; BOOTSTRAP_SET=1; shift ;;
      --verbose|-v)     VERBOSE=1; shift ;;
      --help|-h)        print_help; exit 0 ;;
      *) die "未知参数：$1（试试 --help）" 2 ;;
    esac
  done

  # 简单合法性
  if [[ "$REF" == */* ]]; then
    die "ref 不能包含斜杠：${REF}（GitHub tarball URL 无法表达带斜杠的 ref）" 2
  fi

  if [[ "$ACTION" == "update" && "$BOOTSTRAP_SET" == "0" && "$API_KEY_ARG_SET" == "0" ]]; then
    DO_BOOTSTRAP=0
  fi
}

print_banner() {
  cat >&2 <<EOF
${C_BLUE}┌─────────────────────────────────────────────┐
│  LiMem Agent Plugin · 安装 / 更新              │
│  ${REPO_URL}                                  │
└─────────────────────────────────────────────┘${C_RESET}
EOF
}

# ============================================================
# detect_platform
# ============================================================

detect_platform() {
  step "检测运行环境"
  local uname_s
  uname_s="$(uname -s)"
  case "$uname_s" in
    Darwin) OS="macos" ;;
    Linux)
      OS="linux"
      if grep -qiE '(microsoft|wsl)' /proc/version 2>/dev/null; then
        OS="wsl"
      fi
      ;;
    *) die "暂不支持的系统：${uname_s}（仅支持 macOS / Linux / WSL）" 10 ;;
  esac
  ARCH="$(uname -m)"
  dbg "OS=${OS}  ARCH=${ARCH}"
  ok "环境：$OS / $ARCH"
}

# ============================================================
# ensure_curl_tar
# ============================================================

ensure_curl_tar() {
  step "检查基础工具（curl / tar）"
  if ! command -v curl >/dev/null 2>&1; then
    die "缺少 curl。安装：
  macOS         : 系统自带（如缺失请装 Xcode CLT：xcode-select --install）
  Debian/Ubuntu : sudo apt update && sudo apt install -y curl
  Fedora        : sudo dnf install -y curl
  Arch          : sudo pacman -S curl" 13
  fi
  if ! command -v tar >/dev/null 2>&1; then
    die "缺少 tar，请用包管理器安装" 13
  fi
  ok "curl / tar 已就绪"
}

# ============================================================
# ensure_python
# ============================================================

python_missing() {
  case "$OS" in
    macos)
      die "未找到 Python ≥ 3.10。建议安装：
  brew install python@3.12
然后重新执行本安装命令。" 11
      ;;
    linux|wsl)
      die "未找到 Python ≥ 3.10。请按发行版安装：
  Debian/Ubuntu : sudo apt update && sudo apt install -y python3 python3-venv python3-pip
  Fedora        : sudo dnf install -y python3 python3-pip
  Arch          : sudo pacman -S python python-pip
然后重新执行本安装命令。" 11
      ;;
  esac
}

ensure_python() {
  step "检查 Python ≥ 3.10"
  local py=""
  # 优先精确版本号，避开 macOS 的 /usr/bin/python3 stub
  for cand in python3.13 python3.12 python3.11 python3.10 python3; do
    if command -v "$cand" >/dev/null 2>&1; then
      py="$cand"
      break
    fi
  done
  [[ -n "$py" ]] || python_missing

  local ver major minor
  ver="$("$py" -c 'import sys;print("%d.%d"%sys.version_info[:2])' 2>/dev/null || echo "0.0")"
  IFS=. read -r major minor <<<"$ver"
  if (( major < 3 || (major == 3 && minor < 10) )); then
    warn "${py} 版本 ${ver} 过低（LiMem 要求 ≥ 3.10）"
    python_missing
  fi
  PYTHON="$py"
  ok "Python：${py} (${ver})"
}

# ============================================================
# ensure_pipx
# ============================================================

ensure_pipx() {
  step "检查 pipx"
  if command -v pipx >/dev/null 2>&1; then
    PIPX="$(command -v pipx)"
    ok "pipx：$PIPX"
    return
  fi

  # 已装但 PATH 未刷新
  local user_base
  user_base="$("$PYTHON" -m site --user-base 2>/dev/null || echo "")"
  if [[ -n "$user_base" && -x "$user_base/bin/pipx" ]]; then
    warn "检测到 $user_base/bin/pipx，但 PATH 暂未包含；本次会话内联注入"
    export PATH="$user_base/bin:$PATH"
    PIPX="$user_base/bin/pipx"
    ok "pipx：$PIPX"
    return
  fi

  warn "未找到 pipx，开始安装"
  local pip_out
  if ! pip_out="$("$PYTHON" -m pip install --user pipx 2>&1)"; then
    if printf '%s' "$pip_out" | grep -q 'externally-managed-environment'; then
      die "当前发行版禁止 pip --user 直接装包（PEP 668）。请改用系统包：
  Debian/Ubuntu : sudo apt install -y pipx
  Fedora        : sudo dnf install -y pipx
然后重新执行本安装命令。" 12
    fi
    printf '%s\n' "$pip_out" >&2
    die "pipx 安装失败" 12
  fi

  "$PYTHON" -m pipx ensurepath >/dev/null 2>&1 || true

  user_base="$("$PYTHON" -m site --user-base)"
  export PATH="$user_base/bin:$PATH"

  if [[ ! -x "$user_base/bin/pipx" ]]; then
    die "pipx 已 pip install 但 $user_base/bin/pipx 不可执行" 12
  fi
  PIPX="$user_base/bin/pipx"

  if ! command -v pipx >/dev/null 2>&1; then
    warn "pipx 已装；本次会话已临时把 $user_base/bin 加进 PATH"
    warn "完成后请 source 你的 shell rc（~/.bashrc / ~/.zshrc）或重开终端"
  fi
  ok "pipx：$PIPX"
}

# ============================================================
# fetch_source
# ============================================================

cleanup_workdir() {
  if [[ -n "${WORKDIR:-}" && -d "$WORKDIR" ]]; then
    rm -rf "$WORKDIR"
    dbg "已清理临时目录 $WORKDIR"
  fi
}

fetch_source() {
  step "获取源码（ref=${REF}）"
  WORKDIR="$(mktemp -d -t limem-install.XXXXXX)"
  trap cleanup_workdir EXIT

  local tarball_url
  local downloaded=0

  tarball_url="${REPO_URL}/archive/refs/heads/${REF}.tar.gz"
  if curl -fsSL "$tarball_url" -o "$WORKDIR/src.tar.gz" 2>/dev/null; then
    downloaded=1
    dbg "命中分支 tarball：$tarball_url"
  else
    tarball_url="${REPO_URL}/archive/refs/tags/${REF}.tar.gz"
    if curl -fsSL "$tarball_url" -o "$WORKDIR/src.tar.gz" 2>/dev/null; then
      downloaded=1
      dbg "命中 tag tarball：$tarball_url"
    fi
  fi
  (( downloaded )) || die "下载源码失败：${REPO_URL} @ ${REF}（既不是分支也不是 tag？）" 13

  tar -xzf "$WORKDIR/src.tar.gz" -C "$WORKDIR" || die "解压源码失败" 13

  # 不推导目录名，直接定位 pyproject.toml
  local pkg_pyproject
  pkg_pyproject="$(find "$WORKDIR" -mindepth 2 -maxdepth 4 -type f -name pyproject.toml -path '*/limem-cli/*' -print -quit)"
  [[ -n "$pkg_pyproject" ]] || die "源码结构异常：找不到 limem-cli/pyproject.toml" 13
  SRC_DIR="$(dirname "$pkg_pyproject")"
  ok "源码：$SRC_DIR"
}

# ============================================================
# install_pkg
# ============================================================

install_pkg() {
  if [[ "$ACTION" == "update" ]]; then
    step "更新 limem CLI"
  else
    step "安装 limem CLI"
  fi

  local already_installed=0
  if "$PIPX" list 2>/dev/null | grep -q 'package limem-cli '; then
    already_installed=1
    warn "检测到已安装的 limem-cli，使用 --force 重建 venv（凭证不变）"
  elif [[ "$ACTION" == "update" ]]; then
    warn "未检测到已安装的 limem-cli，本次按全新安装处理"
  fi

  if (( already_installed )); then
    "$PIPX" install --force "$SRC_DIR" || die "pipx install --force 失败" 14
  else
    if ! "$PIPX" install "$SRC_DIR"; then
      warn "pipx install 失败，回退 pip --user（不推荐）"
      "$PYTHON" -m pip install --user --force-reinstall "$SRC_DIR" \
        || die "pip --user 安装也失败" 14
    fi
  fi

  # 二次确认 limem 可执行
  if ! command -v limem >/dev/null 2>&1; then
    local user_base
    user_base="$("$PYTHON" -m site --user-base)"
    if [[ -x "$user_base/bin/limem" ]]; then
      export PATH="$user_base/bin:$PATH"
    else
      die "limem 已安装但 PATH 中找不到（检查 $user_base/bin 是否在 PATH 中）" 14
    fi
  fi
  ok "limem：$(command -v limem)"
}

# ============================================================
# run_init
# ============================================================

run_init() {
  if (( ! DO_INIT )); then
    warn "已跳过 limem init（--no-init）"
    return
  fi
  step "运行 limem init（刷新 hooks / MCP / skills）"

  local has_claude=0 has_codex=0
  [[ -d "$HOME/.claude" ]] && has_claude=1
  [[ -d "$HOME/.codex"  ]] && has_codex=1

  if [[ "$INSTALL_TARGETS" == "auto" ]] && (( ! has_claude && ! has_codex )); then
    warn "未检测到 ~/.claude 与 ~/.codex"
    warn "limem CLI 已就绪。请先安装 Claude Code 或 Codex CLI，再手动跑：limem init"
    return
  fi

  local init_args=()
  if [[ "$INSTALL_TARGETS" != "auto" ]]; then
    init_args+=(--targets "$INSTALL_TARGETS")
  fi

  if ! limem init "${init_args[@]}"; then
    if [[ "$INSTALL_TARGETS" == "auto" ]]; then
      die "limem init 失败，可手动重试：limem init" 15
    else
      die "limem init 失败，可手动重试：limem init --targets ${INSTALL_TARGETS}" 15
    fi
  fi
  ok "init 完成（targets=${INSTALL_TARGETS}, detected claude=${has_claude}, codex=${has_codex}）"
}

# ============================================================
# run_bootstrap
# ============================================================

can_prompt() {
  [[ -r /dev/tty && -w /dev/tty ]]
}

run_bootstrap() {
  if (( ! DO_BOOTSTRAP )); then
    warn "已跳过 limem bootstrap（--no-bootstrap）"
    return
  fi
  step "运行 limem bootstrap（验证 API key + 解析/创建 db）"

  if [[ -n "$API_KEY" ]]; then
    limem bootstrap --api-key "$API_KEY" || die "bootstrap 失败，请检查 token 与网络" 16
    ok "bootstrap 完成"
    return
  fi

  if can_prompt; then
    # 关键：curl|bash 下 fd 0 被管道占用，让 limem 的 click.prompt(hide_input=True) 能读 tty
    if [[ ! -t 0 ]]; then
      dbg "stdin 非 tty（curl|bash 场景），重定向到 /dev/tty"
      exec < /dev/tty
    fi
    limem bootstrap || die "bootstrap 失败，请检查 token 与网络" 16
    ok "bootstrap 完成"
  else
    warn "当前为非交互环境，跳过 bootstrap"
    local raw_url="https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REF}/install.sh"
    cat >&2 <<EOF
请在本机有 tty 的终端执行：
  ${C_GREEN}limem bootstrap --api-key <YOUR_TOKEN>${C_RESET}
或重新带上 --api-key 重跑安装：
  ${C_GREEN}curl -fsSL ${raw_url} | bash -s -- --api-key sk-xxx${C_RESET}
EOF
  fi
}

# ============================================================
# print_next_steps
# ============================================================

print_next_steps() {
  local done_word="安装"
  [[ "$ACTION" == "update" ]] && done_word="更新"
  cat >&2 <<EOF

${C_GREEN}✓ LiMem Agent Plugin ${done_word}完成${C_RESET}

下一步：
  • 重启 Claude Code / Codex 会话，让 SessionStart hook 注入 LiMem skills
  • 试说一句 "remember 我用 pnpm 不用 npm" 验证记忆链路
  • 查看已存规则：${C_BLUE}/limem.list${C_RESET}
  • 文档：${C_BLUE}${REPO_URL}${C_RESET}

如果新终端找不到 limem 命令：
  source ~/.bashrc      # bash
  source ~/.zshrc       # zsh
或直接重开终端窗口。
EOF
}

# ============================================================
# main
# ============================================================

main() {
  parse_args "$@"
  print_banner
  detect_platform
  ensure_curl_tar
  ensure_python
  ensure_pipx
  fetch_source
  install_pkg
  cleanup_workdir
  trap - EXIT
  run_init
  run_bootstrap
  print_next_steps
}

main "$@"
