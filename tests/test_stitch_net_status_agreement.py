"""Cross-tool agreement tests: ``kct stitch`` vs ``kct net-status`` (issue #4679).

``kct net-status`` decides connectivity with the strict copper-contact model
(real shapely geometry with per-fill-island zone connectivity) and prints a
literal ``kct stitch <file> --net <NET>`` remedy for plane-net pads it reports
as "needs via to plane".  Before issue #4679 running that exact command could
answer "No unconnected pads found on target nets": stitch's gate was a 0.5mm
endpoint-proximity heuristic with no notion of "reaches the plane", and its
pad enumeration silently dropped KiCad-10 name-only ``(net "NAME")`` pad
references entirely (the ``kicad-cli pcb drc --save-board`` dialect).

These tests pin the contract that the printed remedy actually applies:

* A pad strict net-status names as stranded from a filled plane is in
  stitch's work list on the SAME file -- it either receives a via or appears
  in ``pads_skipped`` with an explicit reason (fixture A).
* When every pad genuinely reaches the plane both tools agree it is a no-op
  and stitch says "All N pads already connected" (fixture B).
* Naming a net with zero pads produces an explicit "No pads found" message
  that names the net, instead of the misleading catch-all (fixture C).
* Name-only net references (with or without a surviving header net table)
  are enumerated, so the "found no pads at all" failure mode is fixed.
* ``NetStatus.suggested_fix`` embeds the real analyzed filename.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.analysis.net_status import NetStatus, NetStatusAnalyzer
from kicad_tools.cli.stitch_cmd import (
    find_existing_vias,
    find_pads_on_nets,
    get_name_to_net_map,
    get_net_map,
    output_result,
    resolve_net_num,
    run_stitch,
)
from kicad_tools.core.sexp_file import load_pcb

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
#
# 2-layer board, plane net GNDA poured (and FILLED) on B.Cu:
#   C1.1 @ (110, 110): SMD pad on F.Cu tied only to a 2mm stub trace on F.Cu
#       -- proximity-"connected" to stitch's legacy gate, but the copper
#       never reaches the plane: strict net-status reports it stranded.
#   C2.1 @ (120, 110), C3.1 @ (130, 110): SMD pads escaped through a short
#       trace to a via whose barrel lands on the B.Cu fill island -- bonded.
#
# The fill rectangle spans x=105..135 so a stitch via placed next to C1 lands
# on real same-net fill (the #4432 off-fill gate does not interfere).

_BOARD_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
"""

_ZONE_GNDA_FILLED = """  (zone (net 1) (net_name "GNDA") (layer "B.Cu") (uuid "zone-gnda")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (filled_areas_thickness no)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon
      (pts (xy 105 105) (xy 135 105) (xy 135 115) (xy 105 115))
    )
    (filled_polygon
      (layer "B.Cu")
      (pts (xy 105 105) (xy 135 105) (xy 135 115) (xy 105 115))
    )
  )
"""


def _footprint(ref: str, x: float, y: float, net_ref: str) -> str:
    """One-pad SMD footprint on F.Cu with a 1x1mm pad at its origin."""
    return f"""  (footprint "Test:OnePad"
    (layer "F.Cu")
    (uuid "fp-{ref}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS") (uuid "ref-{ref}"))
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu" "F.Paste" "F.Mask") (net {net_ref}))
  )
"""


def _stranded_board(*, stranded_pad_has_via: bool) -> str:
    """Board text for fixtures A (stranded C1.1) and B (all bonded)."""
    parts = [_BOARD_HEADER]
    parts.append('  (net 0 "")\n  (net 1 "GNDA")\n  (net 2 "SIG")\n')
    parts.append(_footprint("C1", 110, 110, '1 "GNDA"'))
    parts.append(_footprint("C2", 120, 110, '1 "GNDA"'))
    parts.append(_footprint("C3", 130, 110, '1 "GNDA"'))
    # C1.1: stub trace to nowhere (fixture A) or a real via escape (fixture B)
    if stranded_pad_has_via:
        parts.append(
            '  (segment (start 110 110) (end 110.8 110) (width 0.25) (layer "F.Cu")'
            ' (net 1) (uuid "seg-c1"))\n'
            '  (via (at 110.8 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
            ' (net 1) (uuid "via-c1"))\n'
        )
    else:
        parts.append(
            '  (segment (start 110 110) (end 112 110) (width 0.25) (layer "F.Cu")'
            ' (net 1) (uuid "seg-c1-stub"))\n'
        )
    # C2.1 and C3.1: trace + via landing on the B.Cu fill island
    parts.append(
        '  (segment (start 120 110) (end 120.8 110) (width 0.25) (layer "F.Cu")'
        ' (net 1) (uuid "seg-c2"))\n'
        '  (via (at 120.8 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
        ' (net 1) (uuid "via-c2"))\n'
        '  (segment (start 130 110) (end 130.8 110) (width 0.25) (layer "F.Cu")'
        ' (net 1) (uuid "seg-c3"))\n'
        '  (via (at 130.8 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
        ' (net 1) (uuid "via-c3"))\n'
    )
    parts.append(_ZONE_GNDA_FILLED)
    parts.append(")\n")
    return "".join(parts)


