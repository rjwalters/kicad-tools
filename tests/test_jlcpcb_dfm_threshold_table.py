"""JLCPCB versioned DFM threshold table (Issue #8839)."""

from __future__ import annotations

from dataclasses import dataclass
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


@dataclass
class _Rules:
    inner_copper_oz: float = 0.5
    min_clearance_mm: float = 0.1
    min_smd_pad_clearance_mm: float = 0.1
    min_annular_ring_mm: float = 0.1
    min_pth_annular_ring_mm: float = 0.1
    min_solder_mask_dam_mm: float = 0.1


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
