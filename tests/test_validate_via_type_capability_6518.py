"""``kct check --mfr`` flags via types the fab cannot build (Issue #6518).

PR #6514 made the writers emit ``(via blind|buried|micro ...)``.  A profile
that has not sourced the matching capability must turn every such via into a
manufacturing error; a through via never triggers the rule.

The four-layer board follows ``tests/test_via_type_keyword_6485.py``, but its
typed vias sit off every pad centre so ``via_in_pad`` stays quiet.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.drc.violation import _TYPE_CATEGORY_MAP, ViolationCategory, ViolationType
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules import ViaTypeCapabilityRule
from kicad_tools.validate.rules.via_type_capability import VIA_TYPE_CAPABILITIES

RULE_ID = "via_type_unsupported"

_FOUR_LAYER_HEAD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal)
    (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
  (gr_rect (start 90 90) (end 120 110) (stroke (width 0.1) (type default)) (fill none)
    (layer "Edge.Cuts") (uuid "e1"))
  (footprint "R" (layer "F.Cu") (uuid "fp1") (at 95 100)
    (property "Reference" "R1" (at 0 0 0) (layer "F.SilkS") (uuid "r1"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "SIG")))
"""

#: via keyword -> its layer pair on the fixture
_SPANS = {
    "blind": ("F.Cu", "In1.Cu"),
    "buried": ("In1.Cu", "In2.Cu"),
    "micro": ("F.Cu", "In1.Cu"),
    None: ("F.Cu", "B.Cu"),
}


def _board_text(via_type: str | None, layers: tuple[str, str] | None = None) -> str:
    a, b = layers or _SPANS[via_type]
    keyword = f"{via_type} " if via_type else ""
    size, drill = (0.3, 0.1) if via_type == "micro" else (0.6, 0.3)
    return (
        _FOUR_LAYER_HEAD
        + f'  (via {keyword}(at 105 100) (size {size}) (drill {drill}) (layers "{a}" "{b}")'
        + ' (net 1) (uuid "v1"))\n'
        + ")\n"
    )


def _pcb(tmp_path: Path, via_type: str | None, **kw) -> PCB:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board_text(via_type, **kw))
    return PCB.load(str(path))


def _rules(**capabilities: bool) -> DesignRules:
    base = get_profile("jlcpcb").get_design_rules(layers=4)
    return dataclasses.replace(base, **capabilities)


def _findings(pcb: PCB, rules: DesignRules):
    return ViaTypeCapabilityRule().check(pcb, rules).filter_by_rule(RULE_ID)


@pytest.mark.parametrize("via_type", ["blind", "buried", "micro"])
def test_typed_via_on_jlcpcb_is_an_error(tmp_path, via_type):
    pcb = _pcb(tmp_path, via_type)
    assert pcb.vias[0].via_type == via_type
    found = _findings(pcb, _rules())
    assert len(found) == 1
    v = found[0]
    assert v.severity == "error"
    attr, label = VIA_TYPE_CAPABILITIES[via_type]
    a, b = _SPANS[via_type]
    assert attr in v.message and label in v.message
    assert f"{a}-{b}" in v.message and "'SIG'" in v.message
    assert v.nets == ("SIG",)
    assert v.items and v.items[0].startswith("Via@")


@pytest.mark.parametrize(
    ("via_type", "capability"),
    [
        ("blind", "blind_buried_vias_supported"),
        ("buried", "blind_buried_vias_supported"),
        ("micro", "micro_vias_supported"),
    ],
)
def test_capability_on_is_clean(tmp_path, via_type, capability):
    assert _findings(_pcb(tmp_path, via_type), _rules(**{capability: True})) == []


def test_capabilities_are_independent(tmp_path):
    """Blind/buried support does not license microvias, and vice versa."""
    assert _findings(_pcb(tmp_path, "micro"), _rules(blind_buried_vias_supported=True))
    assert _findings(_pcb(tmp_path, "blind"), _rules(micro_vias_supported=True))


def test_through_via_never_triggers(tmp_path):
    assert _findings(_pcb(tmp_path, None), _rules()) == []


def test_keywordless_partial_pair_is_a_through_via(tmp_path):
    """KiCad drills a keyword-less via through (Issue #6485): not our rule's business."""
    pcb = _pcb(tmp_path, None, layers=("F.Cu", "In1.Cu"))
    assert pcb.vias[0].via_type is None
    assert _findings(pcb, _rules()) == []


def test_default_is_fail_closed():
    rules = DesignRules(
        min_trace_width_mm=0.1,
        min_clearance_mm=0.1,
        min_via_drill_mm=0.2,
        min_via_diameter_mm=0.4,
        min_annular_ring_mm=0.1,
    )
    assert rules.blind_buried_vias_supported is False
    assert rules.micro_vias_supported is False
    d = rules.to_dict()
    assert d["blind_buried_vias_supported"] is False
    assert d["micro_vias_supported"] is False


#: (profile id, layer config, capability flag) triples that a shipped YAML sets
#: ``true``.  Each one carries a dated citation comment in its profile YAML
#: (Issue #6640); add a triple here only together with that citation.
_SOURCED_CAPABILITIES = {
    # pcbway.yaml: https://www.pcbway.com/hdi-pcb.html, verified 2026-10-14
    ("pcbway", "4layer_1oz", "blind_buried_vias_supported"),
    ("pcbway", "6layer_1oz", "blind_buried_vias_supported"),
}

