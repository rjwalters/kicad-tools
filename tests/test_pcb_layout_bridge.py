"""End-to-end: apply a ``PCBLayout`` to a real, netted ``.kicad_pcb`` (Issue #7022).

The fixture board carries the **real** KiCad 10 library footprints the blocks
name (``tests/fixtures/KiCad10_Library.pretty``: verbatim copies of
``Package_TO_SOT_SMD:SOT-23-5``, ``Capacitor_SMD:C_0805_2012Metric``,
``LED_SMD:LED_0603_1608Metric`` and ``Resistor_SMD:R_0603_1608Metric`` from
KiCad 10.0.6, CC-BY-SA 4.0 with the KiCad library exception) with
schematic-style pad nets, so the bridge runs on real pads and its output can
be DRC-checked by kicad-cli.  Every happy-path trace lands on a same-net pad:
the bar is **no new kicad-cli DRC errors**.

The stock ``LDOBlock``'s internal routing and ``PCBLayout.route()``'s straight
port-to-port lines still short foreign pads (#7062, #7064), so the happy path
uses a hand-routed LDO block and explicit inter-block segments; the stock
layout is kept as the fixture the copper check must *reject*.

The main layout places blocks at 0 and 180 degrees; the 90/270 cases at the
end check that block traces and vias land on the pads KiCad draws (#7046).
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
from collections import Counter
from pathlib import Path

import pytest

from kicad_tools.pcb import (
    KiCadPCBExporter,
    LayoutApplyError,
    LayoutApplyReport,
    PCBLayout,
    update_pcb_placements,
)
from kicad_tools.pcb.blocks import LDOBlock, LEDBlock, PCBBlock
from kicad_tools.pcb.geometry import Layer, Point
from kicad_tools.pcb.placement import get_footprint_pads
from kicad_tools.pcb.primitives import TraceSegment, Via
from kicad_tools.schema.pcb import PCB

LIB = Path(__file__).parent / "fixtures" / "KiCad10_Library.pretty"
TEST_LIB = Path(__file__).parent / "fixtures" / "Test_Library.pretty"
REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "guides" / "schematic-to-pcb-workflow.md"

NET_MAP = {"VOUT": "+3V3", "LED_MID": "Net-(D1-K)"}

REFS = {
    "U1": "SOT-23-5",
    "C1": "C_0805_2012Metric",
    "C2": "C_0805_2012Metric",
    "D1": "LED_0603_1608Metric",
    "R1": "R_0603_1608Metric",
}

# (ref, pad, net): what schematic-to-PCB sync would have given the pads.
PAD_NETS = [
    ("U1", "1", "VIN"),
    ("U1", "2", "GND"),
    ("U1", "5", "+3V3"),
    ("C1", "1", "VIN"),
    ("C1", "2", "GND"),
    ("C2", "1", "+3V3"),
    ("C2", "2", "GND"),
    ("D1", "1", "Net-(D1-K)"),
    ("D1", "2", "+3V3"),
    ("R1", "1", "Net-(D1-K)"),
    ("R1", "2", "GND"),
]

# Copper the happy-path layout writes.
LDO_TRACES = 6  # VIN, VOUT, 4x GND
LED_TRACES = 1  # LED_MID
INTER_TRACES = 4  # 3x +3V3 (C2 -> D1 anode), 1x GND (R1 -> via)
N_TRACES = LDO_TRACES + LED_TRACES + INTER_TRACES
N_VIAS = 2


def _build_board(
    path: Path, refs: dict[str, str], pad_nets, layers: int = 2, lib: Path = LIB
) -> Path:
    pcb = PCB.create(width=40, height=30, layers=layers, center=True)
    for i, (ref, footprint) in enumerate(refs.items()):
        pcb.add_footprint_from_file(lib / f"{footprint}.kicad_mod", ref, 5 + 5 * i, 25)
    for ref, pad, net in pad_nets:
        assert pcb.assign_net_to_footprint_pad(ref, pad, net)
    pcb.save(str(path))
    return path


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("bridge") / "source.kicad_pcb"
    return _build_board(path, REFS, PAD_NETS)


@pytest.fixture(scope="module")
def origin(source) -> tuple[float, float]:
    ox, oy = PCB.load(str(source)).board_origin
    assert (ox, oy) != (0.0, 0.0), "fixture must exercise a nonzero board origin"
    return ox, oy


C0805 = {"1": (-0.95, 0.0), "2": (0.95, 0.0)}  # KiCad 10 C_0805_2012Metric


def _ldo_block() -> PCBBlock:
    """An LDO + in/out caps whose copper really lands on its own pads.

    U1 (SOT-23-5) pins 1-3 down the left at x=-1.1375, pin 5 top-right.  The
    caps are turned 270 degrees so pad 1 is on top: VIN and VOUT run straight
    across at y=-0.95 and GND leaves pin 2 to the left, then runs under the
    package at y=2.5 to C2.
    """
    block = PCBBlock("LDO_U1")
    sot = get_footprint_pads("Package_TO_SOT_SMD:SOT-23-5")
    u1 = block.add_component("U1", "Package_TO_SOT_SMD:SOT-23-5", 0, 0, pads=sot)
    c1 = block.add_component("C1", "Capacitor_SMD:C_0805_2012Metric", -3.5, 0, 270, C0805)
    c2 = block.add_component("C2", "Capacitor_SMD:C_0805_2012Metric", 3.5, 0, 270, C0805)
    block.add_trace(u1.pad_position("1"), c1.pad_position("1"), width=0.4, net="VIN")
    block.add_trace(u1.pad_position("5"), c2.pad_position("1"), width=0.4, net="VOUT")
    gnd = [u1.pad_position("2"), Point(-3.5, 0), Point(-3.5, 2.5), Point(3.5, 2.5)]
    gnd.append(c2.pad_position("2"))
    for a, b in zip(gnd, gnd[1:], strict=False):
        block.add_trace(a, b, width=0.4, net="GND")
    block.add_via(0, 2.5, net="GND")  # a block-internal via on the GND run
    block.add_port("VIN", -5.5, -0.95, direction="power")
    block.add_port("VOUT", 5.5, -0.95, direction="power")
    block.add_port("GND", 0, 2.5, direction="power")
    block.add_port("EN", -2, 3, direction="in", internal_pad="U1.3")
    return block


def _layout(origin: tuple[float, float]) -> PCBLayout:
    """LDO (rotation 0) + LED (rotation 180), joined on VOUT, plus vias."""
    ox, oy = origin
    layout = PCBLayout("bridge")
    ldo = _ldo_block()
    ldo.place(ox + 10, oy + 12, rotation=0)
    layout.add_block(ldo, name="ldo")
    led = LEDBlock(led_ref="D1", res_ref="R1")
    led.place(ox + 24, oy + 12, rotation=180)  # D1 at x=24, R1 at x=21
    layout.add_block(led, name="led")

    def seg(a: tuple[float, float], b: tuple[float, float], net: str) -> None:
        layout.inter_block_traces.append(
            TraceSegment(Point(ox + a[0], oy + a[1]), Point(ox + b[0], oy + b[1]), net=net)
        )

    # C2.1 (13.5, 11.05) up, across above the LED block, down onto D1.2 (24.8, 12).
    path = [(13.5, 11.05), (13.5, 9.5), (24.8, 9.5), (24.8, 12)]
    for a, b in zip(path, path[1:], strict=False):
        seg(a, b, "VOUT")
    seg((20.2, 12), (18.5, 12), "GND")  # R1.2 to a GND via
    layout.inter_block_vias.append(Via(position=Point(ox + 18.5, oy + 12), net="GND"))
    return layout


def _stock_layout(origin: tuple[float, float]) -> PCBLayout:
    """The stock LDOBlock + LEDBlock joined by ``route()`` -- copper that shorts."""
    ox, oy = origin
    layout = PCBLayout("stock")
    ldo = LDOBlock(ldo_ref="U1", input_cap="C1", output_caps=["C2"])
    ldo.place(ox + 10, oy + 12, rotation=0)
    layout.add_block(ldo, name="ldo")
    led = LEDBlock(led_ref="D1", res_ref="R1")
    led.place(ox + 28, oy + 12, rotation=180)
    layout.add_block(led, name="led")
    layout.route("ldo", "VOUT", "led", "ANODE", net="VOUT")
    return layout


@pytest.fixture(scope="module")
def applied(source, origin, tmp_path_factory):
    out = tmp_path_factory.mktemp("bridge_out") / "output.kicad_pcb"
    layout = _layout(origin)
    report = KiCadPCBExporter(layout, net_map=NET_MAP).apply(source, out)
    return layout, report, out


def _net_table(pcb: PCB) -> dict[int, str]:
    return {number: net.name for number, net in pcb.nets.items()}


def _uuids(text: str) -> list[str]:
    return re.findall(r'\(uuid "?([0-9a-fA-F-]+)"?\)', text)


# ---------------------------------------------------------------------------
# The happy path
# ---------------------------------------------------------------------------


def test_report(applied):
    _, report, _ = applied
    assert isinstance(report, LayoutApplyReport)
    assert report.moved == ["U1", "C1", "C2", "D1", "R1"]
    assert report.missing_refs == []
    assert report.skipped == []
    assert report.segments_added == N_TRACES
    assert report.vias_added == N_VIAS
    # The board carries the very library footprints the blocks name.
    assert report.footprint_mismatches == []


def test_footprints_moved_to_sheet_absolute_placements(applied):
    layout, _, out = applied
    pcb = PCB.load(str(out))
    ox, oy = pcb.board_origin
    for placement in layout.export_placements():
        fp = pcb.get_footprint(placement["ref"])
        assert fp is not None
        assert fp.position[0] + ox == pytest.approx(placement["x"], abs=1e-4)
        assert fp.position[1] + oy == pytest.approx(placement["y"], abs=1e-4)
        assert fp.rotation % 360 == pytest.approx(placement["rotation"] % 360, abs=1e-4)


def test_every_trace_and_via_is_board_copper_on_the_mapped_net(source, applied):
    layout, _, out = applied
    src = PCB.load(str(source))
    pcb = PCB.load(str(out))
    ox, oy = pcb.board_origin

    def net_number(name: str) -> int:
        net = src.get_net_by_name(NET_MAP.get(name, name))
        assert net is not None
        return net.number

    segments = [
        (s.net_number, s.layer, s.start, s.end, s.width) for s in pcb.segments
    ]  # fmt: skip
    traces = layout.export_traces()
    assert len(segments) == len(traces)
    for trace in traces:
        start = (trace["start"][0] - ox, trace["start"][1] - oy)
        end = (trace["end"][0] - ox, trace["end"][1] - oy)
        assert any(
            n == net_number(trace["net"])
            and layer == trace["layer"]
            # The board file stores 3 decimals (8.8625 is saved as 8.863).
            and s == pytest.approx(start, abs=1e-3)
            and e == pytest.approx(end, abs=1e-3)
            and w == pytest.approx(trace["width"])
            for n, layer, s, e, w in segments
        ), trace

    vias = [(v.net_number, v.position, tuple(v.layers)) for v in pcb.vias]
    exported = layout.export_vias()
    assert {v["block_id"] for v in exported} == {"LDO_U1", None}
    assert len(vias) == len(exported)
    for via in exported:
        assert any(
            n == net_number(via["net"])
            and pos == pytest.approx((via["x"] - ox, via["y"] - oy), abs=1e-4)
            and layers == via["layers"]
            for n, pos, layers in vias
        ), via


def test_never_invents_nets(source, applied):
    _, _, out = applied
    before = PCB.load(str(source))
    after = PCB.load(str(out))
    assert len(after.nets) == len(before.nets)
    assert _net_table(after) == _net_table(before)


def test_deterministic_byte_identical_and_no_uuid_collision(source, origin, applied, tmp_path):
    _, _, out = applied
    again = tmp_path / "again.kicad_pcb"
    _layout(origin).apply_to_pcb(source, again, net_map=NET_MAP)
    assert again.read_bytes() == out.read_bytes()

    source_uuids = set(_uuids(source.read_text()))
    out_uuids = _uuids(out.read_text())
    assert len(out_uuids) == len(set(out_uuids)), "a UUID appears twice"
    assert len(set(out_uuids) - source_uuids) == N_TRACES + N_VIAS  # one per new item


def test_exporter_has_no_random_uuids():
    text = (REPO / "src" / "kicad_tools" / "pcb" / "exporter.py").read_text()
    assert "uuid4" not in text
    assert "import uuid" not in text


def test_reapply_is_idempotent(origin, applied, tmp_path):
    _, _, out = applied
    again = tmp_path / "reapplied.kicad_pcb"
    report = _layout(origin).apply_to_pcb(out, again, net_map=NET_MAP)
    assert report.segments_added == 0
    assert report.vias_added == 0
    assert len(PCB.load(str(again)).segments) == len(PCB.load(str(out)).segments)


def test_traces_and_vias_switches(source, origin, tmp_path):
    out = tmp_path / "placements_only.kicad_pcb"
    report = _layout(origin).apply_to_pcb(
        source, out, net_map=NET_MAP, traces=False, vias=False
    )  # fmt: skip
    assert report.moved and report.segments_added == 0 and report.vias_added == 0
    pcb = PCB.load(str(out))
    assert pcb.segments == [] and pcb.vias == []


# ---------------------------------------------------------------------------
# Blocks at 90/270 degrees: copper lands on the real pads (#7046)
# ---------------------------------------------------------------------------

# R_0603_1608Metric in tests/fixtures/Test_Library.pretty: pads at x = -/+0.775.
_R0603_PADS = {"1": (-0.775, 0.0), "2": (0.775, 0.0)}


def _rotated_layout(origin: tuple[float, float], rotation: float) -> PCBLayout:
    """A two-resistor block with the fixture's real pad offsets, at ``rotation``.

    R1 is itself rotated 90 degrees inside the block, so the board pad is the
    composition of the block and the footprint rotations.
    """
    ox, oy = origin
    block = PCBBlock("pair", block_id="PAIR")
    d1 = block.add_component("D1", "R_0603_1608Metric", 0, 0, pads=_R0603_PADS)
    r1 = block.add_component("R1", "R_0603_1608Metric", 3, 1, rotation=90, pads=_R0603_PADS)
    block.add_trace(d1.pad_position("1"), r1.pad_position("1"), net="LED_MID")
    gnd = r1.pad_position("2")
    block.add_via(gnd.x, gnd.y, net="GND")
    block.place(ox + 15, oy + 12, rotation=rotation)
    layout = PCBLayout("rotated")
    layout.add_block(block, name="pair")
    return layout


@pytest.mark.parametrize("rotation", [90, 270])
def test_rotated_block_copper_lands_on_kicad_pads(tmp_path, rotation):
    source = _build_board(
        tmp_path / "source.kicad_pcb",
        {"D1": "R_0603_1608Metric", "R1": "R_0603_1608Metric"},
        [("D1", "1", "Net-(D1-K)"), ("R1", "1", "Net-(D1-K)"), ("R1", "2", "GND")],
    )
    origin = PCB.load(str(source)).board_origin
    out = tmp_path / "output.kicad_pcb"
    report = _rotated_layout(origin, rotation).apply_to_pcb(source, out, net_map=NET_MAP)
    assert report.segments_added == 1 and report.vias_added == 1

    # PCB.get_pad_position is the board model's own footprint pad transform.
    pcb = PCB.load(str(out))
    (seg,) = pcb.segments
    (via,) = pcb.vias
    assert seg.start == pytest.approx(pcb.get_pad_position("D1", "1"), abs=1e-4)
    assert seg.end == pytest.approx(pcb.get_pad_position("R1", "1"), abs=1e-4)
    assert via.position == pytest.approx(pcb.get_pad_position("R1", "2"), abs=1e-4)


# ---------------------------------------------------------------------------
# Strict / lenient error policy
# ---------------------------------------------------------------------------


def _bad_layout(origin: tuple[float, float]) -> PCBLayout:
    """The good layout plus one instance of every strict-mode problem."""
    layout = _layout(origin)
    ox, oy = origin
    ghost = PCBBlock("ghost")
    ghost.add_component("U9", "SOT-23-5", 0, 0)  # not on the board
    ghost.add_component("R1", "R_0603_1608Metric", 2, 0)  # R1 is also in "led"
    ghost.place(ox + 30, oy + 25)
    layout.add_block(ghost, name="ghost")
    layout.route("ldo", "GND", "led", "CATHODE")  # net=None
    layout.route("ldo", "VIN", "led", "CATHODE", net="NOT_ON_BOARD")  # unmapped
    layout.route("ldo", "EN", "led", "CATHODE", net="GND", layer=Layer.F_SILKS)
    layout.route("ldo", "EN", "led", "ANODE", net="GND", layer=Layer.IN1_CU)
    return layout


STRICT_CASES = {
    "missing ref": "ref U9",
    "duplicate ref": "ref R1 is in two blocks",
    "net=None": "net=None",
    "unmapped net": "'NOT_ON_BOARD'",
    "non-copper layer": "F.SilkS, which is not a copper layer",
    "absent copper layer": "In1.Cu, which this 2-layer board does not have",
}


def test_strict_raises_one_error_listing_every_problem(source, origin, tmp_path):
    out = tmp_path / "never.kicad_pcb"
    with pytest.raises(LayoutApplyError) as excinfo:
        _bad_layout(origin).apply_to_pcb(source, out, net_map=NET_MAP)
    error = excinfo.value
    assert isinstance(error, ValueError)
    assert len(error.problems) == len(STRICT_CASES)
    for case, needle in STRICT_CASES.items():
        assert sum(needle in p for p in error.problems) == 1, (case, error.problems)
        assert needle in str(error)
    assert not out.exists()


@pytest.mark.parametrize("case", sorted(STRICT_CASES))
def test_each_strict_case_alone_writes_nothing(source, origin, tmp_path, case):
    ox, oy = origin
    layout = _layout(origin)
    if case == "missing ref":
        block = PCBBlock("ghost")
        block.add_component("U9", "SOT-23-5", 0, 0)
        layout.add_block(block, name="ghost")
    elif case == "duplicate ref":
        block = PCBBlock("dup")
        block.add_component("R1", "R_0603_1608Metric", 0, 0)
        block.place(ox + 5, oy + 5)
        layout.add_block(block, name="dup")
    elif case == "net=None":
        layout.route("ldo", "GND", "led", "CATHODE")
    elif case == "unmapped net":
        layout.route("ldo", "VIN", "led", "CATHODE", net="NOT_ON_BOARD")
    elif case == "non-copper layer":
        layout.route("ldo", "EN", "led", "CATHODE", net="GND", layer=Layer.F_SILKS)
    else:
        layout.route("ldo", "EN", "led", "ANODE", net="GND", layer=Layer.IN1_CU)

    out = tmp_path / "never.kicad_pcb"
    with pytest.raises(LayoutApplyError) as excinfo:
        layout.apply_to_pcb(source, out, net_map=NET_MAP)
    assert len(excinfo.value.problems) == 1
    assert STRICT_CASES[case] in excinfo.value.problems[0]
    assert not out.exists()


def test_unmapped_block_net_is_strict_error(source, origin, tmp_path):
    """Without net_map the generic VOUT / LED_MID names are not board nets."""
    out = tmp_path / "never.kicad_pcb"
    with pytest.raises(LayoutApplyError) as excinfo:
        _layout(origin).apply_to_pcb(source, out)
    assert any("'VOUT'" in p for p in excinfo.value.problems)
    assert any("'LED_MID'" in p for p in excinfo.value.problems)
    assert not out.exists()


def test_lenient_skips_and_warns(source, origin, tmp_path, caplog):
    out = tmp_path / "lenient.kicad_pcb"
    with caplog.at_level(logging.WARNING, logger="kicad_tools.pcb.exporter"):
        report = _bad_layout(origin).apply_to_pcb(source, out, net_map=NET_MAP, strict=False)

    assert out.exists()
    assert len(report.skipped) == len(STRICT_CASES)
    for needle in STRICT_CASES.values():
        assert sum(needle in s for s in report.skipped) == 1
        assert sum(needle in r.getMessage() for r in caplog.records) == 1
    assert report.missing_refs == ["U9"]
    # The good items still went in: the five moves and all the good copper.
    assert report.moved == ["U1", "C1", "C2", "D1", "R1"]
    assert report.segments_added == N_TRACES
    assert report.vias_added == N_VIAS
    assert _net_table(PCB.load(str(out))) == _net_table(PCB.load(str(source)))


# ---------------------------------------------------------------------------
# Copper geometry: shorts and open ends are refused, against the real pads
# ---------------------------------------------------------------------------


def _with_bad_copper(origin: tuple[float, float], case: str) -> PCBLayout:
    """The good layout plus one copper item that would short or dangle."""
    ox, oy = origin
    layout = _layout(origin)
    if case == "open end":
        # From the R1.2 GND pad straight down into empty board.
        layout.inter_block_traces.append(
            TraceSegment(Point(ox + 20.2, oy + 12), Point(ox + 20.2, oy + 16), net="GND")
        )
    elif case == "foreign pad":
        # U1.2 (GND) straight down to the GND run at y=14.5 -- through U1.3 (EN).
        layout.inter_block_traces.append(
            TraceSegment(Point(ox + 8.8625, oy + 12), Point(ox + 8.8625, oy + 14.5), net="GND")
        )
    else:  # "via on foreign pad": a GND via on the unconnected U1.4.
        layout.inter_block_vias.append(Via(position=Point(ox + 11.1375, oy + 12.95), net="GND"))
    return layout


GEOMETRY_CASES = {
    "open end": "touches no pad, track or via on net 'GND'",
    "foreign pad": "shorts pad U1.3 on net <no net>",
    "via on foreign pad": "shorts pad U1.4 on net <no net>",
}


@pytest.mark.parametrize("case", sorted(GEOMETRY_CASES))
def test_strict_refuses_copper_that_shorts_or_dangles(source, origin, tmp_path, case):
    out = tmp_path / "never.kicad_pcb"
    with pytest.raises(LayoutApplyError) as excinfo:
        _with_bad_copper(origin, case).apply_to_pcb(source, out, net_map=NET_MAP)
    (problem,) = excinfo.value.problems
    assert GEOMETRY_CASES[case] in problem, problem
    assert problem.startswith("via " if case.startswith("via") else "trace ")
    assert not out.exists()


@pytest.mark.parametrize("case", sorted(GEOMETRY_CASES))
def test_lenient_skips_copper_that_shorts_or_dangles(source, origin, applied, tmp_path, case):
    out = tmp_path / "lenient.kicad_pcb"
    report = _with_bad_copper(origin, case).apply_to_pcb(
        source, out, net_map=NET_MAP, strict=False
    )  # fmt: skip
    (skipped,) = report.skipped
    assert GEOMETRY_CASES[case] in skipped
    # Everything else went in, and nothing more: the board is the happy path's.
    assert (report.segments_added, report.vias_added) == (N_TRACES, N_VIAS)
    assert out.read_bytes() == applied[2].read_bytes()


def test_open_end_is_ok_when_it_lands_on_new_or_existing_same_net_copper(source, origin, applied):
    """An endpoint on another track (new, or already on the board) is connected."""
    ox, oy = origin
    layout = _layout(origin)
    # From the middle of the new GND run at y=14.5 down to a GND via.
    layout.inter_block_traces.append(
        TraceSegment(Point(ox + 10, oy + 14.5), Point(ox + 10, oy + 16.5), net="GND")
    )
    layout.inter_block_vias.append(Via(position=Point(ox + 10, oy + 16.5), net="GND"))
    pcb = PCB.load(str(source))
    report = KiCadPCBExporter(layout, net_map=NET_MAP).apply_to(pcb)
    assert report.segments_added == N_TRACES + 1 and report.skipped == []

    # Re-applied on top of the first result, the stub's ends sit on existing copper.
    stub = PCBLayout("stub")
    stub.inter_block_traces.append(
        TraceSegment(Point(ox + 10, oy + 16.5), Point(ox + 12, oy + 16.5), net="GND")
    )
    stub.inter_block_vias.append(Via(position=Point(ox + 12, oy + 16.5), net="GND"))
    assert KiCadPCBExporter(stub).apply_to(pcb).segments_added == 1


def test_foreign_copper_already_on_the_board_is_checked(source, origin, applied):
    ox, oy = origin
    layout = PCBLayout("cross")
    # Net-(D1-K) from D1.1 up across the existing +3V3 run at y=9.5.
    layout.inter_block_traces.append(
        TraceSegment(Point(ox + 23.2, oy + 12), Point(ox + 23.2, oy + 8), net="LED_MID")
    )
    pcb = PCB.load(str(applied[2]))
    with pytest.raises(LayoutApplyError) as excinfo:
        KiCadPCBExporter(layout, net_map=NET_MAP).apply_to(pcb)
    (problem,) = excinfo.value.problems
    assert "shorts an existing track on net '+3V3'" in problem


def test_clearance_comes_from_the_board_rules(source, origin, tmp_path):
    """A gap legal at KiCad's 0.2 mm default fails a stricter project rule."""
    from kicad_tools.router.board_clearance_rules import BoardClearanceRules

    ox, oy = origin
    layout = _layout(origin)
    # The LDO block's GND run at y=0 clears C1.1 (VIN) by 0.25 mm: fine at
    # 0.2 mm, too close at 0.3 mm.
    strict = BoardClearanceRules(class_clearance={"Default": 0.3})
    with pytest.raises(LayoutApplyError) as excinfo:
        KiCadPCBExporter(layout, net_map=NET_MAP, clearance_rules=strict).apply_to(
            PCB.load(str(source))
        )
    assert any(
        "0.250 mm from pad C1.1 on net 'VIN' (clearance 0.3 mm)" in p
        for p in excinfo.value.problems
    ), excinfo.value.problems

    # The same rule read from a .kicad_pro beside the board.
    board = tmp_path / "rules.kicad_pcb"
    board.write_bytes(source.read_bytes())
    board.with_suffix(".kicad_pro").write_text(
        json.dumps({"net_settings": {"classes": [{"name": "Default", "clearance": 0.3}]}})
    )
    with pytest.raises(LayoutApplyError):
        layout.apply_to_pcb(board, tmp_path / "never.kicad_pcb", net_map=NET_MAP)


