"""A keyword-less partial-span via is read as a through via, as KiCad does (Issue #6493).

KiCad decides a via's drilled span from its ``micro`` / ``blind`` /
``buried`` keyword, not from its ``(layers ...)`` pair.  A ``(via ...)`` node
with no keyword is a through via whatever the pair says, and
``kicad-cli pcb upgrade --force`` rewrites it to ``(layers "F.Cu" "B.Cu")``.
kicad-tools used to take the pair literally, which gave a false open on
board 1 and a false clean on board 2 below.

Goldens, measured with kicad-cli 10.0.6 (``pcb drc --format json``):

* board 1: 0 ``unconnected_items``, no ``shorting_items``;
* board 2: 0 ``unconnected_items``, one ``shorting_items``.

When kicad-cli is installed the live verdict is re-checked; the golden
assertions on kicad-tools run everywhere.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path

import pytest

from kicad_tools.core.layers import kicad_read_via_layers
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer.pcb import parse_vias
from kicad_tools.schema.pcb import PCB, Via

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
    (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "OTHER")
"""


def _fp(ref: str, x: float, y: float, layer: str, net: int, net_name: str) -> str:
    silk = "F.SilkS" if layer == "F.Cu" else "B.SilkS"
    return (
        f'  (footprint "R" (layer "{layer}") (uuid "fp-{ref}") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "{silk}") (uuid "ref-{ref}"))\n'
        f'    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "{layer}") '
        f'(net {net} "{net_name}")))\n'
    )


def board1(via_kw: str = "") -> str:
    """Two F.Cu pads joined only by a B.Cu trace and two ``In1.Cu``/``B.Cu`` vias.

    Keyword-less, KiCad drills the vias through, so the pads are connected.
    With ``via_kw="blind "`` the vias never reach F.Cu and the pads are open.
    """
    return (
        _HEADER
        + _fp("R1", 100, 100, "F.Cu", 1, "SIG")
        + _fp("R2", 110, 100, "F.Cu", 1, "SIG")
        + '  (segment (start 100 100) (end 110 100) (width 0.25) (layer "B.Cu") (net 1) (uuid "s1"))\n'
        + f'  (via {via_kw}(at 100 100) (size 0.6) (drill 0.3) (layers "In1.Cu" "B.Cu") (net 1) (uuid "v1"))\n'
        + f'  (via {via_kw}(at 110 100) (size 0.6) (drill 0.3) (layers "In1.Cu" "B.Cu") (net 1) (uuid "v2"))\n'
        + ")\n"
    )


def board2(via_kw: str = "") -> str:
    """A SIG ``F.Cu``/``In1.Cu`` via with an OTHER-net B.Cu track through its centre.

    Keyword-less, KiCad drills the via through, so it shorts the B.Cu track.
    """
    return (
        _HEADER
        + _fp("R1", 100, 100, "F.Cu", 1, "SIG")
        + _fp("R2", 110, 100, "F.Cu", 1, "SIG")
        + _fp("R3", 105, 95, "B.Cu", 2, "OTHER")
        + _fp("R4", 105, 105, "B.Cu", 2, "OTHER")
        + '  (segment (start 100 100) (end 110 100) (width 0.25) (layer "F.Cu") (net 1) (uuid "s1"))\n'
        + '  (segment (start 105 95) (end 105 105) (width 0.25) (layer "B.Cu") (net 2) (uuid "s2"))\n'
        + f'  (via {via_kw}(at 105 100) (size 0.6) (drill 0.3) (layers "F.Cu" "In1.Cu") (net 1) (uuid "v1"))\n'
        + ")\n"
    )


def _write(tmp_path: Path, name: str, text: str) -> Path:
    path = tmp_path / f"{name}.kicad_pcb"
    path.write_text(text)
    return path


def _check(path: Path, capsys) -> list[dict]:
    from kicad_tools.cli import main

    capsys.readouterr()
    main(["check", str(path), "--format", "json"])
    return json.loads(capsys.readouterr().out)["violations"]


