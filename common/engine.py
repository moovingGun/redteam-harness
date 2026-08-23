#!/usr/bin/env python3
"""Shared state engine for the reusable live-map harness."""

from __future__ import annotations

import fcntl
import json
import os
import re
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple


def _run_root() -> Path:
    """Resolve the engagement root without silently writing state to the wrong folder."""
    configured = os.environ.get("REDTEAM_RUN_DIR")
    if configured:
        return Path(configured).expanduser().resolve()
    # 런처를 거치지 않고 실행된 경우: 기록이 엉뚱한 곳에 조용히 쌓이지 않도록
    # engagement 루트를 명시적으로 찾고, 못 찾으면 실패시킨다.
    start = os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    here = Path(start).expanduser().resolve()
    for candidate in [here, *here.parents]:
        if (candidate / "runtime" / "STATE.json").exists():
            return candidate
        if candidate.name == "engagement":
            return candidate
    raise SystemExit(
        "REDTEAM_RUN_DIR이 설정되지 않았고 engagement 루트를 찾지 못했습니다. "
        "start-redteam.command로 실행하세요. 이 검사가 없으면 MAP·LEDGER가 "
        "엉뚱한 폴더에 조용히 생성됩니다."
    )


ROOT = _run_root()
HARNESS_DIR = ROOT / "runtime"
STATE_PATH = HARNESS_DIR / "STATE.json"
LOCK_PATH = HARNESS_DIR / "state.lock"
EVENTS_PATH = ROOT / "EVENTS.jsonl"
DECISIONS_PATH = ROOT / "DECISIONS.jsonl"
MAP_PATH = ROOT / "MAP.md"
LEDGER_PATH = ROOT / "LEDGER.md"
RAW_DIR = ROOT / "evidence" / "raw"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def default_state() -> Dict[str, Any]:
    return {
        "schema": 4,
        "target": "미설정",
        "scope": "미설정",
        "goal": "미설정",
        "current_goal": "미설정",
        "targets": {},
        "next_target": 1,
        "current_stage": "stage1",
        "next_event": 1,
        "next_clue": 1,
        "next_branch": 2,
        "current_focus": "B-01",
        "branches": {
            "B-01": {
                "status": "FOCUS",
                "from_id": "START",
                "title": "초기 표면 탐색",
                "reason": "실행 초기화",
                "activity": 0,
                "recent_event": "없음",
                "agent": "main",
            }
        },
        "clues": [],
        "observations": [],
        "pending": {},
        "tool_map": {},
        "agents": {"main": {"branch": "B-01", "last_event": None}},
        "updated_at": utc_now(),
    }


def ensure_layout() -> None:
    HARNESS_DIR.mkdir(parents=True, exist_ok=True)
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    try:
        HARNESS_DIR.chmod(0o700)
        RAW_DIR.chmod(0o700)
    except OSError:
        pass


def _load_unlocked() -> Dict[str, Any]:
    if not STATE_PATH.exists():
        return default_state()
    try:
        value = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default_state()
    if not isinstance(value, dict):
        return default_state()
    base = default_state()
    for key, default in base.items():
        value.setdefault(key, default)
    return value


