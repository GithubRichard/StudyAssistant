#!/usr/bin/env bash
# 实时查看模型思考过程日志。
#
# 前提：.env 里 SA_DEBUG_THINKING=1，且容器已用新配置重建
#      （跑一遍 scripts/update-and-logs.sh 即可）。
# 原理：tail -f data/logs/thinking.log（按天轮转，最多保留 5 天）。
#      只有模型/网关实际返回了 reasoning 字段才有日志；没有返回时看不到任何内容，
#      这本身也是有价值的诊断（说明要调模型参数才能拿到思考过程）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
HISTORY=50        # 进入时先看的历史行数
FOLLOW=1

usage() {
  cat <<'EOF'
用法: scripts/watch-thinking.sh [选项]

实时查看模型思考过程日志（data/logs/thinking.log，按天轮转，保留 5 天）。

选项:
  --history N    进入时先回看的历史行数，默认 50
  --no-follow    只看历史，不实时跟随
  -h, --help     显示本帮助

前提:
  .env 里 SA_DEBUG_THINKING=1，且容器已重建（跑 scripts/update-and-logs.sh）。
  只有模型/网关实际返回了 reasoning 字段才有日志。
EOF
}

warn() { printf '  ! %s\n' "$*" >&2; }
info() { printf '%s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --history) shift; [[ "${1:-}" =~ ^[0-9]+$ ]] || { echo "--history 需要整数" >&2; exit 2; }; HISTORY="$1" ;;
    --history=*) HISTORY="${1#*=}"; [[ "$HISTORY" =~ ^[0-9]+$ ]] || { echo "--history 需要整数" >&2; exit 2; } ;;
    --no-follow) FOLLOW=0 ;;
    --context|--context=*|--service|--service=*) warn "该选项已废弃（日志现为独立文件，无需 grep 过滤），忽略。" ;;
    -h|--help) usage; exit 0 ;;
    *) echo "未知参数: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# 从 .env 取值（只取值，不 source，避免把密钥内容当脚本执行）
env_val() {
  local key="$1" default="$2" v=""
  if [[ -f "$REPO_ROOT/.env" ]]; then
    v="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$REPO_ROOT/.env" 2>/dev/null \
          | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//' | tr -d ' \t\r"'"'"'' || true)"
  fi
  printf '%s' "${v:-$default}"
}

thinking="$(env_val SA_DEBUG_THINKING 0 | tr '[:upper:]' '[:lower:]')"
if [[ "$thinking" != "1" && "$thinking" != "true" && "$thinking" != "yes" && "$thinking" != "on" ]]; then
  warn "SA_DEBUG_THINKING 未启用（当前值: '${thinking:-空}'），日志里不会有思考过程。"
  warn "开启：在 .env 加一行 SA_DEBUG_THINKING=1，然后跑 scripts/update-and-logs.sh 重建容器。"
fi

LOG_FILE="$REPO_ROOT/data/logs/thinking.log"
if [[ ! -f "$LOG_FILE" ]]; then
  warn "日志文件尚不存在：$LOG_FILE（开启开关并跑一次批改后才会生成）。"
  exit 1
fi

info "== 实时思考日志 =="
info "文件: $LOG_FILE"
info "退出：Ctrl+C（容器继续运行）"
info "----------------------------------------"

if [[ "$FOLLOW" == "1" ]]; then
  exec tail -n "$HISTORY" -f "$LOG_FILE"
else
  exec tail -n "$HISTORY" "$LOG_FILE"
fi
