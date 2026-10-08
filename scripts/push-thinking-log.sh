#!/usr/bin/env bash
# 把服务器上的 thinking.log 推送到远端 git，方便拉取分析。
#
# 在服务器上运行（仓库根目录的 scripts/ 下）。默认推送到远端 server-logs 分支，
# 不污染 main，也不会干扰 update-and-logs.sh 的部署流程。
#
# 用法: scripts/push-thinking-log.sh [--dry-run] [--branch NAME] [--all]
#
# 实现：git worktree 建临时工作区（主工作树完全不动）→ 拷日志 → 提交 → 推送。
# 日志文件受 .gitignore 的 *.log 规则影响，提交时用 git add -f。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
LOG_DIR="${THINKING_LOG_DIR:-$REPO_ROOT/data/logs}"
MAIN_LOG="$LOG_DIR/thinking.log"
BRANCH="server-logs"
DRY_RUN=0
PUSH_ALL=0
MAX_MB=50

usage() {
  cat <<'EOF'
用法: scripts/push-thinking-log.sh [选项]

把服务器 data/logs/thinking.log 推送到远端 git（默认 server-logs 分支）。

选项:
  --branch NAME   推送的目标分支，默认 server-logs
  --all           一并推送轮转历史（thinking.log.YYYY-MM-DD），默认只推当天
  --dry-run       只打印将要执行的命令，不产生任何副作用
  -h, --help      显示本帮助

说明:
  - 用 git worktree 建临时工作区，主工作树不动；失败自动清理
  - 日志无变化时跳过推送；单文件超过 50MB 时中止（防误推巨大文件）
  - 推送需要该仓库的写权限；权限不足会如实报错
EOF
}

info() { printf '%s\n' "$*"; }
ok() { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*" >&2; }
fail() {
  printf '\n[x] %s\n' "$1" >&2
  if [[ -n "${2:-}" ]]; then printf '    %s\n' "$2" >&2; fi
  exit 1
}

# dry-run 下只打印命令；其余情况如实执行
run() {
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  [dry-run] 将执行: %s\n' "$*"
    return 0
  fi
  "$@"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --branch) shift; [[ -n "${1:-}" ]] || { echo "--branch 需要分支名" >&2; exit 2; }; BRANCH="$1" ;;
    --branch=*) BRANCH="${1#*=}" ;;
    --all) PUSH_ALL=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) printf '未知参数: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

info "== 推送 thinking.log 到远端 =="
if [[ "$DRY_RUN" == "1" ]]; then info "（dry-run：只打印将要执行的命令）"; fi
info "仓库: $REPO_ROOT"
info "分支: $BRANCH"
info ""

# ---------- 前置检查（只读，无副作用） ----------
[[ -d "$REPO_ROOT/.git" ]] || fail "不是 git 仓库: $REPO_ROOT"
command -v git >/dev/null 2>&1 || fail "缺少 git 命令"
[[ -f "$MAIN_LOG" ]] || fail "找不到 $MAIN_LOG" \
  "思考日志未生成：确认服务端 SA_DEBUG_THINKING=1 且有过批改任务（日志在 <data_dir>/logs/ 下）"

files=("$MAIN_LOG")
if [[ "$PUSH_ALL" == "1" ]]; then
  for f in "$LOG_DIR"/thinking.log.[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]; do
    [[ -f "$f" ]] && files+=("$f")
  done
fi
for f in "${files[@]}"; do
  size_mb=$(du -m "$f" | cut -f1)
  if (( size_mb > MAX_MB )); then
    fail "$f 有 ${size_mb}MB，超过 ${MAX_MB}MB 上限，已中止" "确认不是异常膨胀后再推，或调大脚本里的 MAX_MB"
  fi
  ok "$(basename "$f") $(du -h "$f" | cut -f1)"
done

# ---------- 临时工作区（失败自动清理） ----------
WT="$(mktemp -d)"
cleanup() {
  git -C "$REPO_ROOT" worktree remove --force "$WT" 2>/dev/null || true
  git -C "$REPO_ROOT" worktree prune 2>/dev/null || true
}
trap cleanup EXIT

if git ls-remote --exit-code --heads origin "$BRANCH" >/dev/null 2>&1; then
  run git -C "$REPO_ROOT" fetch origin "$BRANCH"
  run git -C "$REPO_ROOT" worktree add --detach "$WT" "origin/$BRANCH"
else
  # 首推：建孤儿分支，分支里只有日志文件，不带 main 的整棵树
  run git -C "$REPO_ROOT" worktree add --detach "$WT" HEAD
  run git -C "$WT" checkout --orphan "$BRANCH"
  run git -C "$WT" rm -rf .
fi

# ---------- 拷日志 → 提交 → 推送 ----------
for f in "${files[@]}"; do
  run cp "$f" "$WT/$(basename "$f")"
done
names=()
for f in "${files[@]}"; do names+=("$(basename "$f")"); done
run git -C "$WT" add -f "${names[@]}"

if [[ "$DRY_RUN" != "1" ]]; then
  if git -C "$WT" diff --cached --quiet; then
    ok "日志与远端一致，无需推送"
    exit 0
  fi
fi

host="$(hostname 2>/dev/null || echo server)"
stamp="$(date '+%F %T')"
total="$(du -ch "${files[@]}" | tail -1 | cut -f1)"
run git -C "$WT" -c user.name="thinking-log" -c user.email="thinking-log@$host" \
  commit -q -m "thinking log $stamp ($host, 共 $total)"
if ! run git -C "$WT" push origin "HEAD:refs/heads/$BRANCH"; then
  fail "推送失败" "检查该仓库是否有写权限（git ls-remote origin 正常不代表能 push）"
fi
if [[ "$DRY_RUN" == "1" ]]; then
  ok "将推送到 origin/$BRANCH（$stamp，共 $total）"
else
  ok "已推送到 origin/$BRANCH（$stamp，共 $total）"
fi
