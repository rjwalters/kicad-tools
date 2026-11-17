"""Placeholder-aware label LVS for unnamed nets (issue #4615, Part B).

An unnamed net has no designed name — the netlister invents one.  kicad-tools
derives it from the connected component's lexicographically smallest pin
(``Net-(R1-1)``); KiCad derives it from its own representative and spells it
with a different convention (``Net-(R3-Pad1)``).  Comparing those strings made
:func:`board_lvs.compare_netlists` report a mismatch on *every* pad of *every*
unnamed net even when both sides describe exactly the same connectivity.

That mattered specifically because ``_lvs_subcheck`` returns on a dirty copper
result before it ever inspects the label result: fixing only the copper side of
#4615 would have handed the ``LVS: FAILED`` verdict straight to this comparator
and the reporter would have seen no improvement.

So when BOTH sides carry a placeholder, the induced **pad partitions** are
compared instead of the strings.  These tests pin exactly how far that goes —
and, just as importantly, how far it does not:

* placeholder vs placeholder, same pad set          -> clean;
* placeholder vs placeholder, DIFFERENT pad set     -> still mismatches;
* placeholder vs a real named net (either way)      -> still mismatches;
* placeholder vs floating / unconnected             -> still mismatches;
* two explicitly named nets                         -> exact string equality,
                                                       unchanged.

The comparator's file-walking front-ends are monkeypatched with canned
``{(ref, pad) -> net}`` maps (the same technique as
``test_board_lvs_nc_sentinel.py``) so the normalization logic is exercised
hermetically.
"""

from __future__ import annotations

import pytest

import kicad_tools.lvs.board_lvs as board_lvs
from kicad_tools.lvs.board_lvs import _is_placeholder_net, compare_netlists


def _compare(
    monkeypatch: pytest.MonkeyPatch,
    sch_map: dict[tuple[str, str], str | None],
    pcb_map: dict[tuple[str, str], str | None],
) -> board_lvs.LVSResult:
    """Run compare_netlists over canned pin->net maps."""
    monkeypatch.setattr(board_lvs, "_schematic_pin_to_net", lambda _p: dict(sch_map))
    monkeypatch.setattr(board_lvs, "_pcb_pin_to_net", lambda _p: dict(pcb_map))
    return compare_netlists("dummy.kicad_sch", "dummy.kicad_pcb")


class TestPlaceholderRecognition:
    """What counts as an auto-generated placeholder name."""

    @pytest.mark.parametrize(
        "name",
        [
            "Net-(C11-2)",  # one convention
            "Net-(C11-Pad2)",  # the other convention
            "Net-(/power/C11-Pad2)",  # sheet-qualified
            "Net-(U10-VDD)",  # pin-name representative
        ],
    )
    def test_placeholder_shapes(self, name: str) -> None:
        assert _is_placeholder_net(name) is True

    @pytest.mark.parametrize(
        "name",
        [
            None,
            "",
            "GND",
            "VCC",
            "DAC_CLK",
            "Net_Tie",  # named net that merely starts with "Net"
            "unconnected-(U1-SWDIO-Pad1)",
        ],
    )
    def test_non_placeholder_shapes(self, name: str | None) -> None:
        assert _is_placeholder_net(name) is False


