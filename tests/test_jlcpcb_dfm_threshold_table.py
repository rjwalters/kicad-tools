"""JLCPCB versioned DFM threshold table (Issue #8839)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli.check_cmd import _apply_dfm_table
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.base import DFM_THRESHOLD_NAMES


@pytest.fixture(scope="module")
def table():
    t = get_profile("jlcpcb").dfm_thresholds
    assert t is not None
    return t


def test_table_has_version(table):
    assert table.version >= 1


def test_every_threshold_has_provenance(table):
    for entry in table.entries:
        assert set(entry.thresholds) == set(DFM_THRESHOLD_NAMES)
        for name, th in entry.thresholds.items():
            assert th.source_url.startswith("http"), (entry.key, name)
            assert len(th.retrieved) == 10 and th.retrieved[4] == "-", (entry.key, name)
            assert th.basis in {"capability_page", "observed_jlcdfm"}


def test_every_design_rule_stackup_is_covered(table):
    p = get_profile("jlcpcb")
    for key, rules in p.design_rules.items():
        layers = int(key.split("layer_")[0])
        assert p.get_dfm_entry(layers, rules.outer_copper_oz, rules.inner_copper_oz), key


def test_pth_ring_varies_by_copper_weight():
    p = get_profile("jlcpcb")
    one = p.get_dfm_entry(2, 1.0, 0.0).thresholds["annular_ring_pth"].danger_mm
    two = p.get_dfm_entry(4, 2.0, 0.5).thresholds["annular_ring_pth"].danger_mm
    assert one == pytest.approx(0.18)
    assert two == pytest.approx(0.254)


def test_observed_2oz_trace_spacing_recorded():
    ts = get_profile("jlcpcb").get_dfm_entry(4, 2.0, 0.5).thresholds["trace_spacing"]
    assert (ts.danger_mm, ts.warning_mm) == (0.10, 0.15)
    assert ts.basis == "observed_jlcdfm"


@pytest.mark.parametrize("stackup", [(4, 3.0, 0.5), (2, 3.0, 0.0), (4, 2.0, 1.0)])
def test_missing_stackup_has_no_fallback(stackup):
    assert get_profile("jlcpcb").get_dfm_entry(*stackup) is None


def _checker(n_layers=4):
    return SimpleNamespace(
        pcb=SimpleNamespace(copper_layers=list(range(n_layers))),
        design_rules=get_profile("jlcpcb").get_design_rules(n_layers, 2.0),
    )


def test_apply_table_missing_stackup_is_incomplete(capsys):
    cw: dict = {}
    assert _apply_dfm_table(_checker(), "jlcpcb", 4, 3.0, 0.5, 1.0, cw) is True
    assert cw["dfm_table"]["entry"] is None
    assert "INCOMPLETE" in capsys.readouterr().err


def test_apply_table_2oz_floors():
    cw: dict = {}
    c = _checker()
    assert _apply_dfm_table(c, "jlcpcb", 4, 2.0, 0.5, 1.0, cw) is False
    assert c.design_rules.min_pth_annular_ring_mm == pytest.approx(0.254)
    assert c.design_rules.min_solder_mask_dam_mm == pytest.approx(0.20)
    assert cw["dfm_table"]["entry"] == "4layer_2oz"


def test_apply_table_none_profile_fields_use_table_floor():
    """Optional profile fields left as None must not crash max() (#8839)."""
    from dataclasses import replace

    c = _checker()
    c.design_rules = replace(
        c.design_rules, min_smd_pad_clearance_mm=None, min_pth_annular_ring_mm=None
    )
    assert _apply_dfm_table(c, "jlcpcb", 4, 2.0, 0.5, 1.0, {}) is False
    assert c.design_rules.min_pth_annular_ring_mm == pytest.approx(0.254)
    assert c.design_rules.min_smd_pad_clearance_mm is not None


def test_verdict_incomplete_when_dfm_missing(tmp_path):
    from kicad_tools.cli.check_cmd import evaluate_check_verdict

    results = SimpleNamespace(violations=[], mask_copper_assessments=[], rules_checked=1)
    v = evaluate_check_verdict(
        tmp_path / "x.kicad_pcb", results, drc_only=True, dfm_incomplete=True
    )
    assert v.verdict == "incomplete" and v.exit_code != 0
    ok = evaluate_check_verdict(
        tmp_path / "x.kicad_pcb", results, drc_only=True, dfm_incomplete=True, allow_incomplete=True
    )
    assert ok.verdict == "incomplete" and ok.exit_code == 0
    clean = evaluate_check_verdict(tmp_path / "x.kicad_pcb", results, drc_only=True)
    assert clean.verdict == "pass"


_REPO = Path(__file__).resolve().parent.parent
_BOARD_4L = _REPO / "boards/03-usb-joystick/output/usb_joystick_routed.kicad_pcb"


@pytest.mark.parametrize("inner", [0.5, 0.434286, 0.45])
def test_lookup_snaps_stock_inner_copper(inner):
    """KiCad's stock 0.0152 mm inner copper is the fab's 0.5 oz (review item 1)."""
    assert get_profile("jlcpcb").get_dfm_entry(4, 1.0, inner).key == "4layer_1oz"


@pytest.mark.parametrize("stackup", [(4, 2.5, 0.5), (4, 1.0, 0.25), (6, 1.0, 2.0)])
def test_lookup_snap_does_not_reach_unoffered_weights(stackup):
    assert get_profile("jlcpcb").get_dfm_entry(*stackup) is None


def test_real_4layer_board_is_not_incomplete(capsys):
    """Regression: a committed 4L board with a stock stackup must not go incomplete."""
    from kicad_tools.cli.check_cmd import main

    main([str(_BOARD_4L), "--mfr", "jlcpcb", "--format", "json"])
    out = capsys.readouterr().out
    data = json.loads(out[out.index("{") :])
    assert data.get("verdict") != "incomplete"


def test_real_board_3oz_is_incomplete_and_allow_incomplete(capsys):
    from kicad_tools.cli.check_cmd import main

    args = [str(_BOARD_4L), "--mfr", "jlcpcb", "--copper", "3"]
    assert main(args) != 0
    assert main([*args, "--allow-incomplete"]) == 0


def test_table_floors_not_looser_than_profile_outer_floor():
    """Table danger floors may only tighten the profile's outer floors."""
    p = get_profile("jlcpcb")
    for key, rules in p.design_rules.items():
        layers = int(key.split("layer_")[0])
        e = p.get_dfm_entry(layers, rules.outer_copper_oz, rules.inner_copper_oz)
        assert e.thresholds["annular_ring_pth"].danger_mm >= (rules.min_pth_annular_ring_mm or 0)
        assert e.thresholds["mask_bridge"].danger_mm >= rules.min_solder_mask_dam_mm - 1e-9


def test_observed_warning_basis_recorded():
    e = get_profile("jlcpcb").get_dfm_entry(4, 2.0, 0.5)
    assert e.thresholds["annular_ring_via"].warning_basis == "observed_jlcdfm"
    assert e.thresholds["pad_spacing"].warning_mm == pytest.approx(0.13)


# Bump ``version`` in jlcpcb.yaml AND this digest together when any number changes.
_PINNED = (1, "40c90941f294a40e")


def _digest(table) -> str:
    rows = [
        (
            e.key,
            e.layers,
            e.outer_oz,
            e.inner_oz,
            n,
            t.danger_mm,
            t.warning_mm,
            t.basis,
            t.warning_basis,
            t.source_url,
            t.retrieved,
        )
        for e in table.entries
        for n, t in sorted(e.thresholds.items())
    ]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()[:16]


def test_version_pinned_to_content_digest(table):
    assert (table.version, _digest(table)) == _PINNED, (
        f"dfm_thresholds changed: bump version and set _PINNED = ({table.version + 1}, "
        f"{_digest(table)!r})"
    )
