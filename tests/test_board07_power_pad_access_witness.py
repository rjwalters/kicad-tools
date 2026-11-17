"""Power-pad access witness for the committed Board07 routed artifact.

Issue #5507 asks for a *durable* witness that ``U4.C2`` physically joins the
remaining ``+1V2`` pads "on saved and plain-native-refilled outputs", with the
other two power nets connected, all 244 physical pads preserved and no shorts
or physical clearance/hole violations introduced.

Every previously retained witness for that acceptance item lived in a
temporary directory (``/tmp/loom-5507-*``, ``/tmp/5164-judge2-*`` and the
``.loom/sweep-checkpoint/evidence/issue-5507/`` trees named in the issue).
None of those paths survive; the frozen board itself
(``2fb7574b83f06e65b23463c9fbffe98f9fe3adae52803553311e47700ee977b5``) is gone
with them.  What *is* durable is the repository's own committed Board07 routed
artifact, ``boards/07-matchgroup-test/regression-fixture/``, which carries the
same 244-pad inventory and the same 156 / 8 / 18 power-pad counts.  This
module turns the witness into a repeatable check against that artifact, so the
acceptance evidence stops depending on a scratch directory.

Scope, stated plainly:

* this is a **preservation / non-regression** witness on an artifact that is
  already whole -- it is *not* a reproduction of the original open, which
  needed the frozen board and is separately reproduced by the constructed
  controls in ``tests/test_board07_pour_repair_barrel_reuse.py`` and
  ``tests/test_board07_pour_repair_th_pad_reuse.py``;
* it does not restore the paused dedicated Board07 CI jobs, and it is not the
  Linux/native combined qualification #5507 still requires before its parents
  (#5164 / #5286 / #5333) can close.

The board carries four pre-existing signal opens (``DQ3``, ``DQ4``,
``TMDS_D0_N``, ``TMDS_D1_N``).  They are asserted as an explicit, unchanged
contract rather than waved through: the point of the assertion is that *no
power net* is ever among them.
"""

from __future__ import annotations

import importlib.util
import json
import shutil
import sys
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli, run_drc, run_fill_zones

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "07-matchgroup-test"
FIXTURE_DIR = BOARD_DIR / "regression-fixture"
ROUTED_STEM = "matchgroup_test_routed"

POWER_NETS = ("GND", "+1V2", "+1V8")

#: Pad partition every power net must land in: exactly one component each.
EXPECTED_POWER_PAD_GROUPS = {"GND": [156], "+1V2": [8], "+1V8": [18]}

#: Total physical pad records on the artifact (the "all 244 pads preserved"
#: inventory the issue's acceptance names).
EXPECTED_PAD_COUNT = 244

#: The board's unchanged signal-open contract: four opens, no power net.
#: Deliberately the same set ``tests/test_board_07_matchgroup_test.py``
#: already pins as ``EXPECTED_OPEN_NETS`` -- this module widens nothing.
EXPECTED_OPEN_SIGNAL_NETS = {"DQ3", "DQ4", "TMDS_D0_N", "TMDS_D1_N"}

#: Physical DRC families this issue forbids introducing.
FORBIDDEN_DRC_TYPES = {
    "shorting_items",
    "clearance",
    "hole_clearance",
    "hole_near_hole",
    "holes_co_located",
    "drill_out_of_range",
}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generate_design_mod():
    """Load ``boards/07-matchgroup-test/generate_design.py`` as a module."""
    gp = _load_module("board_07_generate_pcb_witness", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_07_generate_schematic_witness", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_07_generate_design_witness", BOARD_DIR / "generate_design.py")


def _pad_records(pcb_path: Path) -> list[tuple[str, str, str]]:
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    analyzer = NetStatusAnalyzer(pcb_path)
    return [
        (footprint.reference, pad.number, pad.net_name)
        for footprint in analyzer.pcb.footprints
        for pad in footprint.pads
    ]