@pytest.fixture
def stranded_pad_pcb(tmp_path: Path) -> Path:
    """Fixture A: C1.1 has only a stub trace -- strictly stranded."""
    pcb_file = tmp_path / "stranded_a.kicad_pcb"
    pcb_file.write_text(_stranded_board(stranded_pad_has_via=False))
    return pcb_file


@pytest.fixture
def all_bonded_pcb(tmp_path: Path) -> Path:
    """Fixture B: every GNDA pad reaches the B.Cu plane through a via."""
    pcb_file = tmp_path / "bonded_b.kicad_pcb"
    pcb_file.write_text(_stranded_board(stranded_pad_has_via=True))
    return pcb_file


# ---------------------------------------------------------------------------
# Fixture A: strict net-status names a stranded pad -> stitch must act on it
# ---------------------------------------------------------------------------


class TestStrandedPadAgreement:
    def test_net_status_reports_pad_stranded(self, stranded_pad_pcb: Path):
        """Precondition: the strict model names C1.1 'needs via to plane'."""
        analyzer = NetStatusAnalyzer(stranded_pad_pcb, strict=True)
        result = analyzer.analyze()
        gnda = result.get_net("GNDA")
        assert gnda is not None
        assert gnda.is_plane_net
        assert gnda.status == "incomplete"
        assert [p.full_name for p in gnda.unconnected_pads] == ["C1.1"]

    def test_stitch_acts_on_strictly_stranded_pad(self, stranded_pad_pcb: Path):
        """The pad net-status names must be in stitch's work list.

        Before issue #4679 the 0.5mm proximity gate saw C1.1's stub-trace
        endpoint at the pad center and skipped it as "already connected" --
        the printed net-status remedy was a no-op.
        """
        result = run_stitch(stranded_pad_pcb, ["GNDA"], dry_run=True)

        assert result.pads_found == 3
        acted_on = {f"{p.pad.reference}.{p.pad.pad_number}" for p in result.vias_added} | {
            f"{p.reference}.{p.pad_number}" for p, _reason in result.pads_skipped
        }
        assert "C1.1" in acted_on
        # The two genuinely bonded pads are recognized as connected.
        assert result.already_connected == 2

    def test_stitch_proposes_via_for_stranded_pad(self, stranded_pad_pcb: Path):
        """On this uncongested fixture the stranded pad gets an actual via."""
        result = run_stitch(stranded_pad_pcb, ["GNDA"], dry_run=True)
        via_pads = {f"{p.pad.reference}.{p.pad.pad_number}" for p in result.vias_added}
        assert via_pads == {"C1.1"}

    def test_no_silent_contradiction_message(self, stranded_pad_pcb: Path, capsys):
        """'No unconnected pads found' must be impossible while net-status
        names a stranded pad on the same artifact."""
        result = run_stitch(stranded_pad_pcb, ["GNDA"], dry_run=True)
        output_result(result, dry_run=True)
        out = capsys.readouterr().out
        assert "No unconnected pads found" not in out
        assert "No pads found on target net(s)" not in out


# ---------------------------------------------------------------------------
# Fixture B: both tools agree the net is complete -> explicit no-op message
# ---------------------------------------------------------------------------


class TestAllBondedAgreement:
    def test_net_status_reports_complete(self, all_bonded_pcb: Path):
        analyzer = NetStatusAnalyzer(all_bonded_pcb, strict=True)
        gnda = analyzer.analyze().get_net("GNDA")
        assert gnda is not None
        assert gnda.status == "complete"

    def test_stitch_reports_all_connected(self, all_bonded_pcb: Path, capsys):
        result = run_stitch(all_bonded_pcb, ["GNDA"], dry_run=True)

        assert result.pads_found == 3
        assert result.already_connected == 3
        assert not result.vias_added
        assert not result.pads_skipped

        output_result(result, dry_run=True)
        out = capsys.readouterr().out
        assert "All 3 pads already connected." in out


