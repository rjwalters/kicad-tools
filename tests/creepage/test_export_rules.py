"""Tests for referee-enforceable HV rule export (Issue #4508).

Covers the emitter (``kicad_tools.creepage.export_rules``) and the
``kct creepage-export-rules`` CLI:

* domain derivation groups nets by voltage magnitude;
* pairwise rules contain exactly the above-floor domain pairs with the
  ``build_required_by_domain_pair`` minima; no rules without a voltage map;
* domain-bridging footprints become ``insideCourtyard`` exclusions on their
  pair's rule (the #4506 attach-zone congruence);
* ``.kicad_pro`` netclass assignment is merge-preserving + idempotent;
* the ``.kicad_dru`` sentinel block is created / replaced-in-place / append-only,
  and re-emit is byte-idempotent;
* emitted rules parse back via the DRU importer (``mfr_dru``);
* the emitted board is clean under ``kicad-cli pcb drc`` when available
  (skipped otherwise), and an intentionally-close pair is flagged.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from kicad_tools.cli.commands.creepage_export_rules import run_creepage_export_rules_command
from kicad_tools.cli.mfr_dru import _extract_design_rules
from kicad_tools.cli.parser import create_parser
from kicad_tools.core.project_file import (
    add_netclass_definition,
    create_minimal_project,
    save_project,
)
from kicad_tools.creepage.export_rules import (
    DRU_BLOCK_BEGIN,
    DRU_BLOCK_END,
    NETCLASS_PREFIX,
    build_domains,
    build_export,
    build_rule_clauses,
    detect_bridging_footprints,
    domain_name,
    merge_dru_block,
    render_dru_block_body,
)
from kicad_tools.placement.hv_domains import build_required_by_domain_pair

# ---------------------------------------------------------------------------
# Board fixtures (minimal-but-real KiCad S-expression boards)
# ---------------------------------------------------------------------------

_HEADER = """\
(kicad_pcb
  (version 20240108)
  (generator "test_export_rules")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "AC_LINE")
  (net 2 "GND")
  (net 3 "3V3")
"""

_OUTLINE = """\
  (gr_line (start 100 100) (end 160 100) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 160 100) (end 160 130) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 160 130) (end 100 130) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 100 130) (end 100 100) (layer "Edge.Cuts") (width 0.1))
