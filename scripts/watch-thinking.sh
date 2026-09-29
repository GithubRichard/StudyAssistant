#!/usr/bin/env bash
# 实时查看模型思考过程日志（grader 容器）。
#
# 前提：.env 里 SA_DEBUG_THINKING=1，且容器已用新配置重建
#      （跑一遍 scripts/update-and-logs.sh 即可）。
# 原理：docker compose logs -f grader | grep --line-buffered -A N 模型思考过程
#      （--line-buffered 保证实时输出，不加的话 grep 会攒着不吐）。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="grader"
PATTERN="模型思考过程"
CONTEXT=200       # 每个命中块后面带的行数（思考过程是多行文本）
HISTORY=50        # 进入时先看的历史行数
FOLLOW=1

usage() {
  cat <<'EOF'
用法: scripts/watch-thinking.sh [选项]

实时查看 grader 容器的模型思考过程日志。

选项:
  --context N    每个思考块后面带的行数，默认 200
  --history N    进入时先回看的历史行数，默认 50
  --no-follow    只看历史，不实时跟随
  --service NAME 容器服务名，默认 grader
  -h, --help     显示本帮助

前提:
  .env 里 SA_DEBUG_THINKING=1，且容器已重建（跑 scripts/update-and-logs.sh）。
  只有模型/网关实际返回了 reasoning 字段才有日志；没有返回时看不到任何内容，
  这本身也是有价值的诊断（说明要调模型参数才能拿到思考过程）。
EOF
}

warn() { printf '  ! %s\n' "$*" >&2; }
info() { printf '%s\n' "$*"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --context) shift; [[ "${1:-}" =~ ^[0-9]+$ ]] || { echo "--context 需要整数" >&2; exit 2; }; CONTEXT="$1" ;;
    --context=*) CONTEXT="${1#*=}"; [[ "$CONTEXT" =~ ^[0-9]+$ ]] || { echo "--context 需要整数" >&2; exit 2; } ;;
    --history) shift; [[ "${1:-}" =~ ^[0-9]+$ ]] || { echo "--history 需要整数" >&2; exit 2; }; HISTORY="$1" ;;
    --history=*) HISTORY="${1#*=}"; [[ "$HISTORY" =~ ^[0-9]+$ ]] || { echo "--history 需要整数" >&2; exit 2; } ;;
    --no-follow) FOLLOW=0 ;;
    --service) shift; [[ -n "${1:-}" ]] || { echo "--service 需要一个名字" >&2; exit 2; }; SERVICE="$1" ;;
    --service=*) SERVICE="${1#*=}"; [[ -n "$SERVICE" ]] || { echo "--service 需要一个名字" >&2; exit 2; } ;;
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
if [[ "$thinking" != "1" && "$thinking" != "true" && "$thinking" != "yes" ]]; then
  warn "SA_DEBUG_THINKING 未启用（当前值: '${thinking:-空}'），日志里不会有思考过程。"
  warn "开启：在 .env 加一行 SA_DEBUG_THINKING=1，然后跑 scripts/update-and-logs.sh 重建容器。"
fi

log_level="$(env_val LOG_LEVEL INFO | tr '[:upper:]' '[:lower:]')"
if [[ "$log_level" == "warning" || "$log_level" == "error" || "$log_level" == "critical" ]]; then
  warn "LOG_LEVEL=${log_level} 会屏蔽 INFO 级别的思考日志，建议设为 INFO 后重建容器。"
fi

COMPOSE=()
if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  echo "[x] 未检测到 docker compose" >&2
  exit 1
fi

info "== 实时思考日志 =="
info "服务: $SERVICE ｜ 匹配: $PATTERN ｜ 上下文行数: $CONTEXT"
info "退出：Ctrl+C（容器继续运行）"
info "----------------------------------------"

if [[ "$FOLLOW" == "1" ]]; then
  exec "${COMPOSE[@]}" -f "$REPO_ROOT/docker-compose.yml" logs -f --tail="$HISTORY" "$SERVICE" \
    | grep --line-buffered -A "$CONTEXT" "$PATTERN"
else
  "${COMPOSE[@]}" -f "$REPO_ROOT/docker-compose.yml" logs --tail="$HISTORY" "$SERVICE" \
    | grep -A "$CONTEXT" "$PATTERN"
fi
