#!/usr/bin/env bash
# 一键更新学习助手服务并进入日志（服务器 Docker 部署）。
#
# 顺序：拉取最新代码 → 同步技能副本到 Hermes profile → 重建 grader 容器 → 跟随容器日志。
# 明确不做：不改 config.yaml / .env（两者不进版本库，git pull 不会更新服务器上那两份）、
#           不做数据库迁移、不改 git 历史、不删任何目录。
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE="grader"
SKILL_SRC="$REPO_ROOT/hermes/skills/leo-study-assistant"
SKILL_DIR="${HOME:-/root}/.hermes/skills/leo-study-assistant"
TAIL="100"
READY_TIMEOUT="60"
READY_INTERVAL="3"
DEFAULT_PORT="8000"

DRY_RUN=0
SYNC_SKILL=1
FOLLOW=1
FORCE_REBUILD=0
ALLOW_DIRTY=0

usage() {
  cat <<'EOF'
用法: scripts/update-and-logs.sh [选项]

一键更新（服务器 Docker 部署）：
  git pull --ff-only → 同步技能副本到 Hermes profile → 重建 grader 容器 → 跟随容器日志

选项:
  --dry-run          只打印将要执行的命令，不产生任何副作用
  --no-skill         不同步 hermes/skills/leo-study-assistant 到 Hermes profile
  --no-follow        更新完不跟随日志（脚本化调用时用）
  --force-rebuild    即使没有新提交也重建容器
  --allow-dirty      工作区有未提交改动时也继续（默认中止）
  --tail N           日志尾部行数，默认 100
  --skill-dir DIR    技能同步目标目录，默认 ~/.hermes/skills/leo-study-assistant
  -h, --help         显示本帮助

说明:
  - 不会改动 config.yaml 与 .env（二者不进版本库，git pull 不会更新服务器上那两份）
  - 不做数据库迁移；迁移见 README 里的 scripts/migrate_workspace_accounts.py
  - 跟随日志时 Ctrl+C 只退出查看，容器继续运行
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
    --dry-run) DRY_RUN=1 ;;
    --no-skill) SYNC_SKILL=0 ;;
    --no-follow) FOLLOW=0 ;;
    --force-rebuild) FORCE_REBUILD=1 ;;
    --allow-dirty) ALLOW_DIRTY=1 ;;
    --tail)
      shift
      [[ -n "${1:-}" && "$1" =~ ^[0-9]+$ ]] || { printf '--tail 需要一个整数\n' >&2; exit 2; }
      TAIL="$1"
      ;;
    --tail=*)
      TAIL="${1#*=}"
      [[ "$TAIL" =~ ^[0-9]+$ ]] || { printf '--tail 需要一个整数\n' >&2; exit 2; }
      ;;
    --skill-dir)
      shift
      [[ -n "${1:-}" ]] || { printf '--skill-dir 需要一个目录\n' >&2; exit 2; }
      SKILL_DIR="$1"
      ;;
    --skill-dir=*)
      SKILL_DIR="${1#*=}"
      [[ -n "$SKILL_DIR" ]] || { printf '--skill-dir 需要一个目录\n' >&2; exit 2; }
      ;;
    -h|--help) usage; exit 0 ;;
    *) printf '未知参数: %s\n\n' "$1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done

# ---------- 容器编排命令（v2 优先，回退 docker-compose） ----------
COMPOSE_KIND=""
COMPOSE=()
detect_compose() {
  if command -v docker >/dev/null 2>&1 && docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
    COMPOSE_KIND="docker compose"
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
    COMPOSE_KIND="docker-compose"
  fi
}

# 从 .env 读取 APP_PORT（只取值，不 source 整个文件，避免把密钥内容当脚本体执行）
detect_app_port() {
  local p=""
  if [[ -f "$REPO_ROOT/.env" ]]; then
    p="$(grep -E '^[[:space:]]*APP_PORT[[:space:]]*=' "$REPO_ROOT/.env" 2>/dev/null \
          | tail -1 | sed -E 's/^[^=]*=[[:space:]]*//' | tr -d ' \t\r"'"'"'' || true)"
  fi
  if [[ ! "$p" =~ ^[0-9]+$ ]]; then p="$DEFAULT_PORT"; fi
  printf '%s' "$p"
}

