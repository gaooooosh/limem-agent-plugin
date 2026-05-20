#!/usr/bin/env bash
# install.sh — LiMem Agent Plugin one-shot installer
#
# 用法：
#   curl -fsSL https://raw.githubusercontent.com/gaooooosh/limem-agent-plugin/main/install.sh | bash
#   curl -fsSL .../install.sh | bash -s -- --api-key sk-xxx
#   bash install.sh --ref v0.3.1
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
ACTION="auto"
INSTALL_TARGETS="auto"
DO_INIT=1
DO_BOOTSTRAP=1
BOOTSTRAP_SET=0
VERBOSE=0
DRY_RUN=0
INSTALL_NEEDED=1
SKIP_BOOTSTRAP_REASON=""
VERSION_COMPARE="unknown"

OS=""
ARCH=""
PYTHON=""
UV=""
INSTALL_BACKEND=""
BACKEND_PLAN=""
VENV_DIR="${LIMEM_INSTALL_VENV:-$HOME/.local/share/limem-agent-plugin/venv}"
USER_BIN_DIR="${LIMEM_INSTALL_BIN:-$HOME/.local/bin}"
LIMEM_CMD=""
LIMEM_CMD_DIR=""
WORKDIR=""
SRC_DIR=""
INSTALLED_VERSION=""
TARGET_VERSION=""

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
ok()   { printf "%s[OK]%s %s\n" "$C_GREEN"  "$C_RESET" "$*" >&2; }
warn() { printf "%s[!]%s %s\n" "$C_YELLOW" "$C_RESET" "$*" >&2; }
die()  { printf "%s[ERR]%s %s\n" "$C_RED"   "$C_RESET" "$*" >&2; exit "${2:-1}"; }
dbg()  { (( VERBOSE )) && printf "%s[debug]%s %s\n" "$C_DIM" "$C_RESET" "$*" >&2 || true; }

print_kv() {
  printf "  %-14s %s\n" "$1:" "$2" >&2
}

action_label() {
  case "$ACTION" in
    install) echo "安装" ;;
    update) echo "更新" ;;
    refresh) echo "刷新配置" ;;
    downgrade) echo "回退安装" ;;
    *) echo "自动判断" ;;
  esac
}

# ============================================================
# parse_args
# ============================================================

