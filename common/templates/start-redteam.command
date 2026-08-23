#!/bin/zsh
set -eu

STAGE_DIR=${0:A:h}
ENGAGEMENT_DIR=${STAGE_DIR:h}
ROOT_DIR=${ENGAGEMENT_DIR:h}
COMMON_DIR="$ROOT_DIR/common"
STAGE_NAME=${STAGE_DIR:t}
VIEWER_LOG="$ENGAGEMENT_DIR/runtime/viewer-$STAGE_NAME.log"

case "$STAGE_NAME" in
  stage1) VIEWER_PORT=8765 ;;
  stage2) VIEWER_PORT=8865 ;;
  stage3) VIEWER_PORT=8965 ;;
  *) VIEWER_PORT=9065 ;;
esac

cd "$STAGE_DIR"
mkdir -p "$ENGAGEMENT_DIR/runtime"
export REDTEAM_COMMON="$COMMON_DIR"
export REDTEAM_RUN_DIR="$ENGAGEMENT_DIR"
export REDTEAM_STAGE="$STAGE_NAME"

/usr/bin/python3 "$COMMON_DIR/hook.py" bootstrap </dev/null

env PYTHONDONTWRITEBYTECODE=1 REDTEAM_RUN_DIR="$ENGAGEMENT_DIR" REDTEAM_STAGE="$STAGE_NAME" /usr/bin/python3 \
  "$COMMON_DIR/map_viewer.py" "$ENGAGEMENT_DIR/MAP.md" \
  --stage "$STAGE_NAME" --label "${STAGE_NAME:u}" --port "$VIEWER_PORT" \
  >"$VIEWER_LOG" 2>&1 &
VIEWER_PID=$!

cleanup_viewer() {
  kill "$VIEWER_PID" 2>/dev/null || true
}
trap cleanup_viewer EXIT INT TERM

claude \
  --settings "$COMMON_DIR/settings.json" \
  --append-system-prompt-file "$COMMON_DIR/redteam-map-prompt.md" \
  --name "redteam-harness-$STAGE_NAME"