def _net_status(path: Path, capsys) -> dict[str, str]:
    from kicad_tools.cli import main

    capsys.readouterr()
    main(["net-status", str(path), "--format", "json"])
    return {n["net_name"]: n["status"] for n in json.loads(capsys.readouterr().out)["nets"]}


# ---------------------------------------------------------------------------
# Live kicad-cli (skipped when absent)
# ---------------------------------------------------------------------------


def _kicad_cli() -> Path | None:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli()


def _kicad_drc(path: Path, out_dir: Path) -> tuple[int, int]:
    """Return kicad-cli's ``(unconnected_items, shorting_items)`` counts."""
    out = out_dir / f"{path.stem}.drc.json"
    subprocess.run(
        [str(_kicad_cli()), "pcb", "drc", "--format", "json", "-o", str(out), str(path)],
        capture_output=True,
        check=False,
    )
    report = json.loads(out.read_text())
    shorts = sum(1 for v in report.get("violations", []) if v.get("type") == "shorting_items")
    return len(report.get("unconnected_items", [])), shorts


# (unconnected_items, shorting_items) measured with kicad-cli 10.0.6.
GOLDENS = {"board1": (0, 0), "board2": (0, 1)}
_BOARDS = {"board1": board1, "board2": board2}


@pytest.mark.skipif(_kicad_cli() is None, reason="kicad-cli not installed")
@pytest.mark.parametrize("name", sorted(GOLDENS))
def test_goldens_match_live_kicad_cli(name: str, tmp_path: Path):
    path = _write(tmp_path, name, _BOARDS[name]())
    assert _kicad_drc(path, tmp_path) == GOLDENS[name]


# ---------------------------------------------------------------------------
# kct check / kct net-status agree with the goldens
# ---------------------------------------------------------------------------


def test_board1_keywordless_inner_vias_connect_the_pads(tmp_path: Path, capsys):
    """Board 1: no false open, no dangling vias (kicad-cli: 0 unconnected)."""
    path = _write(tmp_path, "board1", board1())
    assert _net_status(path, capsys)["SIG"] == "complete"
    rules = {v["rule_id"] for v in _check(path, capsys)}
    assert "connectivity" not in rules
    assert "via_dangling" not in rules


def test_board1_with_blind_keyword_stays_open(tmp_path: Path, capsys):
    """Control: the same vias declared ``blind`` really do not reach F.Cu."""
    path = _write(tmp_path, "board1_blind", board1("blind "))
    assert _net_status(path, capsys)["SIG"] != "complete"


def test_board2_keywordless_via_shorts_the_b_cu_track(tmp_path: Path, capsys):
    """Board 2: the through barrel shorts OTHER on B.Cu (kicad-cli: shorting_items)."""
    path = _write(tmp_path, "board2", board2())
    shorts = [
        v
        for v in _check(path, capsys)
        if v["severity"] == "error" and {"SIG", "OTHER"} <= set(v.get("nets") or [])
    ]
    assert shorts, "expected the SIG via barrel to short the OTHER B.Cu track"
    assert any(v["message"].startswith("SHORT") for v in shorts)


def test_board2_with_blind_keyword_has_no_short(tmp_path: Path, capsys):
    """Control: a real blind F.Cu/In1.Cu via never reaches B.Cu."""
    path = _write(tmp_path, "board2_blind", board2("blind "))
    shorts = [
        v
        for v in _check(path, capsys)
        if v["severity"] == "error" and {"SIG", "OTHER"} <= set(v.get("nets") or [])
    ]
    assert not shorts