print_help() {
  cat >&2 <<'EOF'
LiMem Agent Plugin 安装器

用法：
  curl -fsSL <url> | bash                            # 自动安装或更新，必要时交互式 bootstrap
  curl -fsSL <url> | bash -s -- --api-key sk-xxx     # 一行装完
  bash install.sh --ref v0.3.1                       # 安装指定 tag 或分支

选项：
  --api-key TOKEN     LiMem API key（也可通过 $LIMEM_API_KEY）
  --ref REF           git 分支或 tag（默认：main）
  --target TARGET      高级选项：auto / claude-code / codex / both（默认：auto）
  --targets TARGETS    同 --target；也接受 claude-code,codex
  --update             兼容旧用法：强制重装当前 ref，并刷新 hooks / skills
  --no-init           跳过 limem init
  --bootstrap          即使已有凭证也运行 limem bootstrap
  --no-bootstrap      跳过 limem bootstrap
  --dry-run           只检测环境、下载源码并显示版本计划，不安装
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
      --update)         ACTION="update"; INSTALL_NEEDED=1; shift ;;
      --no-init)        DO_INIT=0; shift ;;
      --bootstrap)      DO_BOOTSTRAP=1; BOOTSTRAP_SET=1; shift ;;
      --no-bootstrap)   DO_BOOTSTRAP=0; BOOTSTRAP_SET=1; shift ;;
      --dry-run)        DRY_RUN=1; shift ;;
      --verbose|-v)     VERBOSE=1; shift ;;
      --help|-h)        print_help; exit 0 ;;
      *) die "未知参数：$1（试试 --help）" 2 ;;
    esac
  done

  # 简单合法性
  if [[ "$REF" == */* ]]; then
    die "ref 不能包含斜杠：${REF}（GitHub tarball URL 无法表达带斜杠的 ref）" 2
  fi

  # bootstrap 默认由安装器按凭证状态自动判断；用户显式传参时再覆盖。
}

print_banner() {
  cat >&2 <<EOF
${C_BLUE}┌──────────────────────────────────────────────┐${C_RESET}
${C_BLUE}│        LiMem Agent Plugin Installer          │${C_RESET}
${C_BLUE}└──────────────────────────────────────────────┘${C_RESET}
${C_DIM}${REPO_URL}${C_RESET}

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
# install backend
# ============================================================

detect_uv() {
  if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
  fi
}

ensure_venv_support() {
  if "$PYTHON" -m venv --help >/dev/null 2>&1; then
    return
  fi

  die "当前 Python 缺少 venv 支持。请按发行版安装后重试：
  Debian/Ubuntu : sudo apt update && sudo apt install -y python3-venv python3-pip
  Fedora        : sudo dnf install -y python3-pip
  Arch          : sudo pacman -S python python-pip" 12
}

ensure_user_bin_on_path() {
  mkdir -p "$USER_BIN_DIR"
  case ":$PATH:" in
    *":$USER_BIN_DIR:"*) ;;
    *) export PATH="$USER_BIN_DIR:$PATH" ;;
  esac
}

select_install_backend() {
  step "选择 Python 安装方式"
  detect_uv
  if [[ -n "$UV" ]]; then
    INSTALL_BACKEND="uv"
    BACKEND_PLAN="uv tool ($UV)"
    ok "安装方式：uv tool"
    return
  fi

  ensure_venv_support
  ensure_user_bin_on_path
  INSTALL_BACKEND="venv"
  BACKEND_PLAN="自管 venv + pip ($VENV_DIR -> $USER_BIN_DIR)"
  warn "未找到 uv，改用 LiMem 自管 venv 安装"
  ok "安装方式：自管 venv"
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

  tarball_url="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/heads/${REF}"
  if curl -fsSL "$tarball_url" -o "$WORKDIR/src.tar.gz" 2>/dev/null; then
    downloaded=1
    dbg "命中分支 codeload：$tarball_url"
  else
    tarball_url="https://codeload.github.com/${REPO_OWNER}/${REPO_NAME}/tar.gz/refs/tags/${REF}"
    if curl -fsSL "$tarball_url" -o "$WORKDIR/src.tar.gz" 2>/dev/null; then
      downloaded=1
      dbg "命中 tag codeload：$tarball_url"
    else
      tarball_url="${REPO_URL}/archive/refs/heads/${REF}.tar.gz"
      if curl -fL "$tarball_url" -o "$WORKDIR/src.tar.gz"; then
        downloaded=1
        dbg "命中分支 archive：$tarball_url"
      else
        tarball_url="${REPO_URL}/archive/refs/tags/${REF}.tar.gz"
        if curl -fL "$tarball_url" -o "$WORKDIR/src.tar.gz"; then
          downloaded=1
          dbg "命中 tag archive：$tarball_url"
        fi
      fi
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
# show_version_plan
# ============================================================

read_target_version() {
  TARGET_VERSION="$(sed -nE "s/^[[:space:]]*version[[:space:]]*=[[:space:]]*\"([^\"]+)\".*/\\1/p" "$SRC_DIR/pyproject.toml" | head -n 1 || true)"
  [[ -n "$TARGET_VERSION" ]] || TARGET_VERSION="未知"
}

read_installed_version() {
  INSTALLED_VERSION=""
  if ! command -v limem >/dev/null 2>&1 && [[ -x "$USER_BIN_DIR/limem" ]]; then
    export PATH="$USER_BIN_DIR:$PATH"
  fi
  if command -v limem >/dev/null 2>&1; then
    INSTALLED_VERSION="$(limem --version 2>/dev/null | sed -E 's/.* ([0-9][^[:space:]]*)$/\1/' || true)"
  fi

  [[ -n "$INSTALLED_VERSION" ]] || INSTALLED_VERSION="未安装"
}

compare_versions() {
  if [[ "$INSTALLED_VERSION" == "未安装" || "$TARGET_VERSION" == "未知" ]]; then
    VERSION_COMPARE="unknown"
    return
  fi

  VERSION_COMPARE="$("$PYTHON" - "$INSTALLED_VERSION" "$TARGET_VERSION" <<'PY'
import re
import sys


def parts(v: str) -> tuple[list[int], str]:
    base, _, suffix = v.lstrip("v").partition("-")
    nums = [int(x) for x in re.findall(r"\d+", base)]
    return nums, suffix


installed, target = sys.argv[1], sys.argv[2]
a_nums, a_suffix = parts(installed)
b_nums, b_suffix = parts(target)
max_len = max(len(a_nums), len(b_nums))
a_nums.extend([0] * (max_len - len(a_nums)))
b_nums.extend([0] * (max_len - len(b_nums)))
if a_nums < b_nums:
    print("older")
elif a_nums > b_nums:
    print("newer")
elif a_suffix == b_suffix:
    print("same")
else:
    print("different")
PY
)"
}