# ---------------------------------------------------------------------------
# Fixture C: zero pads on the target net -> explicit, self-diagnosing message
# ---------------------------------------------------------------------------


class TestZeroPadsMessage:
    def test_zero_pad_net_named_explicitly(self, stranded_pad_pcb: Path, capsys):
        """A net with no pads must be reported as such, naming the net --
        not with the ambiguous 'No unconnected pads found on target nets.'"""
        result = run_stitch(stranded_pad_pcb, ["SIG"], dry_run=True)

        assert result.pads_found == 0
        output_result(result, dry_run=True)
        out = capsys.readouterr().out
        assert "No pads found on target net(s): SIG" in out
        assert "No unconnected pads found" not in out

    def test_nonexistent_net_named_explicitly(self, stranded_pad_pcb: Path, capsys):
        result = run_stitch(stranded_pad_pcb, ["NO_SUCH_NET"], dry_run=True)
        assert result.pads_found == 0
        output_result(result, dry_run=True)
        out = capsys.readouterr().out
        assert "No pads found on target net(s): NO_SUCH_NET" in out


# ---------------------------------------------------------------------------
# Name-only net references (issue #4679 pad-enumeration defect)
# ---------------------------------------------------------------------------


def _name_only_board(*, keep_header_table: bool) -> str:
    """Board where every inline net reference is KiCad-10 name-only.

    ``keep_header_table=False`` reproduces the ``kicad-cli pcb drc
    --save-board`` dialect where the top-level ``(net N "name")`` table is
    deleted entirely.
    """
    parts = [_BOARD_HEADER]
    if keep_header_table:
        parts.append('  (net 0 "")\n  (net 1 "GNDA")\n  (net 2 "SIG")\n')
    parts.append(_footprint("C1", 110, 110, '"GNDA"'))
    parts.append(_footprint("C2", 120, 110, '"GNDA"'))
    parts.append(
        '  (segment (start 120 110) (end 120.8 110) (width 0.25) (layer "F.Cu")'
        ' (net "GNDA") (uuid "seg-c2"))\n'
        '  (via (at 120.8 110) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu")'
        ' (net "GNDA") (uuid "via-c2"))\n'
    )
    parts.append(
        """  (zone (net "GNDA") (layer "B.Cu") (uuid "zone-gnda")
    (hatch edge 0.5)
    (connect_pads (clearance 0.2))
    (min_thickness 0.25)
    (fill yes (thermal_gap 0.5) (thermal_bridge_width 0.5))
    (polygon
      (pts (xy 105 105) (xy 135 105) (xy 135 115) (xy 105 115))
    )
    (filled_polygon
      (layer "B.Cu")
      (pts (xy 105 105) (xy 135 105) (xy 135 115) (xy 105 115))
    )
  )
"""
    )
    parts.append(")\n")
    return "".join(parts)


