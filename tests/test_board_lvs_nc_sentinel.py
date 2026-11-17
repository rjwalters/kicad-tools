"""Regression tests for the exact-pad-scoped no-connect sentinel collapse (PR #4003).

KiCad encodes an explicit schematic no-connect on the PCB side as the
single-pad sentinel net ``unconnected-(<REF>-<PINNAME>-Pad<PAD>)``.  The
board-level LVS comparator collapses that sentinel to ``None`` so an
explicitly-NC pad compares equal to a schematic pin with no net — but
ONLY when the sentinel names *this very pad*.  These tests pin the exact
scoping contract:

* own-pad sentinel vs floating schematic pin -> clean;
* own-pad sentinel vs schematic pin that expects a REAL net -> mismatch
  (the collapse must never mask a genuine open);
* a pad carrying some OTHER pad's sentinel -> mismatch, including the
  prefix-collision edge cases ``Pad1`` vs ``Pad11`` and ``U1`` vs ``U10``.

The comparator's file-walking front-ends are monkeypatched with canned
``{(ref, pad) -> net}`` maps (same technique as ``test_lvs_recipe.py``)
so the normalization logic is exercised hermetically, without building
real ``.kicad_sch`` / ``.kicad_pcb`` fixtures.
"""

from __future__ import annotations

import pytest

import kicad_tools.lvs.board_lvs as board_lvs
from kicad_tools.lvs.board_lvs import compare_netlists


def _compare(
    monkeypatch: pytest.MonkeyPatch,
    sch_map: dict[tuple[str, str], str | None],
    pcb_map: dict[tuple[str, str], str | None],
) -> board_lvs.LVSResult:
    """Run compare_netlists over canned pin->net maps."""
    monkeypatch.setattr(board_lvs, "_schematic_pin_to_net", lambda _p: dict(sch_map))
    monkeypatch.setattr(board_lvs, "_pcb_pin_to_net", lambda _p: dict(pcb_map))
    return compare_netlists("dummy.kicad_sch", "dummy.kicad_pcb")


class TestNoConnectSentinelCollapse:
    """Own-pad sentinels collapse to None; everything else still compares."""

    def test_own_pad_sentinel_vs_floating_sch_pin_is_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pad carrying its OWN unconnected sentinel matches a floating pin.

        This is the false-positive the fix removes: KiCad's explicit-NC
        encoding (``unconnected-(U1-SWDIO-Pad1)`` on U1 pad 1) previously
        compared unequal to the schematic's ``None`` and reported a
        phantom LVS mismatch on every no-connect pin.
        """
        result = _compare(
            monkeypatch,
            sch_map={("U1", "1"): None},
            pcb_map={("U1", "1"): "unconnected-(U1-SWDIO-Pad1)"},
        )
        assert result.clean is True
        assert result.mismatches == ()

    def test_own_pad_sentinel_vs_real_sch_net_still_mismatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The collapse must not mask a genuine open.

        A schematic pin that expects a real net over a PCB no-connect is a
        true LVS error: the sentinel normalizes to ``None`` but the
        schematic side is non-None, so the pair still mismatches.
        """
        result = _compare(
            monkeypatch,
            sch_map={("U1", "1"): "SWDIO"},
            pcb_map={("U1", "1"): "unconnected-(U1-SWDIO-Pad1)"},
        )
        assert result.clean is False
        assert len(result.mismatches) == 1
        m = result.mismatches[0]
        assert (m.ref, m.pad) == ("U1", "1")
        assert m.schematic_net == "SWDIO"
        assert m.pcb_net is None  # normalized: the pad is physically NC

    def test_pad1_does_not_collapse_pad11_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``-Pad1)`` scoping must not prefix-match ``-Pad11)``.

        Pad 1 carrying pad 11's sentinel is a genuine anomaly (a net tie
        to some other pad's no-connect placeholder) and must survive as a
        mismatch, while pad 11 carrying that same sentinel is its own NC
        and collapses clean.
        """
        result = _compare(
            monkeypatch,
            sch_map={("U1", "1"): None, ("U1", "11"): None},
            pcb_map={
                ("U1", "1"): "unconnected-(U1-NC-Pad11)",  # foreign sentinel
                ("U1", "11"): "unconnected-(U1-NC-Pad11)",  # own sentinel
            },
        )
        assert result.clean is False
        assert len(result.mismatches) == 1
        m = result.mismatches[0]
        assert (m.ref, m.pad) == ("U1", "1")
        assert m.pcb_net == "unconnected-(U1-NC-Pad11)"  # not normalized away

    def test_ref_u1_does_not_collapse_u10_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """``unconnected-(U1-`` scoping must not prefix-match ``U10``.

        Symmetric ref edge case: U1's pad carrying U10's sentinel (and
        vice versa) must mismatch, while U10's pad carrying its own
        sentinel collapses clean.
        """
        result = _compare(
            monkeypatch,
            sch_map={("U1", "1"): None, ("U10", "1"): None},
            pcb_map={
                ("U1", "1"): "unconnected-(U10-NC-Pad1)",  # foreign (U10's)
                ("U10", "1"): "unconnected-(U10-NC-Pad1)",  # own sentinel
            },
        )
        assert result.clean is False
        assert len(result.mismatches) == 1
        m = result.mismatches[0]
        assert (m.ref, m.pad) == ("U1", "1")
        assert m.pcb_net == "unconnected-(U10-NC-Pad1)"

    def test_u10_pad_does_not_collapse_u1_sentinel(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reverse direction of the ref edge case: U10 carrying U1's sentinel."""
        result = _compare(
            monkeypatch,
            sch_map={("U10", "1"): None},
            pcb_map={("U10", "1"): "unconnected-(U1-NC-Pad1)"},
        )
        assert result.clean is False
        assert result.mismatches[0].pcb_net == "unconnected-(U1-NC-Pad1)"
