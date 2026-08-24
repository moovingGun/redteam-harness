#!/usr/bin/env python3
"""하네스 핵심 흐름 검증: 대상 승인, 범위 차단, Stage 전이, 기록 원자성."""

from __future__ import annotations

import io
import ipaddress
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import unittest
import urllib.request
from contextlib import redirect_stdout
from http.server import ThreadingHTTPServer
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
        shutil.rmtree(engine.WORK_DIR, ignore_errors=True)
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

    def test_stage_workspace_is_created(self) -> None:
        stage1 = engine.WORK_DIR / "stage1"
        self.assertTrue(stage1.is_dir(), "최초 대상 등록 시 work/stage1이 생겨야 한다")
        self.assertTrue((stage1 / "STAGE.md").exists())

        tid, _, _ = engine.propose_target("203.0.113.55", "C-14", "설정 파일에서 발견")
        self.assertFalse((engine.WORK_DIR / "stage2").exists(), "승인 전에는 만들지 않는다")

        engine.decide_target(tid, "approved", "테스트 승인")
        stage2 = engine.WORK_DIR / "stage2"
        self.assertTrue(stage2.is_dir())
        self.assertIn("203.0.113.55", (stage2 / "STAGE.md").read_text(encoding="utf-8"))

    def test_workspace_note_is_not_overwritten(self) -> None:
        note = engine.WORK_DIR / "stage1" / "STAGE.md"
        note.write_text("사용자가 적어둔 메모", encoding="utf-8")
        engine.ensure_stage_workspace("stage1", "192.0.2.10")
        self.assertEqual(note.read_text(encoding="utf-8"), "사용자가 적어둔 메모")

    def test_map_points_to_current_workspace(self) -> None:
        text = engine.MAP_PATH.read_text(encoding="utf-8")
        self.assertIn("작업 폴더: work/stage1/", text)

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


class InternalCallDetection(unittest.TestCase):
    """하네스 자기 호출 판정은 범위 검사와 이벤트 기록을 통째로 건너뛴다.

    느슨하면 그대로 우회 통로가 되므로, 마커 문자열이 명령 어딘가에 있다는 이유로
    참이 되어서는 안 된다. 아래는 예전 substring 판정에서 전부 통과하던 형태들이다.
    """

    def setUp(self) -> None:
        with engine.locked_state() as state:
            state.update(engine.default_state())
        with engine.locked_state() as state:
            engine.register_initial_target(state, "192.0.2.10", "stage1")
            engine.render_unlocked(state)
        clear_pending()

    def _internal(self, command: str) -> bool:
        return engine.is_internal_harness_call(
            {"tool_name": "Bash", "tool_input": {"command": command}}
        )

    def test_real_mapctl_call_is_internal(self) -> None:
        self.assertTrue(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status'))
        self.assertTrue(self._internal("/usr/bin/python3 {0}/mapctl.py status".format(COMMON)))

    def test_marker_in_comment_is_not_internal(self) -> None:
        self.assertFalse(self._internal("nmap 203.0.113.55  # mapctl.py"))

    def test_marker_as_filename_is_not_internal(self) -> None:
        self.assertFalse(self._internal("curl http://203.0.113.55/ -o mapctl.py"))

    def test_chained_command_is_not_internal(self) -> None:
        self.assertFalse(self._internal("echo mapctl.py; nmap 203.0.113.55"))
        self.assertFalse(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status && nmap 203.0.113.55'))
        self.assertFalse(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status | tee /tmp/out'))

    def test_command_substitution_is_not_internal(self) -> None:
        self.assertFalse(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status --x "$(nmap 203.0.113.55)"'))
        self.assertFalse(self._internal('python3 "$REDTEAM_COMMON/mapctl.py" status --x `nmap 203.0.113.55`'))

    def test_script_outside_harness_is_not_internal(self) -> None:
        self.assertFalse(self._internal("python3 /tmp/mapctl.py --anything"))

    def test_smuggled_commands_are_denied_by_scope(self) -> None:
        """판정이 막히면 범위 검사가 이어받아 실제로 거부해야 한다."""
        for command in (
            "nmap 203.0.113.55  # mapctl.py",
            "curl http://203.0.113.55/ -o mapctl.py",
            "echo mapctl.py; nmap 203.0.113.55",
        ):
            with self.subTest(command=command):
                clear_pending()
                self.assertEqual(decision_of(pre("Bash", {"command": command})), "deny")


class ScopeMatching(unittest.TestCase):
    def setUp(self) -> None:
        with engine.locked_state() as state:
            state.update(engine.default_state())
        clear_pending()

    def _approve(self, value: str) -> None:
        with engine.locked_state() as state:
            engine.register_initial_target(state, value, "stage1")
            engine.render_unlocked(state)

    def test_cidr_scope_allows_hosts_in_range(self) -> None:
        self._approve("192.0.2.0/24")
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "curl http://192.0.2.10/"})))
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "nmap 192.0.2.254"})))

    def test_cidr_scope_still_blocks_outside(self) -> None:
        self._approve("192.0.2.0/24")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 198.51.100.7"})), "deny")

    def test_single_host_scope_does_not_widen(self) -> None:
        self._approve("192.0.2.10")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 192.0.2.11"})), "deny")

    def test_decimal_and_hex_ip_forms_are_detected(self) -> None:
        # 203.0.113.55 를 정수·16진수로 적어도 같은 주소로 해석해야 한다.
        self.assertIn(
            ipaddress.IPv4Address("203.0.113.55"),
            engine.extract_targets("curl http://3405803831/"),
        )
        self.assertIn(
            ipaddress.IPv4Address("203.0.113.55"),
            engine.extract_targets("curl http://0xCB007137/"),
        )
        self._approve("192.0.2.10")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "curl http://3405803831/"})), "deny")

    def test_ipv6_literal_is_detected(self) -> None:
        self.assertIn(
            ipaddress.IPv6Address("2001:db8::dead:beef"),
            engine.extract_targets("nmap 2001:db8::dead:beef"),
        )
        self._approve("192.0.2.10")
        clear_pending()
        self.assertEqual(decision_of(pre("Bash", {"command": "nmap 2001:db8::dead:beef"})), "deny")

    def test_approved_ipv6_is_allowed(self) -> None:
        self._approve("2001:db8::/32")
        clear_pending()
        self.assertIsNone(decision_of(pre("Bash", {"command": "nmap 2001:db8::dead:beef"})))

    def test_ports_and_versions_are_not_read_as_ips(self) -> None:
        self.assertEqual(engine.extract_targets("listening on 8080 nginx/1.27.5"), set())
        self.assertEqual(engine.extract_targets("elapsed 1699999999 ms"), set())