def _pad_groups(mod, pcb_path: Path) -> dict[str, list[list[str]]]:
    audit = mod._audit_pour_nets(pcb_path, list(POWER_NETS))
    return {
        net: [sorted(pad for pad, _is_th in group) for group in audit[net]["pad_groups"]]
        for net in POWER_NETS
    }


@pytest.fixture(scope="module")
def saved_pcb() -> Path:
    pcb = FIXTURE_DIR / f"{ROUTED_STEM}.kicad_pcb"
    assert pcb.exists(), f"Committed Board07 routed artifact missing: {pcb}"
    return pcb


def test_saved_output_keeps_every_power_pad_in_one_component(generate_design_mod, saved_pcb):
    """AC: ``U4.C2`` joins the remaining ``+1V2`` pads on the saved output."""
    groups = _pad_groups(generate_design_mod, saved_pcb)
    assert {net: [len(g) for g in groups[net]] for net in POWER_NETS} == (EXPECTED_POWER_PAD_GROUPS)
    assert "U4.C2" in groups["+1V2"][0]


def test_saved_output_keeps_the_full_pad_inventory(saved_pcb):
    pads = _pad_records(saved_pcb)
    assert len(pads) == EXPECTED_PAD_COUNT
    assert ("U4", "C2", "+1V2") in pads


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_plain_native_refill_preserves_power_connectivity_and_pads(
    generate_design_mod, tmp_path: Path
):
    """AC: the same verdict after a *plain* native refill, plus a clean DRC.

    "Plain" is load-bearing: ``run_fill_zones`` is called with its default
    policy, so the board's own authored project / ``.kicad_dru`` rules are the
    only physical authority.  Nothing here writes native clearance overrides,
    relaxes a rule or re-runs the repair.
    """
    work = tmp_path / "refill"
    work.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy2(FIXTURE_DIR / f"{ROUTED_STEM}{suffix}", work / f"{ROUTED_STEM}{suffix}")
    pcb = work / f"{ROUTED_STEM}.kicad_pcb"

    saved_groups = _pad_groups(generate_design_mod, pcb)
    saved_pads = _pad_records(pcb)

    result = run_fill_zones(pcb)
    assert result.success, result.stderr

    refilled_groups = _pad_groups(generate_design_mod, pcb)
    assert {net: [len(g) for g in refilled_groups[net]] for net in POWER_NETS} == (
        EXPECTED_POWER_PAD_GROUPS
    )
    assert "U4.C2" in refilled_groups["+1V2"][0]
    # The refill did not merely keep the counts -- it kept the same partition.
    assert refilled_groups == saved_groups
    # And it moved no pad: a refill recomputes zone fills, nothing else.
    assert _pad_records(pcb) == saved_pads
    assert len(saved_pads) == EXPECTED_PAD_COUNT


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_native_drc_reports_no_shorts_or_physical_violations(tmp_path: Path):
    """AC: no shorts and no physical clearance/hole violations, power nets whole."""
    work = tmp_path / "drc"
    work.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy2(FIXTURE_DIR / f"{ROUTED_STEM}{suffix}", work / f"{ROUTED_STEM}{suffix}")
    pcb = work / f"{ROUTED_STEM}.kicad_pcb"

    report = work / "drc.json"
    assert run_drc(pcb, report, schematic_parity=False).success
    data = json.loads(report.read_text())

    errors = [v for v in data.get("violations", []) if v.get("severity") == "error"]
    assert errors == [], f"native DRC reported error-severity violations: {errors}"
    offending = {
        v["type"] for v in data.get("violations", []) if v.get("type") in FORBIDDEN_DRC_TYPES
    }
    assert offending == set(), f"physical violations present at any severity: {sorted(offending)}"

    # The unchanged signal-open contract -- and no power net among it.
    open_nets: set[str] = set()
    for item in data.get("unconnected_items", []):
        for member in item.get("items", []):
            description = member.get("description", "")
            if "[" in description and "]" in description:
                open_nets.add(description.split("[", 1)[1].split("]", 1)[0])
    assert open_nets == EXPECTED_OPEN_SIGNAL_NETS
    assert not open_nets & set(POWER_NETS)