_CAPABILITY_FLAGS = ("blind_buried_vias_supported", "micro_vias_supported")


def test_shipped_profiles_claim_no_unsourced_capability():
    """A shipped profile sets a via-type capability only where a source is cited.

    Every (profile, config, flag) outside ``_SOURCED_CAPABILITIES`` relies on
    the documented fail-closed ``False`` default.  Flipping one to ``true``
    must come with a cited, dated source in its YAML and an entry above.
    """
    from kicad_tools.manufacturers import list_manufacturers

    claimed = set()
    for profile in list_manufacturers():
        for config, rules in profile.design_rules.items():
            for flag in _CAPABILITY_FLAGS:
                if getattr(rules, flag):
                    claimed.add((profile.id, config, flag))
    assert claimed == _SOURCED_CAPABILITIES


def test_sourced_capability_yaml_carries_dated_citation():
    """Each sourced flag line in the YAML cites the issue and a verification date."""
    import re

    data_dir = Path(__file__).resolve().parents[1] / "src/kicad_tools/manufacturers/data"
    text = (data_dir / "pcbway.yaml").read_text()
    flag_lines = [
        line
        for line in text.splitlines()
        if re.match(r"\s+(blind_buried_vias_supported|micro_vias_supported):", line)
    ]
    assert len(flag_lines) == 2
    for line in flag_lines:
        assert ": true" in line
        assert "pcbway.com" in line or "hdi-pcb.html" in line, line
        assert re.search(r"verified \d{4}-\d{2}-\d{2}", line), line


def test_pcbway_two_layer_stays_fail_closed():
    """No HDI build fits two copper layers: the 2-layer configs keep ``False``."""
    profile = get_profile("pcbway")
    rules = profile.get_design_rules(layers=2)
    assert rules.blind_buried_vias_supported is False
    assert rules.micro_vias_supported is False


def test_pcbway_micro_vias_stay_fail_closed():
    """Micro vias are unchecked by dimensions.py, so no pcbway config enables them."""
    profile = get_profile("pcbway")
    for rules in profile.design_rules.values():
        assert rules.micro_vias_supported is False


class TestCheckerWiring:
    def test_check_all_methods_includes_rule(self):
        assert "check_via_type_capability" in DRCChecker.CHECK_ALL_METHODS

    def test_cli_category_registered(self):
        assert "via_type" in check_cmd.CHECK_CATEGORIES

    def test_rule_classified_manufacturing(self):
        assert DRCChecker.category_for_rule(RULE_ID) == DRCChecker.CATEGORY_MANUFACTURING

    def test_violation_type_alias(self):
        vt = ViolationType.from_string(RULE_ID)
        assert vt is ViolationType.VIA_TYPE_UNSUPPORTED
        assert _TYPE_CATEGORY_MAP[vt] is ViolationCategory.MANUFACTURING

    def test_checker_reports_absolute_location(self, tmp_path):
        checker = DRCChecker(_pcb(tmp_path, "blind"), manufacturer="jlcpcb", layers=4)
        found = checker.check_via_type_capability().filter_by_rule(RULE_ID)
        assert len(found) == 1
        assert found[0].location == (105.0, 100.0)


@pytest.mark.parametrize("via_type", ["blind", "buried", "micro"])
def test_cli_json_reports_error(tmp_path, capsys, via_type):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board_text(via_type))
    rc = check_cmd.main(
        [str(path), "--mfr", "jlcpcb", "--only", "via_type", "--drc-only", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)
    found = [v for v in data["violations"] if v["rule_id"] == RULE_ID]
    assert len(found) == 1
    assert found[0]["severity"] == "error"
    assert rc != 0


def test_cli_through_via_is_clean(tmp_path, capsys):
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board_text(None))
    check_cmd.main(
        [str(path), "--mfr", "jlcpcb", "--only", "via_type", "--drc-only", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)
    assert all(v["rule_id"] != RULE_ID for v in data["violations"])


@pytest.mark.parametrize("via_type", ["blind", "buried"])
def test_cli_pcbway_four_layer_is_clean_on_supported_typed_via(tmp_path, capsys, via_type):
    """Issue #6640: a shipped profile that sources the capability clears the via."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board_text(via_type))
    rc = check_cmd.main(
        [str(path), "--mfr", "pcbway", "--only", "via_type", "--drc-only", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)
    assert all(v["rule_id"] != RULE_ID for v in data["violations"])
    assert rc == 0


def test_cli_pcbway_micro_via_still_errors(tmp_path, capsys):
    """Micro vias stay rejected on pcbway: dimensions.py skips them (#3118)."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board_text("micro"))
    rc = check_cmd.main(
        [str(path), "--mfr", "pcbway", "--only", "via_type", "--drc-only", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)
    assert [v["rule_id"] for v in data["violations"]].count(RULE_ID) == 1
    assert rc != 0


@pytest.mark.parametrize("via_type", ["blind", "buried", "micro"])
def test_cli_unsourced_profile_still_errors_where_pcbway_is_clean(tmp_path, capsys, via_type):
    """Companion: the same board still fails on a profile with no cited source."""
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_board_text(via_type))
    rc = check_cmd.main(
        [str(path), "--mfr", "jlcpcb-tier1", "--only", "via_type", "--drc-only", "--format", "json"]
    )
    data = json.loads(capsys.readouterr().out)
    assert [v["rule_id"] for v in data["violations"]].count(RULE_ID) == 1
    assert rc != 0
