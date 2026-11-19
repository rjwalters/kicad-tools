"""Board 01 same-net via / pad-aperture repair (issue #9058).

``boards/01-voltage-divider/generate_design.py`` Step 5.55 slides any via
whose mask aperture overlaps its own pad's aperture out along its attached
traces.  The committed routed board still carries both offending VOUT vias
(at R1.2 and R2.1), so a scratch copy of it is a hermetic fixture: no
router, no kicad-cli.
"""

from __future__ import annotations

import importlib.util
import re
import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "01-voltage-divider"
COMMITTED_ROUTED = BOARD_DIR / "output" / "voltage_divider_routed.kicad_pcb"

# The two moves the repair makes on the committed copper (also what a full
# pinned-toolchain regen logged in the PR #9277 review).
EXPECTED_MOVES = {
    (16.0, 8.9): (16.0, 9.5),  # out of R1/2's aperture
    (14.0, 16.0): (14.0, 15.5),  # out of R2/1's aperture
}

# B.Cu segment attached to the (14.0, 16.0) via; vertical in the committed
# board, so the via slides along the y axis.
_B_CU_SEGMENT_AT_R2 = "0ebaff6b-fd8c-54f7-897f-312849b5246c"


def _load_board01_module():
    gen = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board01_generate_design_9058", gen)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rounded(xy) -> tuple[float, float]:
    return (round(xy[0], 4), round(xy[1], 4))


@pytest.fixture
def board_copy(tmp_path: Path) -> Path:
    dst = tmp_path / "voltage_divider_routed.kicad_pcb"
    shutil.copyfile(COMMITTED_ROUTED, dst)
    return dst


def _apertures(pcb, expansion: float):
    from kicad_tools.validate.rules.via_pad_geometry import pad_absolute_bbox

    out = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number == 0:
                continue
            x0, y0, x1, y1 = pad_absolute_bbox(pad, fp)
            out.append(
                (
                    f"{fp.reference}/{pad.number}",
                    (x0 - expansion, y0 - expansion, x1 + expansion, y1 + expansion),
                )
            )
    return out


def _circle_hits(pos, radius: float, box) -> bool:
    x0, y0, x1, y1 = box
    dx = max(x0 - pos[0], 0.0, pos[0] - x1)
    dy = max(y0 - pos[1], 0.0, pos[1] - y1)
    return dx * dx + dy * dy < radius * radius


def test_fixture_still_has_both_offending_vias() -> None:
    from kicad_tools.schema.pcb import PCB

    pcb = PCB.load(str(COMMITTED_ROUTED))
    assert {_rounded(v.position) for v in pcb.vias} == set(EXPECTED_MOVES)


def test_repair_makes_exactly_the_two_moves_and_is_idempotent(board_copy: Path) -> None:
    from kicad_tools.schema.pcb import PCB

    module = _load_board01_module()

    moves = module.repair_same_net_via_opening_overlaps(board_copy)
    assert len(moves) == 2, moves
    for old, new in EXPECTED_MOVES.items():
        assert any(f"({old[0]}, " in m and f"to ({new[0]}, {new[1]})" in m for m in moves), (
            old,
            new,
            moves,
        )

    pcb = PCB.load(str(board_copy))
    assert {_rounded(v.position) for v in pcb.vias} == set(EXPECTED_MOVES.values())

    # No via circle intersects any (over-estimated) pad aperture.
    apertures = _apertures(pcb, module._PAD_MASK_EXPANSION_MM)
    for via in pcb.vias:
        for name, box in apertures:
            assert not _circle_hits(via.position, via.size / 2.0, box), (
                f"via at {via.position} still overlaps aperture {name}"
            )

    # Each moved via keeps a segment endpoint on both copper layers.
    for via in pcb.vias:
        pos = _rounded(via.position)
        layers = {
            s.layer
            for s in pcb.segments_in_net(via.net_number)
            if pos in (_rounded(s.start), _rounded(s.end))
        }
        assert layers >= {"F.Cu", "B.Cu"}, (pos, layers)

    # A second pass finds nothing left to repair and leaves the file alone.
    before = board_copy.read_bytes()
    assert module.repair_same_net_via_opening_overlaps(board_copy) == []
    assert board_copy.read_bytes() == before


def test_misaligned_attached_segment_fails_closed(board_copy: Path) -> None:
    """A via whose attached segment is not along the slide axis must raise.

    Bend the B.Cu segment attached to the (14.0, 16.0) via so it runs
    mostly along x while the slide axis is y; the repair cannot prove the
    slide keeps that connection and must refuse, writing nothing.
    """
    text = board_copy.read_text()
    pattern = re.compile(
        r"\(start 147\.5 88\.2\)(\s*\(end 147\.5 91\)\s*\(width [^)]*\)\s*"
        r"\(layer \"B\.Cu\"\)\s*\(uuid \"" + _B_CU_SEGMENT_AT_R2 + r"\"\))"
    )
    bent, n = pattern.subn(r"(start 143.5 90)\1", text)
    assert n == 1, "fixture segment not found; committed board changed?"
    board_copy.write_text(bent)
    before = board_copy.read_bytes()

    module = _load_board01_module()
    with pytest.raises(RuntimeError, match="not aligned with the slide axis"):
        module.repair_same_net_via_opening_overlaps(board_copy)
    assert board_copy.read_bytes() == before, "a refused repair must not write the board"
