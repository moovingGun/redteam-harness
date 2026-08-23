#!/bin/zsh
set -eu

# 하나의 문제 전체를 이 런처 하나로 진행한다.
# Stage는 미리 고르지 않는다. 시작 IP 하나로 출발하고, 새 IP가 나오면
# 대시보드에서 승인하는 순간 다음 Stage가 열린다.

ROOT_DIR=${0:A:h}
COMMON_DIR="$ROOT_DIR/common"
ENGAGEMENT_DIR="$ROOT_DIR/engagement"
VIEWER_LOG="$ENGAGEMENT_DIR/runtime/viewer.log"
VIEWER_PORT=${REDTEAM_PORT:-8765}

mkdir -p "$ENGAGEMENT_DIR/runtime"
cd "$ENGAGEMENT_DIR"
export REDTEAM_COMMON="$COMMON_DIR"
export REDTEAM_RUN_DIR="$ENGAGEMENT_DIR"

/usr/bin/python3 "$COMMON_DIR/hook.py" bootstrap </dev/null

env PYTHONDONTWRITEBYTECODE=1 REDTEAM_RUN_DIR="$ENGAGEMENT_DIR" /usr/bin/python3 \
  "$COMMON_DIR/map_viewer.py" "$ENGAGEMENT_DIR/MAP.md" \
  --label "ENGAGEMENT" --port "$VIEWER_PORT" \
  >"$VIEWER_LOG" 2>&1 &
VIEWER_PID=$!

cleanup_viewer() {
  kill "$VIEWER_PID" 2>/dev/null || true
}
trap cleanup_viewer EXIT INT TERM

claude \
  --settings "$COMMON_DIR/settings.json" \
  --append-system-prompt-file "$COMMON_DIR/redteam-map-prompt.md" \
  --name "redteam-harness"