def test_stock_blocks_and_route_are_refused_not_shorted(source, origin, tmp_path):
    """The stock LDOBlock routing and route()'s straight line short foreign pads."""
    out = tmp_path / "never.kicad_pcb"
    with pytest.raises(LayoutApplyError) as excinfo:
        _stock_layout(origin).apply_to_pcb(source, out, net_map=NET_MAP)
    problems = excinfo.value.problems
    assert not out.exists()
    # route() VOUT -> ANODE: a straight line across the LED block's pads.
    assert any(
        p.startswith("trace") and "(inter-block) on net" in p and "shorts pad R1." in p
        for p in problems
    ), problems
    # LDOBlock: its GND traces cut through U1's VIN / +3V3 pads.
    assert any("(block 'LDO_U1') on net 'GND'" in p and "shorts pad U1." in p for p in problems)
    # The LEDBlock fix (#7022 review): its LED_MID trace no longer crosses D1.2;
    # all that is wrong with it here is the inter-block line drawn over it.
    led = [p for p in problems if p.split(" on net ")[0].endswith("(block 'LED_D1')")]
    assert led and all("pad" not in p for p in led), led


# ---------------------------------------------------------------------------
# update_pcb_placements: still lenient, now honest about what it moved
# ---------------------------------------------------------------------------