"""


def _footprint(ref: str, x: float, y: float, pads: list[tuple]) -> str:
    """A multi-pad SMD footprint with a real F.CrtYd courtyard.

    ``pads`` = (num, net_number, net_name, lx, ly); each pad is 1x1 mm.  A
    courtyard rectangle enclosing all pads + 0.5 mm margin is emitted so KiCad's
    ``insideCourtyard('<ref>')`` rule token resolves (the referee needs real
    courtyard geometry for the #4506 attach-zone exemption).
    """
    pad_lines = "\n".join(
        f'    (pad "{num}" smd rect (at {lx} {ly}) (size 1 1) (layers "F.Cu")\n'
        f'      (net {nn} "{name}"))'
        for num, nn, name, lx, ly in pads
    )
    xs = [lx for _n, _nn, _name, lx, _ly in pads]
    ys = [ly for _n, _nn, _name, _lx, ly in pads]
    cx0, cx1 = min(xs) - 1.0, max(xs) + 1.0
    cy0, cy1 = min(ys) - 1.0, max(ys) + 1.0
    crtyd = (
        f"    (fp_rect (start {cx0} {cy0}) (end {cx1} {cy1})\n"
        f'      (stroke (width 0.05) (type solid)) (fill none) (layer "F.CrtYd"))\n'
    )
    ref_prop = f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
    return (
        f'  (footprint "test:fp" (layer "F.Cu") (at {x} {y})\n{ref_prop}{crtyd}{pad_lines}\n  )\n'
    )


def _board_with_bridge() -> str:
    """Board: AC_LINE (150 V), GND (0 V), 3V3 (3.3 V) + a bridging footprint.

    ``R1`` holds an AC_LINE pad and a GND pad (a domain-bridging sense divider):
    it straddles the 150 V<->0 V domain pair and must become an insideCourtyard
    exclusion on that pair's rule.  ``P*`` place each net on its own footprint,
    far apart, so the standalone board is compliant.
    """
    parts = [_HEADER, _OUTLINE]
    parts.append(_footprint("P1", 110, 110, [("1", 1, "AC_LINE", 0, 0)]))
    parts.append(_footprint("P2", 150, 125, [("1", 2, "GND", 0, 0)]))
    parts.append(_footprint("P3", 110, 125, [("1", 3, "3V3", 0, 0)]))
    # Bridging footprint: AC_LINE + GND pads at 1.5 mm centre pitch (0.5 mm
    # copper gap) -- clears the 0.2 mm base floor but is far below the ~1.5 mm
    # 150 V creepage requirement, so ONLY the courtyard exemption keeps it clean.
    parts.append(_footprint("R1", 130, 115, [("1", 1, "AC_LINE", 0, 0), ("2", 2, "GND", 0, 1.5)]))
    parts.append(")\n")
    return "".join(parts)


def _voltage_map() -> dict:
    return {"AC_LINE": 150.0, "GND": 0.0, "3V3": 3.3}


def _write_project(tmp_path, name="board"):
    """Write a minimal .kicad_pro + sibling .kicad_pcb + voltage-map JSON."""
    pro = tmp_path / f"{name}.kicad_pro"
    save_project(create_minimal_project(f"{name}.kicad_pro"), pro)
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(_board_with_bridge())
    vmap = tmp_path / "voltages.json"
    vmap.write_text(json.dumps(_voltage_map()))
    return pro, pcb, vmap


def _run(argv):
    return run_creepage_export_rules_command(create_parser().parse_args(argv))


# ---------------------------------------------------------------------------
# Unit: domain derivation
# ---------------------------------------------------------------------------


def test_domain_name_sanitizes_decimal():
    assert domain_name(150) == "kct_150V"
    assert domain_name(3.3) == "kct_3p3V"
    assert domain_name(0) == "kct_0V"
    # Magnitude-only: a signed potential collapses to its magnitude domain.
    assert domain_name(-170) == "kct_170V"


def test_build_domains_groups_by_magnitude():
    net_domains, domain_voltages = build_domains(
        {"AC_LINE": 150, "GND": 0, "3V3": 3.3, "/AC_LINE2": 150},
        ["AC_LINE", "GND", "3V3", "AC_LINE2", "UNMAPPED"],
    )
    # UNMAPPED gets no domain; the two 150 V nets share one domain.
    assert net_domains == {
        "AC_LINE": "kct_150V",
        "GND": "kct_0V",
        "3V3": "kct_3p3V",
        "AC_LINE2": "kct_150V",
    }
    assert domain_voltages == {"kct_150V": 150.0, "kct_0V": 0.0, "kct_3p3V": 3.3}


# ---------------------------------------------------------------------------
# Unit: rule clause construction
# ---------------------------------------------------------------------------


def test_rules_only_above_floor_pairs_with_lookup_minima():
    domain_voltages = {"kct_150V": 150.0, "kct_0V": 0.0, "kct_3p3V": 3.3}
    expected = build_required_by_domain_pair(
        domain_voltages, standard_id="iec60664", pollution_degree=2, material_group="IIIa"
    )
    # The 0V<->3.3V pair (|dV| 3.3 < 30) must not appear at all.
    assert ("kct_0V", "kct_3p3V") not in expected

    clauses = build_rule_clauses(
        domain_voltages,
        {},
        standard_id="iec60664",
        pollution_degree=2,
        material_group="IIIa",
        hv_threshold=30.0,
        dru_floor_mm=0.2,
    )
    by_name = {c.name: c for c in clauses}
    # Exactly the two above-threshold pairs, each above the DRU floor.
    assert set(by_name) == {"kct_creepage_kct_0V_vs_kct_150V", "kct_creepage_kct_150V_vs_kct_3p3V"}
    for (a, b), req in expected.items():
        clause = by_name[f"kct_creepage_{a}_vs_{b}"]
        assert clause.min_mm == req
        assert req > 0.2


def test_no_rules_without_voltage_map():
    plan = build_export(None, ["AC_LINE", "GND"], [])
    assert plan.is_empty
    assert plan.rules == []
    assert plan.net_domains == {}


def test_high_dru_floor_suppresses_rules():
    # A floor above the 150 V requirement leaves nothing to add over board-wide.
    clauses = build_rule_clauses(
        {"kct_150V": 150.0, "kct_0V": 0.0},
        {},
        dru_floor_mm=100.0,
    )
    assert clauses == []


def test_out_of_table_voltage_fails_loud():
    from kicad_tools.creepage.standards import StandardLookupError

    with pytest.raises(StandardLookupError):
        build_rule_clauses({"kct_big": 100000.0, "kct_0V": 0.0}, {}, dru_floor_mm=0.2)


# ---------------------------------------------------------------------------
# Unit: bridging-footprint detection -> insideCourtyard exclusion
# ---------------------------------------------------------------------------


def test_detect_bridging_footprints():
    from kicad_tools.schema.pcb import PCB

    net_domains = {"AC_LINE": "kct_150V", "GND": "kct_0V", "3V3": "kct_3p3V"}
    pcb = _load_board(PCB, _board_with_bridge())
    bridging = detect_bridging_footprints(pcb.footprints, net_domains)
    # R1 bridges 150V<->0V (sorted: kct_0V, kct_150V).
    assert bridging == {("kct_0V", "kct_150V"): ["R1"]}


def test_bridging_footprint_becomes_condition_exclusion():
    clauses = build_rule_clauses(
        {"kct_150V": 150.0, "kct_0V": 0.0},
        {("kct_0V", "kct_150V"): ["R1", "R2"]},
        dru_floor_mm=0.2,
    )
    assert len(clauses) == 1
    cond = clauses[0].condition
    assert "A.NetClass == 'kct_0V' && B.NetClass == 'kct_150V'" in cond
    assert "!(A.insideCourtyard('R1') && B.insideCourtyard('R1'))" in cond
    assert "!(A.insideCourtyard('R2') && B.insideCourtyard('R2'))" in cond


# ---------------------------------------------------------------------------
# Unit: .kicad_pro netclass assignment (merge-preserving + idempotent)
# ---------------------------------------------------------------------------


def _sample_plan():
    from kicad_tools.schema.pcb import PCB

    pcb = _load_board(PCB, _board_with_bridge())
    return build_export(
        _voltage_map(),
        [n.name for n in pcb.nets.values() if n.number != 0 and n.name],
        pcb.footprints,
        dru_floor_mm=0.2,
    )


def test_apply_netclass_preserves_user_classes_and_is_idempotent():
    from kicad_tools.creepage.export_rules import apply_netclass_assignments

    data = create_minimal_project("x.kicad_pro")
    add_netclass_definition(data, "MyUserClass", clearance=0.3)

    plan = _sample_plan()
    apply_netclass_assignments(data, plan)
    classes_1 = [c["name"] for c in data["net_settings"]["classes"]]
    patterns_1 = list(data["net_settings"]["netclass_patterns"])

    # User + Default classes preserved; kct domains added.
    assert "MyUserClass" in classes_1
    assert "Default" in classes_1
    assert "kct_150V" in classes_1 and "kct_0V" in classes_1 and "kct_3p3V" in classes_1
    # Patterns map each mapped net to its domain.
    pat = {(p["netclass"], p["pattern"]) for p in patterns_1}
    assert ("kct_150V", "AC_LINE") in pat
    assert ("kct_0V", "GND") in pat
    assert ("kct_3p3V", "3V3") in pat

    # Idempotent: a second application yields identical classes/patterns (no dupes).
    apply_netclass_assignments(data, plan)
    assert [c["name"] for c in data["net_settings"]["classes"]] == classes_1
    assert data["net_settings"]["netclass_patterns"] == patterns_1


def test_apply_netclass_drops_stale_kct_domains():
    from kicad_tools.creepage.export_rules import apply_netclass_assignments

    data = create_minimal_project("x.kicad_pro")
    # A stale kct domain from a prior run that no longer applies.
    add_netclass_definition(data, f"{NETCLASS_PREFIX}999V", clearance=0.2)

    apply_netclass_assignments(data, _sample_plan())
    names = [c["name"] for c in data["net_settings"]["classes"]]
    assert f"{NETCLASS_PREFIX}999V" not in names


# ---------------------------------------------------------------------------
# Unit: .kicad_dru sentinel-block merge
# ---------------------------------------------------------------------------


def test_merge_dru_block_creates_file_with_version_header():
    out = merge_dru_block(None, '# body\n(rule "r" (constraint clearance (min 1mm)))')
    assert out.startswith("(version 1)")
    assert DRU_BLOCK_BEGIN in out and DRU_BLOCK_END in out


def test_merge_dru_block_replaces_in_place_and_preserves_handwritten():
    handwritten = (
        "(version 1)\n\n"
        '(rule "hand_rule" (constraint track_width (min 0.15mm)))\n\n'
        f"{DRU_BLOCK_BEGIN}\nOLD\n{DRU_BLOCK_END}\n"
    )
    out = merge_dru_block(handwritten, "NEW_BODY")
    assert "hand_rule" in out  # preserved
    assert "OLD" not in out
    assert "NEW_BODY" in out
    # Only one block after replace.
    assert out.count(DRU_BLOCK_BEGIN) == 1


def test_merge_dru_block_appends_when_absent_and_is_idempotent():
    handwritten = '(version 1)\n\n(rule "hand_rule" (constraint track_width (min 0.15mm)))\n'
    once = merge_dru_block(handwritten, "BODY")
    assert "hand_rule" in once
    assert once.count(DRU_BLOCK_BEGIN) == 1
    # Re-merging the produced content with the same body is byte-idempotent.
    twice = merge_dru_block(once, "BODY")
    assert twice == once


# ---------------------------------------------------------------------------
# Round-trip: emitted rules parse back via the DRU importer
# ---------------------------------------------------------------------------


def test_emitted_rules_parse_back(tmp_path):
    from kicad_tools.core.sexp_file import load_design_rules

    plan = _sample_plan()
    body = render_dru_block_body(plan)
    dru_text = merge_dru_block(None, body)
    dru_path = tmp_path / "parse.kicad_dru"
    dru_path.write_text(dru_text)
    sexp = load_design_rules(dru_path)
    parsed = _extract_design_rules(sexp)
    names = {r["name"] for r in parsed["rules"]}
    assert "kct_creepage_kct_0V_vs_kct_150V" in names
    assert "kct_creepage_kct_150V_vs_kct_3p3V" in names
    for r in parsed["rules"]:
        assert r["constraint"]["type"] == "clearance"
        assert r["constraint"]["min"]["unit"] == "mm"
        assert r["constraint"]["min"]["value"] > 0.2


# ---------------------------------------------------------------------------
# CLI end-to-end
# ---------------------------------------------------------------------------


def test_cli_writes_netclasses_and_rules(tmp_path, capsys):
    pro, _pcb, vmap = _write_project(tmp_path)
    rc = _run(["creepage-export-rules", str(pro), "--voltage-map", str(vmap)])
    assert rc == 0

    project = json.loads(pro.read_text())
    class_names = {c["name"] for c in project["net_settings"]["classes"]}
    assert {"kct_150V", "kct_0V", "kct_3p3V"} <= class_names

    dru = (tmp_path / "board.kicad_dru").read_text()
    assert DRU_BLOCK_BEGIN in dru
    assert "kct_creepage_kct_0V_vs_kct_150V" in dru
    # Bridging exemption for R1 present on the 150V<->0V rule.
    assert "insideCourtyard('R1')" in dru


def test_cli_no_voltage_map_is_noop(tmp_path, capsys):
    pro, _pcb, _vmap = _write_project(tmp_path)
    rc = _run(["creepage-export-rules", str(pro)])
    out = capsys.readouterr().out
    assert rc == 0
    assert "no-op" in out.lower() or "nothing to export" in out.lower()
    # Nothing written.
    assert not (tmp_path / "board.kicad_dru").exists()


def test_cli_dry_run_writes_nothing(tmp_path, capsys):
    pro, _pcb, vmap = _write_project(tmp_path)
    before = pro.read_text()
    rc = _run(["creepage-export-rules", str(pro), "--voltage-map", str(vmap), "--dry-run"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "dry run" in out.lower()
    assert not (tmp_path / "board.kicad_dru").exists()
    assert pro.read_text() == before  # project untouched


def test_cli_reemit_is_idempotent(tmp_path):
    pro, _pcb, vmap = _write_project(tmp_path)
    _run(["creepage-export-rules", str(pro), "--voltage-map", str(vmap)])
    pro_1 = pro.read_text()
    dru_1 = (tmp_path / "board.kicad_dru").read_text()
    _run(["creepage-export-rules", str(pro), "--voltage-map", str(vmap)])
    assert pro.read_text() == pro_1
    assert (tmp_path / "board.kicad_dru").read_text() == dru_1


def test_cli_missing_voltage_map_file_errors(tmp_path, capsys):
    pro, _pcb, _vmap = _write_project(tmp_path)
    rc = _run(["creepage-export-rules", str(pro), "--voltage-map", str(tmp_path / "nope.json")])
    assert rc == 1


# ---------------------------------------------------------------------------
# kicad-cli referee integration (skipped when kicad-cli is unavailable)
# ---------------------------------------------------------------------------


def _find_kicad_cli():
    try:
        from kicad_tools.export import find_kicad_cli

        return find_kicad_cli()
    except Exception:
        return shutil.which("kicad-cli")


@pytest.mark.skipif(_find_kicad_cli() is None, reason="kicad-cli not available")
def test_kicad_cli_drc_enforces_emitted_rules(tmp_path):
    """The compliant board is clean; a too-close HV<->LV pair is flagged."""
    kicad_cli = _find_kicad_cli()
    pro, pcb, vmap = _write_project(tmp_path)
    rc = _run(["creepage-export-rules", str(pro), "--voltage-map", str(vmap)])
    assert rc == 0

    def _drc_violation_count(pcb_path):
        report = tmp_path / "drc.json"
        subprocess.run(
            [
                str(kicad_cli),
                "pcb",
                "drc",
                "--format",
                "json",
                "--severity-error",
                "--exit-code-violations",
                "-o",
                str(report),
                str(pcb_path),
            ],
            capture_output=True,
        )
        data = json.loads(report.read_text())
        return len(data.get("violations", []))

    # Compliant board: the P1/P2/P3 nets sit far apart -> no creepage violation.
    assert _drc_violation_count(pcb) == 0

    # Now pull GND right next to AC_LINE on distinct footprints (board-fixable
    # approach, NOT inside R1's courtyard) -> the referee must flag it.
    close = _board_with_bridge().replace(
        '(footprint "test:fp" (layer "F.Cu") (at 150 125)',
        '(footprint "test:fp" (layer "F.Cu") (at 111 110)',
    )
    pcb.write_text(close)
    assert _drc_violation_count(pcb) >= 1


def _load_board(PCB, source):
    """Load a PCB from an in-memory S-expression source via a temp file."""
    import tempfile
    from pathlib import Path

    with tempfile.NamedTemporaryFile("w", suffix=".kicad_pcb", delete=False) as fh:
        fh.write(source)
        path = Path(fh.name)
    try:
        return PCB.load(path)
    finally:
        path.unlink(missing_ok=True)
