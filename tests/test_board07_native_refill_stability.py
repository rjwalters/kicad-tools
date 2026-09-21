"""Board07 power-pad access must survive a plain native refill (issue #5507).

PROVENANCE of the fixture directory ``tests/fixtures/board07_native_refill_5507/``:
captured 2026-09-20 from a fresh current-main route step
(``generate_design.py <dir> --step route --seed 42``, source ``f1d81c88``,
KiCad CLI 10.0.6, macOS arm64; routed input sha256 ``a4b3e4cd…``), then
refilled twice with the plain native filler (``run_fill_zones`` default
policy) to a stable state.

The recipe's own in-loop audits had reported PASS for that run -- GND 156 /
+1V2 8 / +1V8 18 pads, each in one copper component -- yet the native
refill splits ``+1V2`` into 7+1: the recipe's fill engine keeps the In2.Cu
``+1V2`` pocket under the U4 BGA attached to the net body, while KiCad's
own engine severs it around the escape-via barrels, stranding ``U4.E6``
(whose only net item is its stitching via).  A power pad that reads
connected on the saved bytes but strands under the board's own authored
refill rules is exactly the #5507 acceptance clause: connectivity "on saved
AND plain-native-refilled outputs".

The stranded state also exposed a second, non-physical defect this module
pins: ``_repair_pour_connectivity`` derived its keep-in-board bounds from
``generate_pcb`` origin/size CONSTANTS -- the historical (100, 100) routing
frame -- while the shipped artifact is translated to its sheet-centered
frame, so repairing the shipped bytes rejected every candidate at the
bounds check and printed ``UNREPAIRED`` for a reason that was never
physical.  The bounds now follow the board's own Edge.Cuts bbox, making the
stage's verdict frame-independent; in the authored routing frame the
derived bounds equal the old constants exactly, so in-recipe behaviour is
unchanged.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import sys
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.pcb.center_sheet import edge_cuts_bbox, translate_pcb_text

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "07-matchgroup-test"
FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "board07_native_refill_5507"
ROUTED_STEM = "matchgroup_test_routed"

POWER_NETS = ("GND", "+1V2", "+1V8")

#: Pinned so a fixture swap is caught: this exact byte state is the
#: documented 7+1 native-refill split of the fresh f1d81c88 route.
FIXTURE_PCB_SHA256 = "3291369a7df3aa7b79bc6c661f9ccb90bcbe8feb6a29d2dd0cc9fbb119429e9c"

#: The documented split: U4.E6 alone, the other seven +1V2 pads together.
EXPECTED_SPLIT = {"GND": [156], "+1V2": [7, 1], "+1V8": [18]}
STRANDED_PAD = "U4.E6"

EXPECTED_PAD_COUNT = 244


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
    gp = _load_module("board_07_generate_pcb_nrs", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_07_generate_schematic_nrs", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_07_generate_design_nrs", BOARD_DIR / "generate_design.py")


@pytest.fixture(scope="module")
def fixture_pcb() -> Path:
    pcb = FIXTURE_DIR / f"{ROUTED_STEM}.kicad_pcb"
    assert pcb.exists(), f"fixture missing: {pcb}"
    return pcb


def _groups(mod, pcb_path: Path, net_names=POWER_NETS) -> dict[str, list[list[str]]]:
    audit = mod._audit_pour_nets(pcb_path, list(net_names))
    return {
        net: [sorted(pad for pad, _ in group) for group in audit[net]["pad_groups"]]
        for net in net_names
    }


def _group_sizes(groups: dict[str, list[list[str]]]) -> dict[str, list[int]]:
    return {net: [len(g) for g in groups[net]] for net in groups}


def test_fixture_bytes_are_the_documented_state(fixture_pcb: Path):
    digest = hashlib.sha256(fixture_pcb.read_bytes()).hexdigest()
    assert digest == FIXTURE_PCB_SHA256


def test_fixture_carries_the_documented_native_refill_split(generate_design_mod, fixture_pcb):
    """Input contract: +1V2 split 7+1 with U4.E6 stranded; the rest whole."""
    groups = _groups(generate_design_mod, fixture_pcb)
    assert _group_sizes(groups) == {net: list(sizes) for net, sizes in EXPECTED_SPLIT.items()}
    assert groups["+1V2"][1] == [STRANDED_PAD]


def test_repair_reconnects_the_native_refill_split(
    generate_design_mod, fixture_pcb, capsys, tmp_path
):
    """The stranded pad is repairable in the SHIPPED sheet-centered frame.

    Before the #5507 bounds fix this exact call printed ``UNREPAIRED`` and
    placed nothing: every candidate point of the real board fell outside the
    ``generate_pcb`` constant-derived bounds of the historical routing
    frame.  With Edge.Cuts-derived bounds one lawful bridge exists -- a
    0.7 mm In2.Cu segment threading past the BGA edge, no new drills.
    """
    work = tmp_path / "repaired.kicad_pcb"
    shutil.copy2(fixture_pcb, work)
    before = work.read_text()

    vias, bridges = generate_design_mod._repair_pour_connectivity(work, list(POWER_NETS))
    out = capsys.readouterr().out
    assert "UNREPAIRED" not in out
    assert (vias, bridges) == (0, 1)

    groups = _groups(generate_design_mod, work)
    assert _group_sizes(groups) == {"GND": [156], "+1V2": [8], "+1V8": [18]}
    assert STRANDED_PAD in groups["+1V2"][0]

    after = work.read_text()
    added = [ln for ln in after.splitlines() if ln not in before.splitlines()]
    segs = [ln for ln in added if "(segment " in ln]
    assert len(segs) == 1
    assert '(layer "In2.Cu")' in segs[0]
    # Preservation: the repair only appends; nothing pre-existing is dropped.
    for line in before.splitlines():
        assert line in after.splitlines()

    # The merged state is stable: a second pass has nothing left to do.
    again = generate_design_mod._repair_pour_connectivity(work, list(POWER_NETS))
    assert again == (0, 0)
    assert "UNREPAIRED" not in capsys.readouterr().out


def test_repair_verdict_is_frame_independent(generate_design_mod, fixture_pcb, tmp_path):
    """Same board, same copper, any sheet position: same repair verdict.

    This is the property the constant-derived bounds violated: translating
    the identical board (and nothing else) moved it into the historical
    frame's bounds and flipped the verdict from UNREPAIRED to a legal
    bridge.  A physical legality verdict must not depend on where the sheet
    happens to sit.
    """
    work = tmp_path / "translated.kicad_pcb"
    work.write_text(translate_pcb_text(fixture_pcb.read_text(), 50.0, 50.0))

    vias, bridges = generate_design_mod._repair_pour_connectivity(work, list(POWER_NETS))
    assert (vias, bridges) == (0, 1)
    groups = _groups(generate_design_mod, work)
    assert _group_sizes(groups) == {"GND": [156], "+1V2": [8], "+1V8": [18]}


def test_edge_cuts_bounds_equal_the_authored_constants_in_recipe_frame(generate_design_mod):
    """No in-recipe behaviour change: derived bounds == the old constants.

    The recipe's opening leg translates the sheet-centered input into the
    historical (100, 100) routing frame before routing, and every in-recipe
    repair call sees that translated board.  For that frame the Edge.Cuts
    bbox of the generated board is exactly the authored origin and size, so
    every in-recipe repair decision is numerically identical before and
    after the #5507 bounds fix.
    """
    unrouted = BOARD_DIR / "regression-fixture" / "matchgroup_test.kicad_pcb"
    sheet_bbox = edge_cuts_bbox(unrouted.read_text())
    assert sheet_bbox is not None
    gp = generate_design_mod.generate_pcb
    # The recipe's own opening-leg translation (route_pcb): frame_dx/dy move
    # the sheet bbox min corner onto the authored origin.
    historical = translate_pcb_text(
        unrouted.read_text(),
        gp.BOARD_ORIGIN_X - sheet_bbox[0],
        gp.BOARD_ORIGIN_Y - sheet_bbox[1],
    )
    bbox = edge_cuts_bbox(historical)
    assert bbox == (
        gp.BOARD_ORIGIN_X,
        gp.BOARD_ORIGIN_Y,
        gp.BOARD_ORIGIN_X + gp.BOARD_WIDTH,
        gp.BOARD_ORIGIN_Y + gp.BOARD_HEIGHT,
    )


def test_repair_keeps_the_full_pad_inventory(generate_design_mod, fixture_pcb, tmp_path):
    from kicad_tools.analysis.net_status import NetStatusAnalyzer

    work = tmp_path / "inventory.kicad_pcb"
    shutil.copy2(fixture_pcb, work)
    before = [
        (fp.reference, pad.number, pad.net_name)
        for fp in NetStatusAnalyzer(work).pcb.footprints
        for pad in fp.pads
    ]
    generate_design_mod._repair_pour_connectivity(work, list(POWER_NETS))
    after = [
        (fp.reference, pad.number, pad.net_name)
        for fp in NetStatusAnalyzer(work).pcb.footprints
        for pad in fp.pads
    ]
    assert before == after
    assert len(after) == EXPECTED_PAD_COUNT
    assert ("U4", "E6", "+1V2") in after


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
def test_stability_pass_repairs_the_split_and_verifies_clean(generate_design_mod, tmp_path):
    """The recipe's stability pass converges on exactly this state.

    Drives ``_native_refill_stability_pass`` with the fixture as the
    artifact: round 1 must detect the 7+1 split under a plain native
    refill, repair it, restore the recipe fills, and round 2's fresh native
    refill must then audit clean -- the pass only returns True on a
    verified-clean native refill, never on the saved-state audit alone.
    """
    work = tmp_path / ROUTED_STEM
    work.mkdir()
    for suffix in (".kicad_pcb", ".kicad_pro", ".kicad_dru"):
        shutil.copy2(FIXTURE_DIR / f"{ROUTED_STEM}{suffix}", work / f"{ROUTED_STEM}{suffix}")
    pcb = work / f"{ROUTED_STEM}.kicad_pcb"
    fill_argv = [sys.executable, "-m", "kicad_tools.cli", "zones", "fill", str(pcb)]

    assert generate_design_mod._native_refill_stability_pass(pcb, list(POWER_NETS), fill_argv)

    groups = _groups(generate_design_mod, pcb)
    assert _group_sizes(groups) == {"GND": [156], "+1V2": [8], "+1V8": [18]}
    assert STRANDED_PAD in groups["+1V2"][0]