class DashboardCsrf(unittest.TestCase):
    """/api/target은 상태를 바꾸는 엔드포인트다.

    client_address만 보면 사용자가 열어둔 아무 웹페이지나 127.0.0.1로 요청을 보내
    대상을 대신 승인시킬 수 있다. text/plain은 프리플라이트도 없다.
    """

    server: ThreadingHTTPServer
    token = "test-token-" + "a" * 32

    @classmethod
    def setUpClass(cls) -> None:
        import map_viewer

        root = Path(os.environ["REDTEAM_RUN_DIR"])
        page = (
            map_viewer.PAGE.replace("__DASHBOARD_LABEL__", "TEST")
            .replace("__CSRF_TOKEN__", cls.token)
            .encode("utf-8")
        )

        def quiet(self, *_args: object) -> None:  # 테스트 출력에 접근 로그를 섞지 않는다
            return

        handler = type(
            "TestDashboardHandler",
            (map_viewer.DashboardHandler,),
            {
                "map_path": root / "MAP.md",
                "events_path": root / "EVENTS.jsonl",
                "ledger_path": root / "LEDGER.md",
                "state_path": root / "runtime" / "STATE.json",
                "page_bytes": page,
                "stage_filter": None,
                "csrf_token": cls.token,
                "allowed_origins": frozenset(),
                "log_message": quiet,
            },
        )
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.server.server_address[1]
        handler.allowed_origins = frozenset({"http://127.0.0.1:{0}".format(cls.port)})
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        with engine.locked_state() as state:
            state.update(engine.default_state())
        with engine.locked_state() as state:
            engine.register_initial_target(state, "192.0.2.10", "stage1")
            engine.render_unlocked(state)
        self.tid, _, _ = engine.propose_target("203.0.113.55", "E-0001", "테스트")

    def _post(self, headers: dict) -> int:
        body = json.dumps({"id": self.tid, "action": "approve"}).encode("utf-8")
        request = "POST /api/target HTTP/1.1\r\nHost: 127.0.0.1:{0}\r\n".format(self.port)
        for key, value in headers.items():
            request += "{0}: {1}\r\n".format(key, value)
        request += "Content-Length: {0}\r\nConnection: close\r\n\r\n".format(len(body))
        sock = socket.create_connection(("127.0.0.1", self.port), 5)
        try:
            sock.sendall(request.encode("utf-8") + body)
            head = sock.recv(4096).decode("utf-8", "replace").split("\r\n")[0]
        finally:
            sock.close()
        return int(head.split()[1])

    def _status_of(self, tid: str) -> str:
        with engine.locked_state() as state:
            return str(state["targets"][tid]["status"])

    def test_cross_origin_simple_request_is_rejected(self) -> None:
        status = self._post({"Origin": "https://evil.example", "Content-Type": "text/plain"})
        self.assertEqual(status, 403)
        self.assertEqual(self._status_of(self.tid), "pending")

    def test_missing_token_is_rejected(self) -> None:
        self.assertEqual(self._post({"Content-Type": "text/plain"}), 403)
        self.assertEqual(self._status_of(self.tid), "pending")

    def test_cross_site_fetch_metadata_is_rejected(self) -> None:
        status = self._post(
            {
                "Content-Type": "application/json",
                "Sec-Fetch-Site": "cross-site",
                "X-Redteam-Token": self.token,
            }
        )
        self.assertEqual(status, 403)
        self.assertEqual(self._status_of(self.tid), "pending")

    def test_wrong_token_is_rejected(self) -> None:
        status = self._post({"Content-Type": "application/json", "X-Redteam-Token": "nope"})
        self.assertEqual(status, 403)
        self.assertEqual(self._status_of(self.tid), "pending")

    def test_dashboard_request_still_works(self) -> None:
        status = self._post(
            {
                "Origin": "http://127.0.0.1:{0}".format(self.port),
                "Sec-Fetch-Site": "same-origin",
                "Content-Type": "application/json",
                "X-Redteam-Token": self.token,
            }
        )
        self.assertEqual(status, 200)
        self.assertEqual(self._status_of(self.tid), "approved")

    def test_page_carries_the_token(self) -> None:
        with urllib.request.urlopen("http://127.0.0.1:{0}/".format(self.port), timeout=5) as page:
            text = page.read().decode("utf-8")
        self.assertIn(self.token, text)
        self.assertNotIn("__CSRF_TOKEN__", text)


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
