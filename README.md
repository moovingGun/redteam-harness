# 재사용형 Red Team Harness

`common/`의 코드와 일반 지침 하나를 공유한다. Stage 1–3은 하나의 큰 문제로 보고 MAP·E/C/B 번호·증적을 계속 이어 쓰며, 각 Stage의 작업 파일만 하위 폴더로 나눈다.

## 사용법

1. 풀 문제의 Stage 폴더를 연다.
2. `start-redteam.command`를 실행한다.
3. 열린 Claude에 대상, 허용 범위, 최종 목표를 말한다.

실시간 지도는 자동으로 브라우저에 열리며 Claude가 종료되면 뷰어도 종료된다. Claude는 반드시 Stage 안의 `start-redteam.command`로 시작해야 공용 Hook과 지침이 적용된다.

Stage별 홈페이지는 서로 다른 주소로 열린다.

- Stage 1: `http://127.0.0.1:8765`
- Stage 2: `http://127.0.0.1:8865`
- Stage 3: `http://127.0.0.1:8965`

각 홈페이지의 행동 목록과 행동 수는 해당 Stage만 표시한다. 전체 MAP·LEDGER·E/C/B 번호는 하나의 큰 문제 흐름으로 공유한다.

## 폴더 역할

- `common/`: 재사용 코드, Hook 설정, 중립 프롬프트, 뷰어. 특정 대상의 답·단서·증적을 넣지 않는다.
- `engagement/stage1`~`stage3`: 각 단계에서 새로 만드는 작업 파일과 메모.
- `engagement/runtime/`, `MAP.md`, `LEDGER.md`, `EVENTS.jsonl`, `DECISIONS.jsonl`, `evidence/`: Stage 1–3이 공유하는 전체 문제 기록.
- `new-stage.command`: 같은 구조의 새 Stage를 추가한다.

Stage가 바뀌어도 전체 문제 기록은 상위 `engagement/`에서 계속 누적된다. 공용 폴더에는 방법론과 작동 코드만 둔다.