probe_ready() {
  local port="$1"
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --max-time "$READY_INTERVAL" "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
      return 0
    fi
    return 1
  fi
  if command -v wget >/dev/null 2>&1; then
    if wget -q -O /dev/null --timeout="$READY_INTERVAL" "http://127.0.0.1:$port/healthz" >/dev/null 2>&1; then
      return 0
    fi
    return 1
  fi
  if [[ -n "$COMPOSE_KIND" ]]; then
    if "${COMPOSE[@]}" exec -T "$SERVICE" python3 -c \
        "import urllib.request; urllib.request.urlopen('http://127.0.0.1:$port/healthz', timeout=$READY_INTERVAL)" >/dev/null 2>&1; then
      return 0
    fi
    return 1
  fi
  return 2   # 没有可用的探测手段，由调用方如实说明
}

info "== 学习助手 · 一键更新并进入日志 =="
if [[ "$DRY_RUN" == "1" ]]; then info "（dry-run：只打印将要执行的命令）"; fi
info "仓库: $REPO_ROOT"
info ""

# -------------------------------- 1/5 前置检查 --------------------------------
info "[1/5] 前置检查"
[[ -d "$REPO_ROOT/.git" ]] || fail "不是 git 仓库: $REPO_ROOT" "请在仓库内运行（脚本位于 scripts/ 下）"
command -v git >/dev/null 2>&1 || fail "缺少 git 命令" "先安装 git"
ok "git 可用"

detect_compose
if [[ -n "$COMPOSE_KIND" ]]; then
  ok "容器编排: $COMPOSE_KIND"
elif [[ "$DRY_RUN" == "1" ]]; then
  warn "未检测到 docker compose（dry-run 继续）"
else
  fail "未检测到 docker compose" "确认已安装 Docker 与 compose 插件，且当前用户在 docker 组内（docker ps 能跑）"
fi

for f in .env config.yaml; do
  if [[ -f "$REPO_ROOT/$f" ]]; then
    ok "$f 存在"
  elif [[ "$DRY_RUN" == "1" ]]; then
    warn "$f 缺失（dry-run 继续）"
  else
    fail "缺少 $REPO_ROOT/$f" "首次部署：cp config.example.yaml config.yaml && cp .env.example .env（再按需填写）"
  fi
done

dirty="$(git -C "$REPO_ROOT" status --porcelain --untracked-files=no || true)"
if [[ -n "$dirty" ]]; then
  if [[ "$ALLOW_DIRTY" == "1" ]]; then
    warn "已跟踪文件有未提交改动（--allow-dirty 已放行）"
  elif [[ "$DRY_RUN" == "1" ]]; then
    warn "已跟踪文件有未提交改动（dry-run 继续；实际执行会中止）"
  else
    printf '%s\n' "$dirty" | sed 's/^/      /' >&2
    fail "工作区有未提交改动，已中止（避免 pull 冲突或覆盖）" \
         "先看：git -C $REPO_ROOT status；提交或暂存后重跑；确实要跳过可加 --allow-dirty"
  fi
else
  ok "工作区干净（已跟踪文件无改动）"
fi

# -------------------------------- 2/5 拉取代码 --------------------------------
info ""
info "[2/5] 拉取最新代码"
before="$(git -C "$REPO_ROOT" rev-parse HEAD 2>/dev/null || echo "")"
if [[ "$DRY_RUN" == "1" ]]; then
  run git -C "$REPO_ROOT" pull --ff-only
  after="$before"
  changed=1
else
  if ! git -C "$REPO_ROOT" pull --ff-only; then
    fail "git pull --ff-only 失败" \
         "先看：git -C $REPO_ROOT status / git -C $REPO_ROOT log --oneline origin/main..HEAD"
  fi
  after="$(git -C "$REPO_ROOT" rev-parse HEAD)"
  changed=0
  if [[ "$before" != "$after" ]]; then changed=1; fi
