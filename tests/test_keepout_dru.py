"""Explicit ``.kicad_dru`` keepout rules per rule area (Issue #6039).

``kicad-cli pcb drc`` 10.0.1 does not enforce keepout rule areas on its own,
so kct emits one ``A.intersectsArea('<uuid>')`` disallow rule per area.  The
pure-Python half pins rendering, flag mapping and managed-block merging; the
KiCad-gated half proves the emitted sidecar actually makes native DRC fire.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.manufacturers.dru_generator import DRU_FLOORS_BLOCK_BEGIN
from kicad_tools.manufacturers.keepout_dru import (
    DRU_KEEPOUT_BLOCK_BEGIN,
    DRU_KEEPOUT_BLOCK_END,
    KeepoutDruRule,
    keepout_rules_for_board,
    keepout_rules_from_zones,
    merge_keepout_block,
)
from kicad_tools.schema.pcb import Zone, ZoneKeepout

WALL_UUID = "cccccccc-0000-0000-0000-000000000001"
SQUARE = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]


def _zone(
    *,
    uuid: str = WALL_UUID,
    name: str = "wall",
    layers: list[str] | None = None,
    **flags: bool,
) -> Zone:
    layers = ["F.Cu", "B.Cu"] if layers is None else layers
    return Zone(
        net_number=0,
        net_name="",
        layer=layers[0] if layers else "",
        uuid=uuid,
        name=name,
        layers=list(layers),
        keepout=ZoneKeepout(**flags),
        polygon=list(SQUARE),
    )


# ---------------------------------------------------------------------------
# Rule derivation
# ---------------------------------------------------------------------------


def test_flags_map_to_exact_disallow_kinds():
    zones = [
        _zone(tracks_allowed=False, vias_allowed=False),
        _zone(uuid="u-pads", pads_allowed=False),
        _zone(uuid="u-pour", copperpour_allowed=False),
        _zone(uuid="u-fp", footprints_allowed=False),
        _zone(
            uuid="u-all",
            tracks_allowed=False,
            vias_allowed=False,
            pads_allowed=False,
            copperpour_allowed=False,
            footprints_allowed=False,
        ),
    ]
    assert [r.disallow for r in keepout_rules_from_zones(zones)] == [
        ("track", "via"),
        ("pad",),
        ("zone",),
        ("footprint",),
        ("track", "via", "pad", "zone", "footprint"),
    ]


def test_area_forbidding_nothing_and_plain_pours_emit_no_rule():
    permissive = _zone()  # every flag allowed
    pour = Zone(net_number=1, net_name="GND", layer="B.Cu", uuid="p", polygon=list(SQUARE))
    degenerate = _zone(tracks_allowed=False)
    degenerate.polygon = SQUARE[:2]
    assert keepout_rules_from_zones([permissive, pour, degenerate]) == []


def test_rule_keys_on_uuid_and_falls_back_to_name():
    named = _zone(tracks_allowed=False)
    unnamed = _zone(uuid="dddd-1", name="", tracks_allowed=False)
    no_uuid = _zone(uuid="", name="legacy", tracks_allowed=False)
    neither = _zone(uuid="", name="", tracks_allowed=False)
    rules = keepout_rules_from_zones([named, unnamed, no_uuid, neither])
    assert [(r.reference, r.name) for r in rules] == [
        (WALL_UUID, f"kct keepout wall [{WALL_UUID}]"),
        ("dddd-1", "kct keepout dddd-1"),
        ("legacy", "kct keepout legacy"),
    ]


def test_single_copper_layer_gets_layer_clause_multi_and_wildcards_do_not():
    rules = keepout_rules_from_zones(
        [
            _zone(uuid="a", layers=["F.Cu"], tracks_allowed=False),
            _zone(uuid="b", layers=["In2.Cu"], tracks_allowed=False),
            _zone(uuid="c", layers=["F.Cu", "B.Cu"], tracks_allowed=False),
            _zone(uuid="d", layers=["*.Cu"], tracks_allowed=False),
            _zone(uuid="e", layers=["F&B.Cu"], tracks_allowed=False),
        ]
    )
    assert [r.layer for r in rules] == ["F.Cu", "In2.Cu", None, None, None]
    assert '(layer "F.Cu")' in rules[0].render()
    assert "(layer" not in rules[2].render()


def test_render_escapes_quotes_and_backslashes():
    rule = KeepoutDruRule(
        name='kct keepout it\'s "q"', reference='it\'s "q"\\x', disallow=("track",)
    )
    assert rule.render() == (
        '(rule "kct keepout it\'s \\"q\\""\n'
        '  (condition "A.intersectsArea(\'it\\\\\'s \\"q\\"\\\\\\\\x\')")\n'
        "  (constraint disallow track))"
    )


def test_render_keeps_non_ascii_names_literal():
    rule = keepout_rules_from_zones([_zone(uuid="", name="Zone µ", tracks_allowed=False)])[0]
    assert "Zone µ" in rule.render()


# ---------------------------------------------------------------------------
# Managed-block merge
# ---------------------------------------------------------------------------

USER = '(version 1)\n\n(rule "mine"\n  (constraint clearance (min 0.3mm)))\n'


def _rules(*uuids: str) -> list[KeepoutDruRule]:
    return keepout_rules_from_zones([_zone(uuid=u, tracks_allowed=False) for u in uuids])


def test_merge_is_idempotent_and_preserves_user_rules():
    once = merge_keepout_block(USER, _rules("a"))
    assert once is not None
    assert once.startswith(USER.rstrip("\n"))
    assert merge_keepout_block(once, _rules("a")) == once
    assert once.count(DRU_KEEPOUT_BLOCK_BEGIN) == 1


def test_merge_replaces_rather_than_duplicates_changed_rules():
    first = merge_keepout_block(USER, _rules("a", "b"))
    second = merge_keepout_block(first, _rules("c"))
    assert second is not None
    assert second.count(DRU_KEEPOUT_BLOCK_BEGIN) == 1
    assert "'c'" in second and "'a'" not in second and "'b'" not in second
    assert '(rule "mine"' in second


def test_merge_without_rules_removes_stale_block_and_is_noop_otherwise():
    assert merge_keepout_block(USER, []) == USER
    assert merge_keepout_block(None, []) is None
    with_block = merge_keepout_block(USER, _rules("a"))
    assert merge_keepout_block(with_block, []) == USER


def test_merge_into_empty_and_headerless_content():
    fresh = merge_keepout_block(None, _rules("a"))
    assert fresh is not None and fresh.startswith("(version 1)\n\n" + DRU_KEEPOUT_BLOCK_BEGIN)
    assert fresh.rstrip().endswith(DRU_KEEPOUT_BLOCK_END)
    headerless = merge_keepout_block('(rule "x" (constraint track_width (min 1mm)))\n', _rules("a"))
    assert headerless is not None and headerless.startswith("(version 1)\n")


# ---------------------------------------------------------------------------
# Writer integration (no KiCad needed)
# ---------------------------------------------------------------------------


def _board(
    path: Path,
    *,
    keepout: str | None = "(tracks not_allowed) (vias not_allowed)",
    zone_layers: str = '"F.Cu" "B.Cu"',
    track_layer: str = "F.Cu",
    x0: float = 113.0,
    x1: float = 117.0,
    extra: str = "",
) -> Path:
    """The PR #6034 judge's crossing board: one /SIG track through a wall."""
    zone = ""
    if keepout is not None:
        zone = f"""  (zone (net 0) (net_name "") (name "wall") (layers {zone_layers})
    (uuid "{WALL_UUID}") (hatch edge 0.5)
    (keepout {keepout})
    (polygon (pts (xy {x0} 99.0) (xy {x1} 99.0) (xy {x1} 117.0) (xy {x0} 117.0))))
"""
    fps = "".join(
        f"""  (footprint "R" (layer "F.Cu") (uuid "00000000-0000-0000-0000-00000000001{i}")
    (at {x} 108.0)
    (property "Reference" "R{i}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "/SIG")))
"""
        for i, x in ((1, 104.0), (2, 126.0))
    )
    path.write_text(
        f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "/SIG")
  (net 2 "GND")
  (gr_rect (start 100.0 100.0) (end 130.0 116.0)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{fps}{zone}  (segment (start 104 108) (end 126 108) (width 0.25) (layer "{track_layer}") (net 1)
    (uuid "dddddddd-0000-0000-0000-000000000001"))
{extra})
""",
        encoding="utf-8",
    )
    return path


def _emit(path: Path) -> str:
    rules = get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1)
    write_drc_constraints(path, rules, manufacturer_id="jlcpcb", layers=2)
    return path.with_suffix(".kicad_dru").read_text(encoding="utf-8")


def test_write_drc_constraints_emits_block_and_is_idempotent(tmp_path):
    board = _board(tmp_path / "cross.kicad_pcb")
    first = _emit(board)
    assert first.count(DRU_FLOORS_BLOCK_BEGIN) == 1
    assert first.count(DRU_KEEPOUT_BLOCK_BEGIN) == 1
    assert f"A.intersectsArea('{WALL_UUID}')" in first
    assert "(constraint disallow track via))" in first
    assert _emit(board) == first


def test_write_drc_constraints_preserves_user_rules(tmp_path):
    board = _board(tmp_path / "cross.kicad_pcb")
    board.with_suffix(".kicad_dru").write_text(USER, encoding="utf-8")
    first = _emit(board)
    assert first.startswith(USER.rstrip("\n"))
    assert _emit(board) == first


def test_keepout_free_board_gets_no_block(tmp_path):
    board = _board(tmp_path / "clean.kicad_pcb", keepout=None)
    assert keepout_rules_for_board(board) == []
    assert DRU_KEEPOUT_BLOCK_BEGIN not in _emit(board)


def test_removing_the_last_keepout_drops_the_block(tmp_path):
    board = _board(tmp_path / "cross.kicad_pcb")
    assert DRU_KEEPOUT_BLOCK_BEGIN in _emit(board)
    _board(board, keepout=None)
    assert DRU_KEEPOUT_BLOCK_BEGIN not in _emit(board)


def test_renamed_destination_reads_keepouts_from_destination_board(tmp_path):
    (tmp_path / "src").mkdir()
    source = _board(tmp_path / "src" / "in.kicad_pcb", keepout=None)
    dest = _board(tmp_path / "out.kicad_pcb")
    rules = get_profile("jlcpcb").get_design_rules(layers=2, copper_oz=1)
    write_drc_constraints(dest, rules, manufacturer_id="jlcpcb", layers=2, source_pcb_path=source)
    assert f"A.intersectsArea('{WALL_UUID}')" in dest.with_suffix(".kicad_dru").read_text()


# ---------------------------------------------------------------------------
# KiCad-gated: the emitted sidecar makes native DRC fire
# ---------------------------------------------------------------------------


def _native_not_allowed(board: Path) -> list[dict]:
    cli = find_kicad_cli()
    if cli is None:
        pytest.skip("Native KiCad CLI is not installed")
    report = board.with_suffix(".drc.json")
    subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--severity-all",
            "--format",
            "json",
            "-o",
            str(report),
            str(board),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    return [v for v in data["violations"] if v["type"] == "items_not_allowed"]


def _kct_rule_hits(findings: list[dict]) -> list[dict]:
    return [v for v in findings if "kct keepout" in v["description"]]


def test_native_drc_flags_track_through_keepout_after_emit(tmp_path):
    board = _board(tmp_path / "cross.kicad_pcb")
    _emit(board)
    hits = _kct_rule_hits(_native_not_allowed(board))
    assert len(hits) >= 1
    assert any("Track [/SIG] on F.Cu" in i["description"] for v in hits for i in v["items"])


def test_native_drc_baseline_without_emitted_rule(tmp_path):
    """Pins the KiCad gap this issue works around: no rule, no finding.

    Only asserted on 10.0.1 (the measured version); a KiCad that restores
    the implicit keepout rule is allowed to report it on its own.
    """
    board = _board(tmp_path / "cross.kicad_pcb")
    findings = _native_not_allowed(board)
    from kicad_tools.manufacturers.project_generator import _installed_kicad_cli_version

    _installed_kicad_cli_version.cache_clear()
    version = _installed_kicad_cli_version() or ""
    if not version.startswith("10.0.1"):
        pytest.skip(f"baseline only pinned on kicad-cli 10.0.1 (have {version!r})")
    assert findings == []


def test_native_drc_layer_scoped_area_ignores_other_layer(tmp_path):
    board = _board(tmp_path / "fwall.kicad_pcb", zone_layers='"F.Cu"', track_layer="B.Cu")
    dru = _emit(board)
    assert '(layer "F.Cu")' in dru
    assert _kct_rule_hits(_native_not_allowed(board)) == []


def test_native_drc_clean_board_reports_nothing_new(tmp_path):
    board = _board(tmp_path / "clean.kicad_pcb", x0=101.0, x1=103.0)
    assert DRU_KEEPOUT_BLOCK_BEGIN in _emit(board)
    assert _native_not_allowed(board) == []


_CARVED_GND = """  (zone (net 2) (net_name "GND") (layer "B.Cu") (uuid "ffffffff-0000-0000-0000-000000000001")
    (hatch edge 0.5) (connect_pads (clearance 0.2)) (min_thickness 0.2)
    (filled_areas_thickness no) (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
    (polygon (pts (xy 101 101) (xy 129 101) (xy 129 115) (xy 101 115)))
    (filled_polygon (layer "B.Cu") (pts {pts})){more})
"""


def test_native_drc_pour_only_area_is_quiet_on_carved_fill_and_fires_on_real_copper(tmp_path):
    """``disallow zone`` judges the saved fill, not the pour outline.

    A pour outline overlapping a ``(copperpour not_allowed)`` area whose
    saved fill was carved around it (KiCad's own fill result) is clean;
    fill copper actually inside the area is a real violation.
    """
    pour_only = "(tracks allowed) (vias allowed) (pads allowed) (copperpour not_allowed)"
    carved = _CARVED_GND.format(
        pts="(xy 101 101) (xy 112.5 101) (xy 112.5 115) (xy 101 115)",
        more='\n    (filled_polygon (layer "B.Cu") (pts (xy 117.5 101) (xy 129 101) (xy 129 115) (xy 117.5 115)))',
    )
    board = _board(
        tmp_path / "carved.kicad_pcb",
        keepout=pour_only,
        zone_layers='"B.Cu"',
        extra=carved,
    )
    assert "(constraint disallow zone))" in _emit(board)
    assert _kct_rule_hits(_native_not_allowed(board)) == []

    solid = _CARVED_GND.format(pts="(xy 101 101) (xy 129 101) (xy 129 115) (xy 101 115)", more="")
    bad = _board(tmp_path / "solid.kicad_pcb", keepout=pour_only, zone_layers='"B.Cu"', extra=solid)
    _emit(bad)
    assert len(_kct_rule_hits(_native_not_allowed(bad))) == 1
