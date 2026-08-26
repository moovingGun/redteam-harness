#!/bin/zsh
set -eu

# 하나의 문제 전체를 이 런처 하나로 진행한다.
# Stage는 미리 고르지 않는다. 시작 IP 하나로 출발하고, 새 IP가 나오면
# 대시보드에서 승인하는 순간 다음 Stage가 열린다.
#
# 실행마다 runs/<run_id>/engagement로 기록을 격리한다. 한 폴더에 계속 이어 쓰면
# 여러 번 돌린 뒤 어느 이벤트가 어느 실행 것인지 사후에 갈라낼 수 없고, 앞선
# 실행의 MAP·STATE가 다음 실행의 출발점을 오염시킨다. 구성만 바꿔 반복 실행한
# 뒤 common/runstat.py로 구성끼리 비교하려면 실행 경계가 물리적으로 갈려야 한다.

ROOT_DIR=${0:A:h}
COMMON_DIR="$ROOT_DIR/common"

# 라벨 변수 이름은 REDTEAM_CONFIG_LABEL 하나다. REDTEAM_CONFIG로 잘못 쓰면 조용히
# 무시되고 default로 기록되는데, 그 사실은 실행이 다 끝난 뒤 runstat을 볼 때야
# 드러난다. 라벨이 틀린 실행은 구성 비교에 못 쓰므로 여기서 즉시 멈춘다.
if [ -n "${REDTEAM_CONFIG:-}" ]; then
  print -u2 "REDTEAM_CONFIG는 없는 변수다. REDTEAM_CONFIG_LABEL을 써라: REDTEAM_CONFIG_LABEL=${REDTEAM_CONFIG} $0"
  exit 1
fi

# 끊긴 실행 이어받기. REDTEAM_RESUME=<run_id>를 주면 그 실행의 확정 상태를
# 새 실행 폴더에 심고 시작한다. 원본 폴더는 건드리지 않는다.
#
# 같은 폴더에 이어 쓰지 않는 이유: run_id 하나 아래에 서로 기억이 끊긴 세션 둘이
# 들어가면 runstat은 그걸 깨끗한 실행 하나로 집계한다. 앞뒤 harness_rev가 다를
# 수도 있다. 이벤트 단위에서 사후 재구성을 막아놓고 실행 단위에서 허용하는 꼴이다.
RESUME_DIR=""
if [ -n "${REDTEAM_RESUME:-}" ]; then
  case "$REDTEAM_RESUME" in
    */*) RESUME_DIR=${REDTEAM_RESUME:A} ;;
    *)   RESUME_DIR="$ROOT_DIR/runs/$REDTEAM_RESUME" ;;
  esac
  if [ ! -d "$RESUME_DIR" ]; then
    print -u2 "이어받을 실행이 없다: $RESUME_DIR"
    print -u2 "가능한 실행: $(ls "$ROOT_DIR/runs" 2>/dev/null | tr '\n' ' ')"
    exit 1
  fi
  if [ ! -f "$RESUME_DIR/engagement/runtime/STATE.json" ]; then
    print -u2 "실행 폴더는 있는데 상태 파일이 없다: $RESUME_DIR/engagement/runtime/STATE.json"
    exit 1
  fi
fi

RUN_ID=${REDTEAM_RUN_ID:-"$(date -u +%Y%m%dT%H%M%SZ)-$(printf '%04x' $RANDOM)"}
# 실행 ID는 그대로 폴더 이름이 된다. 경로 조각이 섞이면 기록이 runs/ 밖에 생긴다.
case "$RUN_ID" in
  ""|*/*|*..*)
    print -u2 "REDTEAM_RUN_ID는 폴더 이름 하나여야 한다 (/ 와 .. 금지): $RUN_ID"
    exit 1
    ;;
esac

# 비교의 기준이 되는 구성 이름. 같은 라벨을 붙인 실행들이 한 그룹으로 묶인다.
CONFIG_LABEL=${REDTEAM_CONFIG_LABEL:-default}

