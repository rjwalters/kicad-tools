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
    # Omit the kwarg on a real run so the regression tests fail behaviourally,
    # not on a signature error, against the unfixed tree.
    if dry_run:
        pkg._write_drc_constraints(result, dry_run=True)
    else:
        pkg._write_drc_constraints(result)
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
    """``--dry-run`` raises the same message (writer label included), writes nothing.

    Forces the real loosening gate to report one loosening so the production
    code builds the writer label in both arms (#9430: it used to name the
    dry-run's scratch directory).
    """
    from kicad_tools.core import rule_loosening
    from kicad_tools.core.rule_loosening import Loosening

    pcb = _board(tmp_path, 2.0)
    _seed_stale_block(pcb, 0.15)
    monkeypatch.setattr(
        rule_loosening,
        "rule_loosenings",
        lambda *_a, **_k: [Loosening("dru.resolved.track_track", "lowered", 0.15, 0.1016)],
    )
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}

    with pytest.raises(RuleLooseningError) as dry:
        _export(pcb, dry_run=True)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before

    with pytest.raises(RuleLooseningError) as real:
        _export(pcb)
    assert str(pcb.with_suffix(".kicad_dru")) in str(real.value)
    assert "kct-export-dry-run-" not in str(dry.value)
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
    assert calls == [pcb.with_suffix(".kicad_dru")]
    assert not pcb.with_suffix(".kicad_dru").exists()


def test_dry_run_writes_nothing_when_merge_succeeds(tmp_path: Path) -> None:
    pcb = _board(tmp_path, 2.0)
    _seed_stale_block(pcb, 0.15)
    before = {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()}
    _export(pcb, dry_run=True)
    assert {p.name: p.read_bytes() for p in tmp_path.iterdir() if p.is_file()} == before


# ---------------------------------------------------------------------------
# Reporter's board shape (#9422 follow-up): hand-written unscoped rule beside
# the managed block.  The split-floor (2 oz) profile is emitted as
# ``outer``/``inner``-scoped rules only, so the loosening gate's unscoped
# probe used to see nothing but the hand-written rule.
# ---------------------------------------------------------------------------

_HAND_RULE = '(rule "Minimum Clearance"\n  (constraint clearance (min 0.127mm)))\n\n'
_ZONE = (
    '(zone (net 1) (net_name "VIN") (layer "{layer}") (connect_pads (clearance 0.2))'
    " (polygon (pts (xy 1 1) (xy 9 1) (xy 9 9))))\n"
)


def _reporter_board(tmp_path: Path, zone_layer: str | None) -> Path:
    pcb = _board(tmp_path, 2.0)
    if zone_layer is not None:
        text = pcb.read_text(encoding="utf-8").rstrip()
        assert text.endswith(")")
        pcb.write_text(text[:-1] + _ZONE.format(layer=zone_layer) + ")\n", encoding="utf-8")
    _seed_stale_block(pcb, 0.15)
    dru = pcb.with_suffix(".kicad_dru")
    body = dru.read_text(encoding="utf-8").replace("(version 1)\n", "", 1)
    dru.write_text("(version 1)\n" + _HAND_RULE + body, encoding="utf-8")
    return pcb


@pytest.mark.parametrize("zone_layer", [None, "In2.Cu", "F.Cu"])
def test_hand_written_unscoped_rule_does_not_block_a_two_oz_export(
    tmp_path: Path, zone_layer: str | None
) -> None:
    pcb = _reporter_board(tmp_path, zone_layer)

    _export(pcb)  # used to raise: dru.resolved.track_track lowered 0.15 -> 0.127

    dru = pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8")
    assert re.search(r'rule "Clearance \(outer\) - jlcpcb".*?\(min 0\.15mm\)', dru, re.S)
    assert '(rule "Minimum Clearance"' in dru  # authored rule kept


# ---------------------------------------------------------------------------
# Router-declared net classes (``meta.kct_router_netclasses``, #9422 review):
# kct's own "Reviewed clearance" rule for such a class carries an
# ``isCoupledDiffPair()`` call and an ``A.Reference == B.Reference`` term.
# Unmodelled, the rule dropped out of the gate and the inner-layer probe
# landed on ``Clearance (inner)`` 0.1016 against the 0.15 net-class floor.
# ---------------------------------------------------------------------------


def _router_class_board(tmp_path: Path, zone_layer: str | None, *, class_name: str) -> Path:
    pcb = _reporter_board(tmp_path, zone_layer)
    dru = pcb.with_suffix(".kicad_dru")
    # Drop the hand rule: the reviewer's shapes S7/S8 carry none.
    dru.write_text(dru.read_text(encoding="utf-8").replace(_HAND_RULE, ""), encoding="utf-8")
    pro = pcb.with_suffix(".kicad_pro")
    data = json.loads(pro.read_text(encoding="utf-8"))
    classes = data["net_settings"]["classes"]
    declared = dict(classes[0], name=class_name, clearance=0.15)
    classes.append(declared)
    data.setdefault("meta", {})["kct_router_netclasses"] = {
        "classes": [class_name],
        "assignments": [],
    }
    pro.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return pcb


@pytest.mark.parametrize("zone_layer", [None, "In1.Cu"])
@pytest.mark.parametrize("dry_run", [False, True])
def test_router_declared_class_does_not_block_a_two_oz_export(
    tmp_path: Path, zone_layer: str | None, dry_run: bool
) -> None:
    pcb = _router_class_board(tmp_path, zone_layer, class_name="Signal")

    _export(pcb, dry_run=dry_run)  # used to raise: track_track lowered 0.15 -> 0.1016

    if not dry_run:
        dru = pcb.with_suffix(".kicad_dru").read_text(encoding="utf-8")
        assert "isCoupledDiffPair" in dru  # the router-class rule really was written
        assert _managed_clearance(pcb) == pytest.approx(0.15)
