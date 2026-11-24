"""운영 감시 작업(.github/workflows/monitor.yml) 스크립트를 꺼내 가짜 서버·웹훅으로 돌린다."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

WORKFLOW = Path(__file__).resolve().parents[2] / ".github/workflows/monitor.yml"
SECRETS = ("test-monitor-code", "test-github-token", "/hook-secret-path")


def monitor_script() -> str:
    lines = WORKFLOW.read_text().splitlines()
    start = next(i for i, line in enumerate(lines) if line.strip() == "python3 - <<'PY'")
    end = next(i for i in range(start + 1, len(lines)) if lines[i].strip() == "PY")
    return textwrap.dedent("\n".join(lines[start + 1:end]))


@dataclass
class Fake:
    """한 서버가 운영 API·GitHub API·웹훅 역할을 모두 한다."""

    health: list[int] = field(default_factory=lambda: [200])
    alerts: tuple[int, dict[str, Any] | None, dict[str, str]] = (200, {"ok": True}, {})
    runs: tuple[int, dict[str, Any]] = (200, {"workflow_runs": []})
    requests: list[tuple[str, str, dict[str, str], bytes]] = field(default_factory=list)

    def reply(self, method: str, path: str) -> tuple[int, Any, dict[str, str]]:
        if path == "/v1/health":
            status = self.health.pop(0) if len(self.health) > 1 else self.health[0]
            return status, {"status": "ok"}, {}
        if path == "/ops/api/alerts":
            return self.alerts
        if path.startswith("/repos/collog/app/actions/workflows/monitor.yml/runs"):
            return (*self.runs, {})
        if method == "POST" and path == SECRETS[2]:
            return 204, None, {}
        return 404, None, {}


def serve(fake: Fake) -> tuple[ThreadingHTTPServer, str]:
    class Handler(BaseHTTPRequestHandler):
        def handle_one(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            fake.requests.append((method, self.path, dict(self.headers), body))
            status, payload, headers = fake.reply(method, self.path)
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(status)
            for key, value in headers.items():
                self.send_header(key, value)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self) -> None:  # noqa: N802
            self.handle_one("GET")

        def do_POST(self) -> None:  # noqa: N802
            self.handle_one("POST")

        def log_message(self, *args: Any) -> None:
            del args

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, f"http://127.0.0.1:{server.server_address[1]}"


@pytest.fixture
def servers() -> Iterator[tuple[Fake, str, Fake, str]]:
    main, elsewhere = Fake(), Fake()
    first, main_url = serve(main)
    second, other_url = serve(elsewhere)
    try:
        yield main, main_url, elsewhere, other_url
    finally:
        for server in (first, second):
            server.shutdown()
            server.server_close()


def run(url: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    env = {
        "API_BASE": url,
        "OPS_MONITOR_CODE": SECRETS[0],
        "ALERT_WEBHOOK_URL": url + SECRETS[2],
        "GITHUB_TOKEN": SECRETS[1],
        "GITHUB_REPOSITORY": "collog/app",
        "GITHUB_RUN_ID": "99",
        "GITHUB_REF_NAME": "main",
        "GITHUB_API_URL": url,
        "HEALTH_RETRY_SECONDS": "0",
        "PATH": "/usr/bin:/bin",
    }
    result = subprocess.run(
        [sys.executable, "-I", "-"], input=monitor_script(), env=env, cwd=tmp_path,
        capture_output=True, text=True, timeout=60, check=False,
    )
    for secret in SECRETS:
        assert secret not in result.stdout + result.stderr
    return result


def hooks(fake: Fake) -> list[dict[str, Any]]:
    return [json.loads(body) for method, path, _, body in fake.requests
            if method == "POST" and path == SECRETS[2]]


def runs(*conclusions: str) -> tuple[int, dict[str, Any]]:
    # 지금 실행(99)은 아직 끝나지 않았지만, 섞여 와도 건너뛰는지 본다
    items = [{"id": 99, "conclusion": "failure"}]
    items += [{"id": 98 - i, "conclusion": c} for i, c in enumerate(conclusions)]
    return 200, {"workflow_runs": items}


BACKLOG = (200, {"ok": False, "problems": [{
    "code": "ANALYSIS_BACKLOG",
    "message": "통화가 끝나고 30분 넘게 분석을 기다리는 통화가 7건 있습니다",
}]}, {})


def test_all_good_after_a_good_run_sends_nothing(servers, tmp_path: Path) -> None:
    main, url, _, _ = servers
    main.runs = runs("success")
    result = run(url, tmp_path)
    assert result.returncode == 0, result.stderr
    assert "이상 없음" in result.stdout
    assert hooks(main) == []
    api = [r for r in main.requests if r[1].startswith("/repos/")]
    assert "branch=main" in api[0][1] and "status=completed" in api[0][1]
    assert api[0][2]["Authorization"] == f"Bearer {SECRETS[1]}"


def test_new_problem_alerts_once_and_the_log_shows_only_codes(servers, tmp_path: Path) -> None:
    main, url, _, _ = servers
    main.alerts, main.runs = BACKLOG, runs("success")
    result = run(url, tmp_path)
    assert result.returncode == 1
    assert "문제 종류: ANALYSIS_BACKLOG" in result.stdout
    assert "7건" not in result.stdout and "분석을 기다리는" not in result.stdout
    [sent] = hooks(main)
    assert sent["allowed_mentions"] == {"parse": []}
    assert "7건" in sent["content"] and sent["content"] == sent["text"]
    alerts = [r for r in main.requests if r[1] == "/ops/api/alerts"]
    assert alerts[0][2]["X-Ops-Code"] == SECRETS[0]


def test_ongoing_problem_does_not_alert_again(servers, tmp_path: Path) -> None:
    main, url, _, _ = servers
    main.alerts, main.runs = BACKLOG, runs("failure", "success")
    result = run(url, tmp_path)
    assert result.returncode == 1
    assert hooks(main) == []


def test_recovery_is_announced_once(servers, tmp_path: Path) -> None:
    main, url, _, _ = servers
    main.runs = runs("failure")
    result = run(url, tmp_path)
    assert result.returncode == 0
    [sent] = hooks(main)
    assert "해제" in sent["content"]


@pytest.mark.parametrize("ok", [True, False])
def test_unreadable_previous_run_still_alerts_but_does_not_announce_recovery(
    servers, tmp_path: Path, ok: bool
) -> None:
    main, url, _, _ = servers
    main.runs = (500, {"message": "error"})
    if not ok:
        main.alerts = BACKLOG
    result = run(url, tmp_path)
    assert result.returncode == (0 if ok else 1)
    assert len(hooks(main)) == (0 if ok else 1)


def test_health_is_checked_again_before_alerting(servers, tmp_path: Path) -> None:
    main, url, _, _ = servers
    main.health = [503, 200]
    assert run(url, tmp_path).returncode == 0
    assert len([r for r in main.requests if r[1] == "/v1/health"]) == 2

    main.requests.clear()
    main.health = [503, 503]
    result = run(url, tmp_path)
    assert result.returncode == 1
    assert "문제 종류: HEALTH_STATUS" in result.stdout
    assert "503" not in result.stdout
    assert "503" in hooks(main)[0]["content"]


def test_redirects_are_not_followed_so_the_code_stays_home(servers, tmp_path: Path) -> None:
    main, url, elsewhere, other_url = servers
    main.alerts = (302, None, {"Location": other_url + "/collect"})
    result = run(url, tmp_path)
    assert result.returncode == 1
    assert "문제 종류: ALERTS_STATUS" in result.stdout
    assert elsewhere.requests == []


def test_odd_problem_codes_are_not_printed(servers, tmp_path: Path) -> None:
    main, url, _, _ = servers
    main.alerts = (200, {"ok": False, "problems": [{"code": "see https://x", "message": "m"}]}, {})
    result = run(url, tmp_path)
    assert "문제 종류: UNKNOWN" in result.stdout
    assert "https://x" not in result.stdout
