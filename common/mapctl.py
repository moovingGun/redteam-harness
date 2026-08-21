#!/usr/bin/env python3
"""Small structured interface used by Claude to classify events and steer the map."""

from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from engine import (
    DECISIONS_PATH,
    EVENTS_PATH,
    allocate_branch,
    allocate_clue,
    append_jsonl,
    bootstrap,
    locked_state,
    render_unlocked,
    utc_now,
)


def short(value: str, limit: int = 240) -> str:
    return " ".join(str(value).split())[:limit]


def pending_item(state: Dict[str, Any], eid: str) -> Dict[str, Any]:
    item = state.get("pending", {}).get(eid)
    if not isinstance(item, dict):
        raise SystemExit("분류 대기 중인 이벤트가 아닙니다: " + eid)
    return item


def append_classification(
    eid: str,
    item: Dict[str, Any],
    summary: str,
    promotion_state: str,
    clue_ids: list[str],
) -> None:
    value = {
        "event_id": eid,
        "ts_utc": utc_now(),
        "action_type": "classification",
        "parent_event": item.get("parent_event"),
        "branch_id": item.get("branch"),
        "agent_id": item.get("agent"),
        "stage_id": item.get("stage"),
        "status": "classified",
        "observation_summary": summary,
        "promotion_state": promotion_state,
        "clue_ids": clue_ids,
        "map_changed": True,
        "phase": "classification",
    }
    append_jsonl(EVENTS_PATH, value)
    append_jsonl(DECISIONS_PATH, value)


def command_init(args: argparse.Namespace) -> None:
    with locked_state() as state:
        state["target"] = short(args.target)
        state["scope"] = short(args.scope, 400)
        state["goal"] = short(args.goal, 400)
        state["current_goal"] = state["goal"]
        render_unlocked(state)
    print("하네스 초기화 완료")


def command_resolve(args: argparse.Namespace) -> None:
    with locked_state() as state:
        item = pending_item(state, args.event)
        summary = short(args.summary)
        if args.outcome in ("candidate", "closed"):
            state["observations"].append(
                {
                    "event": args.event,
                    "state": args.outcome,
                    "summary": summary,
                    "stage": item.get("stage", "미지정"),
                }
            )
        promotion = "closed" if args.outcome == "no-change" else args.outcome
        del state["pending"][args.event]
        append_classification(args.event, item, summary, promotion, [])
        render_unlocked(state)
    print("{0} -> {1}".format(args.event, args.outcome))


def command_clue(args: argparse.Namespace) -> None:
    with locked_state() as state:
        item = pending_item(state, args.event)
        cid = allocate_clue(state)
        branch = args.branch or item.get("branch") or state.get("current_focus")
        clue = {
            "id": cid,
            "event": args.event,
            "branch": branch,
            "stage": item.get("stage", "미지정"),
            "summary": short(args.summary),
            "level": short(args.level, 80),
            "existence": args.existence,
            "status": args.status,
            "relation": args.relation,
            "door": short(args.door, 160) if args.door else None,
            "created_at": utc_now(),
        }
        state["clues"].append(clue)
        state["current_clue"] = cid
        if branch in state["branches"]:
            state["branches"][branch]["from_id"] = cid
            state["branches"][branch]["recent_event"] = args.event
            state["branches"][branch]["reason"] = short(args.summary, 160)
        if args.door:
            state["current_goal"] = short(args.door, 240)
        del state["pending"][args.event]
        append_classification(args.event, item, clue["summary"], "promoted", [cid])
        render_unlocked(state)
    print(cid)


def command_branch(args: argparse.Namespace) -> None:
    with locked_state() as state:
        bid = args.branch
        if not bid:
            bid = "B-{0:02d}".format(int(state["next_branch"]))
            state["next_branch"] = int(state["next_branch"]) + 1
        current = state["branches"].get(bid, {})
        current.update(
            {
                "status": args.status,
                "from_id": args.from_id or current.get("from_id", "START"),
                "title": short(args.title or current.get("title", "탐색 가지"), 160),
                "reason": short(args.reason or current.get("reason", "사용자/AI 초점 조정"), 180),
                "activity": int(current.get("activity", 0)),
                "recent_event": args.event or current.get("recent_event", "없음"),
                "agent": args.agent,
            }
        )
        if args.status == "FOCUS":
            old = state.get("current_focus")
            if old and old != bid and old in state["branches"] and state["branches"][old].get("status") == "FOCUS":
                state["branches"][old]["status"] = "OPEN"
            state["current_focus"] = bid
            state["agents"].setdefault(args.agent, {"last_event": None})["branch"] = bid
        state["branches"][bid] = current
        decision = {
            "ts_utc": utc_now(),
            "kind": "branch",
            "branch_id": bid,
            "status": args.status,
            "title": current["title"],
            "reason": current["reason"],
        }
        append_jsonl(DECISIONS_PATH, decision)
        render_unlocked(state)
    print(bid)


def command_status(_args: argparse.Namespace) -> None:
    with locked_state() as state:
        render_unlocked(state)
        result = {
            "target": state.get("target"),
            "current_goal": state.get("current_goal"),
            "focus": state.get("current_focus"),
            "pending": sorted(state.get("pending", {}).keys()),
            "clues": len(state.get("clues", [])),
            "branches": len(state.get("branches", {})),
        }
    print(json.dumps(result, ensure_ascii=False))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="공용 구조화 지도 제어기")
    sub = parser.add_subparsers(dest="command", required=True)

    init = sub.add_parser("init")
    init.add_argument("--target", required=True)
    init.add_argument("--scope", required=True)
    init.add_argument("--goal", required=True)
    init.set_defaults(func=command_init)

    resolve = sub.add_parser("resolve")
    resolve.add_argument("--event", required=True)
    resolve.add_argument("--outcome", choices=("no-change", "candidate", "closed"), required=True)
    resolve.add_argument("--summary", required=True)
    resolve.set_defaults(func=command_resolve)

    clue = sub.add_parser("clue")
    clue.add_argument("--event", required=True)
    clue.add_argument("--summary", required=True)
    clue.add_argument("--level", default="현재")
    clue.add_argument("--existence", choices=("confirmed", "hypothesis"), default="confirmed")
    clue.add_argument("--status", choices=("verified", "progress", "closed"), default="verified")
    clue.add_argument("--relation", choices=("child", "door", "alternate"), default="child")
    clue.add_argument("--door")
    clue.add_argument("--branch")
    clue.set_defaults(func=command_clue)

    branch = sub.add_parser("branch")
    branch.add_argument("--branch")
    branch.add_argument("--status", choices=("FOCUS", "OPEN", "PARKED", "CLOSED"), required=True)
    branch.add_argument("--title")
    branch.add_argument("--reason")
    branch.add_argument("--from-id")
    branch.add_argument("--event")
    branch.add_argument("--agent", default="main")
    branch.set_defaults(func=command_branch)

    status = sub.add_parser("status")
    status.set_defaults(func=command_status)
    return parser


def main() -> None:
    bootstrap()
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
