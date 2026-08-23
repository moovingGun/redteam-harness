#!/usr/bin/env python3
"""하네스 핵심 흐름 검증: 대상 승인, 범위 차단, Stage 전이, 기록 원자성."""

from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COMMON = ROOT / "common"

# engine은 임포트 시점에 루트를 확정하므로 그 전에 임시 engagement를 지정한다.
_TEMP = tempfile.TemporaryDirectory()
os.environ["REDTEAM_RUN_DIR"] = _TEMP.name
os.environ["REDTEAM_SCOPE_ENFORCE"] = "1"
sys.path.insert(0, str(COMMON))

import engine  # noqa: E402
import hook  # noqa: E402


def pre(tool_name: str, tool_input: dict) -> dict | None:
    """PreToolUse 훅을 직접 호출하고 훅이 낸 결정을 돌려준다."""
    buffer = io.StringIO()
    with redirect_stdout(buffer):
        hook.handle_pre(
            {
                "tool_name": tool_name,
                "tool_input": tool_input,
                "tool_use_id": "tu-" + str(len(buffer.getvalue())),
                "agent_id": "main",
            }
        )
    raw = buffer.getvalue().strip()
    return json.loads(raw) if raw else None


def decision_of(result: dict | None) -> str | None:
    if not result:
        return None
    return result.get("hookSpecificOutput", {}).get("permissionDecision")


def clear_pending() -> None:
    """분류 대기 게이트가 범위 검사 테스트를 가리지 않도록 비운다."""
    with engine.locked_state() as state:
        state["pending"] = {}


class TargetApprovalFlow(unittest.TestCase):
    def setUp(self) -> None:
        with engine.locked_state() as state:
            state.update(engine.default_state())
        with engine.locked_state() as state:
            engine.register_initial_target(state, "192.0.2.10", "stage1")
            state["scope"] = "단일 호스트"
            state["goal"] = "다음 경계 확인"
            engine.render_unlocked(state)

    def test_initial_target_is_approved(self) -> None:
        with engine.locked_state() as state:
            self.assertEqual(engine.approved_values(state), {"192.0.2.10"})
            self.assertEqual(state["current_stage"], "stage1")

    def test_approved_target_is_allowed(self) -> None:
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "curl http://192.0.2.10/"})))

    def test_unapproved_ip_is_denied(self) -> None:
        clear_pending()
        result = pre("Bash", {"command": "nmap 203.0.113.55"})
        self.assertEqual(decision_of(result), "deny")
        reason = result["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("203.0.113.55", reason)
        self.assertIn("target-propose", reason)

    def test_loopback_and_versions_are_not_blocked(self) -> None:
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "curl http://127.0.0.1:8765/"})))
        clear_pending()
        # 버전 문자열은 IPv4가 아니므로 걸리지 않아야 한다.
        self.assertIsNone(decision_of(pre("Bash", {"command": "echo nginx/1.27.5 grafana 11.2.0"})))

    def test_octet_range_is_validated(self) -> None:
        self.assertEqual(engine.extract_ipv4("999.1.1.1 and 203.0.113.9"), {"203.0.113.9"})

    def test_propose_then_approve_opens_next_stage(self) -> None:
        tid, status, created = engine.propose_target("203.0.113.55", "C-14", "설정 파일에서 발견")
        self.assertTrue(created)
        self.assertEqual(status, "pending")

        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 203.0.113.55"})), "deny")

        result = engine.decide_target(tid, "approved", "테스트 승인")
        self.assertEqual(result["stage"], "stage2")

        with engine.locked_state() as state:
            self.assertEqual(state["current_stage"], "stage2")
            self.assertIn("203.0.113.55", engine.approved_values(state))
            focus = state["branches"][state["current_focus"]]
            self.assertEqual(focus["status"], "FOCUS")
            self.assertIn("203.0.113.55", focus["title"])
            # 이전 대상 가지는 닫히지 않고 남아 있어야 되돌아갈 수 있다.
            self.assertNotIn("CLOSED", [b["status"] for b in state["branches"].values()])

        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "nmap 203.0.113.55"})))

    def test_reject_keeps_blocking(self) -> None:
        tid, _, _ = engine.propose_target("203.0.113.99", "C-20", "가능성만 있음")
        engine.decide_target(tid, "rejected", "범위 밖")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "curl 203.0.113.99"})), "deny")

    def test_duplicate_proposal_is_idempotent(self) -> None:
        first, _, created_first = engine.propose_target("203.0.113.77", "C-30", "최초")
        second, status, created_second = engine.propose_target("203.0.113.77", "C-31", "중복")
        self.assertEqual(first, second)
        self.assertTrue(created_first)
        self.assertFalse(created_second)
        self.assertEqual(status, "pending")

    def test_harness_own_commands_are_not_scope_checked(self) -> None:
        clear_pending()
        command = 'python3 "$REDTEAM_COMMON/mapctl.py" target-propose --value 203.0.113.55'
        self.assertIsNone(decision_of(pre("Bash", {"command": command})))

    def test_map_renders_target_section(self) -> None:
        tid, _, _ = engine.propose_target("203.0.113.55", "C-14", "설정 파일에서 발견")
        text = engine.MAP_PATH.read_text(encoding="utf-8")
        self.assertIn("## 대상 범위 (T-*)", text)
        self.assertIn("T-01", text)
        self.assertIn("203.0.113.55", text)
        self.assertIn("승인 대기", text)
        ledger = engine.LEDGER_PATH.read_text(encoding="utf-8")
        self.assertIn(tid, ledger)

    def test_scope_enforcement_can_be_disabled(self) -> None:
        clear_pending()
        os.environ["REDTEAM_SCOPE_ENFORCE"] = "0"
        try:
            self.assertIsNone(decision_of(pre("Bash", {"command": "curl 198.51.100.7"})))
        finally:
            os.environ["REDTEAM_SCOPE_ENFORCE"] = "1"

    def test_pending_classification_gate_still_works(self) -> None:
        clear_pending()
        pre("Bash", {"command": "curl http://192.0.2.10/"})
        with engine.locked_state() as state:
            for item in state["pending"].values():
                item["status"] = "AWAITING_CLASSIFICATION"
        result = pre("Bash", {"command": "curl http://192.0.2.10/robots.txt"})
        self.assertEqual(decision_of(result), "deny")
        self.assertIn("동기화 대기", result["hookSpecificOutput"]["permissionDecisionReason"])


class StageLabelling(unittest.TestCase):
    def test_next_stage_skips_used_labels(self) -> None:
        state = engine.default_state()
        state["targets"] = {
            "T-01": {"value": "a", "status": "approved", "stage": "stage1"},
            "T-02": {"value": "b", "status": "approved", "stage": "stage2"},
        }
        self.assertEqual(engine.next_stage_label(state), "stage3")


if __name__ == "__main__":
    unittest.main(verbosity=2)