def test_update_pcb_placements_counts_moves_and_warns_missing(
    source, origin, tmp_path, capsys, caplog
):
    layout = _layout(origin)
    ghost = PCBBlock("ghost")
    ghost.add_component("U9", "SOT-23-5", 0, 0)
    layout.add_block(ghost, name="ghost")
    out = tmp_path / "placed.kicad_pcb"

    with caplog.at_level(logging.WARNING, logger="kicad_tools.pcb.exporter"):
        update_pcb_placements(str(source), layout, str(out))

    assert "Updated 5 components" in capsys.readouterr().out
    assert "U9" in caplog.text
    pcb = PCB.load(str(out))
    ox, oy = pcb.board_origin
    u1 = next(p for p in layout.export_placements() if p["ref"] == "U1")
    fp = pcb.get_footprint("U1")
    assert fp is not None
    assert fp.position[0] + ox == pytest.approx(u1["x"], abs=1e-4)
    assert fp.position[1] + oy == pytest.approx(u1["y"], abs=1e-4)
    assert pcb.segments == []


# ---------------------------------------------------------------------------
# Public API surface
# ---------------------------------------------------------------------------


def test_public_api():
    import kicad_tools.pcb as pcb_pkg
    import kicad_tools.pcb.blocks as blocks_pkg

    for name in (
        "KiCadPCBExporter",
        "update_pcb_placements",
        "LayoutApplyError",
        "LayoutApplyReport",
    ):  # fmt: skip
        assert name in pcb_pkg.__all__
    assert blocks_pkg.KiCadPCBExporter is KiCadPCBExporter
    assert "LayoutApplyError" not in blocks_pkg.__all__


