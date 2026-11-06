"""Tests for ``kct pcb add-board-info`` (issue #8533)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("shapely")

from kicad_tools.cli.pcb_add_board_info import resolve_fields, run_add_board_info  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.validate.rules.silkscreen import (  # noqa: E402
    check_silk_edge_clearance,
    check_silk_over_copper,
    check_silk_overlap,
)

TITLE = '(title_block (title "Widget") (date "2026-11-06") (rev "B"))'


def _pcb(origin=(100.0, 100.0), size=(40.0, 30.0), title=TITLE, extra="") -> str:
    ox, oy = origin
    w, h = size
    return f"""(kicad_pcb (version 20240108) (generator "pcbnew")
  (general (thickness 1.6))
  (paper "A4")
  {title}
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user)
          (44 "Edge.Cuts" user))
  (gr_rect (start {ox} {oy}) (end {ox + w} {oy + h}) (layer "Edge.Cuts")
    (stroke (width 0.1) (type default)) (fill none))
  (footprint "TestFP" (layer "F.Cu") (at {ox + 3} {oy + h - 3})
    (fp_text reference "R1" (at 0 -2) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -1 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask"))
    (pad "2" smd rect (at 1 0) (size 1.5 1.5) (layers "F.Cu" "F.Paste" "F.Mask")))
  {extra}
)
"""


def _write(tmp_path: Path, **kw) -> Path:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(_pcb(**kw))
    return p


def _run(pcb: Path, **kw) -> int:
    return run_add_board_info(pcb, **kw)


def test_field_fill(tmp_path):
    p = _write(tmp_path)
    assert _run(p, auto=True) == 0
    texts = [t.text for t in PCB.load(p).texts]
    assert "Widget B 2026-11-06" in texts


def test_missing_field_error_names_it(tmp_path, capsys):
    p = _write(tmp_path, title='(title_block (title "Widget"))')
    assert _run(p, auto=True) == 1
    err = capsys.readouterr().err
    assert "{rev}" in err
    assert "gr_text" not in p.read_text()


def test_unknown_field_error(tmp_path):
    pcb = PCB.load(_write(tmp_path))
    from kicad_tools.cli.pcb_add_board_info import BoardInfoError

    with pytest.raises(BoardInfoError, match="foo"):
        resolve_fields("{foo}", pcb, tmp_path / "b.kicad_pcb")


def test_dry_run_does_not_write(tmp_path):
    p = _write(tmp_path)
    before = p.read_text()
    assert _run(p, auto=True, dry_run=True) == 0
    assert p.read_text() == before


def test_auto_placement_is_drc_clean(tmp_path):
    p = _write(tmp_path)
    assert _run(p, auto=True, order_placeholder="JLCJLCJLCJLC") == 0
    pcb = PCB.load(p)
    assert {t.text for t in pcb.texts} >= {"Widget B 2026-11-06", "JLCJLCJLCJLC"}
    from kicad_tools.manufacturers import get_profile

    rules = get_profile("jlcpcb").get_design_rules(2)
    for check in (check_silk_edge_clearance, check_silk_over_copper, check_silk_overlap):
        res = check(pcb, rules)
        assert not res.violations, [v.message for v in res.violations]


def test_back_layer_is_mirrored(tmp_path):
    p = _write(tmp_path)
    assert _run(p, auto=True, layer="B.SilkS") == 0
    src = p.read_text()
    assert '"B.SilkS"' in src and "mirror" in src


def test_explicit_at_uses_file_coordinates(tmp_path):
    p = _write(tmp_path)
    assert _run(p, at=(120.0, 115.0)) == 0
    assert "(at 120 115)" in p.read_text()


def test_crowded_board_fails_clearly(tmp_path, capsys):
    p = _write(tmp_path, size=(6.0, 6.0))
    assert _run(p, auto=True) == 1
    assert "no free silkscreen region" in capsys.readouterr().err


def test_requires_at_or_auto(tmp_path):
    assert _run(_write(tmp_path)) == 1


def test_json_output(tmp_path, capsys):
    p = _write(tmp_path)
    assert _run(p, auto=True, dry_run=True, output_format="json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["items"][0]["layer"] == "F.SilkS"


def _logo(x: float, y: float) -> str:
    return (
        f'(gr_text "LOGO TEXT HERE" (at {x} {y}) (layer "F.SilkS") '
        "(effects (font (size 1 1) (thickness 0.15))))"
    )


_LOGO = _logo(110, 128.9)  # file coords for a board whose origin is (100, 100)


def test_auto_avoids_board_level_silk_on_nonzero_origin(tmp_path):
    p = _write(tmp_path, extra=_LOGO)
    assert _run(p, auto=True) == 0
    pcb = PCB.load(p)
    from kicad_tools.manufacturers import get_profile

    rules = get_profile("jlcpcb").get_design_rules(2)
    res = check_silk_overlap(pcb, rules)
    assert not res.violations, [v.message for v in res.violations]


@pytest.mark.parametrize("kw", [{"auto": True}, {"at": (120.0, 115.0)}])
def test_rerun_is_idempotent(tmp_path, kw):
    p = _write(tmp_path)
    for _ in range(3):
        assert _run(p, **kw) == 0
    stamps = [t for t in PCB.load(p).texts if t.text == "Widget B 2026-11-06"]
    assert len(stamps) == 1


def test_explicit_at_warns_when_not_clear(tmp_path, capsys):
    p = _write(tmp_path)
    assert _run(p, at=(103.0, 127.0), output_format="json") == 0
    data = json.loads(capsys.readouterr().out)
    assert data["warnings"]


def _kicad_cli_silk_findings(pcb: Path) -> list[str]:
    import subprocess

    out = pcb.with_suffix(".drc.json")
    cfg = pcb.parent / "kicad-cfg"
    cfg.mkdir(exist_ok=True)
    subprocess.run(
        ["kicad-cli", "pcb", "drc", "--format", "json", "--severity-all", "-o", str(out), str(pcb)],
        check=True,
        capture_output=True,
        timeout=120,
        env={**__import__("os").environ, "KICAD_CONFIG_HOME": str(cfg)},
    )
    data = json.loads(out.read_text())
    return [
        f"{v['type']}: {v['description']}"
        for v in data.get("violations", [])
        if v["type"].startswith("silk_")
    ]


@pytest.mark.skipif(
    __import__("shutil").which("kicad-cli") is None, reason="kicad-cli not installed"
)
@pytest.mark.parametrize(
    "origin,layer",
    [((100.0, 100.0), "F.SilkS"), ((0.0, 0.0), "F.SilkS"), ((100.0, 100.0), "B.SilkS")],
)
def test_auto_output_is_clean_under_kicad_cli(tmp_path, origin, layer):
    """AC5: --auto output has 0 silk findings from kicad-cli pcb drc (#8533)."""
    # Keep LOGO on the 40x30 board for either origin (10, 28.9 board-relative).
    logo = _logo(origin[0] + 10, origin[1] + 28.9)
    p = _write(tmp_path, origin=origin, extra=logo)
    assert _kicad_cli_silk_findings(p) == []
    assert _run(p, auto=True, order_placeholder="JLCJLCJLCJLC", layer=layer) == 0
    assert _kicad_cli_silk_findings(p) == []
