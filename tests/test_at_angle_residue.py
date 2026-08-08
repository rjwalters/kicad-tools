"""No zero-angle residue in the ``(at ...)`` write-through (issue #4752).

KiCad omits the angle token entirely when a footprint/pad rotation is zero,
so ``(at x y)`` is the on-disk form for an unrotated part.  The
rotation write-through in ``schema/pcb.py`` used to *append* an angle token
on the first nonzero assignment and then ``set_value`` it forever after --
so a transient rotation that returned to zero (the recovery applicator's
mirror path, which flips ``theta -> 180 - theta`` twice) left a cosmetic
``(at x y 0)`` behind.  Harmless to KiCad, pure noise in a diff.

The token this write-through appended is now dropped again when the angle
returns to zero; a token the *file* supplied is never removed, so boards
written with an explicit ``(at x y 0)`` keep their bytes.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.recovery import (
    Action,
    Difficulty,
    ResolutionStrategy,
    StrategyApplicator,
    StrategyType,
)
from kicad_tools.schema.pcb import PCB

_BOARD_TEMPLATE = """(kicad_pcb
	(version 20260206)
	(generator "pcbnew")
	(generator_version "10.0")
	(paper "A4")
	(layers
		(0 "F.Cu" signal)
		(2 "B.Cu" signal)
		(1 "F.Mask" user)
		(3 "B.Mask" user)
		(5 "F.SilkS" user "F.Silkscreen")
		(7 "B.SilkS" user "B.Silkscreen")
		(13 "F.Paste" user)
		(15 "B.Paste" user)
		(25 "Edge.Cuts" user)
		(29 "B.CrtYd" user "B.Courtyard")
		(31 "F.CrtYd" user "F.Courtyard")
		(33 "B.Fab" user)
		(35 "F.Fab" user)
	)
	(net 0 "")
	(footprint "Test:Residue"
		(layer "F.Cu")
		(uuid "11111111-1111-1111-1111-111111111111")
		(at 100 100{fp_angle})
		(property "Reference" "U1"
			(at 0 2 0)
			(layer "F.SilkS")
			(uuid "22222222-2222-2222-2222-222222222222")
			(effects
				(font
					(size 1 1)
					(thickness 0.15)
				)
			)
		)
		(pad "1" smd rect
			(at 0 0{pad_angle})
			(size 1 1)
			(layers "F.Cu" "F.Paste" "F.Mask")
			(uuid "33333333-3333-3333-3333-333333333333")
		)
		(pad "2" smd rect
			(at 1 0 45)
			(size 1 1)
			(layers "F.Cu" "F.Paste" "F.Mask")
			(uuid "44444444-4444-4444-4444-444444444444")
		)
	)
)
"""


def _write_board(tmp_path: Path, *, fp_angle: str = "", pad_angle: str = "") -> Path:
    path = tmp_path / "residue.kicad_pcb"
    path.write_text(_BOARD_TEMPLATE.format(fp_angle=fp_angle, pad_angle=pad_angle))
    return path


def _at_lines(text: str) -> list[str]:
    """Every ``(at ...)`` node in *text*, whitespace-normalized."""
    return [re.sub(r"\s+", " ", m).strip() for m in re.findall(r"\(at [^)]*\)", text)]


def _saved(pcb: PCB, path: Path) -> str:
    pcb.save(str(path))
    return path.read_text()


def _mirror(pcb: PCB) -> None:
    strategy = ResolutionStrategy(
        type=StrategyType.MIRROR_COMPONENT,
        difficulty=Difficulty.MEDIUM,
        confidence=1.0,
        actions=[Action(type="mirror", target="U1", params={})],
    )
    result = StrategyApplicator().apply_strategy(pcb, strategy)
    assert result.success is True, result.message


class TestRotationWriteThroughResidue:
    def test_footprint_angle_token_dropped_when_rotation_returns_to_zero(self, tmp_path: Path):
        path = _write_board(tmp_path)
        pcb = PCB.load(str(path))
        fp = pcb.footprints[0]

        fp.rotation = 90.0
        assert "(at 100 100 90)" in _saved(pcb, path)

        fp.rotation = 0.0
        assert "(at 100 100)" in _at_lines(_saved(pcb, path))
        assert "(at 100 100 0)" not in _saved(pcb, path)

    def test_pad_angle_token_dropped_when_rotation_returns_to_zero(self, tmp_path: Path):
        path = _write_board(tmp_path)
        pcb = PCB.load(str(path))
        pad = pcb.footprints[0].pads[0]

        pad.rotation = 90.0
        assert "(at 0 0 90)" in _at_lines(_saved(pcb, path))

        pad.rotation = 0.0
        assert "(at 0 0 0)" not in _at_lines(_saved(pcb, path))
        assert "(at 0 0)" in _at_lines(_saved(pcb, path))

    @pytest.mark.parametrize("angle", [90.0, 180.0, 45.5])
    def test_nonzero_angles_still_written(self, tmp_path: Path, angle: float):
        path = _write_board(tmp_path)
        pcb = PCB.load(str(path))
        pcb.footprints[0].rotation = angle
        text = _saved(pcb, path)
        assert f"(at 100 100 {angle:g})" in text
        assert PCB.load(str(path)).footprints[0].rotation == pytest.approx(angle)

    def test_file_supplied_zero_angle_token_is_preserved(self, tmp_path: Path):
        """A board written as ``(at x y 0)`` keeps its explicit zero -- the
        write-through only removes the token it added itself."""
        path = _write_board(tmp_path, fp_angle=" 0", pad_angle=" 0")
        pcb = PCB.load(str(path))
        pcb.footprints[0].rotation = 0.0
        pcb.footprints[0].pads[0].rotation = 0.0
        lines = _at_lines(_saved(pcb, path))
        assert "(at 100 100 0)" in lines
        assert "(at 0 0 0)" in lines


class TestMirrorRoundTripLeavesNoResidue:
    """The originating report: mirror + un-mirror grew ``(at 0 0 0)``."""

    def test_double_mirror_restores_original_at_tokens(self, tmp_path: Path):
        path = _write_board(tmp_path)
        before = _at_lines(path.read_text())

        pcb = PCB.load(str(path))
        _mirror(pcb)
        _mirror(pcb)
        after = _at_lines(_saved(pcb, path))

        assert after == before

    def test_single_mirror_still_writes_the_angle(self, tmp_path: Path):
        path = _write_board(tmp_path)
        pcb = PCB.load(str(path))
        _mirror(pcb)
        lines = _at_lines(_saved(pcb, path))
        # theta -> 180 - theta for both the footprint and its pads.
        assert "(at 100 100 180)" in lines
        assert "(at 0 0 180)" in lines
        assert "(at 1 0 135)" in lines