prompt_yes_no() {
  local question="$1"
  local default="${2:-yes}"
  local hint="[Y/n]"
  local answer=""

  [[ "$default" == "no" ]] && hint="[y/N]"
  if ! can_prompt; then
    [[ "$default" == "yes" ]]
    return
  fi
  printf "%s %s " "$question" "$hint" > /dev/tty
  IFS= read -r answer < /dev/tty || answer=""
  answer="$(printf '%s' "$answer" | tr '[:upper:]' '[:lower:]')"
  if [[ -z "$answer" ]]; then
    [[ "$default" == "yes" ]]
    return
  fi
  [[ "$answer" == "y" || "$answer" == "yes" ]]
}

credentials_present() {
  local cred="$HOME/.config/limem/credentials.json"
  [[ -s "$cred" ]] || return 1
  "$PYTHON" - "$cred" <<'PY' >/dev/null 2>&1
import json
import sys

try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
api_key = data.get("api_key") or ""
db_id = data.get("db_id") or ""
sys.exit(0 if api_key and db_id else 1)
PY
}

decide_install_plan() {
  read_installed_version
  read_target_version
  compare_versions

  if [[ "$ACTION" == "update" ]]; then
    ACTION="update"
    INSTALL_NEEDED=1
  elif [[ "$INSTALLED_VERSION" == "未安装" ]]; then
    ACTION="install"
    INSTALL_NEEDED=1
  elif [[ "$VERSION_COMPARE" == "older" || "$VERSION_COMPARE" == "different" ]]; then
    ACTION="update"
    INSTALL_NEEDED=1
  elif [[ "$VERSION_COMPARE" == "same" ]]; then
    ACTION="refresh"
    INSTALL_NEEDED=0
  elif [[ "$VERSION_COMPARE" == "newer" ]]; then
    ACTION="refresh"
    INSTALL_NEEDED=0
    if prompt_yes_no "已安装版本 ${INSTALLED_VERSION} 高于目标版本 ${TARGET_VERSION}，是否回退到目标版本？" "no"; then
      ACTION="downgrade"
      INSTALL_NEEDED=1
    else
      warn "保持当前版本，仅刷新 hooks / MCP / skills"
    fi
  else
    ACTION="update"
    INSTALL_NEEDED=1
    warn "无法可靠比较版本，默认重装当前 ref"
  fi

  if [[ "$BOOTSTRAP_SET" == "0" && "$API_KEY_ARG_SET" == "0" ]]; then
    if credentials_present; then
      DO_BOOTSTRAP=0
      SKIP_BOOTSTRAP_REASON="已有凭证"
    else
      DO_BOOTSTRAP=1
    fi
  fi
}

detect_backend_plan() {
  if [[ -n "${INSTALL_BACKEND:-}" ]]; then
    return
  fi
  detect_uv
  if [[ -n "$UV" ]]; then
    BACKEND_PLAN="uv tool ($UV)"
  else
    BACKEND_PLAN="自管 venv + pip ($VENV_DIR -> $USER_BIN_DIR)"
  fi
}

bootstrap_plan_text() {
  if (( DO_BOOTSTRAP )); then
    if [[ -n "$API_KEY" ]]; then
      echo "是（使用已提供的 API key）"
    else
      echo "是（有 tty 时交互输入）"
    fi
  else
    echo "否"
  fi
}

show_version_plan() {
  detect_backend_plan

  cat >&2 <<EOF

${C_BLUE}┌─ 安装计划${C_RESET}
EOF
  print_kv "动作" "$(action_label)"
  print_kv "源码" "${REPO_OWNER}/${REPO_NAME}@${REF}"
  print_kv "当前版本" "$INSTALLED_VERSION"
  print_kv "目标版本" "$TARGET_VERSION"
  print_kv "版本判断" "$VERSION_COMPARE"
  print_kv "安装方式" "$BACKEND_PLAN"
  print_kv "重装 CLI" "$([[ "$INSTALL_NEEDED" == "1" ]] && echo 是 || echo 否)"
  print_kv "Agent 目标" "$INSTALL_TARGETS"
  print_kv "刷新配置" "$([[ "$DO_INIT" == "1" ]] && echo 是 || echo 否)"
  print_kv "Bootstrap" "$(bootstrap_plan_text)"
  printf "%s└────────────%s\n\n" "$C_BLUE" "$C_RESET" >&2
}

# ============================================================
# install_pkg
# ============================================================

