"""#9422: ``kct export`` must write the copper-weight-aware clearance floor.

A 2 oz board whose project already sits at the 2 oz floor (0.15 mm) used to be
refused with ``RuleLooseningError`` because export selected the 1 oz preset
(0.1016 mm) regardless of the declared stackup, and ``--dry-run`` never
reached the DRU merge at all.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.core.rule_loosening import RuleLooseningError
from kicad_tools.export.manufacturing import (
    ManufacturingConfig,
    ManufacturingPackage,
    ManufacturingResult,
)
from kicad_tools.manufacturers import get_profile, write_drc_constraints

_OUTLINE = (
    '(gr_line (start 0 0) (end 50 0) (layer "Edge.Cuts") (width 0.1))\n'
    '(gr_line (start 50 0) (end 50 50) (layer "Edge.Cuts") (width 0.1))\n'
    '(gr_line (start 50 50) (end 0 50) (layer "Edge.Cuts") (width 0.1))\n'
    '(gr_line (start 0 50) (end 0 0) (layer "Edge.Cuts") (width 0.1))\n'
)
_OZ_MM = {1.0: 0.035, 2.0: 0.07}


def _board(tmp_path: Path, outer_oz: float | None) -> Path:
    """Four-layer board; ``outer_oz`` None leaves the stackup undeclared."""
    stackup = ""
    if outer_oz is not None:
        cu = lambda n, t: f'(layer "{n}" (type "copper") (thickness {t}))'  # noqa: E731
        stackup = (
            "(setup (stackup "
            f"{cu('F.Cu', _OZ_MM[outer_oz])} {cu('In1.Cu', 0.0152)} "
            f"{cu('In2.Cu', 0.0152)} {cu('B.Cu', _OZ_MM[outer_oz])}))"
        )
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text(
        '(kicad_pcb (version 20231014) (generator "pcbnew")\n'
        '  (general (thickness 1.6))\n  (paper "A4")\n'
        '  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)'
        ' (31 "B.Cu" signal) (44 "Edge.Cuts" user))\n'
        f'  {stackup}\n  (net 0 "") (net 1 "VIN")\n{_OUTLINE})\n'
    )
    return pcb


def _seed_stale_block(pcb: Path, project_clearance: float) -> None:
    """Write kct's 1 oz sidecars, then raise the project clearance above them."""
    rules = get_profile("jlcpcb").get_design_rules(layers=4, copper_oz=1.0)
    write_drc_constraints(pcb, rules, manufacturer_id="jlcpcb", layers=4, copper_oz=1.0)
    pro = pcb.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8"))
    for cls in data["net_settings"]["classes"]:
        cls["clearance"] = project_clearance
    pro.write_text(json.dumps(data, indent=2), encoding="utf-8")


def _managed_clearance(pcb: Path) -> float:
    dru = pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    block = dru.split("# BEGIN kct fab floors", 1)[1].split("# END kct fab floors", 1)[0]
    m = re.search(r"constraint clearance \(min ([0-9.]+)mm\)", block)
    assert m, block
    return float(m.group(1))


def _export(pcb: Path, *, dry_run: bool = False) -> ManufacturingResult:
    pkg = ManufacturingPackage(
        pcb, manufacturer="jlcpcb", config=ManufacturingConfig(output_dir=pcb.parent / "out")
    )
    result = ManufacturingResult(output_dir=pcb.parent / "out")
    pkg._write_drc_constraints(result, dry_run=dry_run)
    return result


def test_detect_layer_config_follows_declared_stackup(tmp_path: Path) -> None:
    assert ManufacturingPackage(_board(tmp_path, 2.0))._detect_layer_config() == (4, 2.0)
    assert ManufacturingPackage(_board(tmp_path, 1.0))._detect_layer_config() == (4, 1.0)
    assert ManufacturingPackage(_board(tmp_path, None))._detect_layer_config() == (4, 1.0)


def test_two_oz_board_replaces_stale_one_oz_block(tmp_path: Path) -> None:
    pcb = _board(tmp_path, 2.0)
    _seed_stale_block(pcb, 0.15)
    assert _managed_clearance(pcb) == pytest.approx(0.1016)

    _export(pcb)  # must not raise RuleLooseningError

    assert _managed_clearance(pcb) == pytest.approx(0.15)


def test_one_oz_board_keeps_one_oz_floor(tmp_path: Path) -> None:
    pcb = _board(tmp_path, 1.0)
    _seed_stale_block(pcb, 0.15)
    _export(pcb)
    assert _managed_clearance(pcb) == pytest.approx(0.1016)


def test_dry_run_surfaces_the_refusal_the_real_run_hits(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``--dry-run`` reaches the DRU merge, raises the same error, writes nothing."""
    from kicad_tools.core.rule_loosening import Loosening
    from kicad_tools.manufacturers import dru_generator

    def _refuse(*_args, **_kwargs):
        raise RuleLooseningError([Loosening("dru.resolved.track_track", "lowered", 0.15, 0.1016)])

    pcb = _board(tmp_path, 2.0)
    _seed_stale_block(pcb, 0.15)
    monkeypatch.setattr(dru_generator, "merge_dru_floors", _refuse)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}

    with pytest.raises(RuleLooseningError) as dry:
        _export(pcb, dry_run=True)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before

    with pytest.raises(RuleLooseningError) as real:
        _export(pcb)
    assert str(dry.value) == str(real.value)


def test_dry_run_package_export_reaches_the_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The public ``export(dry_run=True)`` path runs the merge, not just ``_write_drc_constraints``."""
    from kicad_tools.manufacturers import dru_generator

    calls: list[object] = []
    real_merge = dru_generator.merge_dru_floors

    def _spy(*args, **kwargs):
        calls.append(kwargs.get("path"))
        return real_merge(*args, **kwargs)

    monkeypatch.setattr(dru_generator, "merge_dru_floors", _spy)
    pcb = _board(tmp_path, 2.0)
    ManufacturingPackage(
        pcb, manufacturer="jlcpcb", config=ManufacturingConfig(output_dir=tmp_path / "out")
    ).export(dry_run=True)
    assert len(calls) == 1
    assert not pcb.with_suffix(".kicad_dru").exists()


def test_dry_run_writes_nothing_when_merge_succeeds(tmp_path: Path) -> None:
    pcb = _board(tmp_path, 2.0)
    _seed_stale_block(pcb, 0.15)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    _export(pcb, dry_run=True)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before
