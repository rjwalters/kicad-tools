"""End-to-end test of the ``manufacturing-readiness`` sign-off gates (issue #7136).

The agent-facing sign-off skill promises to refuse a board if any gate fails or
could not run.  Issue #7136 found that the documented commands did not keep
that promise: Gate 1 exited 2 on every board, the kicad-cli cross-gate exited 0
on errors, and the export never saw the DRC report.

This test does not mirror the skill's commands -- it **extracts** the
``# sign-off gate N`` bash blocks from the package-data source of truth,
substitutes the placeholders, and runs them on four copies of board 00:

* ``good`` -- the routed board, unmodified;
* ``unrouted`` -- every ``(segment`` and ``(zone`` form stripped (3 opens);
* ``clearance`` -- one GND track injected 0.05 mm from the LED_ANODE track;
* ``nosch`` -- the schematic deleted (ERC and LVS cannot run).

Each block's exit code is the gate verdict.  The good board must pass gates
1-3 and leave a manifest; each bad board must be refused at the right gate.

The gate-marker test never skips: a skill edit that drops a marker must fail
CI.  The board runs skip locally when ``kicad-cli``, ``jq`` or ``uv`` is missing,
but FAIL when ``CI`` is set so the coverage cannot silently vanish.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL = REPO_ROOT / "src" / "kicad_tools" / "agent_skills" / "kct" / "manufacturing-readiness.md"
BOARD_DIR = REPO_ROOT / "boards" / "00-simple-led" / "output"
PCB_NAME = "simple_led_routed.kicad_pcb"
BOARD_FILES = (
    PCB_NAME,
    "simple_led_routed.kicad_pro",
    "simple_led_routed.kicad_dru",
    "simple_led.kicad_pro",
    "simple_led.kicad_sch",
)
TIER = "jlcpcb"
GATES = (1, 2, 3)
CASES = ("good", "unrouted", "clearance", "nosch")

_BLOCK_RE = re.compile(r"```bash\n(.*?)```", re.DOTALL)
_MARKER_RE = re.compile(r"\A# sign-off gate (\d+)\s*\n")


def extract_gate_blocks(text: str) -> dict[int, str]:
    """Return ``{gate number: bash block}`` for every ``# sign-off gate N`` block."""
    blocks: dict[int, str] = {}
    for match in _BLOCK_RE.finditer(text):
        body = match.group(1)
        marker = _MARKER_RE.match(body)
        if marker:
            gate = int(marker.group(1))
            assert gate not in blocks, f"duplicate '# sign-off gate {gate}' block"
            blocks[gate] = body
    return blocks


def test_skill_gate_markers_present() -> None:
    """Gates 1-3 (and the conditional HV Gate 4) are extractable.  Never skips."""
    blocks = extract_gate_blocks(SKILL.read_text(encoding="utf-8"))
    assert {1, 2, 3, 4} <= set(blocks), (
        f"{SKILL} lost a '# sign-off gate N' block marker; found gates {sorted(blocks)}"
    )
    # The pass rules the #7136 fix depends on stay in the blocks themselves.
    assert "--allow-incomplete" not in blocks[1]
    assert '.meta_checks.erc.status == "PASSED"' in blocks[1]
    # A stale manifest from a previous bundle must not fail Gate 1 (#8158).
    assert "rm -f <dir>/check-report.json <dir>/manifest.json" in blocks[1]
    for flag in ("--refill-zones", "--severity-error", "--exit-code-violations", "--format json"):
        assert flag in blocks[2]
    assert "unconnected_items" in blocks[2]
    assert "--drc-report <pcb-dir>/drc_report.json" in blocks[3]
    assert "rm -f <dir>/manifest.json" in blocks[3]
    assert ".isolation" in blocks[4]


# ---------------------------------------------------------------------------
# Board fixtures
# ---------------------------------------------------------------------------