# ---------------------------------------------------------------------------
# The shared rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("layers", "via_type", "expected"),
    [
        (("In1.Cu", "B.Cu"), None, (("F.Cu", "B.Cu"), True)),
        (("F.Cu", "In1.Cu"), None, (("F.Cu", "B.Cu"), True)),
        (("In1.Cu", "In2.Cu"), None, (("F.Cu", "B.Cu"), True)),
        (("F.Cu", "B.Cu"), None, (("F.Cu", "B.Cu"), False)),
        (("B.Cu", "F.Cu"), None, (("B.Cu", "F.Cu"), False)),
        (("F.Cu", "In1.Cu"), "blind", (("F.Cu", "In1.Cu"), False)),
        (("In1.Cu", "In2.Cu"), "buried", (("In1.Cu", "In2.Cu"), False)),
        (("F.Cu", "In1.Cu"), "micro", (("F.Cu", "In1.Cu"), False)),
        (("In1.Cu", "In1.Cu"), None, (("F.Cu", "B.Cu"), True)),
        (("F.Cu",), None, (("F.Cu",), False)),
        ((), None, ((), False)),
        (("*.Cu", "B.Cu"), None, (("*.Cu", "B.Cu"), False)),
    ],
)
def test_kicad_read_via_layers(layers, via_type, expected):
    assert kicad_read_via_layers(layers, via_type) == expected


# ---------------------------------------------------------------------------
# Parser 1: schema ``Via.from_sexp`` and the PCB loader warning
# ---------------------------------------------------------------------------


def _schema_via(text: str) -> Via:
    from kicad_tools.sexp import parse_string

    return Via.from_sexp(parse_string(text))


def test_schema_keywordless_partial_via_reads_as_through():
    via = _schema_via('(via (at 1 2) (size 0.6) (drill 0.3) (layers "In1.Cu" "B.Cu") (net 1))')
    assert via.via_type is None
    assert via.layers == ["F.Cu", "B.Cu"]
    assert via.layers_read_as_through is True


@pytest.mark.parametrize(
    ("kw", "layers"),
    [
        ("blind", ["F.Cu", "In1.Cu"]),
        ("buried", ["In1.Cu", "In2.Cu"]),
        ("micro", ["F.Cu", "In1.Cu"]),
    ],
)
def test_schema_typed_via_keeps_declared_span(kw: str, layers: list[str]):
    pair = " ".join(f'"{name}"' for name in layers)
    via = _schema_via(f"(via {kw} (at 1 2) (size 0.6) (drill 0.3) (layers {pair}) (net 1))")
    assert via.via_type == kw
    assert via.layers == layers
    assert via.layers_read_as_through is False


def _normalised_warnings(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if "no blind/buried/micro keyword" in r.getMessage()]


def test_pcb_loader_logs_one_warning_with_the_count(tmp_path: Path, caplog):
    path = _write(tmp_path, "board1", board1())
    with caplog.at_level(logging.WARNING):
        pcb = PCB.load(path)
    assert [v.layers for v in pcb.vias] == [["F.Cu", "B.Cu"]] * 2
    records = _normalised_warnings(caplog)
    assert len(records) == 1
    assert ": 2 via(s)" in records[0].getMessage()


def test_pcb_loader_does_not_rewrite_the_source_node(tmp_path: Path):
    """Normalising the dataclass leaves untouched vias byte-identical on save."""
    path = _write(tmp_path, "board1", board1())
    pcb = PCB.load(path)
    out = tmp_path / "saved.kicad_pcb"
    pcb.save(out)
    assert out.read_text().count('(layers "In1.Cu" "B.Cu")') == 2


@pytest.mark.parametrize("via_kw", ["", "blind "])
def test_remove_vias_uuidless_fallback_removes_the_source_node(tmp_path: Path, via_kw: str):
    """The uuid-less (x, y, layers) fallback keys both sides on the read span.

    ``Via.layers`` is widened to F.Cu/B.Cu for a keyword-less partial via,
    while the raw node still says ``In1.Cu``/``B.Cu``; the node must still
    be removed so the via does not come back on save.
    """
    text = board1(via_kw).replace(' (uuid "v1")', "").replace(' (uuid "v2")', "")
    pcb = PCB.load(_write(tmp_path, "uuidless", text))
    assert all(not v.uuid for v in pcb.vias)
    assert pcb.remove_vias(pcb.vias[:]) == 2
    assert pcb.vias == []
    out = tmp_path / "saved.kicad_pcb"
    pcb.save(out)
    assert "(via " not in out.read_text()
    assert PCB.load(out).vias == []


