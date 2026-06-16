"""Pcbnew-oracle regression test for the pad rotation convention (#3739).

KiCad's own ``pcbnew`` 10.0.1 was probed directly to establish the
authoritative forward local->world pad transform.  For a footprint at
(100, 100) mm with a pad at footprint-local offset (2, 0):

    ===  ====================
    deg  pcbnew GetPosition()
    ===  ====================
      0  (102.0, 100.0)
     90  (100.0,  98.0)
    180  ( 98.0, 100.0)
    270  (100.0, 102.0)
    ===  ====================

These four values are the regression oracle.  They were captured with::

    KPY=/Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/\
Versions/3.9/bin/python3
    # board, FOOTPRINT at (100,100), PAD whose world pos at deg0 is (102,100),
    # then fp.SetOrientationDegrees(deg); pcbnew.ToMM(pad.GetPosition())

KiCad applies the footprint orientation as the *negated* angle relative
to standard CCW math.  PR #738 used the un-negated (standard CCW) form,
which produced the mirror-image positions at 90°/270° (0°/180° agree
under both signs — the long-known "test trap").

This test runs WITHOUT pcbnew by asserting against the hardcoded oracle
constants above.  When pcbnew *is* importable (e.g. on a developer's
machine with KiCad installed) an extra test re-derives the oracle live
and asserts kicad-tools matches it exactly.

MUST exercise 90° and 270° — a 0°/180°-only test cannot detect the bug.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.core.geometry import rotate_pad_offset

# Footprint origin and the pad's footprint-local offset used by the oracle.
FP_POS = (100.0, 100.0)
PAD_LOCAL = (2.0, 0.0)

# pcbnew 10.0.1-verified world positions (mm) for PAD_LOCAL at each angle.
PCBNEW_ORACLE: dict[float, tuple[float, float]] = {
    0.0: (102.0, 100.0),
    90.0: (100.0, 98.0),
    180.0: (98.0, 100.0),
    270.0: (100.0, 102.0),
}

EPS = 1e-9


def _world_via_helper(rotation_deg: float) -> tuple[float, float]:
    """kicad-tools world position via the shared rotate_pad_offset helper."""
    rx, ry = rotate_pad_offset(PAD_LOCAL[0], PAD_LOCAL[1], rotation_deg)
    return (FP_POS[0] + rx, FP_POS[1] + ry)


class TestRotatePadOffsetMatchesPcbnewOracle:
    """``core.geometry.rotate_pad_offset`` must match pcbnew at all cardinals."""

    @pytest.mark.parametrize("rotation", sorted(PCBNEW_ORACLE))
    def test_matches_oracle(self, rotation: float) -> None:
        expected = PCBNEW_ORACLE[rotation]
        actual = _world_via_helper(rotation)
        assert actual[0] == pytest.approx(expected[0], abs=1e-6), (
            f"deg{rotation}: x={actual[0]} != pcbnew {expected[0]}"
        )
        assert actual[1] == pytest.approx(expected[1], abs=1e-6), (
            f"deg{rotation}: y={actual[1]} != pcbnew {expected[1]}"
        )

    def test_90_and_270_differ_from_standard_ccw(self) -> None:
        """Negative control: the standard-CCW form (PR #738) would FAIL here.

        A 0°/180°-only test is a blind spot; this asserts that 90° and
        270° genuinely discriminate KiCad's negated convention from the
        un-negated one that this fix overturned.
        """
        for rotation in (90.0, 270.0):
            px, py = PAD_LOCAL
            rad = math.radians(rotation)  # un-negated (the old, wrong form)
            ccw = (
                FP_POS[0] + px * math.cos(rad) - py * math.sin(rad),
                FP_POS[1] + px * math.sin(rad) + py * math.cos(rad),
            )
            oracle = PCBNEW_ORACLE[rotation]
            assert ccw != pytest.approx(oracle, abs=0.5), (
                f"deg{rotation}: standard-CCW {ccw} unexpectedly matches "
                f"pcbnew {oracle} — fixture no longer discriminates the bug."
            )


class TestGetPadPositionMatchesPcbnewOracle:
    """``PCB.get_pad_position`` (the public API) must match pcbnew too."""

    @pytest.mark.parametrize("rotation", sorted(PCBNEW_ORACLE))
    def test_get_pad_position_matches_oracle(self, tmp_path, rotation: float) -> None:
        from kicad_tools.schema.pcb import PCB

        pcb_text = f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (net 0 "")
  (net 1 "SIG1")
  (footprint "Oracle"
    (layer "F.Cu")
    (at {FP_POS[0]} {FP_POS[1]} {rotation})
    (attr smd)
    (property "Reference" "U1")
    (pad "1" smd rect (at {PAD_LOCAL[0]} {PAD_LOCAL[1]}) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG1"))
  )
)
"""
        pcb_file = tmp_path / "oracle.kicad_pcb"
        pcb_file.write_text(pcb_text)
        pcb = PCB.load(str(pcb_file))

        pos = pcb.get_pad_position("U1", "1")
        assert pos is not None
        expected = PCBNEW_ORACLE[rotation]
        assert pos[0] == pytest.approx(expected[0], abs=1e-6)
        assert pos[1] == pytest.approx(expected[1], abs=1e-6)


class TestLivePcbnewOracle:
    """Re-derive the oracle from a live pcbnew if it is importable."""

    def test_helper_matches_live_pcbnew(self) -> None:
        pcbnew = pytest.importorskip("pcbnew", reason="pcbnew not importable in this environment")

        board = pcbnew.BOARD()
        fp = pcbnew.FOOTPRINT(board)
        fp.SetPosition(pcbnew.VECTOR2I(pcbnew.FromMM(FP_POS[0]), pcbnew.FromMM(FP_POS[1])))
        board.Add(fp)

        pad = pcbnew.PAD(fp)
        fp.Add(pad)
        # Define the pad's local offset by setting its world pos at deg 0.
        fp.SetOrientationDegrees(0)
        pad.SetPosition(
            pcbnew.VECTOR2I(
                pcbnew.FromMM(FP_POS[0] + PAD_LOCAL[0]),
                pcbnew.FromMM(FP_POS[1] + PAD_LOCAL[1]),
            )
        )

        for rotation in (0.0, 90.0, 180.0, 270.0):
            fp.SetOrientationDegrees(rotation)
            p = pad.GetPosition()
            live = (pcbnew.ToMM(p.x), pcbnew.ToMM(p.y))
            ours = _world_via_helper(rotation)
            assert ours[0] == pytest.approx(live[0], abs=1e-4), (
                f"deg{rotation}: kicad-tools x={ours[0]} != live pcbnew {live[0]}"
            )
            assert ours[1] == pytest.approx(live[1], abs=1e-4), (
                f"deg{rotation}: kicad-tools y={ours[1]} != live pcbnew {live[1]}"
            )
            # And the hardcoded oracle must equal live pcbnew too.
            assert PCBNEW_ORACLE[rotation] == pytest.approx(live, abs=1e-4)