# 구성을 비교하는데 그 사이 하네스 코드가 바뀌었다면 결과 해석이 무의미해진다.
# 커밋만으로는 워킹트리 수정이 드러나지 않으므로 dirty 여부를 함께 남긴다.
HARNESS_REV=$(git -C "$ROOT_DIR" rev-parse --short HEAD 2>/dev/null || echo unknown)
if [ -n "$(git -C "$ROOT_DIR" status --porcelain 2>/dev/null || true)" ]; then
  HARNESS_REV="${HARNESS_REV}-dirty"
fi

RUN_DIR="$ROOT_DIR/runs/$RUN_ID"
ENGAGEMENT_DIR="$RUN_DIR/engagement"
SETTINGS_FILE="$RUN_DIR/settings.json"
MCP_CONFIG="$ROOT_DIR/.mcp.json"
VIEWER_LOG="$ENGAGEMENT_DIR/runtime/viewer.log"
VIEWER_PORT=${REDTEAM_PORT:-8765}

if [ -e "$ENGAGEMENT_DIR" ]; then
  print -u2 "이미 있는 실행 폴더다. 다른 REDTEAM_RUN_ID를 쓰거나 폴더를 지워라: $ENGAGEMENT_DIR"
  exit 1
fi

mkdir -p "$ENGAGEMENT_DIR/runtime"
cd "$ENGAGEMENT_DIR"
export REDTEAM_COMMON="$COMMON_DIR"
export REDTEAM_RUN_DIR="$ENGAGEMENT_DIR"
export REDTEAM_RUN_ID="$RUN_ID"
export REDTEAM_CONFIG_LABEL="$CONFIG_LABEL"
export REDTEAM_HARNESS_REV="$HARNESS_REV"

/usr/bin/python3 "$COMMON_DIR/hook.py" bootstrap </dev/null

# 훅 배선을 이 실행 폴더에 절대 경로로 굳힌다. 상대 경로 배선은 실행 폴더
# 깊이가 바뀌는 순간 조용히 끊기고, 훅이 하나도 로드되지 않은 채로 진행된다.
/usr/bin/python3 "$COMMON_DIR/hook.py" prepare-run "$RUN_DIR" </dev/null >/dev/null

# .mcp.json은 저장소 루트에 있는데 claude는 실행 폴더에서 뜬다. 그 자리에서는
# 자동 발견되지 않으므로 절대 경로로 직접 넘긴다. settings.json과 같은 이유다.
#
# --strict-mcp-config로 사용자 전역 MCP 서버를 배제한다. 어떤 도구가 붙어 있었는지
# 모르는 실행은 harness_rev가 같아도 사실은 다른 구성이고, 그러면 구성 비교가
# 조용히 거짓말을 한다. Burp 말고 다른 MCP를 함께 쓰려면 .mcp.json에 추가한다.
MCP_ARGS=()
if [ -f "$MCP_CONFIG" ]; then
  MCP_ARGS=(--mcp-config "$MCP_CONFIG" --strict-mcp-config)
else
  print "MCP 구성 없음 ($MCP_CONFIG). MCP 도구 없이 진행한다."
fi

print "실행 ID: $RUN_ID | 구성: $CONFIG_LABEL | 하네스: $HARNESS_REV"
print "기록 폴더: $ENGAGEMENT_DIR"

env PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 REDTEAM_RUN_DIR="$ENGAGEMENT_DIR" /usr/bin/python3 \
  "$COMMON_DIR/map_viewer.py" "$ENGAGEMENT_DIR/MAP.md" \
  --label "ENGAGEMENT" --port "$VIEWER_PORT" \
  >"$VIEWER_LOG" 2>&1 &
VIEWER_PID=$!

cleanup_viewer() {
  kill "$VIEWER_PID" 2>/dev/null || true
}
trap cleanup_viewer EXIT INT TERM

# 뷰어가 실제로 연 주소를 실행 창에도 보여준다. 포트가 밀려도 사용자가
# 주소를 볼 수 있도록, 로그에만 남기지 않고 여기서 한 줄 출력한다.
for _ in {1..20}; do
  if grep -q '실시간 지도:' "$VIEWER_LOG" 2>/dev/null; then
    grep '실시간 지도:' "$VIEWER_LOG"
    break
  fi
  sleep 0.2
done

claude \
  --settings "$SETTINGS_FILE" \
  "${MCP_ARGS[@]}" \
  --append-system-prompt-file "$COMMON_DIR/redteam-map-prompt.md" \
  --name "redteam-harness"
