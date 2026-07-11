"""Tests for net_class_map sidecar threading in report generation (Part B of #4008).

``kct export``'s report.md DRC section used to run ``ManufacturingAudit`` with
``net_class_map=None``, so the three sidecar-gated rule families
(``diffpair_length_skew``, ``diffpair_routing_continuity``,
``match_group_length_skew``) silently no-op'd — report.md printed
"Errors 0 / PASS" on board 07 which has real blocking diff-pair errors.

These tests assert:

* ``resolve_committed_net_class_map`` returns the committed sidecar for
  boards 03/06/07 and ``None`` for the no-sidecar boards (00/01/02/04/05).
* ``ReportDataCollector`` forwards ``net_class_map_path`` so the DRC snapshot
  evaluates the sidecar-gated families (board 07: passed=False, blocking > 0).
* A no-sidecar board keeps the graceful no-op behavior (no exception, no
  false-positive gating).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.report.collector import ReportDataCollector
from kicad_tools.report.net_class_map import (
    SIDECAR_FILENAME,
    resolve_committed_net_class_map,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

BOARD_07_PCB = REPO_ROOT / "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"
BOARD_04_PCB = REPO_ROOT / "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"
BOARD_05_PCB = REPO_ROOT / "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"

# Sidecar-gated rule families that no-op without a net_class_map (#2684, #4008).
GATED_RULE_FAMILIES = {
    "diffpair_length_skew",
    "diffpair_routing_continuity",
    "match_group_length_skew",
}


def _collect_drc(pcb_path: Path, sidecar: Path | None, tmp_path: Path) -> dict:
    """Run the collector's DRC snapshot and return its ``data`` payload."""
    collector = ReportDataCollector(
        pcb_path=pcb_path,
        net_class_map_path=sidecar,
        skip_erc=True,
    )
    files = collector.collect_all(tmp_path)
    drc_json = json.loads(Path(files["drc_summary"]).read_text())
    return drc_json["data"]


# --------------------------------------------------------------------------
# resolve_committed_net_class_map
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "board_dir",
    ["03-usb-joystick", "06-diffpair-test", "07-matchgroup-test"],
)
def test_resolver_finds_committed_sidecar(board_dir: str) -> None:
    """Boards 03/06/07 all commit a net_class_map.json sidecar."""
    output = REPO_ROOT / "boards" / board_dir / "output"
    routed = next(output.glob("*_routed.kicad_pcb"), None)
    if routed is None:
        pytest.skip(f"{board_dir} has no routed PCB checked in")

    sidecar = resolve_committed_net_class_map(routed)
    assert sidecar is not None
    assert sidecar.name == SIDECAR_FILENAME
    assert sidecar.is_file()
    # The sidecar must sit next to the routed PCB.
    assert sidecar.parent == routed.resolve().parent


@pytest.mark.parametrize(
    "board_dir",
    [
        "00-simple-led",
        "01-voltage-divider",
        "02-charlieplex-led",
        "04-stm32-devboard",
        "05-bldc-motor-controller",
    ],
)
def test_resolver_returns_none_without_sidecar(board_dir: str) -> None:
    """No-sidecar boards resolve to None (graceful no-op contract, AC 4)."""
    output = REPO_ROOT / "boards" / board_dir / "output"
    routed = next(output.glob("*_routed.kicad_pcb"), None)
    if routed is None:
        # Some early boards ship only an unrouted PCB; any PCB works for the
        # "no sidecar committed" assertion.
        routed = next(output.glob("*.kicad_pcb"), None)
    if routed is None:
        pytest.skip(f"{board_dir} has no PCB checked in")

    assert resolve_committed_net_class_map(routed) is None


def test_resolver_returns_none_for_missing_neighbor(tmp_path: Path) -> None:
    """A PCB with no adjacent sidecar resolves to None, not an exception."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert resolve_committed_net_class_map(pcb) is None


# --------------------------------------------------------------------------
# ReportDataCollector threading
# --------------------------------------------------------------------------


def test_collector_init_defaults_net_class_map_to_none() -> None:
    """The new parameter defaults to None (backward compatible)."""
    collector = ReportDataCollector(pcb_path=Path("board.kicad_pcb"))
    assert collector.net_class_map_path is None


def test_collector_init_coerces_net_class_map_to_path() -> None:
    """A string net_class_map_path is coerced to a Path."""
    collector = ReportDataCollector(
        pcb_path=Path("board.kicad_pcb"),
        net_class_map_path="some/net_class_map.json",
    )
    assert collector.net_class_map_path == Path("some/net_class_map.json")


@pytest.mark.slow
def test_board_07_gates_with_sidecar(tmp_path: Path) -> None:
    """With the committed sidecar, board 07's DRC snapshot gates (FAIL).

    This is acceptance criterion 3: the report's DRC section must evaluate
    the sidecar-gated families rather than reporting a false PASS.
    """
    if not BOARD_07_PCB.is_file():
        pytest.skip("board 07 routed PCB not checked in")

    sidecar = resolve_committed_net_class_map(BOARD_07_PCB)
    assert sidecar is not None, "board 07 must have a committed sidecar"

    drc = _collect_drc(BOARD_07_PCB, sidecar, tmp_path)

    assert drc["passed"] is False
    assert drc["blocking_count"] > 0
    # At least one sidecar-gated family must now appear in the breakdown.
    evaluated = set(drc.get("violations_by_type", {})) & GATED_RULE_FAMILIES
    assert evaluated, (
        "expected a sidecar-gated rule family in violations_by_type, "
        f"got {sorted(drc.get('violations_by_type', {}))}"
    )


@pytest.mark.slow
def test_board_07_no_op_without_sidecar(tmp_path: Path) -> None:
    """Without a sidecar, board 07 keeps the graceful no-op (today's behavior).

    Contrast with the gated case above: passing net_class_map_path=None must
    suppress the diff-pair / match-group families and report PASS. This proves
    the fix is driven by the sidecar and not an unconditional behavior change.
    """
    if not BOARD_07_PCB.is_file():
        pytest.skip("board 07 routed PCB not checked in")

    drc = _collect_drc(BOARD_07_PCB, None, tmp_path)

    assert drc["passed"] is True
    assert drc["blocking_count"] == 0
    # None of the sidecar-gated families may appear when no map is supplied.
    assert not (set(drc.get("violations_by_type", {})) & GATED_RULE_FAMILIES)


@pytest.mark.slow
@pytest.mark.parametrize("pcb_path", [BOARD_04_PCB, BOARD_05_PCB])
def test_no_sidecar_board_graceful(pcb_path: Path, tmp_path: Path) -> None:
    """A board with no committed sidecar produces a DRC snapshot with no
    sidecar-gated families and no exception (AC 4, regression)."""
    if not pcb_path.is_file():
        pytest.skip(f"{pcb_path.name} not checked in")

    # Resolution yields None, so the report keeps its no-op behavior.
    assert resolve_committed_net_class_map(pcb_path) is None

    drc = _collect_drc(pcb_path, None, tmp_path)
    assert not (set(drc.get("violations_by_type", {})) & GATED_RULE_FAMILIES)