fi
if [[ "$changed" == "1" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    ok "将更新到远端最新提交"
  else
    ok "已更新: ${before:0:8} -> ${after:0:8}"
  fi
else
  ok "本次没有新提交（${after:0:8}）"
fi

# -------------------------------- 3/5 同步技能 --------------------------------
info ""
info "[3/5] 同步技能副本到 Hermes profile"
if [[ "$SYNC_SKILL" != "1" ]]; then
  ok "已按 --no-skill 跳过"
elif [[ ! -d "$SKILL_SRC" ]]; then
  warn "仓库里没有 $SKILL_SRC，跳过技能同步"
elif [[ "$changed" != "1" && "$FORCE_REBUILD" != "1" ]]; then
  ok "代码无变更，跳过技能同步（加 --force-rebuild 可强制）"
else
  skill_root="$(dirname "$SKILL_DIR")"
  if [[ -d "$skill_root" ]]; then
    run mkdir -p "$SKILL_DIR"
    run cp -R "$SKILL_SRC/." "$SKILL_DIR/"
    if [[ "$DRY_RUN" == "1" ]]; then
      ok "将同步技能 → $SKILL_DIR"
    else
      ok "已同步 → $SKILL_DIR（技能在新会话生效）"
    fi
  else
    warn "未检测到 Hermes profile 技能目录 $skill_root，已跳过技能同步（不假装已同步）"
    warn "Hermes 装好后：mkdir -p \"$SKILL_DIR\" && cp -R \"$SKILL_SRC/.\" \"$SKILL_DIR/\""
  fi
fi

# -------------------------------- 4/5 重建容器 --------------------------------
info ""
info "[4/5] 重建服务容器（$SERVICE）"
if [[ -z "$COMPOSE_KIND" ]]; then
  warn "缺少 docker compose，跳过重建步骤"
elif [[ "$changed" != "1" && "$FORCE_REBUILD" != "1" ]]; then
  ok "代码无变更，跳过重建（加 --force-rebuild 可强制）"
else
  run "${COMPOSE[@]}" up -d --build --force-recreate "$SERVICE"
  if [[ "$DRY_RUN" == "1" ]]; then
    ok "将重建容器"
  else
    ok "容器已重建（--force-recreate：重新挂载 config.yaml 并注入 .env）"
  fi
fi

# -------------------------------- 5/5 就绪与日志 --------------------------------
info ""
info "[5/5] 等待服务就绪并进入日志"
port="$(detect_app_port)"
if [[ -z "$COMPOSE_KIND" ]]; then
  warn "缺少 docker compose，跳过就绪探测（端口按 $port 解析）"
elif [[ "$DRY_RUN" == "1" ]]; then
  ok "将轮询 http://127.0.0.1:$port/healthz（最多 ${READY_TIMEOUT}s）"
else
  waited=0
  ready=0
  probe_state=1
  while (( waited < READY_TIMEOUT )); do
    probe_state=0
    probe_ready "$port" || probe_state=$?   # set -e 下不能用裸调用
    if (( probe_state == 0 )); then ready=1; break; fi
    if (( probe_state == 2 )); then break; fi
    sleep "$READY_INTERVAL"
    waited=$(( waited + READY_INTERVAL ))
  done
  if (( ready == 1 )); then
    ok "服务已就绪（http://127.0.0.1:$port/healthz）"
  elif (( probe_state == 2 )); then
    warn "本机没有 curl/wget 也无法在容器内探测，未做就绪检查（服务可能仍在启动）"
  else
    warn "等待 ${READY_TIMEOUT}s 仍未就绪，继续进入日志以便排查（端口按 $port 解析）"
  fi
fi

if [[ "$FOLLOW" != "1" ]]; then
  info "已按 --no-follow 跳过日志跟随；需要时执行: ${COMPOSE_KIND:-docker compose} logs -f --tail=$TAIL $SERVICE"
elif [[ -z "$COMPOSE_KIND" ]]; then
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  [dry-run] 将执行: docker compose logs -f --tail=%s %s（本机未检测到 compose，未执行）\n' \
      "$TAIL" "$SERVICE"
  else
    warn "没有可用的 docker compose，无法进入日志"
    exit 1
  fi
else
  info "进入日志（Ctrl+C 退出，容器继续运行）"
  info "----------------------------------------"
  if [[ "$DRY_RUN" == "1" ]]; then
    printf '  [dry-run] 将执行: %s\n' "${COMPOSE[*]} logs -f --tail=$TAIL $SERVICE"
  else
    exec "${COMPOSE[@]}" logs -f --tail="$TAIL" "$SERVICE"
  fi
fi
