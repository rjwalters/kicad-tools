"""Regression coverage for the deployed-artifact verification gate (#5318).

The live kicad-tools.org site served a pre-repair Board05 PCB (41
arbitrary-angle B.Cu segments) for weeks after the source-level fix (#5045)
landed on main, because the site is deployed manually and nothing ever
checked the deployed bytes against current source. ``scripts/lib/site-verify.sh``
(``verify_deployed_pcbs``) is the fix: it fetches each board's publicly
served ``board.kicad_pcb`` and diffs its SHA-256 against a locally staged
copy, failing loudly on any mismatch.

This exercises that shell function directly -- against a real local HTTP
server, never a live network call -- covering: an exact match, a byte
mismatch, a fetch failure (404), a transient failure that a retry recovers
from, and the "nothing staged" no-op case.
"""

from __future__ import annotations

import http.server
import shutil
import subprocess
import threading
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LIB = REPO_ROOT / "scripts/lib/site-verify.sh"


def _run_verify(
    staged_dir: Path, base_url: str, env_extra: dict[str, str] | None = None
) -> subprocess.CompletedProcess:
    """Source the library and invoke verify_deployed_pcbs against a fixture."""
    env = {
        "VERIFY_RETRY_ATTEMPTS": "2",
        "VERIFY_RETRY_SLEEP_SECONDS": "0",
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        **(env_extra or {}),
    }
    script = f'source {LIB} && verify_deployed_pcbs "{base_url}" "{staged_dir}"'
    return subprocess.run(
        ["bash", "-c", script],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


class _StatefulHandler(http.server.BaseHTTPRequestHandler):
    """Serves ``board_bytes`` (a dict keyed by request path), with an
    optional one-shot failure before succeeding, to exercise retries."""

    board_bytes: dict[str, bytes] = {}
    fail_once_paths: set[str] = set()

    def do_GET(self):  # noqa: N802 -- stdlib method name
        if self.path in self.fail_once_paths:
            self.fail_once_paths.discard(self.path)
            self.send_response(503)
            self.end_headers()
            return
        body = self.board_bytes.get(self.path)
        if body is None:
            self.send_response(404)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):  # silence stdlib request logging
        pass


@pytest.fixture
def http_server():
    handler = type("Handler", (_StatefulHandler,), {"board_bytes": {}, "fail_once_paths": set()})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, handler
    finally:
        server.shutdown()
        thread.join(timeout=5)


def _stage(staged_dir: Path, slug: str, content: bytes) -> Path:
    board_dir = staged_dir / slug
    board_dir.mkdir(parents=True, exist_ok=True)
    pcb = board_dir / "board.kicad_pcb"
    pcb.write_bytes(content)
    return pcb


def test_lib_defines_expected_functions():
    text = LIB.read_text()
    assert "verify_deployed_pcbs()" in text
    assert "sha256_of_file()" in text


def test_matching_deployed_pcb_passes(tmp_path, http_server):
    server, handler = http_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    staged = tmp_path / "staged"
    content = b"(kicad_pcb (version 20240101) ; matching bytes\n"
    _stage(staged, "05-bldc-motor-controller", content)
    handler.board_bytes["/boards/05-bldc-motor-controller/board.kicad_pcb"] = content

    result = _run_verify(staged, base_url)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: 1 deployed board PCB(s) match" in result.stdout


def test_stale_deployed_pcb_fails(tmp_path, http_server):
    """The exact issue #5318 scenario: live bytes are the pre-repair artifact."""
    server, handler = http_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    staged = tmp_path / "staged"
    corrected = b"(kicad_pcb ; corrected, zero off-angle segments\n"
    stale = b"(kicad_pcb ; PRE-REPAIR, 41 off-angle B.Cu segments\n"
    _stage(staged, "05-bldc-motor-controller", corrected)
    handler.board_bytes["/boards/05-bldc-motor-controller/board.kicad_pcb"] = stale

    result = _run_verify(staged, base_url)

    assert result.returncode == 1
    assert "MISMATCH board=05-bldc-motor-controller" in result.stderr
    assert "do NOT match staged source" in result.stderr


def test_fetch_failure_counts_as_mismatch(tmp_path, http_server):
    """A board that 404s publicly (never deployed, or deploy failed) is a
    reported failure, not a silent skip."""
    server, _handler = http_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    staged = tmp_path / "staged"
    _stage(staged, "09-usbc-pd-power", b"whatever bytes")

    result = _run_verify(staged, base_url)

    assert result.returncode == 1
    assert "MISMATCH board=09-usbc-pd-power" in result.stderr
    assert "<fetch failed>" in result.stderr


def test_transient_failure_recovers_via_retry(tmp_path, http_server):
    """A single flaky response (e.g. brief CDN propagation lag) must not
    fail the whole check -- retry must actually retry, not just fail fast."""
    server, handler = http_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    staged = tmp_path / "staged"
    content = b"(kicad_pcb ; eventually consistent\n"
    _stage(staged, "01-voltage-divider", content)
    handler.board_bytes["/boards/01-voltage-divider/board.kicad_pcb"] = content
    handler.fail_once_paths.add("/boards/01-voltage-divider/board.kicad_pcb")

    result = _run_verify(staged, base_url, env_extra={"VERIFY_RETRY_ATTEMPTS": "3"})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "OK: 1 deployed board PCB(s) match" in result.stdout


def test_multiple_boards_reports_partial_mismatch_count(tmp_path, http_server):
    server, handler = http_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    staged = tmp_path / "staged"
    good = b"good bytes"
    _stage(staged, "board-a", good)
    _stage(staged, "board-b", good)
    handler.board_bytes["/boards/board-a/board.kicad_pcb"] = good
    handler.board_bytes["/boards/board-b/board.kicad_pcb"] = b"stale bytes"

    result = _run_verify(staged, base_url)

    assert result.returncode == 1
    assert "1/2 deployed board PCB(s)" in result.stderr
    assert "MISMATCH board=board-b" in result.stderr
    assert "MISMATCH board=board-a" not in result.stderr


def test_no_staged_boards_is_a_noop_not_a_failure(tmp_path, http_server):
    server, _handler = http_server
    base_url = f"http://127.0.0.1:{server.server_port}"
    staged = tmp_path / "staged"
    staged.mkdir()

    result = _run_verify(staged, base_url)

    assert result.returncode == 0, result.stdout + result.stderr
    assert "nothing to verify" in result.stderr


@pytest.mark.skipif(shutil.which("bash") is None, reason="requires bash")
def test_deploy_site_sh_sources_the_shared_library():
    """deploy-site.sh must reuse the library rather than reimplementing
    hash comparison inline (keeps the deploy path and the standalone
    verify-site-deployment.sh script from drifting apart)."""
    text = (REPO_ROOT / "scripts/deploy-site.sh").read_text()
    assert 'source "${SCRIPT_DIR}/lib/site-verify.sh"' in text
    assert "verify_deployed_pcbs" in text
    assert "--no-verify" in text


def test_verify_site_deployment_sh_sources_the_shared_library():
    text = (REPO_ROOT / "scripts/verify-site-deployment.sh").read_text()
    assert 'source "${SCRIPT_DIR}/lib/site-verify.sh"' in text
    assert "verify_deployed_pcbs" in text
