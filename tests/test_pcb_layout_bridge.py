"""End-to-end: apply a ``PCBLayout`` to a real, netted ``.kicad_pcb`` (Issue #7022).

The fixture board carries real footprints from ``tests/fixtures/Test_Library.pretty``
with schematic-style pad nets, so the bridge runs on a real file with real pads.
Blocks sit at rotations 0 and 180 only: 90/270 use the opposite sign to KiCad's
footprint rotation (#7046), which this bridge deliberately does not correct.
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
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
from kicad_tools.pcb.primitives import Via
from kicad_tools.schema.pcb import PCB

LIB = Path(__file__).parent / "fixtures" / "Test_Library.pretty"
REPO = Path(__file__).resolve().parent.parent
GUIDE = REPO / "docs" / "guides" / "schematic-to-pcb-workflow.md"

NET_MAP = {"VOUT": "+3V3", "LED_MID": "Net-(D1-K)"}

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


def _build_board(path: Path, refs: dict[str, str], pad_nets, layers: int = 2) -> Path:
    pcb = PCB.create(width=40, height=30, layers=layers, center=True)
    for i, (ref, footprint) in enumerate(refs.items()):
        pcb.add_footprint_from_file(LIB / f"{footprint}.kicad_mod", ref, 5 + 5 * i, 25)
    for ref, pad, net in pad_nets:
        assert pcb.assign_net_to_footprint_pad(ref, pad, net)
    pcb.save(str(path))
    return path


@pytest.fixture(scope="module")
def source(tmp_path_factory) -> Path:
    path = tmp_path_factory.mktemp("bridge") / "source.kicad_pcb"
    refs = {
        "U1": "SOT-23-5",
        "C1": "R_0603_1608Metric",
        "C2": "R_0603_1608Metric",
        "D1": "R_0603_1608Metric",
        "R1": "R_0603_1608Metric",
    }
    return _build_board(path, refs, PAD_NETS)


@pytest.fixture(scope="module")
def origin(source) -> tuple[float, float]:
    ox, oy = PCB.load(str(source)).board_origin
    assert (ox, oy) != (0.0, 0.0), "fixture must exercise a nonzero board origin"
    return ox, oy


def _layout(origin: tuple[float, float]) -> PCBLayout:
    """LDO (rotation 0) + LED (rotation 180), joined on VOUT, plus vias."""
    ox, oy = origin
    layout = PCBLayout("bridge")
    ldo = LDOBlock(ldo_ref="U1", input_cap="C1", output_caps=["C2"])
    ldo.add_via(0, 3, net="GND")  # a block-internal via at the GND port
    ldo.place(ox + 10, oy + 12, rotation=0)
    layout.add_block(ldo, name="ldo")
    led = LEDBlock(led_ref="D1", res_ref="R1")
    led.place(ox + 28, oy + 12, rotation=180)
    layout.add_block(led, name="led")
    layout.route("ldo", "VOUT", "led", "ANODE", net="VOUT")
    layout.inter_block_vias.append(Via(position=Point(ox + 20, oy + 20), net="GND"))
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
    # LDOBlock: 4 traces (VIN, VOUT, 2x GND); LEDBlock: 1; inter-block: 1.
    assert report.segments_added == 6
    assert report.vias_added == 2
    # The block footprints are C_0805 / LED_0603; the board has R_0603.
    assert report.footprint_mismatches == ["C1", "C2", "D1"]


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
            and s == pytest.approx(start, abs=1e-4)
            and e == pytest.approx(end, abs=1e-4)
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
    assert len(set(out_uuids) - source_uuids) == 6 + 2  # one per new segment/via


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
    # The good items still went in: the five moves, 6 segments and 2 vias.
    assert report.moved == ["U1", "C1", "C2", "D1", "R1"]
    assert report.segments_added == 6
    assert report.vias_added == 2
    assert _net_table(PCB.load(str(out))) == _net_table(PCB.load(str(source)))


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


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
def test_kicad_cli_loads_the_output(applied, tmp_path):
    _, _, out = applied
    report = tmp_path / "drc.json"
    # DRC violations are allowed (block pad geometry is approximate); this
    # only shows KiCad parses and checks the board.
    result = subprocess.run(
        [str(_kicad_cli()), "pcb", "drc", "--format", "json", "-o", str(report), str(out)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert isinstance(json.loads(report.read_text()), dict)


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
    assert report.segments_added == len(layout.export_traces())
    assert (tmp_path / "board_placed.kicad_pcb").exists()