def _strip_forms(text: str, heads: tuple[str, ...]) -> str:
    """Remove every balanced ``(head ...)`` s-expression form from *text*."""
    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        head = next(
            (h for h in heads if text.startswith("(" + h, i) and text[i + 1 + len(h)] in " \t\n)"),
            None,
        )
        if head is None:
            out.append(text[i])
            i += 1
            continue
        depth = 0
        j = i
        while True:
            c = text[j]
            if c == '"':
                j = text.index('"', j + 1)
            elif c == "(":
                depth += 1
            elif c == ")":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        i = j + 1
    return "".join(out)


# One GND track on F.Cu at y=85.75: its edge sits 0.05 mm from the 0.2 mm
# LED_ANODE track at y=85.5 (net 3 = GND, net 2 = LED_ANODE in board 00).
_CLEARANCE_SEGMENT = (
    "\t(segment\n"
    "\t\t(start 150.5 85.75)\n"
    "\t\t(end 154.5 85.75)\n"
    "\t\t(width 0.2)\n"
    '\t\t(layer "F.Cu")\n'
    '\t\t(uuid "71360000-0000-4000-8000-000000000001")\n'
    "\t\t(net 3)\n"
    "\t)\n"
)


def _make_case(case: str, dest: Path) -> Path:
    dest.mkdir(parents=True)
    for name in BOARD_FILES:
        shutil.copy2(BOARD_DIR / name, dest / name)
    pcb = dest / PCB_NAME
    text = pcb.read_text(encoding="utf-8")
    if case == "unrouted":
        text = _strip_forms(text, ("segment", "zone"))
        assert "(segment" not in text and "(zone\n" not in text
    elif case == "clearance":
        assert '(net 2 "LED_ANODE")' in text and '(net 3 "GND")' in text
        # Insert at top level, just before the board's closing forms.
        k = text.rindex("\n\t(embedded_fonts")
        text = text[: k + 1] + _CLEARANCE_SEGMENT + text[k + 1 :]
    elif case == "nosch":
        (dest / "simple_led.kicad_sch").unlink()
    pcb.write_text(text, encoding="utf-8")
    return pcb


def _render(block: str, pcb: Path, out_dir: Path) -> str:
    return (
        block.replace("<board.kicad_pcb>", shlex.quote(str(pcb)))
        .replace("<pcb-dir>", shlex.quote(str(pcb.parent)))
        .replace("<dir>", shlex.quote(str(out_dir)))
        .replace("<tier>", TIER)
    )


def _tool_env() -> dict[str, str]:
    env = dict(os.environ)
    # ``kct`` comes from this interpreter's environment, as it would from a
    # consumer repo's venv; ``uv run python`` (the kicad-cli lookup) resolves
    # the same project from REPO_ROOT and must not re-sync it mid-test.
    env["PATH"] = os.pathsep.join([str(Path(sys.executable).parent), env.get("PATH", "")])
    env["UV_NO_SYNC"] = "1"
    return env


def _prerequisites_missing() -> str | None:
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        return "kicad-cli not installed"
    if shutil.which("jq") is None:
        return "jq not installed"
    if shutil.which("uv") is None:
        return "uv not installed (the skill resolves kicad-cli via 'uv run python')"
    if not BOARD_DIR.joinpath(PCB_NAME).exists():
        return f"board 00 fixture missing: {BOARD_DIR / PCB_NAME}"
    return None


def _run_case(case: str, root: Path) -> dict:
    """Run every gate block on one case (regardless of earlier refusals)."""
    reason = _prerequisites_missing()
    if reason:
        # CI must not silently lose this coverage: a missing tool fails there.
        if os.environ.get("CI"):
            pytest.fail(f"sign-off e2e prerequisite missing in CI: {reason}")
        pytest.skip(reason)

    blocks = extract_gate_blocks(SKILL.read_text(encoding="utf-8"))
    assert set(GATES) <= set(blocks), f"gate markers missing: {sorted(blocks)}"
    env = _tool_env()
    pcb = _make_case(case, root / case)
    out_dir = pcb.parent / "manufacturing"
    if case == "good":
        # Re-tapeout into a populated bundle dir (#8158): a stale manifest
        # listing check-report.json must not make Gate 1 refuse.
        out_dir.mkdir(parents=True, exist_ok=True)
        (out_dir / "manifest.json").write_text(
            json.dumps({"files": [{"path": "check-report.json"}]}), encoding="utf-8"
        )
    results: dict[int, subprocess.CompletedProcess[str]] = {}
    for gate in GATES:
        results[gate] = subprocess.run(
            ["bash", "-c", _render(blocks[gate], pcb, out_dir)],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=240,
        )
    return {"pcb": pcb, "out_dir": out_dir, "results": results}


