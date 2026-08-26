#!/usr/bin/env python3
"""끊긴 실행에서 확정된 상태만 뽑아 다음 실행에 기계적으로 넘긴다.

세션은 아무 때나 죽는다. 안전장치에 걸려서, 타임아웃으로, 학교에서 집으로
옮기느라. 그때마다 처음부터 다시 시작하면 실습이 성립하지 않는다.

넘기는 방식이 중요하다. LLM에게 "지금까지 한 걸 요약해"라고 시키면 그게 바로
이 하네스가 훅으로 막으려던 사후 재구성이다. 요약하는 LLM은 자기가 확정한 것과
추측한 것을 구분하지 못하고, 확정으로 승격시켜 다음 실행에 넘긴다. 그래서 이
모듈은 STATE.json과 EVENTS.jsonl만 읽고, 거기 이미 확정으로 기록된 것만
그대로 옮긴다. 요약도 해석도 하지 않는다.

넘기는 것:
  - 대상·범위·목표 (사용자가 정한 것)
  - 승인된 대상 T-* (사용자가 승인한 것)
  - 브랜치와 그 상태 (FOCUS/OPEN — 어디까지 갔고 뭐가 열려 있는지)
  - existence=confirmed 단서만 (MAP에서 `#`로 표기된 것)
  - 마지막으로 완료된 이벤트, 그리고 끊긴 지점

넘기지 않는 것:
  - existence=inferred 단서 (`?`). 추측은 다음 실행이 다시 확인해야 한다
  - observations (candidate 상태의 미분류 관찰)
  - pending 대상 제안. 사용자가 아직 승인하지 않은 것을 승인된 것처럼 넘기면
    범위 차단이 무의미해진다

이벤트 번호는 부모의 next_event에서 이어간다. E-* 참조가 부모 실행과 자식
실행에 걸쳐 유일해야 브랜치의 recent_event 같은 참조가 깨지지 않는다.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

CARRYOVER_SCHEMA = 1

# STATE.json에서 그대로 옮기는 스칼라. 사용자가 정했거나 이미 확정된 값들이다.
_SCALAR_KEYS = ("target", "scope", "goal", "current_goal", "current_stage")

# 번호 카운터. 부모에서 이어받아야 E-*/C-*/B-*/T-* 참조가 충돌하지 않는다.
_COUNTER_KEYS = ("next_event", "next_clue", "next_branch", "next_target")


class CarryoverError(RuntimeError):
    """이어받기를 진행하면 안 되는 상태. 조용히 빈 값으로 넘기지 않는다."""


# ------------------------------------------------------------ 읽기


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise CarryoverError("파일이 없다: {0}".format(path))
    except (ValueError, OSError) as exc:
        raise CarryoverError("읽을 수 없다: {0} ({1})".format(path, exc))


def read_events(engagement: Path) -> List[Dict[str, Any]]:
    """EVENTS.jsonl을 읽는다. 마지막 줄이 잘려 있어도 앞부분은 살린다.

    실행이 비정상 종료되면 마지막 줄이 쓰다 만 상태로 남는 게 정상이다.
    그 한 줄 때문에 27분치를 통째로 버리면 이 모듈의 존재 이유가 없어진다.
    """
    path = engagement / "EVENTS.jsonl"
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except ValueError:
            continue  # 잘린 마지막 줄
        if isinstance(value, dict):
            events.append(value)
    return events


def last_finished(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for event in reversed(events):
        if event.get("phase") == "finish":
            return event
    return None


def interrupted_action(events: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """finish가 없는 start. 실행이 정확히 어디서 끊겼는지다.

    이걸 넘기는 이유: 그 행동은 절반쯤 실행됐을 수 있다. 대상에 이미 요청이
    갔는지 아닌지를 다음 실행이 모르면, 같은 행동을 무심코 반복한다.
    """
    finished = {
        str(event.get("event_id"))
        for event in events
        if event.get("phase") == "finish" and event.get("event_id")
    }
    for event in reversed(events):
        if event.get("phase") != "start":
            continue
        if str(event.get("event_id")) not in finished:
            return event
    return None


# ------------------------------------------------------------ 조립


def build(engagement: Path, parent_run_id: str = "") -> Dict[str, Any]:
    """부모 실행 폴더에서 인수인계 블록을 만든다."""
    engagement = Path(engagement).expanduser().resolve()
    state = _read_json(engagement / "runtime" / "STATE.json")
    events = read_events(engagement)

    if not state.get("target") or state.get("target") == "미설정":
        raise CarryoverError(
            "부모 실행에 대상이 설정되어 있지 않다. 이어받을 상태가 없다: {0}".format(engagement)
        )

    manifest: Dict[str, Any] = {}
    run_json = engagement.parent / "RUN.json"
    if run_json.exists():
        try:
            manifest = _read_json(run_json)
        except CarryoverError:
            manifest = {}

    approved = {
        tid: dict(target)
        for tid, target in (state.get("targets") or {}).items()
        if target.get("status") == "approved"
    }
    pending_count = sum(
        1
        for target in (state.get("targets") or {}).values()
        if target.get("status") == "pending"
    )

    # `#` 확정만. `?` 추론은 다음 실행이 스스로 다시 확인해야 한다.
    confirmed = [
        dict(clue)
        for clue in (state.get("clues") or [])
        if clue.get("existence") == "confirmed"
    ]
    dropped_clues = len(state.get("clues") or []) - len(confirmed)

    carry: Dict[str, Any] = {
        "carryover_schema": CARRYOVER_SCHEMA,
        "parent_run": parent_run_id or manifest.get("run_id") or engagement.parent.name,
        "parent_config_label": manifest.get("config_label", "unknown"),
        "parent_harness_rev": manifest.get("harness_rev", "unknown"),
        "parent_path": str(engagement),
        "targets": approved,
        "pending_targets_dropped": pending_count,
        "branches": dict(state.get("branches") or {}),
        "clues": confirmed,
        "inferred_clues_dropped": dropped_clues,
        "current_focus": state.get("current_focus"),
        "current_clue": state.get("current_clue"),
        "agents": dict(state.get("agents") or {}),
        "counters": {key: state.get(key) for key in _COUNTER_KEYS if state.get(key)},
        "last_finished": last_finished(events),
        "interrupted": interrupted_action(events),
        "parent_actions": len(
            {
                str(event.get("event_id"))
                for event in events
                if event.get("phase") == "start" and event.get("event_id")
            }
        ),
    }
    for key in _SCALAR_KEYS:
        carry[key] = state.get(key)
    return carry


# ------------------------------------------------------------ 새 실행 STATE 시드


def seed_state(carry: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """default_state() 위에 인수인계 내용을 얹는다.

    EVENTS.jsonl은 시드하지 않는다. 새 실행의 이벤트 파일은 비어서 시작해야
    runstat이 "이 실행에서 실제로 한 일"만 센다. 부모의 행동이 자식 실행의
    행동 수에 섞이면 구성 비교가 그 순간 거짓말이 된다.
    """
    state = dict(base)
    for key in _SCALAR_KEYS:
        if carry.get(key):
            state[key] = carry[key]

    state["targets"] = dict(carry.get("targets") or {})
    state["branches"] = dict(carry.get("branches") or {})
    state["clues"] = list(carry.get("clues") or [])
    state["agents"] = dict(carry.get("agents") or {})
    if carry.get("current_focus"):
        state["current_focus"] = carry["current_focus"]
    if carry.get("current_clue"):
        state["current_clue"] = carry["current_clue"]

    # 번호를 1로 되돌리면 자식 실행의 E-0001이 부모의 E-0001과 충돌한다.
    # 브랜치의 recent_event 같은 참조가 조용히 엉뚱한 이벤트를 가리키게 된다.
    for key, value in (carry.get("counters") or {}).items():
        state[key] = value

    # 미분류 관찰과 승인 대기는 넘기지 않는다. 전자는 확정이 아니고, 후자는
    # 사용자 결정이라 새 실행에서 다시 물어야 한다.
    state["observations"] = []
    state["pending"] = {}
    state["tool_map"] = {}
    return state


# ------------------------------------------------------------ 렌더링


def _fmt(value: Any, limit: int = 400) -> str:
    text = "" if value is None else str(value).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 1] + "…"


def render(carry: Dict[str, Any]) -> str:
    lines: List[str] = []
    add = lines.append

    add("# CARRYOVER — 이어받은 실행 상태")
    add("")
    add("이 문서는 코드가 부모 실행의 STATE.json에서 기계적으로 뽑았다.")
    add("요약이 아니라 그대로 옮긴 것이다. 여기 없는 것은 확정되지 않았다는 뜻이니,")
    add("필요하면 이번 실행에서 다시 확인해라. 추측을 확정으로 올리지 마라.")
    add("")
    add("- 부모 실행: `{0}`".format(carry.get("parent_run")))
    add(
        "- 부모 구성: {0} / 하네스 {1} / 행동 {2}건".format(
            carry.get("parent_config_label"),
            carry.get("parent_harness_rev"),
            carry.get("parent_actions", 0),
        )
    )
    add("")

    add("## 대상과 목표")
    add("")
    add("- 대상: {0}".format(_fmt(carry.get("target"))))
    add("- 범위: {0}".format(_fmt(carry.get("scope"))))
    add("- 최종 목표: {0}".format(_fmt(carry.get("goal"))))
    add("- 현재 목표: {0}".format(_fmt(carry.get("current_goal"))))
    add("- 현재 Stage: {0}".format(_fmt(carry.get("current_stage"))))
    add("")

    add("## 승인된 대상")
    add("")
    targets = carry.get("targets") or {}
    if targets:
        for tid in sorted(targets):
            target = targets[tid]
            add(
                "- `{0}` {1} ({2}) — {3}".format(
                    tid,
                    target.get("value"),
                    target.get("stage"),
                    _fmt(target.get("reason"), 120),
                )
            )
    else:
        add("_없음_")
    if carry.get("pending_targets_dropped"):
        add("")
        add(
            "> 부모 실행에 승인 대기 {0}건이 있었으나 넘기지 않았다. "
            "승인은 사용자 결정이므로 이번 실행에서 다시 제안해야 한다.".format(
                carry["pending_targets_dropped"]
            )
        )
    add("")

    add("## 가지 (B-*)")
    add("")
    branches = carry.get("branches") or {}
    if branches:
        add("```")
        for bid in sorted(branches):
            branch = branches[bid]
            add(
                "{0} | {1:<6} | {2} | 최근 {3} | 활동 {4}".format(
                    bid,
                    branch.get("status", "?"),
                    _fmt(branch.get("title"), 60),
                    branch.get("recent_event", "-"),
                    branch.get("activity", 0),
                )
            )
        add("```")
        if carry.get("current_focus"):
            add("")
            add("현재 초점: `{0}`".format(carry["current_focus"]))
    else:
        add("_없음_")
    add("")

    add("## 확정 단서 (`#`)")
    add("")
    clues = carry.get("clues") or []
    if clues:
        add("```")
        for clue in clues:
            add(
                "{0} | {1} | {2} | ev:{3} | {4}".format(
                    clue.get("id"),
                    _fmt(clue.get("level"), 40),
                    _fmt(clue.get("summary"), 300),
                    clue.get("event", "-"),
                    clue.get("stage", "-"),
                )
            )
        add("```")
    else:
        add("_없음_")
    if carry.get("inferred_clues_dropped"):
        add("")
        add(
            "> 추론(`?`) 단서 {0}건은 넘기지 않았다. 확정된 것만 이어받는다.".format(
                carry["inferred_clues_dropped"]
            )
        )
    add("")

    add("## 끊긴 지점")
    add("")
    finished = carry.get("last_finished")
    if finished:
        add(
            "- 마지막 완료: `{0}` {1} — {2} ({3})".format(
                finished.get("event_id"),
                finished.get("action_type"),
                _fmt(finished.get("observation_summary") or finished.get("action_label"), 200),
                finished.get("ts_utc"),
            )
        )
    else:
        add("- 완료된 행동 없음")

    interrupted = carry.get("interrupted")
    if interrupted:
        add("")
        add(
            "- **중단된 행동**: `{0}` {1} — {2}".format(
                interrupted.get("event_id"),
                interrupted.get("action_type"),
                _fmt(interrupted.get("action_label"), 200),
            )
        )
        add("")
        add(
            "  이 행동은 시작 기록만 있고 완료 기록이 없다. 즉 대상에 요청이 갔는지,"
        )
        add(
            "  갔다면 어디까지 처리됐는지 알 수 없다. 같은 행동을 그대로 반복하기 전에"
        )
        add("  상태를 먼저 확인해라.")
    add("")

    add("## 이번 실행에서 할 일")
    add("")
    add("1. 위 확정 단서와 가지 상태를 출발점으로 삼는다. 이미 확인한 것을 다시 확인하지 마라.")
    add("2. 중단된 행동이 있으면 그 결과부터 확인한다.")
    add("3. 이벤트 번호는 부모에서 이어진다. 새로 매기지 마라.")
    add("")
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ CLI


def resolve_engagement(path: Path) -> Path:
    """runs/<run_id> 를 줘도, runs/<run_id>/engagement 를 줘도 받는다."""
    path = Path(path).expanduser().resolve()
    if (path / "runtime" / "STATE.json").exists():
        return path
    nested = path / "engagement"
    if (nested / "runtime" / "STATE.json").exists():
        return nested
    raise CarryoverError("실행 폴더가 아니다 (runtime/STATE.json 없음): {0}".format(path))


def main() -> None:
    parser = argparse.ArgumentParser(
        description="끊긴 실행에서 확정 상태를 뽑아 인수인계 블록을 만든다."
    )
    parser.add_argument("run", help="부모 실행 폴더 (runs/<run_id> 또는 그 안의 engagement)")
    parser.add_argument("--json", action="store_true", help="CARRYOVER.md 대신 JSON으로 출력")
    parser.add_argument("--out", help="출력 파일 경로 (없으면 표준출력)")
    args = parser.parse_args()

    try:
        engagement = resolve_engagement(Path(args.run))
        carry = build(engagement)
    except CarryoverError as exc:
        print("이어받기 실패: {0}".format(exc), file=sys.stderr)
        raise SystemExit(1)

    text = (
        json.dumps(carry, ensure_ascii=False, indent=2) + "\n" if args.json else render(carry)
    )
    if args.out:
        Path(args.out).expanduser().write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)


if __name__ == "__main__":
    main()