install_pkg() {
  if (( ! INSTALL_NEEDED )); then
    step "检查 limem CLI"
    LIMEM_CMD="$(command -v limem 2>/dev/null || true)"
    [[ -n "$LIMEM_CMD" ]] || die "内部错误：计划刷新配置，但 PATH 中找不到 limem" 14
    LIMEM_CMD_DIR="$(dirname "$LIMEM_CMD")"
    ok "已是目标版本：$LIMEM_CMD ($INSTALLED_VERSION)"
    return
  fi

  step "$(action_label) limem CLI"

  case "$INSTALL_BACKEND" in
    uv)
      if ! install_pkg_uv; then
        warn "uv tool 安装失败，切换到 LiMem 自管 venv"
        ensure_venv_support
        INSTALL_BACKEND="venv"
        BACKEND_PLAN="自管 venv + pip ($VENV_DIR -> $USER_BIN_DIR)"
        install_pkg_venv
      fi
      ;;
    venv) install_pkg_venv ;;
    *) die "内部错误：未知安装方式 ${INSTALL_BACKEND:-空}" 14 ;;
  esac

  # 二次确认 limem 可执行
  if ! command -v limem >/dev/null 2>&1; then
    if [[ -x "$USER_BIN_DIR/limem" ]]; then
      export PATH="$USER_BIN_DIR:$PATH"
    else
      die "limem 已安装但 PATH 中找不到（检查 $USER_BIN_DIR 是否在 PATH 中）" 14
    fi
  fi
  LIMEM_CMD="$(command -v limem)"
  LIMEM_CMD_DIR="$(dirname "$LIMEM_CMD")"
  ok "limem：$LIMEM_CMD"
}

install_pkg_uv() {
  local uv_bin_dir
  uv_bin_dir="$("$UV" tool dir --bin 2>/dev/null || true)"
  if [[ -n "$uv_bin_dir" ]]; then
    export PATH="$uv_bin_dir:$PATH"
  fi

  "$UV" tool install --force --python "$PYTHON" "$SRC_DIR" || return 1
}

install_pkg_venv() {
  ensure_user_bin_on_path
  mkdir -p "$(dirname "$VENV_DIR")" "$USER_BIN_DIR"

  if [[ -d "$VENV_DIR" ]]; then
    warn "检测到已有自管 venv，重新安装 limem-cli（凭证不变）"
  else
    "$PYTHON" -m venv "$VENV_DIR" || die "创建 venv 失败：$VENV_DIR" 14
  fi

  "$VENV_DIR/bin/python" -m pip install --upgrade pip >/dev/null \
    || die "venv 内升级 pip 失败" 14
  "$VENV_DIR/bin/python" -m pip install --force-reinstall "$SRC_DIR" \
    || die "venv 安装 limem-cli 失败" 14

  local bin
  for bin in limem limem-mcp limemd limem-statusline; do
    [[ -x "$VENV_DIR/bin/$bin" ]] || die "安装后缺少可执行文件：$bin" 14
    ln -sfn "$VENV_DIR/bin/$bin" "$USER_BIN_DIR/$bin"
  done
  ok "已链接命令到 $USER_BIN_DIR"
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

  if [[ "$INSTALL_TARGETS" == "auto" ]]; then
    limem init
  else
    limem init --targets "$INSTALL_TARGETS"
  fi
  local init_status=$?
  if (( init_status != 0 )); then
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
    if [[ -n "$SKIP_BOOTSTRAP_REASON" ]]; then
      ok "已跳过 bootstrap（${SKIP_BOOTSTRAP_REASON}）"
    else
      warn "已跳过 limem bootstrap（--no-bootstrap）"
    fi
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
  local done_word
  done_word="$(action_label)"
  cat >&2 <<EOF

${C_GREEN}┌─ 完成${C_RESET}
EOF
  print_kv "结果" "LiMem Agent Plugin ${done_word}完成"
  print_kv "版本" "${INSTALLED_VERSION} -> ${TARGET_VERSION}"
  print_kv "安装方式" "$BACKEND_PLAN"
  print_kv "命令路径" "${LIMEM_CMD:-$(command -v limem 2>/dev/null || echo 未找到)}"
  printf "%s└────────%s\n" "$C_GREEN" "$C_RESET" >&2
  cat >&2 <<EOF

下一步：
  - 重启 Claude Code / Codex 会话，让 SessionStart hook 注入 LiMem skills
  - 试说一句 "remember 我用 pnpm 不用 npm" 验证记忆链路
  - 查看已存规则：${C_BLUE}/limem.list${C_RESET}
  - 文档：${C_BLUE}${REPO_URL}${C_RESET}

如果新终端找不到 limem 命令：
  export PATH="${LIMEM_CMD_DIR:-$USER_BIN_DIR}:\$PATH"
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
  fetch_source
  decide_install_plan
  show_version_plan
  if (( DRY_RUN )); then
    warn "dry-run：已停止在安装前"
    cleanup_workdir
    trap - EXIT
    return
  fi
  if (( INSTALL_NEEDED )); then
    select_install_backend
  fi
  install_pkg
  cleanup_workdir
  trap - EXIT
  run_init
  run_bootstrap
  print_next_steps
}

main "$@"