# ---------------------------------------------------------------------------
# KiCad loads the result
# ---------------------------------------------------------------------------


def _kicad_cli() -> Path | None:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli()


def _drc_errors(board: Path, tmp_path: Path) -> Counter[str]:
    """kicad-cli's error-severity DRC violations of ``board``, by type."""
    report = tmp_path / f"{board.stem}.drc.json"
    result = subprocess.run(
        [str(_kicad_cli()), "pcb", "drc", "--format", "json", "--severity-error",
         "-o", str(report), str(board)],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    # A non-zero exit means kicad-cli did not check the board (#6724).
    assert result.returncode == 0, result.stderr
    data = json.loads(report.read_text())
    return Counter(v["type"] for v in data["violations"] if v.get("severity") == "error")


requires_kicad_cli = pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")


@requires_kicad_cli
def test_kicad_cli_drc_finds_no_new_errors(source, applied, tmp_path):
    """The applied board has no error-severity DRC violation the source lacks."""
    _, _, out = applied
    before = _drc_errors(source, tmp_path)
    after = _drc_errors(out, tmp_path)
    assert after - before == Counter(), f"new DRC errors: {dict(after - before)}"


@requires_kicad_cli
def test_kicad_cli_drc_lenient_stock_layout_adds_no_errors(source, origin, tmp_path):
    """Lenient mode drops the stock layout's shorts instead of writing them."""
    out = tmp_path / "stock_lenient.kicad_pcb"
    report = _stock_layout(origin).apply_to_pcb(source, out, net_map=NET_MAP, strict=False)
    assert report.skipped
    before = _drc_errors(source, tmp_path)
    after = _drc_errors(out, tmp_path)
    assert after - before == Counter(), f"new DRC errors: {dict(after - before)}"


# ---------------------------------------------------------------------------
# The guide's PCBLayout snippets run
# ---------------------------------------------------------------------------


def _guide_block(marker: str) -> str:
    blocks = re.findall(r"^```python\s*\n(.*?)^```\s*$", GUIDE.read_text(), re.S | re.M)
    matches = [b for b in blocks if marker in b]
    assert len(matches) == 1, marker
    return matches[0]


def test_guide_pcblayout_snippets_run(tmp_path, monkeypatch):
    namespace: dict = {}
    exec(_guide_block('PCBLayout("power_system")'), namespace)
    layout = namespace["layout"]
    refs = [p["ref"] for p in namespace["placements"]]
    assert len(refs) == len(set(refs)) == 6
    assert all(t["net"] for t in layout.export_traces())

    # A board with those footprints and the nets the snippet's net_map names.
    monkeypatch.chdir(tmp_path)
    _build_board(
        tmp_path / "board.kicad_pcb",
        {
            "U1": "SOT-23-5",
            "U2": "SOT-23-5",
            "C1": "R_0603_1608Metric",
            "C2": "R_0603_1608Metric",
            "C3": "R_0603_1608Metric",
            "C4": "R_0603_1608Metric",
        },
        [("C1", "1", "/VIN"), ("C2", "1", "+3V3"), ("C1", "2", "GND")],
    )
    exec(_guide_block("apply_to_pcb("), namespace)
    report = namespace["report"]
    assert report.moved == refs
    # The stock blocks' copper shorts (#7062, #7064): the lenient snippet
    # writes what is clean and lists the rest.
    skipped_traces = sum(p.startswith("trace ") for p in report.skipped)
    assert skipped_traces > 0
    assert report.segments_added + skipped_traces == len(layout.export_traces())
    assert (tmp_path / "board_placed.kicad_pcb").exists()
