"""Label-leg vacuity guard for a net-less PCB side (issue #4681).

The #4681 filer's board produced 264 label-LVS records, every one with
``pcb_net: null``: the PCB side supplied **zero** net bindings, so every
bound schematic pin trivially "mismatched".  N pseudo-mismatches read as
N real defects instead of one degraded input.

Mirroring the copper leg's #4005 vacuity guard, :func:`compare_netlists`
now refuses to diff a zero-PCB-evidence board pad-by-pad: it returns a
single synthetic mismatch (``ref="<vacuous>"``,
``pcb_net="<no-pcb-evidence>"``) and the result is still dirty — no
evidence is not a pass.  These tests pin exactly when the guard fires:

* board has pads, schematic binds pins, PCB binds nothing -> vacuous;
* >=1 net-bearing PCB pad                                 -> per-pad diff
  unchanged (genuine partial mismatches keep their records);
* NC sentinels are *raw* evidence (deliberate per-pad bindings), so an
  all-NC board still gets the real per-pad diff;
* schematic binds zero pins -> guard never fires (that zero-evidence
  case belongs to the copper leg's #4005 guard);
* board has zero pads at all -> guard never fires (a missing PCB side is
  a different failure and keeps its per-key records).

The comparator's file-walking front-ends are monkeypatched with canned
``{(ref, pad) -> net}`` maps (same technique as
``test_board_lvs_nc_sentinel.py``) so the guard logic is exercised
hermetically.
"""

from __future__ import annotations

import pytest

import kicad_tools.lvs.board_lvs as board_lvs
from kicad_tools.lvs.board_lvs import (
    NETLIST_VACUOUS_NET,
    NETLIST_VACUOUS_REF,
    LVSMismatch,
    LVSResult,
    compare_netlists,
)


def _compare(
    monkeypatch: pytest.MonkeyPatch,
    sch_map: dict[tuple[str, str], str | None],
    pcb_map: dict[tuple[str, str], str | None],
) -> board_lvs.LVSResult:
    """Run compare_netlists over canned pin->net maps."""
    monkeypatch.setattr(board_lvs, "_schematic_pin_to_net", lambda _p: dict(sch_map))
    monkeypatch.setattr(board_lvs, "_pcb_pin_to_net", lambda _p: dict(pcb_map))
    return compare_netlists("dummy.kicad_sch", "dummy.kicad_pcb")


class TestVacuityGuardFires:
    """Zero PCB net bindings + bound schematic pins -> one synthetic record."""

    def test_all_pads_without_nets_yields_single_synthetic_record(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #4681 shape: every pad lacks a ``(net ...)`` child.

        Before the guard this emitted one ``pcb_net=null`` record per
        bound schematic pin (264 on the filer's board).  Now: exactly one
        synthetic record, still dirty.
        """
        sch_map = {
            ("R7", "2"): "DAC_CLK",
            ("U10", "2"): "DAC_CLK",
            ("C1", "1"): "PWR_FLAG",
            ("C10", "1"): "+3.3V",
        }
        pcb_map: dict[tuple[str, str], str | None] = dict.fromkeys(sch_map, None)
        result = _compare(monkeypatch, sch_map, pcb_map)

        assert result.clean is False  # no evidence is not a pass
        assert result.vacuous is True
        assert len(result.mismatches) == 1  # NOT one per bound pin
        m = result.mismatches[0]
        assert m.ref == NETLIST_VACUOUS_REF
        assert m.pcb_net == NETLIST_VACUOUS_NET
        # Evidence counters ride on the synthetic record.
        assert m.pad == "board_pads=4"
        assert m.schematic_net == "sch_bound_pins=4"

    def test_empty_net0_names_are_not_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Pads carrying only the empty ``(net 0 "")`` name are unbound too."""
        result = _compare(
            monkeypatch,
            sch_map={("R1", "1"): "VCC", ("R1", "2"): "GND"},
            pcb_map={("R1", "1"): "", ("R1", "2"): None},
        )
        assert result.vacuous is True
        assert len(result.mismatches) == 1


class TestVacuityGuardDoesNotFire:
    """Any real PCB evidence (or no schematic evidence) keeps the old diff."""

    def test_single_net_bearing_pad_keeps_per_pad_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Genuine partial mismatches are unchanged (acceptance criterion 2)."""
        result = _compare(
            monkeypatch,
            sch_map={("R1", "1"): "VCC", ("R1", "2"): "GND", ("C1", "1"): "GND"},
            pcb_map={("R1", "1"): "VCC", ("R1", "2"): None, ("C1", "1"): None},
        )
        assert result.vacuous is False
        assert result.clean is False
        # The two unbound pads each keep their own genuine record.
        assert {(m.ref, m.pad) for m in result.mismatches} == {("R1", "2"), ("C1", "1")}
        assert all(m.pcb_net is None for m in result.mismatches)

    def test_nc_sentinels_count_as_raw_evidence(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An all-NC board is deliberately bound pad-by-pad, not evidence-free.

        The sentinel normalizes to ``None`` for comparison, but it is a
        per-pad binding the writer produced on purpose — so the genuine
        open on U1.1 must survive as its own record (the
        ``test_board_lvs_nc_sentinel.py`` contract), not collapse into a
        vacuity verdict.
        """
        result = _compare(
            monkeypatch,
            sch_map={("U1", "1"): "SWDIO", ("U1", "2"): None},
            pcb_map={
                ("U1", "1"): "unconnected-(U1-SWDIO-Pad1)",
                ("U1", "2"): "unconnected-(U1-NC-Pad2)",
            },
        )
        assert result.vacuous is False
        assert result.clean is False
        assert len(result.mismatches) == 1
        m = result.mismatches[0]
        assert (m.ref, m.pad) == ("U1", "1")
        assert m.schematic_net == "SWDIO"
        assert m.pcb_net is None

    def test_wireless_schematic_never_trips_the_label_guard(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Zero *schematic* evidence is the copper leg's #4005 territory.

        A fixture schematic that binds no pins has nothing for the label
        leg to compare; both sides are ``None`` everywhere, so the result
        is clean here and the copper leg's vacuity guard carries the
        zero-evidence verdict (boards 06/07 behavior must not change).
        """
        result = _compare(
            monkeypatch,
            sch_map={("R1", "1"): None, ("R1", "2"): None},
            pcb_map={("R1", "1"): None, ("R1", "2"): None},
        )
        assert result.vacuous is False
        assert result.clean is True

    def test_board_with_zero_pads_keeps_per_key_records(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No pads at all is a different failure than unbound pads."""
        result = _compare(
            monkeypatch,
            sch_map={("R1", "1"): "VCC", ("R1", "2"): "GND"},
            pcb_map={},
        )
        assert result.vacuous is False
        assert result.clean is False
        assert len(result.mismatches) == 2


class TestVacuousProperty:
    """``LVSResult.vacuous`` is derived, so round-trips preserve it."""

    def test_hand_built_results_report_vacuous_correctly(self) -> None:
        genuine = LVSResult(
            clean=False,
            mismatches=(LVSMismatch(ref="D1", pad="1", schematic_net="GND", pcb_net=None),),
        )
        assert genuine.vacuous is False

        synthetic = LVSResult(
            clean=False,
            mismatches=(
                LVSMismatch(
                    ref=NETLIST_VACUOUS_REF,
                    pad="board_pads=2",
                    schematic_net="sch_bound_pins=2",
                    pcb_net=NETLIST_VACUOUS_NET,
                ),
            ),
        )
        assert synthetic.vacuous is True

        assert LVSResult(clean=True, mismatches=()).vacuous is False
