#!/bin/zsh
set -eu

ROOT_DIR=${0:A:h}
COMMON_DIR="$ROOT_DIR/common"

read -r "STAGE_NAME?새 Stage 이름을 입력하세요 (예: stage4): "
if [[ ! "$STAGE_NAME" =~ '^[A-Za-z0-9][A-Za-z0-9._-]*$' ]]; then
  echo "영문자·숫자·점·밑줄·하이픈만 사용할 수 있습니다."
  exit 1
fi

STAGE_DIR="$ROOT_DIR/engagement/$STAGE_NAME"
if [[ -e "$STAGE_DIR" ]]; then
  echo "이미 존재합니다: $STAGE_DIR"
  exit 1
fi

mkdir -p "$STAGE_DIR"
cp "$COMMON_DIR/templates/start-redteam.command" "$STAGE_DIR/start-redteam.command"
cp "$COMMON_DIR/templates/STAGE.md" "$STAGE_DIR/STAGE.md"
chmod 700 "$STAGE_DIR/start-redteam.command"

env REDTEAM_RUN_DIR="$ROOT_DIR/engagement" REDTEAM_STAGE="$STAGE_NAME" /usr/bin/python3 "$COMMON_DIR/hook.py" bootstrap </dev/null
echo "생성 완료: $STAGE_DIR"
echo "start-redteam.command를 실행하면 됩니다."