class TestPlaceholderVsPlaceholder:
    """Both sides invented a name for the same node -> compare partitions."""

    def test_same_node_different_spelling_and_representative_is_clean(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #4615 label-side false positive: same node, different names."""
        result = _compare(
            monkeypatch,
            sch_map={
                ("C11", "2"): "Net-(C11-2)",
                ("C37", "2"): "Net-(C11-2)",
                ("R5", "1"): "Net-(C11-2)",
            },
            pcb_map={
                ("C11", "2"): "Net-(C37-Pad2)",
                ("C37", "2"): "Net-(C37-Pad2)",
                ("R5", "1"): "Net-(C37-Pad2)",
            },
        )
        assert result.clean is True, result.mismatches

    def test_pad_bound_to_a_different_unnamed_node_still_mismatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The genuine defect the partition compare must still catch.

        The PCB puts ``R5.1`` on the *other* unnamed node.  Both sides still
        carry placeholders everywhere, so string comparison is off — but the
        induced pad sets differ, so the pads on both affected nodes mismatch.
        """
        result = _compare(
            monkeypatch,
            sch_map={
                ("C11", "2"): "Net-(C11-2)",
                ("C37", "2"): "Net-(C11-2)",
                ("R5", "1"): "Net-(C11-2)",
                ("R9", "1"): "Net-(R9-1)",
                ("R9", "2"): "Net-(R9-1)",
            },
            pcb_map={
                ("C11", "2"): "Net-(C11-Pad2)",
                ("C37", "2"): "Net-(C11-Pad2)",
                ("R5", "1"): "Net-(R9-Pad1)",  # <-- wrong node
                ("R9", "1"): "Net-(R9-Pad1)",
                ("R9", "2"): "Net-(R9-Pad1)",
            },
        )
        assert result.clean is False
        offenders = {(m.ref, m.pad) for m in result.mismatches}
        assert ("R5", "1") in offenders
        # The other members of both affected nodes disagree too.
        assert ("C11", "2") in offenders
        assert ("R9", "1") in offenders

    def test_pcb_fuses_two_unnamed_nodes_still_mismatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two schematic nodes collapsed to one PCB net is not a renaming."""
        result = _compare(
            monkeypatch,
            sch_map={
                ("R1", "2"): "Net-(R1-2)",
                ("C1", "1"): "Net-(R1-2)",
                ("R2", "2"): "Net-(R2-2)",
                ("C2", "1"): "Net-(R2-2)",
            },
            pcb_map=dict.fromkeys(
                [("R1", "2"), ("C1", "1"), ("R2", "2"), ("C2", "1")],
                "Net-(R1-Pad2)",
            ),
        )
        assert result.clean is False
        assert len(result.mismatches) == 4

    def test_pcb_splits_one_unnamed_node_still_mismatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One schematic node split across two PCB nets is not a renaming."""
        result = _compare(
            monkeypatch,
            sch_map={
                ("R1", "2"): "Net-(R1-2)",
                ("C1", "1"): "Net-(R1-2)",
                ("C2", "1"): "Net-(R1-2)",
            },
            pcb_map={
                ("R1", "2"): "Net-(R1-Pad2)",
                ("C1", "1"): "Net-(R1-Pad2)",
                ("C2", "1"): "Net-(C2-Pad1)",
            },
        )
        assert result.clean is False
        assert {(m.ref, m.pad) for m in result.mismatches} == {
            ("R1", "2"),
            ("C1", "1"),
            ("C2", "1"),
        }


class TestPlaceholderVsRealName:
    """Only placeholder-versus-placeholder may be normalized."""

    def test_schematic_named_net_vs_pcb_placeholder_mismatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A designed net that lost its name on the board is a real defect."""
        result = _compare(
            monkeypatch,
            sch_map={("U1", "2"): "DAC_CLK", ("R7", "2"): "DAC_CLK"},
            pcb_map={("U1", "2"): "Net-(R7-Pad2)", ("R7", "2"): "Net-(R7-Pad2)"},
        )
        assert result.clean is False
        assert len(result.mismatches) == 2

    def test_schematic_placeholder_vs_pcb_named_net_mismatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unnamed schematic node bound to a real board net is a defect."""
        result = _compare(
            monkeypatch,
            sch_map={("R1", "2"): "Net-(R1-2)", ("C1", "1"): "Net-(R1-2)"},
            pcb_map={("R1", "2"): "GND", ("C1", "1"): "GND"},
        )
        assert result.clean is False
        assert len(result.mismatches) == 2

    def test_placeholder_vs_unconnected_pcb_pad_mismatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A schematic-connected pad left unrouted is still an open."""
        result = _compare(
            monkeypatch,
            sch_map={("R1", "2"): "Net-(R1-2)", ("C1", "1"): "Net-(R1-2)"},
            pcb_map={("R1", "2"): "Net-(R1-Pad2)", ("C1", "1"): None},
        )
        assert result.clean is False
        assert {(m.ref, m.pad) for m in result.mismatches} == {("C1", "1")}

    def test_floating_schematic_pin_vs_pcb_placeholder_mismatches(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A pad the board wires up but the schematic leaves floating."""
        result = _compare(
            monkeypatch,
            sch_map={("R1", "2"): None},
            pcb_map={("R1", "2"): "Net-(R1-Pad2)"},
        )
        assert result.clean is False
        assert len(result.mismatches) == 1


class TestNamedNetsUnaffected:
    """Explicitly named nets keep byte-exact string equality."""

    def test_named_nets_still_compare_by_string(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _compare(
            monkeypatch,
            sch_map={("U1", "1"): "VCC", ("U1", "2"): "GND"},
            pcb_map={("U1", "1"): "VCC", ("U1", "2"): "+3V3"},
        )
        assert result.clean is False
        assert {(m.ref, m.pad) for m in result.mismatches} == {("U1", "2")}

    def test_two_named_nets_swapped_still_mismatches(self, monkeypatch: pytest.MonkeyPatch) -> None:
        result = _compare(
            monkeypatch,
            sch_map={("U1", "1"): "VCC", ("U1", "2"): "GND"},
            pcb_map={("U1", "1"): "GND", ("U1", "2"): "VCC"},
        )
        assert result.clean is False
        assert len(result.mismatches) == 2


class TestOneSidedPadsDoNotSmear:
    """A pad present on only one side is reported on its own key only."""

    def test_pcb_only_pad_on_a_placeholder_net_is_reported_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A mounting hole tied to an unnamed net must not dirty its peers.

        Placeholder groups are built over keys present on BOTH sides, so the
        extra PCB-only pad is reported by the ``None``-vs-name rule on its
        own key and does not smear a mismatch across every pad sharing the
        net.
        """
        result = _compare(
            monkeypatch,
            sch_map={("R1", "2"): "Net-(R1-2)", ("C1", "1"): "Net-(R1-2)"},
            pcb_map={
                ("R1", "2"): "Net-(R1-Pad2)",
                ("C1", "1"): "Net-(R1-Pad2)",
                ("H1", "1"): "Net-(R1-Pad2)",  # PCB-only pad
            },
        )
        assert result.clean is False
        assert {(m.ref, m.pad) for m in result.mismatches} == {("H1", "1")}


class TestOnDiskFixture:
    """The committed fixture exercises the whole path, not just the maps."""

    def test_fixture_label_lvs_is_clean(self) -> None:
        from pathlib import Path

        fixtures = Path(__file__).parent / "fixtures" / "unnamed_net_lvs"
        result = compare_netlists(
            fixtures / "unnamed_net.kicad_sch", fixtures / "unnamed_net.kicad_pcb"
        )
        assert result.clean is True, result.mismatches
