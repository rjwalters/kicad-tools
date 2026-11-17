"""Boards 01, 02, 04 and 06 commit the ``.kicad_dru`` their recipes emit (Issue #8385).

The follow-up to #8357 (board 05, ``test_board05_recipe_dru_reproduces_8357.py``):
under the same operator ruling (commit what the recipe emits, consistent with
#8029), a board's committed routed ``.kicad_dru`` must be the file its recipe
leaves on disk, including the #8048 attach-zone block.

Each recipe's *last* DRU write is replayed on the committed routed board in a
scratch directory, starting -- as a fresh recipe run does -- from no
``.kicad_dru``, and the result is byte-compared with the committed file:

* boards 01, 02 and 06: ``generate_design.py``'s (board 06: ``assembled-demo/build.py``'s
  follow-on export) ``kct export --mfr jlcpcb``, whose
  ``ManufacturingPackage._write_drc_constraints`` re-runs the sidecar writer
  next to the routed board (after ``kct route`` wrote it first);
* board 04: ``manufacturing_process.py:apply_native_floors``, which overwrites
  the stock tier1 floors ``kct route`` wrote with the reviewed paid-drill
  floors (its ``kct export`` runs with ``emit_drc_constraints=False``).

Board 06 (Issue #8404): its committed board is the one ``assembled-demo/build.py``
emits (182 segments / 55 vias, native DRC 0 on the pinned toolchain); the
committed ``.kicad_dru`` is byte-identical to the one that build leaves on disk,
and to this replay.

Each replay was checked against a full recipe run on the pinned toolchain
(``KCT_KICAD_DOCKER=1``, kicad-cli 10.0.6): byte-identical, both from no DRU and
from the committed one.  None of them needs kicad-cli: these boards have no
rule areas, so the keepout step writes nothing.

Board 04 is strict ``xfail``: its recipe emits the block, but its
evidence cannot be re-pinned on main yet (``refresh_readiness.py`` refuses on
unaccepted ``redundant_copper_*`` warnings, independent of the DRU; #8399).
When that lands and the DRU is committed, the strict xfail turns into a
failure that says to drop the marker.

Boards not covered, and why (traced in #8385): board 00's recipe routes
in-process and its routed ``.kicad_pro`` declares no router class, so it never
emits the block; board 03 is suppressed by its own designer clearance rule;
board 05 has its own test; board 07's ``real_design/build.py`` emits the block inside a kicad-cli
validation run and is blocked on the same #8399 refusal.
"""

from __future__ import annotations

import runpy
import shutil
from collections.abc import Callable
from pathlib import Path

import pytest

from kicad_tools.export.manufacturing import ManufacturingPackage, ManufacturingResult
from kicad_tools.manufacturers.attach_zone_dru import has_attach_zone_block

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARDS = REPO_ROOT / "boards"

PENDING_REPIN = pytest.mark.xfail(
    strict=True,
    reason=(
        "recipe emits the attach-zone block, but the evidence re-pin is refused "
        "on main by redundant_copper_* warnings (#8399)"
    ),
)


def _export_writer(routed: Path) -> None:
    """``kct export --mfr jlcpcb`` writes the sidecars next to the input board."""
    result = ManufacturingResult(output_dir=routed.parent / "manufacturing")
    ManufacturingPackage(routed, manufacturer="jlcpcb")._write_drc_constraints(result)
    assert routed.with_suffix(".kicad_dru") in result.drc_constraint_paths


def _board06_pair_rules_then_export(routed: Path) -> None:
    """The floors and attach-zone block, then ``build.py``'s LVDS pair block last.

    Board 06 routes only IN/OUT, so the LVDS pairs need ``build.py``
    (Issue #8404).  The recipe's final DRU carries the pair block after the
    attach-zone block (Issue #8801: the replay used to re-run the exporter
    after the pair rules, which put the pair block first and no longer
    matches a fresh recipe run).
    """
    _export_writer(routed)
    rules = runpy.run_path(str(BOARDS / "06-diffpair-test" / "assembled-demo" / "build.py"))
    rules["write_pair_rules"](routed, BOARDS / "06-diffpair-test" / "output" / "net_class_map.json")


def _board04_native_floors(routed: Path) -> None:
    process = runpy.run_path(str(BOARDS / "04-stm32-devboard" / "manufacturing_process.py"))
    process["apply_native_floors"](routed)


CASES: dict[str, tuple[str, Callable[[Path], None]]] = {
    "01-voltage-divider": ("voltage_divider_routed", _export_writer),
    "02-charlieplex-led": ("charlieplex_3x3_routed", _export_writer),
    "04-stm32-devboard": ("stm32_devboard_routed", _board04_native_floors),
    "06-diffpair-test": ("diffpair_test_routed", _board06_pair_rules_then_export),
}

PARAMS = [
    pytest.param("01-voltage-divider", id="board01"),
    pytest.param("02-charlieplex-led", id="board02"),
    pytest.param("04-stm32-devboard", id="board04", marks=PENDING_REPIN),
    pytest.param("06-diffpair-test", id="board06"),
]


# Board 02's committed DRU was re-pinned from a full recipe run (#8759) and carries
# rules (ampacity, max hole size, B.Silkscreen floors) that this exporter-only
# replay does not emit, so the byte-for-byte replay is still pending there.
BYTE_FOR_BYTE_PARAMS = [
    pytest.param(
        p.values[0],
        id=p.id,
        marks=pytest.mark.xfail(
            strict=True,
            reason="committed DRU re-pinned from a full recipe run; export-only replay differs (#8759)",
        ),
    )
    if p.id == "board02"
    else p
    for p in PARAMS
]


def _committed_dru(board: str) -> str:
    stem = CASES[board][0]
    return (BOARDS / board / "output" / f"{stem}.kicad_dru").read_text(encoding="utf-8")


def _recipe_dru(board: str, tmp_path: Path) -> str:
    stem, last_writer = CASES[board]
    output = BOARDS / board / "output"
    for suffix in (".kicad_pcb", ".kicad_pro"):
        shutil.copy2(output / f"{stem}{suffix}", tmp_path / f"{stem}{suffix}")
    routed = tmp_path / f"{stem}.kicad_pcb"
    assert not routed.with_suffix(".kicad_dru").exists()
    last_writer(routed)
    return routed.with_suffix(".kicad_dru").read_text(encoding="utf-8")


@pytest.mark.parametrize("board", PARAMS)
def test_committed_dru_carries_the_attach_zone_block(board):
    assert has_attach_zone_block(_committed_dru(board))


@pytest.mark.parametrize("board", BYTE_FOR_BYTE_PARAMS)
def test_recipe_emits_the_committed_dru_byte_for_byte(board, tmp_path):
    assert _recipe_dru(board, tmp_path) == _committed_dru(board), (
        f"board {board}'s recipe no longer emits the committed .kicad_dru; commit "
        "what it emits, refill the zones and re-pin the evidence on the pinned "
        "toolchain (scripts/boards/refresh_readiness.py)"
    )


@pytest.mark.parametrize("board", ["02-charlieplex-led", "04-stm32-devboard"])
def test_pending_boards_recipe_does_emit_the_block(board, tmp_path):
    """The xfails above are pending the re-pin, not a recipe that stopped emitting."""
    assert has_attach_zone_block(_recipe_dru(board, tmp_path))