@pytest.mark.parametrize("via_kw", ["", "blind "])
def test_no_warning_for_through_or_typed_vias(tmp_path: Path, caplog, via_kw: str):
    text = board1(via_kw)
    if not via_kw:
        text = text.replace('(layers "In1.Cu" "B.Cu")', '(layers "F.Cu" "B.Cu")')
    path = _write(tmp_path, "clean", text)
    with caplog.at_level(logging.WARNING):
        PCB.load(path)
    assert _normalised_warnings(caplog) == []


# ---------------------------------------------------------------------------
# Parser 2: ``router.optimizer.pcb.parse_vias``
# ---------------------------------------------------------------------------


def test_optimizer_keywordless_partial_via_reads_as_through(caplog):
    with caplog.at_level(logging.WARNING):
        vias = parse_vias(board1())["SIG"]
    assert [v.layers for v in vias] == [(Layer.F_CU, Layer.B_CU)] * 2
    records = _normalised_warnings(caplog)
    assert len(records) == 1
    assert ": 2 via(s)" in records[0].getMessage()


@pytest.mark.parametrize(
    ("kw", "pair", "expected"),
    [
        ("blind", '"F.Cu" "In1.Cu"', (Layer.F_CU, Layer.IN1_CU)),
        ("buried", '"In1.Cu" "In2.Cu"', (Layer.IN1_CU, Layer.IN2_CU)),
        ("micro", '"F.Cu" "In1.Cu"', (Layer.F_CU, Layer.IN1_CU)),
    ],
)
def test_optimizer_typed_via_keeps_declared_span(kw, pair, expected, caplog):
    text = _HEADER + f"  (via {kw} (at 1 2) (size 0.6) (drill 0.3) (layers {pair}) (net 1))\n)\n"
    with caplog.at_level(logging.WARNING):
        (via,) = parse_vias(text)["SIG"]
    assert via.layers == expected
    assert _normalised_warnings(caplog) == []


# ---------------------------------------------------------------------------
# Parser 3: ``router.io.verify_output_connectivity``
# ---------------------------------------------------------------------------


def _sig_pads():
    from kicad_tools.router.primitives import Pad

    return {
        1: [
            Pad(x=100, y=100, width=0.6, height=0.6, net=1, net_name="SIG", ref="R1", pin="1"),
            Pad(x=110, y=100, width=0.6, height=0.6, net=1, net_name="SIG", ref="R2", pin="1"),
        ]
    }


def test_router_io_keywordless_partial_via_reads_as_through(caplog):
    from kicad_tools.router.io import verify_output_connectivity

    with caplog.at_level(logging.WARNING):
        report = verify_output_connectivity(board1(), _sig_pads(), {1: "SIG"})
    assert report[1]["connected"] is True
    records = _normalised_warnings(caplog)
    assert len(records) == 1
    assert ": 2 via(s)" in records[0].getMessage()


@pytest.mark.parametrize("kw", ["blind", "buried", "micro"])
def test_router_io_typed_via_keeps_declared_span(kw: str, caplog):
    """A typed ``In1.Cu``/``B.Cu`` via never reaches the F.Cu pads."""
    from kicad_tools.router.io import verify_output_connectivity

    with caplog.at_level(logging.WARNING):
        report = verify_output_connectivity(board1(f"{kw} "), _sig_pads(), {1: "SIG"})
    assert report[1]["connected"] is False
    assert _normalised_warnings(caplog) == []