def _atomic_text(path: Path, content: str, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _save_unlocked(state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    _atomic_text(STATE_PATH, json.dumps(state, ensure_ascii=False, indent=2) + "\n")


@contextmanager
def locked_state() -> Iterator[Dict[str, Any]]:
    ensure_layout()
    with LOCK_PATH.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        state = _load_unlocked()
        yield state
        _save_unlocked(state)
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def append_jsonl(path: Path, value: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        path.chmod(0o600)
    except OSError:
        pass


def event_id(number: int) -> str:
    return "E-{0:04d}".format(number)


def clue_id(number: int) -> str:
    return "C-{0:02d}".format(number)


def branch_id(number: int) -> str:
    return "B-{0:02d}".format(number)


def allocate_event(state: Dict[str, Any]) -> str:
    value = event_id(int(state["next_event"]))
    state["next_event"] = int(state["next_event"]) + 1
    return value


def allocate_clue(state: Dict[str, Any]) -> str:
    value = clue_id(int(state["next_clue"]))
    state["next_clue"] = int(state["next_clue"]) + 1
    return value


def allocate_branch(state: Dict[str, Any]) -> str:
    value = branch_id(int(state["next_branch"]))
    state["next_branch"] = int(state["next_branch"]) + 1
    return value


def target_id(number: int) -> str:
    return "T-{0:02d}".format(number)


def allocate_target(state: Dict[str, Any]) -> str:
    value = target_id(int(state["next_target"]))
    state["next_target"] = int(state["next_target"]) + 1
    return value


def approved_values(state: Dict[str, Any]) -> Set[str]:
    return {
        str(item.get("value"))
        for item in state.get("targets", {}).values()
        if item.get("status") == "approved" and item.get("value")
    }


def pending_targets(state: Dict[str, Any]) -> List[Dict[str, Any]]:
    result = []
    for tid, item in sorted(state.get("targets", {}).items()):
        if item.get("status") == "pending":
            entry = dict(item)
            entry["id"] = tid
            result.append(entry)
    return result


def next_stage_label(state: Dict[str, Any]) -> str:
    used = {item.get("stage") for item in state.get("targets", {}).values() if item.get("stage")}
    number = 1
    while "stage{0}".format(number) in used:
        number += 1
    return "stage{0}".format(number)


# ---------------------------------------------------------------- 범위 검사

_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")

# 하네스 자신과 루프백은 언제나 허용한다. 대상이 아니라 도구이기 때문이다.
ALWAYS_ALLOWED = frozenset({"127.0.0.1", "0.0.0.0", "255.255.255.255"})


def extract_ipv4(text: str) -> Set[str]:
    """문자열에서 유효한 IPv4 리터럴만 추출한다. 버전 문자열은 걸리지 않는다."""
    found: Set[str] = set()
    for candidate in _IPV4_RE.findall(text or ""):
        octets = candidate.split(".")
        if all(part.isdigit() and 0 <= int(part) <= 255 for part in octets):
            found.add(candidate)
    return found


def scope_enforced() -> bool:
    return str(os.environ.get("REDTEAM_SCOPE_ENFORCE", "1")).strip().lower() not in {
        "0",
        "false",
        "off",
        "no",
    }


def unapproved_in(state: Dict[str, Any], text: str) -> List[str]:
    """승인되지 않은 대상 IP 목록. 도메인은 조사·연구를 막지 않도록 검사하지 않는다."""
    approved = approved_values(state)
    blocked = [
        ip
        for ip in extract_ipv4(text)
        if ip not in ALWAYS_ALLOWED and ip not in approved and not ip.startswith("127.")
    ]
    return sorted(set(blocked))


def register_initial_target(state: Dict[str, Any], value: str, stage: str = "stage1") -> str:
    """init 시점의 첫 대상을 승인 상태로 등록한다."""
    for tid, item in state.get("targets", {}).items():
        if item.get("value") == value:
            return tid
    tid = allocate_target(state)
    state["targets"][tid] = {
        "value": value,
        "status": "approved",
        "stage": stage,
        "evidence": None,
        "reason": "실행 시작 시 사용자가 지정한 최초 대상",
        "proposed_at": utc_now(),
        "decided_at": utc_now(),
    }
    state["current_stage"] = stage
    state["target"] = value
    return tid


def propose_target(value: str, evidence: Optional[str] = None, reason: str = "") -> Tuple[str, str, bool]:
    """새로 발견한 대상을 승인 대기로 올린다. 승인 전에는 접근이 차단된다."""
    value = " ".join(str(value).split())[:200]
    with locked_state() as state:
        for tid, item in sorted(state.get("targets", {}).items()):
            if item.get("value") == value:
                render_unlocked(state)
                return tid, str(item.get("status")), False
        tid = allocate_target(state)
        state["targets"][tid] = {
            "value": value,
            "status": "pending",
            "stage": None,
            "evidence": evidence,
            "reason": " ".join(str(reason).split())[:300] or "근거 미기재",
            "proposed_at": utc_now(),
            "decided_at": None,
        }
        append_jsonl(
            DECISIONS_PATH,
            {
                "ts_utc": utc_now(),
                "kind": "target-propose",
                "target_id": tid,
                "value": value,
                "evidence": evidence,
                "reason": state["targets"][tid]["reason"],
            },
        )
        render_unlocked(state)
    return tid, "pending", True


def decide_target(
    tid: str,
    decision: str,
    reason: str = "",
    stage: Optional[str] = None,
    agent: str = "main",
) -> Dict[str, Any]:
    """대상을 승인하거나 거부한다. 승인하면 새 Stage 라벨과 FOCUS 가지가 생긴다."""
    if decision not in {"approved", "rejected"}:
        raise ValueError("decision must be approved or rejected")
    with locked_state() as state:
        item = state.get("targets", {}).get(tid)
        if not isinstance(item, dict):
            raise KeyError(tid)
        item["status"] = decision
        item["decided_at"] = utc_now()
        item["decided_reason"] = " ".join(str(reason).split())[:300]
        result: Dict[str, Any] = {"id": tid, **item}
        if decision == "approved":
            if not item.get("stage"):
                item["stage"] = stage or next_stage_label(state)
            state["current_stage"] = item["stage"]
            state["target"] = item["value"]
            state["current_goal"] = "{0} 표면 탐색".format(item["value"])
            bid = allocate_branch(state)
            state["branches"][bid] = {
                "status": "FOCUS",
                "from_id": item.get("evidence") or "START",
                "title": "{0} 진입 ({1})".format(item["stage"], item["value"]),
                "reason": "사용자가 {0} 대상을 승인".format(tid),
                "activity": 0,
                "recent_event": item.get("evidence") or "없음",
                "agent": agent,
            }
            previous = state.get("current_focus")
            if (
                previous
                and previous != bid
                and previous in state["branches"]
                and state["branches"][previous].get("status") == "FOCUS"
            ):
                state["branches"][previous]["status"] = "OPEN"
            state["current_focus"] = bid
            state["agents"].setdefault(agent, {"last_event": None})["branch"] = bid
            result = {"id": tid, **item, "branch": bid}
        append_jsonl(
            DECISIONS_PATH,
            {
                "ts_utc": utc_now(),
                "kind": "target-decision",
                "target_id": tid,
                "value": item.get("value"),
                "decision": decision,
                "stage": item.get("stage"),
                "reason": item.get("decided_reason"),
            },
        )
        render_unlocked(state)
    return result


def ensure_agent_branch(state: Dict[str, Any], agent: str) -> str:
    agents = state["agents"]
    if agent in agents:
        return agents[agent]["branch"]
    branch = allocate_branch(state)
    agents[agent] = {"branch": branch, "last_event": None}
    state["branches"][branch] = {
        "status": "OPEN",
        "from_id": "START",
        "title": "병렬 에이전트 {0}".format(agent[:12]),
        "reason": "훅이 자동 생성한 실행 가지",
        "activity": 0,
        "recent_event": "없음",
        "agent": agent,
    }
    return branch


def safe_action_label(hook: Dict[str, Any]) -> str:
    tool_name = str(hook.get("tool_name") or "Tool")
    tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    description = tool_input.get("description")
    if isinstance(description, str) and description.strip():
        return description.strip()[:180]
    file_path = tool_input.get("file_path")
    if isinstance(file_path, str) and file_path:
        name = Path(file_path).name
        if name.startswith(".env") or "key" in name.lower() or "secret" in name.lower():
            name = "[민감 파일]"
        return "{0} {1}".format(tool_name, name)[:180]
    return "{0} 실행".format(tool_name)


def is_internal_harness_call(hook: Dict[str, Any]) -> bool:
    if str(hook.get("tool_name") or "") != "Bash":
        return False
    tool_input = hook.get("tool_input") if isinstance(hook.get("tool_input"), dict) else {}
    command = str(tool_input.get("command") or "")
    markers = ("mapctl.py", "hook.py", "start-redteam.command")
    return any(marker in command for marker in markers)


def record_private_evidence(eid: str, phase: str, hook: Dict[str, Any]) -> str:
    ensure_layout()
    path = RAW_DIR / "{0}-hook.json".format(eid)
    value: Dict[str, Any] = {}
    if path.exists():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                value = loaded
        except (OSError, json.JSONDecodeError):
            value = {}
    value[phase] = hook
    value["event_id"] = eid
    _atomic_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=0o600)
    return str(path.relative_to(ROOT))


def _clue_line(clue: Dict[str, Any]) -> str:
    existence = "#" if clue.get("existence") == "confirmed" else "?"
    status_map = {"verified": "[v]", "progress": "[~]", "closed": "[x]"}
    status = status_map.get(str(clue.get("status")), "[~]")
    relation = str(clue.get("summary") or "관찰")
    if clue.get("door"):
        relation += " >> 문:" + str(clue["door"])
    return "{id} | {level} | {relation} | {existence} | {status} | ev:{event} | stage:{stage}".format(
        id=clue.get("id"),
        level=clue.get("level") or "현재",
        relation=relation,
        existence=existence,
        status=status,
        event=clue.get("event"),
        stage=clue.get("stage", "미지정"),
    )


def render_unlocked(state: Dict[str, Any]) -> None:
    state["updated_at"] = utc_now()
    pending = list(state.get("pending", {}).values())
    pending.sort(key=lambda item: str(item.get("event_id")))
    clues = list(state.get("clues", []))
    branches = state.get("branches", {})

    targets = state.get("targets", {})
    waiting = pending_targets(state)
    approved_count = len(approved_values(state))

    map_lines: List[str] = [
        "# MAP — 구조적 실시간 침투 지도 V3",
        "",
        "대상: {0} ({1})".format(state.get("target", "미설정"), state.get("current_stage", "stage1")),
        "범위: {0}".format(state.get("scope", "미설정")),
        "최종 목표: {0}".format(state.get("goal", "미설정")),
        "현재 목표: {0}".format(state.get("current_goal", "미설정")),
        "승인 대상: {0}건 · 승인 대기: {1}건".format(approved_count, len(waiting)),
        "",
        "## 대상 범위 (T-*)",
        "",
        "```",
    ]
    if targets:
        for tid in sorted(targets):
            item = targets[tid]
            map_lines.append(
                "{tid} | [{status}] | {value} | {stage} | 근거:{evidence} | {reason}".format(
                    tid=tid,
                    status=item.get("status", "pending"),
                    value=item.get("value", "?"),
                    stage=item.get("stage") or "미배정",
                    evidence=item.get("evidence") or "없음",
                    reason=item.get("reason") or "",
                )
            )
    else:
        map_lines.append("_아직 등록된 대상 없음_")
    if waiting:
        map_lines.append("")
        map_lines.append(
            "!! 승인 대기 {0}건. 승인 전까지 해당 IP로 향하는 외부 행동은 훅이 차단한다.".format(len(waiting))
        )
    map_lines.extend(
        [
            "```",
            "",
            "## 실시간 행동 상태",
            "",
            "```",
        ]
    )
    if pending:
        for item in pending:
            map_lines.append(
                "{event_id} | [{status}] | {branch} | {action} | stage:{stage} | agent:{agent}".format(
                    event_id=item.get("event_id"),
                    status=item.get("status"),
                    branch=item.get("branch"),
                    action=item.get("action"),
                    stage=item.get("stage", "미지정"),
                    agent=item.get("agent"),
                )
            )
    else:
        map_lines.append("_분류 대기 행동 없음_")
    map_lines.extend(["```", "", "## 상승 경로", "", "```"])
    if clues:
        for clue in reversed(clues):
            marker = " <현재 위치>" if clue.get("id") == state.get("current_clue") else ""
            map_lines.append(_clue_line(clue) + marker)
    else:
        map_lines.append("_아직 승격된 단서 없음_")
    map_lines.extend(["```", "", "## 탐색 가지 (B-*)", "", "```"])
    for bid in sorted(branches):
        branch = branches[bid]
        map_lines.append(
            "{bid} | [{status}] | from:{from_id} | {title} | 근거:{reason} | 활동:{activity} | 최근변화:{recent_event}".format(
                bid=bid, **branch
            )
        )
    map_lines.extend(["```", "", "## 미승격 관찰", "", "```"])
    observations = state.get("observations", [])[-30:]
    if observations:
        for item in observations:
            map_lines.append(
                "{event} | [{state}] | {summary} | ev:{event} | stage:{stage}".format(
                    event=item.get("event"),
                    state=item.get("state"),
                    summary=item.get("summary"),
                    stage=item.get("stage", "미지정"),
                )
            )
    else:
        map_lines.append("_아직 없음_")
    map_lines.extend(
        [
            "```",
            "",
            "## 동기화 상태",
            "",
            "- 분류 대기: {0}".format(len(pending)),
            "- 마지막 구조 갱신: {0}".format(state.get("updated_at", utc_now())),
            "- MAP과 LEDGER는 하네스가 생성한다. 직접 편집하지 않는다.",
            "",
        ]
    )
    _atomic_text(MAP_PATH, "\n".join(map_lines), mode=0o600)

    ledger_lines: List[str] = [
        "# LEDGER — 구조화 단서 대장 V3",
        "",
        "대상: {0} ({1})".format(state.get("target", "미설정"), state.get("current_stage", "stage1")),
        "범위: {0}".format(state.get("scope", "미설정")),
        "목표: {0}".format(state.get("goal", "미설정")),
        "",
        "## 대상 범위 (T-*)",
        "",
    ]
    if targets:
        for tid in sorted(targets):
            item = targets[tid]
            ledger_lines.append(
                "- {tid} | {status} | {value} | stage:{stage} | 근거:{evidence} | {reason}".format(
                    tid=tid,
                    status=item.get("status", "pending"),
                    value=item.get("value", "?"),
                    stage=item.get("stage") or "미배정",
                    evidence=item.get("evidence") or "없음",
                    reason=item.get("reason") or "",
                )
            )
    else:
        ledger_lines.append("_아직 등록된 대상 없음_")
    ledger_lines.extend(["", "## 단서 (C-*)", ""])
    if clues:
        for clue in clues:
            ledger_lines.extend(
                [
                    "### {0} | {1}".format(clue.get("id"), clue.get("summary")),
                    "- 수준: {0}".format(clue.get("level")),
                    "- 존재: {0}".format(clue.get("existence")),
                    "- 상태: {0}".format(clue.get("status")),
                    "- 분기: {0}".format(clue.get("branch")),
                    "- Stage: {0}".format(clue.get("stage", "미지정")),
                    "- 관계: {0}".format(clue.get("relation")),
                    "- 문: {0}".format(clue.get("door") or "없음"),
                    "- 근거: ev:{0}".format(clue.get("event")),
                    "",
                ]
            )
    else:
        ledger_lines.extend(["_아직 없음_", ""])
    ledger_lines.extend(["## 정리 대장 (cleanup)", "", "_생성/변경 항목 없음_", ""])
    _atomic_text(LEDGER_PATH, "\n".join(ledger_lines), mode=0o600)


def bootstrap() -> None:
    with locked_state() as state:
        render_unlocked(state)