class TestNameOnlyNetReferences:
    def test_pads_found_with_header_table(self, tmp_path: Path):
        """Name-only pad refs resolve through the header net table."""
        pcb_file = tmp_path / "name_only_header.kicad_pcb"
        pcb_file.write_text(_name_only_board(keep_header_table=True))
        sexp = load_pcb(pcb_file)

        pads = find_pads_on_nets(sexp, {"GNDA"})
        assert {f"{p.reference}.{p.pad_number}" for p in pads} == {"C1.1", "C2.1"}
        assert all(p.net_number == 1 for p in pads)
        assert all(p.net_name == "GNDA" for p in pads)

    def test_pads_found_without_header_table(self, tmp_path: Path):
        """kicad-cli --save-board strips the header net table; a synthesized
        table keeps pads visible (before #4679: empty pad list -> the
        misleading 'No unconnected pads found on target nets.')."""
        pcb_file = tmp_path / "name_only_stripped.kicad_pcb"
        pcb_file.write_text(_name_only_board(keep_header_table=False))
        sexp = load_pcb(pcb_file)

        net_map = get_net_map(sexp)
        assert "GNDA" in net_map.values()

        pads = find_pads_on_nets(sexp, {"GNDA"})
        assert {f"{p.reference}.{p.pad_number}" for p in pads} == {"C1.1", "C2.1"}

        # Same-net copper is attributed consistently (vias resolve to the
        # same synthesized number the pads got).
        net_numbers = {p.net_number for p in pads}
        vias = find_existing_vias(sexp, net_numbers)
        assert len(vias) == 1

    def test_run_stitch_end_to_end_on_name_only_board(self, tmp_path: Path, capsys):
        """The filer's failure shape: name-only board, stranded SMD pad.

        Stitch must find the pads and propose a via for the stranded one
        instead of reporting that no pads exist.
        """
        pcb_file = tmp_path / "name_only_e2e.kicad_pcb"
        pcb_file.write_text(_name_only_board(keep_header_table=False))

        result = run_stitch(pcb_file, ["GNDA"], dry_run=True)
        assert result.pads_found == 2

        output_result(result, dry_run=True)
        out = capsys.readouterr().out
        assert "No pads found on target net(s)" not in out
        assert "No unconnected pads found" not in out

    def test_resolve_net_num_dialects(self):
        """Unit: numeric, numeric+name, and name-only refs all resolve."""
        from kicad_tools.sexp import parse_string

        name_to_num = {"GNDA": 7}
        numeric = parse_string("(pad (net 7))").find_child("net")
        numeric_named = parse_string('(pad (net 7 "GNDA"))').find_child("net")
        name_only = parse_string('(pad (net "GNDA"))').find_child("net")
        unknown = parse_string('(pad (net "OTHER"))').find_child("net")

        assert resolve_net_num(numeric, name_to_num) == 7
        assert resolve_net_num(numeric_named, name_to_num) == 7
        assert resolve_net_num(name_only, name_to_num) == 7
        assert resolve_net_num(unknown, name_to_num) is None
        assert resolve_net_num(None, name_to_num) is None

    def test_get_name_to_net_map(self, tmp_path: Path):
        pcb_file = tmp_path / "name_map.kicad_pcb"
        pcb_file.write_text(_name_only_board(keep_header_table=True))
        sexp = load_pcb(pcb_file)
        assert get_name_to_net_map(sexp)["GNDA"] == 1


# ---------------------------------------------------------------------------
# suggested_fix embeds the real filename (issue #4679 bonus defect)
# ---------------------------------------------------------------------------


class TestSuggestedFixFilename:
    def test_suggested_fix_uses_source_file(self):
        status = NetStatus(
            net_number=1,
            net_name="GNDA",
            is_plane_net=True,
            plane_layers=["In1.Cu"],
            source_file="boards/chorus_v24.kicad_pcb",
        )
        assert "kct stitch boards/chorus_v24.kicad_pcb --net GNDA" in status.suggested_fix

    def test_suggested_fix_falls_back_without_source(self):
        status = NetStatus(
            net_number=1,
            net_name="GNDA",
            is_plane_net=True,
            plane_layers=["In1.Cu"],
        )
        assert "kct stitch board.kicad_pcb --net GNDA" in status.suggested_fix

    def test_analyzer_threads_path_into_statuses(self, stranded_pad_pcb: Path):
        analyzer = NetStatusAnalyzer(stranded_pad_pcb, strict=True)
        gnda = analyzer.analyze().get_net("GNDA")
        assert gnda is not None
        assert str(stranded_pad_pcb) in gnda.suggested_fix
        assert "board.kicad_pcb" not in gnda.suggested_fix


# ---------------------------------------------------------------------------
# Scoped strict analysis (NetStatusAnalyzer.analyze_nets)
# ---------------------------------------------------------------------------


class TestAnalyzeNetsScoped:
    def test_scoped_analysis_matches_full(self, stranded_pad_pcb: Path):
        analyzer = NetStatusAnalyzer(stranded_pad_pcb, strict=True)
        scoped = analyzer.analyze_nets({"GNDA"})

        assert [n.net_name for n in scoped.nets] == ["GNDA"]
        gnda = scoped.nets[0]
        assert [p.full_name for p in gnda.unconnected_pads] == ["C1.1"]
        assert {p.full_name for p in gnda.connected_pads} == {"C2.1", "C3.1"}

    def test_scoped_analysis_unknown_net_is_absent(self, stranded_pad_pcb: Path):
        analyzer = NetStatusAnalyzer(stranded_pad_pcb, strict=True)
        scoped = analyzer.analyze_nets({"NOT_A_NET"})
        assert scoped.nets == []
