#!/usr/bin/env python3
"""Shared Claude Code hook: record the active Stage without sharing run state."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict

from engine import (
    EVENTS_PATH,
    allocate_event,
    append_jsonl,
    bootstrap,
    ensure_agent_branch,
    is_internal_harness_call,
    locked_state,
    record_private_evidence,
    render_unlocked,
    safe_action_label,
    utc_now,
)


MAPCTL_PATH = Path(__file__).resolve().with_name("mapctl.py")


def read_input() -> Dict[str, Any]:
    try:
        value = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError):
        return {}
    return value if isinstance(value, dict) else {}


def emit(value: Dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")


def agent_key(hook: Dict[str, Any]) -> str:
    return str(hook.get("agent_id") or "main")


def stage_key() -> str:
    return str(os.environ.get("REDTEAM_STAGE") or "미지정")[:80]


def requires_classification(hook: Dict[str, Any]) -> bool:
    tool_name = str(hook.get("tool_name") or "")
    return tool_name in {"Bash", "WebFetch", "WebSearch"} or tool_name.startswith("mcp__")


def deny_for_pending(hook: Dict[str, Any], pending_ids: list[str]) -> None:
    mapctl = str(MAPCTL_PATH)
    ids = ", ".join(pending_ids)
    reason = (
        "실시간 지도 동기화 대기: {0}. 결과를 판단한 뒤 mapctl로 분류하세요. "
        "변화가 없으면 `/usr/bin/python3 {1} resolve --event {2} "
        "--outcome no-change --summary '짧은 판단'`; 단서면 같은 도구의 clue 명령을 사용하세요. "
        "이는 탐색 방향 제한이 아니라 기록 원자성 보장입니다."
    ).format(ids, mapctl, pending_ids[0])
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": "PreToolUse",
                "permissionDecision": "deny",
                "permissionDecisionReason": reason,
            }
        }
    )


def handle_pre(hook: Dict[str, Any]) -> None:
    if is_internal_harness_call(hook):
        return
    agent = agent_key(hook)
    with locked_state() as state:
        awaiting = [
            eid
            for eid, item in state["pending"].items()
            if item.get("agent") == agent and item.get("status") == "AWAITING_CLASSIFICATION"
        ]
        if awaiting and requires_classification(hook):
            deny_for_pending(hook, sorted(awaiting))
            return
        branch = ensure_agent_branch(state, agent)
        eid = allocate_event(state)
        parent = state["agents"][agent].get("last_event")
        action = safe_action_label(hook)
        started = utc_now()
        item = {
            "event_id": eid,
            "status": "RUNNING",
            "branch": branch,
            "action": action,
            "agent": agent,
            "stage": stage_key(),
            "parent_event": parent,
            "started_at": started,
            "tool_name": str(hook.get("tool_name") or "Tool"),
            "tool_use_id": str(hook.get("tool_use_id") or ""),
            "requires_classification": requires_classification(hook),
        }
        state["pending"][eid] = item
        if item["tool_use_id"]:
            state["tool_map"][item["tool_use_id"]] = eid
        state["agents"][agent]["last_event"] = eid
        state["branches"][branch]["activity"] = int(state["branches"][branch].get("activity", 0)) + 1
        state["branches"][branch]["recent_event"] = eid
        evidence_path = record_private_evidence(eid, "pre", hook)
        append_jsonl(
            EVENTS_PATH,
            {
                "event_id": eid,
                "ts_utc": started,
                "action_type": item["tool_name"],
                "action_label": action,
                "parent_event": parent,
                "branch_id": branch,
                "agent_id": agent,
                "stage_id": item["stage"],
                "scope_ref": action,
                "evidence_path": evidence_path,
                "phase": "start",
            },
        )
        render_unlocked(state)


def _finish(hook: Dict[str, Any], failed: bool) -> None:
    if is_internal_harness_call(hook):
        return
    agent = agent_key(hook)
    tool_use_id = str(hook.get("tool_use_id") or "")
    with locked_state() as state:
        eid = state["tool_map"].get(tool_use_id)
        if not eid or eid not in state["pending"]:
            branch = ensure_agent_branch(state, agent)
            eid = allocate_event(state)
            action = safe_action_label(hook)
            state["pending"][eid] = {
                "event_id": eid,
                "status": "RUNNING",
                "branch": branch,
                "action": action,
                "agent": agent,
                "stage": stage_key(),
                "parent_event": state["agents"][agent].get("last_event"),
                "started_at": utc_now(),
                "tool_name": str(hook.get("tool_name") or "Tool"),
                "tool_use_id": tool_use_id,
                "requires_classification": requires_classification(hook),
            }
        item = state["pending"][eid]
        strict = bool(item.get("requires_classification"))
        item["status"] = "AWAITING_CLASSIFICATION" if strict else "AUTO_CLASSIFIED"
        status = "failed" if failed else "success"
        evidence_path = record_private_evidence(eid, "failure" if failed else "post", hook)
        append_jsonl(
            EVENTS_PATH,
            {
                "event_id": eid,
                "ts_utc": utc_now(),
                "duration_ms": hook.get("duration_ms"),
                "action_type": item.get("tool_name"),
                "action_label": item.get("action"),
                "parent_event": item.get("parent_event"),
                "branch_id": item.get("branch"),
                "agent_id": item.get("agent"),
                "stage_id": item.get("stage"),
                "status": status,
                "exit_code": None,
                "evidence_path": evidence_path,
                "observation_summary": item.get("action"),
                "promotion_state": "unreviewed" if strict else "closed",
                "clue_ids": [],
                "map_changed": True,
                "phase": "finish",
            },
        )
        if not strict:
            del state["pending"][eid]
        render_unlocked(state)
    if not strict:
        return
    event_name = "PostToolUseFailure" if failed else "PostToolUse"
    context = (
        "{0} 자동 기록 완료. 다음 외부 행동 전에 이 결과를 mapctl로 no-change/candidate/closed 또는 clue로 분류하세요. "
        "MAP은 코드가 즉시 재생성하므로 직접 편집하지 마세요."
    ).format(eid)
    emit(
        {
            "hookSpecificOutput": {
                "hookEventName": event_name,
                "additionalContext": context,
            }
        }
    )


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "session"
    hook = read_input()
    if mode in ("session", "bootstrap"):
        bootstrap()
    elif mode == "pre":
        handle_pre(hook)
    elif mode == "post":
        _finish(hook, failed=False)
    elif mode == "failure":
        _finish(hook, failed=True)
    else:
        raise SystemExit("unknown hook mode: " + mode)


if __name__ == "__main__":
    main()