@pytest.fixture(scope="module")
def _case_cache(tmp_path_factory: pytest.TempPathFactory) -> dict:
    return {"root": tmp_path_factory.mktemp("signoff"), "runs": {}}


@pytest.fixture
def run_case(_case_cache):
    """Return ``run_case(case)``: run (once per module) the gates for one case."""

    def _get(case: str) -> dict:
        if case not in _case_cache["runs"]:
            _case_cache["runs"][case] = _run_case(case, _case_cache["root"])
        return _case_cache["runs"][case]

    return _get


def _rc(run: dict, gate: int) -> int:
    return run["results"][gate].returncode


def _diag(run: dict, case: str, gate: int) -> str:
    proc = run["results"][gate]
    return f"{case} gate {gate} rc={proc.returncode}\nSTDOUT:\n{proc.stdout[-3000:]}\nSTDERR:\n{proc.stderr[-3000:]}"


def _first_refusal(run: dict) -> int | None:
    return next((g for g in GATES if _rc(run, g) != 0), None)


@pytest.mark.integration
@pytest.mark.timeout(300)
class TestSignoffSkillEndToEnd:
    @pytest.mark.parametrize(
        ("case", "gate"),
        [("good", None), ("unrouted", 1), ("clearance", 1), ("nosch", 1)],
    )
    def test_first_refusing_gate(self, run_case, case, gate):
        run = run_case(case)
        assert _first_refusal(run) == gate, "\n\n".join(_diag(run, case, g) for g in GATES)

    def test_good_board_signs_off_with_fresh_manifest(self, run_case):
        run = run_case("good")
        for gate in GATES:
            assert _rc(run, gate) == 0, _diag(run, "good", gate)
        out_dir = run["out_dir"]
        assert (out_dir / "manifest.json").stat().st_size > 0
        report = json.loads((out_dir / "check-report.json").read_text())
        # kct check itself said INCOMPLETE (Manifest NOT RUN before export);
        # the gate rule, not that exit code, is the verdict.
        assert report["meta_checks"]["manifest"]["status"] == "NOT RUN"

    @pytest.mark.parametrize("case", ["unrouted", "clearance"])
    def test_drc_and_export_gates_refuse_independently(self, run_case, case):
        """Gates 2 and 3 refuse the defective copper even if Gate 1 were misread."""
        run = run_case(case)
        assert _rc(run, 2) != 0, _diag(run, case, 2)
        assert _rc(run, 3) != 0, _diag(run, case, 3)
        assert not (run["out_dir"] / "manifest.json").exists()

        drc = json.loads((run["pcb"].parent / "drc_report.json").read_text())
        if case == "unrouted":
            assert len(drc["unconnected_items"]) > 0
        else:
            types = {v["type"] for v in drc["violations"] if v["severity"] == "error"}
            assert "clearance" in types, drc["violations"]

    def test_schematic_less_board_refused_for_erc_and_lvs_not_run(self, run_case):
        run = run_case("nosch")
        report = json.loads((run["out_dir"] / "check-report.json").read_text())
        assert report["meta_checks"]["erc"]["status"] == "NOT RUN"
        assert report["meta_checks"]["lvs"]["status"] == "NOT RUN"
        # The copper itself is clean: only Gate 1's NOT RUN rule catches it.
        assert _rc(run, 2) == 0, _diag(run, "nosch", 2)

    @pytest.mark.parametrize("case", CASES)
    def test_refused_export_leaves_no_manifest(self, run_case, case):
        run = run_case(case)
        manifest = run["out_dir"] / "manifest.json"
        if _rc(run, 3) == 0:
            assert manifest.stat().st_size > 0
        else:
            assert not manifest.exists(), _diag(run, case, 3)
