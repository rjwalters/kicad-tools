"""Exit-code logic of ``scripts/ci/board_05_unattended.py`` (issue #7834).

The gate is pure (a graded row in, failure reasons out), so it is tested on
fixture rows; the route itself is exercised by the scheduled workflow
``.github/workflows/nightly-board-05-unattended.yml``.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ci" / "board_05_unattended.py"
WORKFLOW = REPO / ".github" / "workflows" / "nightly-board-05-unattended.yml"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("board_05_unattended", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["board_05_unattended"] = module
    spec.loader.exec_module(module)
    return module


def clean_row(**overrides) -> dict:
    row = {
        "seed": 42,
        "exit_code": 0,
        "artifact": "final",
        "cache_hit": False,
        "oracle_unverified": False,
        "nets_complete": 37,
        "nets_total": 37,
        "kicad_drc_errors": 0,
        "kicad_drc_unconnected": 0,
        "kicad_erc_violations": 0,
        "kct_check_errors": 0,
        "lvs_copper_clean": True,
        "lvs_label_clean": True,
    }
    row.update(overrides)
    return row


def test_clean_row_passes(mod):
    assert mod.gate_failures(clean_row()) == []
    assert mod.exit_code([clean_row(), clean_row(seed=7)]) == 0


@pytest.mark.parametrize(
    ("overrides", "needle"),
    [
        ({"nets_complete": 36}, "nets 36/37"),
        ({"kicad_drc_unconnected": 1}, "unconnected"),
        ({"kicad_drc_errors": 2}, "kicad-cli DRC errors: 2"),
        ({"kct_check_errors": 1}, "kct check"),
        ({"kicad_erc_violations": 3}, "ERC"),
        ({"lvs_copper_clean": False}, "copper LVS dirty"),
        ({"lvs_label_clean": False}, "label LVS dirty"),
        ({"exit_code": 5}, "exited 5"),
        ({"artifact": "deadline"}, "deadline"),
        ({"cache_hit": True}, "cached"),
        (
            {"oracle_unverified": True, "oracle_unverified_reason": "oracle_failed"},
            "oracle did not answer (oracle_failed)",
        ),
        ({"error": "no routed output (exit_code=124)"}, "no routed output"),
    ],
)
def test_any_single_failure_fails_the_run(mod, overrides, needle):
    failures = mod.gate_failures(clean_row(**overrides))
    assert any(needle in f for f in failures), failures
    # One bad seed fails the whole run, however many seeds are clean.
    assert mod.exit_code([clean_row(), clean_row(**overrides), clean_row()]) == 1


@pytest.mark.parametrize(
    "missing",
    [
        "nets_complete",
        "kicad_drc_errors",
        "kicad_drc_unconnected",
        "kicad_erc_violations",
        "kct_check_errors",
        "lvs_copper_clean",
        "lvs_label_clean",
    ],
)
def test_a_gate_that_did_not_run_is_a_failure(mod, missing):
    row = clean_row()
    del row[missing]
    assert mod.gate_failures(row), f"absent {missing} must not read as clean"
    assert mod.exit_code([row]) == 1


def test_zero_nets_is_not_complete(mod):
    assert mod.gate_failures(clean_row(nets_complete=0, nets_total=0))


def test_no_rows_fails(mod):
    assert mod.exit_code([]) == 1


def test_grade_only_row_without_route_fields_can_pass(mod):
    row = clean_row()
    for key in ("exit_code", "artifact", "cache_hit", "oracle_unverified"):
        del row[key]
    assert mod.gate_failures(row) == []


def test_no_allowlist_machinery(mod):
    text = SCRIPT.read_text()
    # No identity allowance, no accepted-warnings table, no tolerated-net list.
    for token in ("identity_allowance", "KNOWN_WARNINGS", "allowed_nets", "tolerated"):
        assert token not in text


def test_route_flags_are_the_6508_protocol(mod):
    flags = mod.route_flags(7)
    for flag in (
        "--no-auto-layers",
        "--deterministic-budget",
        "--no-placement-delta-feedback",
        "--deterministic-rescue",
        "--no-fallback-bidirectional",
    ):
        assert flag in flags
    assert flags[flags.index("--per-net-iterations") + 1] == "1000000"
    assert flags[-2:] == ["--seed", "7"]
    # The replay path is never touched.
    assert "routing.json" not in " ".join(flags)


def test_never_reads_routing_json(mod):
    code = SCRIPT.read_text()
    assert "apply_routing" not in code
    assert "from routing import" not in code


def test_workflow_runs_the_script_at_three_seeds_and_is_not_per_pr():
    text = WORKFLOW.read_text()
    assert "scripts/ci/board_05_unattended.py" in text
    assert "pull_request" not in text
    assert "schedule:" in text and "workflow_dispatch" in text
    assert "kct build-native" in text
    assert "continue-on-error" not in text
    seeds = [s for s in ("42", "7", "1337") if f"- {s}" in text or f"{s}," in text]
    assert len(seeds) >= 3, seeds
    # The image digest is read from ci.yml, never re-pinned here.
    assert not re.search(r"kicad/kicad@sha256:[0-9a-f]{64}", text)


def test_non_pinned_kicad_cli_refuses_with_exit_2(mod, monkeypatch, tmp_path):
    """A kicad-cli other than the pinned release is a refusal (2), never a pass."""
    import contextlib

    from kicad_tools.cli import kicad_toolchain

    @contextlib.contextmanager
    def refuse(_mounts):
        raise kicad_toolchain.KiCadEnvironmentError("kicad-cli 10.0.7 is not the pinned release")
        yield  # pragma: no cover

    monkeypatch.setattr(kicad_toolchain, "evidence_toolchain", refuse)
    graded = []
    monkeypatch.setattr(mod, "grade", lambda *a, **k: graded.append(a) or {})
    pcb = tmp_path / "routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert mod.main(["--work-dir", str(tmp_path / "w"), "--grade-only", str(pcb)]) == 2
    assert graded == []
