"""Rule-file rewrites may only tighten authored rules (Issue #7385).

Three ``.kicad_pro`` writers used to assign the manufacturer profile over an
existing project unconditionally -- ``kct mfr apply-rules <x.kicad_pro>``,
``kicad-fab-rules apply`` and ``kct init`` on an existing project -- bypassing
the #6191 stricter-wins policy ``merge_project_rules`` applies.  On the
issue's reproduction (authored ``min_clearance 0.2``, ``Default`` clearance
0.25 / ``diff_pair_gap`` 0.12, jlcpcb 2-layer profile) they wrote
``min_clearance 0.127``, ``Default`` clearance 0.127 and reset the gap to
0.25.

These tests pin:

* the shared :func:`rule_loosenings` check, category by category;
* each writer, on the issue's reproduction (all failed on main bd3c039ac);
* ``add_netclass_definition`` merging into an existing class;
* the ``KCT_PRESERVE_BOARD_RULES=0`` escape hatch still loosening on request;
* the writers never downgrading an authored severity (they did not touch
  ``rule_severities`` before #7385), and every policy-permitted relaxation
  being reported as ``relaxed_by_policy`` -- never silent;
* the refusal path: a merge that would loosen refuses, writes nothing and
  names the items plus the ``KCT_PRESERVE_BOARD_RULES=0`` hint;
* a guard over every committed ``boards/**/*.kicad_pro``: no writer's
  regeneration loosens a board's rules beyond the documented allowances.
"""

from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

import kicad_tools.manufacturers.project_generator as project_generator
from kicad_tools.cli.fab_rules_cmd import main as fab_rules_main
from kicad_tools.cli.init_cmd import init_project
from kicad_tools.cli.mfr import main as mfr_main
from kicad_tools.core.netclass_templates import apply_design_template
from kicad_tools.core.project_file import add_netclass_definition, get_netclass_definitions
from kicad_tools.core.rule_loosening import (
    LOOSENING_CHANGED,
    LOOSENING_EXCLUSION_ADDED,
    LOOSENING_LOWERED,
    LOOSENING_REMOVED,
    LOOSENING_SEVERITY_DOWNGRADED,
    RuleLooseningError,
    assert_no_loosenings,
    rule_loosenings,
)
from kicad_tools.manufacturers import get_manufacturer_ids, get_profile
from kicad_tools.manufacturers.project_generator import (
    merge_policy_allowances,
    merge_project_rules,
    merge_project_rules_guarded,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# jlcpcb 2-layer 1oz: min clearance / trace 0.127 mm.
PROFILE = "jlcpcb"


def _authored_project() -> dict:
    """The issue's reproduction: stricter-than-profile authored rules."""
    return {
        "meta": {"filename": "board.kicad_pro", "version": 1},
        "board": {
            "design_settings": {
                "rules": {"min_clearance": 0.2, "min_track_width": 0.2},
                "defaults": {"clearance_min": 0.2, "track_min_width": 0.2},
                "rule_severities": {
                    "isolated_copper": "error",
                    "lib_footprint_mismatch": "error",
                    "clearance": "error",
                },
                "drc_exclusions": [],
            }
        },
        "net_settings": {
            "classes": [
                {
                    "name": "Default",
                    "clearance": 0.25,
                    "track_width": 0.2,
                    "via_diameter": 0.6,
                    "via_drill": 0.3,
                    "diff_pair_gap": 0.12,
                    "diff_pair_width": 0.15,
                    "bus_width": 12,
                },
                {"name": "Power", "clearance": 0.3, "track_width": 0.5},
            ],
            "netclass_patterns": [{"netclass": "Power", "pattern": "VCC*"}],
            "meta": {"version": 3},
        },
        "text_variables": {},
    }


def _default(data: dict) -> dict:
    return next(c for c in data["net_settings"]["classes"] if c["name"] == "Default")


def _assert_authored_rules_kept(before: dict, after: dict) -> None:
    rules = after["board"]["design_settings"]["rules"]
    assert rules["min_clearance"] == pytest.approx(0.2)
    assert rules["min_track_width"] == pytest.approx(0.2)
    defaults = after["board"]["design_settings"]["defaults"]
    assert defaults["clearance_min"] == pytest.approx(0.2)
    default_cls = _default(after)
    assert default_cls["clearance"] == pytest.approx(0.25)
    assert default_cls["diff_pair_gap"] == pytest.approx(0.12)
    assert default_cls["diff_pair_width"] == pytest.approx(0.15)
    # The writers never downgrade an authored severity (#7385 review).
    assert (
        after["board"]["design_settings"]["rule_severities"]
        == (before["board"]["design_settings"]["rule_severities"])
    )
    assert rule_loosenings(before, after) == []


def _write(path: Path, data: dict) -> Path:
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# rule_loosenings()
# ---------------------------------------------------------------------------


class TestRuleLoosenings:
    def test_identical_is_empty(self):
        data = _authored_project()
        assert rule_loosenings(data, copy.deepcopy(data)) == []

    def test_tightening_and_additions_are_not_loosenings(self):
        before = _authored_project()
        after = copy.deepcopy(before)
        after["board"]["design_settings"]["rules"]["min_clearance"] = 0.3
        after["board"]["design_settings"]["rules"]["min_hole_to_hole"] = 0.1
        after["board"]["design_settings"]["rule_severities"]["new_rule"] = "ignore"
        after["net_settings"]["classes"].append({"name": "HS", "clearance": 0.05})
        after["net_settings"]["netclass_patterns"].append({"netclass": "HS", "pattern": "D*"})
        assert rule_loosenings(before, after) == []

    def test_lowered_minimum(self):
        before = _authored_project()
        after = copy.deepcopy(before)
        after["board"]["design_settings"]["rules"]["min_clearance"] = 0.127
        after["board"]["design_settings"]["defaults"]["clearance_min"] = 0.127
        _default(after)["clearance"] = 0.127
        found = {(x.path, x.kind) for x in rule_loosenings(before, after)}
        assert found == {
            ("rules.min_clearance", LOOSENING_LOWERED),
            ("defaults.clearance_min", LOOSENING_LOWERED),
            ("netclasses.Default.clearance", LOOSENING_LOWERED),
        }

    def test_severity_downgrade_and_removal(self):
        before = _authored_project()
        after = copy.deepcopy(before)
        sev = after["board"]["design_settings"]["rule_severities"]
        sev["isolated_copper"] = "warning"
        del sev["clearance"]
        found = {(x.path, x.kind) for x in rule_loosenings(before, after)}
        assert found == {
            ("rule_severities.isolated_copper", LOOSENING_SEVERITY_DOWNGRADED),
            ("rule_severities.clearance", LOOSENING_REMOVED),
        }

    def test_new_drc_exclusion(self):
        before = _authored_project()
        after = copy.deepcopy(before)
        after["board"]["design_settings"]["drc_exclusions"].append("clearance|1|2|3|4")
        [item] = rule_loosenings(before, after)
        assert item.kind == LOOSENING_EXCLUSION_ADDED

    def test_dropped_netclass_key_pattern_and_class(self):
        before = _authored_project()
        after = copy.deepcopy(before)
        del _default(after)["bus_width"]
        _default(after)["diff_pair_gap"] = 0.25
        after["net_settings"]["classes"] = [_default(after)]
        after["net_settings"]["netclass_patterns"] = []
        found = {(x.path, x.kind) for x in rule_loosenings(before, after)}
        assert found == {
            ("netclasses.Default.bus_width", LOOSENING_REMOVED),
            ("netclasses.Default.diff_pair_gap", LOOSENING_CHANGED),
            ("netclasses.Power", LOOSENING_REMOVED),
            ("netclass_patterns.Power:VCC*", LOOSENING_REMOVED),
        }

    def test_netclass_assignment_removed_or_changed(self):
        before = {"net_settings": {"netclass_assignments": {"GND": "Power", "SDA": "I2C"}}}
        after = {"net_settings": {"netclass_assignments": {"GND": "Default"}}}
        found = {(x.path, x.kind) for x in rule_loosenings(before, after)}
        assert found == {
            ("netclass_assignments.GND", LOOSENING_CHANGED),
            ("netclass_assignments.SDA", LOOSENING_REMOVED),
        }

    def test_allow_patterns(self):
        before = _authored_project()
        after = copy.deepcopy(before)
        after["board"]["design_settings"]["rule_severities"]["isolated_copper"] = "warning"
        after["board"]["design_settings"]["rules"]["min_clearance"] = 0.1
        assert [x.path for x in rule_loosenings(before, after, allow=["rule_severities.*"])] == [
            "rules.min_clearance"
        ]
        assert rule_loosenings(before, after, allow=["*"]) == []

    def test_assert_no_loosenings_message_lists_items(self):
        before = _authored_project()
        after = copy.deepcopy(before)
        after["board"]["design_settings"]["rules"]["min_clearance"] = 0.127
        with pytest.raises(RuleLooseningError) as exc:
            assert_no_loosenings(before, after, writer="test-writer")
        message = str(exc.value)
        assert "test-writer" in message
        assert "rules.min_clearance: lowered 0.2 -> 0.127" in message
        assert "KCT_PRESERVE_BOARD_RULES=0" in message


class TestMergePolicyAllowances:
    def test_default_mode_allows_documented_severities_only(self):
        allow = merge_policy_allowances(_authored_project())
        assert allow == frozenset(
            {"rule_severities.isolated_copper", "rule_severities.lib_footprint_mismatch"}
        )

    def test_writer_allowances_exclude_severities(self):
        assert merge_policy_allowances(_authored_project(), apply_severities=False) == frozenset()

    def test_template_default_netclass_is_relaxable(self):
        data = _authored_project()
        _default(data).update(clearance=0.2, track_width=0.2, via_diameter=0.6, via_drill=0.3)
        assert "netclasses.Default.clearance" in merge_policy_allowances(data)

    def test_opt_out_allows_everything(self):
        data = _authored_project()
        data["text_variables"]["KCT_PRESERVE_BOARD_RULES"] = "0"
        assert merge_policy_allowances(data) == frozenset({"*"})

    def test_full_mode_allows_nothing(self):
        data = _authored_project()
        data["text_variables"]["KCT_PRESERVE_BOARD_RULES"] = "1"
        assert merge_policy_allowances(data) == frozenset()

    def test_guarded_merge_reports_kept_values(self):
        data = _authored_project()
        before = copy.deepcopy(data)
        rules = get_profile(PROFILE).get_design_rules(2, 1.0)
        report = merge_project_rules_guarded(data, rules, writer="test")
        _assert_authored_rules_kept(before, data)
        kept = {item.path for item in report.kept_stricter}
        assert {"rules.min_clearance", "netclasses.Default.clearance"} <= kept
        assert any("kept 0.2 (profile 0.127)" in line for line in report.messages())
        assert report.relaxed_by_policy == []
        assert report.to_dict()["relaxed_by_policy"] == []

    def test_route_path_still_applies_non_blocking_severities(self):
        # merge_project_rules' default (kct route / --emit-drc-constraints)
        # keeps the #6191 downgrade; only the CLI writers opt out.
        data = merge_project_rules(_authored_project(), get_profile(PROFILE).get_design_rules(2))
        severities = data["board"]["design_settings"]["rule_severities"]
        assert severities["isolated_copper"] == "warning"
        assert severities["lib_footprint_mismatch"] == "ignore"

    def test_guarded_merge_reports_template_default_relaxation(self):
        data = _authored_project()
        _default(data).update(clearance=0.2, track_width=0.2, via_diameter=0.6, via_drill=0.3)
        rules = get_profile(PROFILE).get_design_rules(2, 1.0)
        report = merge_project_rules_guarded(data, rules, writer="test")
        relaxed = {item.path for item in report.relaxed_by_policy}
        assert "netclasses.Default.clearance" in relaxed
        assert not any(path.startswith("rule_severities.") for path in relaxed)
        text = "\n".join(report.messages())
        assert "Relaxed" in text and "KCT_PRESERVE_BOARD_RULES=1" in text
        assert "netclasses.Default.clearance: lowered 0.2 -> 0.127" in text

    def test_guarded_merge_reports_opt_out_relaxation(self):
        data = _authored_project()
        data["text_variables"]["KCT_PRESERVE_BOARD_RULES"] = "0"
        rules = get_profile(PROFILE).get_design_rules(2, 1.0)
        report = merge_project_rules_guarded(data, rules, writer="test")
        relaxed = {item.path for item in report.relaxed_by_policy}
        assert "rules.min_clearance" in relaxed
        assert report.relaxed_by_policy == report.loosened_by_opt_in
        # Severities are untouched even under =0: these writers never set them.
        assert data["board"]["design_settings"]["rule_severities"]["isolated_copper"] == "error"
        assert {item["path"] for item in report.to_dict()["relaxed_by_policy"]} == relaxed


# ---------------------------------------------------------------------------
# add_netclass_definition / apply_design_template merge
# ---------------------------------------------------------------------------


class TestNetclassMerge:
    def test_add_netclass_definition_keeps_authored_keys(self):
        data = _authored_project()
        add_netclass_definition(
            data, "Default", track_width=0.127, clearance=0.127, via_diameter=0.45, via_drill=0.2
        )
        default_cls = _default(data)
        assert default_cls["clearance"] == pytest.approx(0.25)
        assert default_cls["track_width"] == pytest.approx(0.2)
        assert default_cls["via_diameter"] == pytest.approx(0.6)
        assert default_cls["diff_pair_gap"] == pytest.approx(0.12)
        assert default_cls["diff_pair_width"] == pytest.approx(0.15)
        assert default_cls["bus_width"] == 12

    def test_add_netclass_definition_tightens(self):
        data = _authored_project()
        add_netclass_definition(data, "Default", clearance=0.4)
        assert _default(data)["clearance"] == pytest.approx(0.4)
        assert _default(data)["diff_pair_gap"] == pytest.approx(0.12)

    def test_add_netclass_definition_explicit_overwrite(self):
        data = _authored_project()
        add_netclass_definition(data, "Default", clearance=0.127, keep_stricter=False)
        assert _default(data)["clearance"] == pytest.approx(0.127)

    def test_design_template_keeps_authored_default_and_classes(self):
        data = _authored_project()
        before = copy.deepcopy(data)
        apply_design_template(data, "power_supply")
        assert rule_loosenings(before, data) == []
        assert _default(data)["clearance"] == pytest.approx(0.25)

    def test_design_template_still_sets_fresh_default(self):
        data: dict = {}
        get_netclass_definitions(data)  # template-signature Default
        apply_design_template(data, "audio")
        assert _default(data)["track_width"] == pytest.approx(0.2)
        assert _default(data)["clearance"] == pytest.approx(0.15)


# ---------------------------------------------------------------------------
# The three writers, on the issue's reproduction
# ---------------------------------------------------------------------------


class TestWritersKeepStricterRules:
    def test_mfr_apply_rules_kicad_pro(self, tmp_path, capsys):
        before = _authored_project()
        pro = _write(tmp_path / "board.kicad_pro", before)
        mfr_main(["apply-rules", str(pro), PROFILE])
        _assert_authored_rules_kept(before, _read(pro))
        assert "Kept" in capsys.readouterr().out

    def test_mfr_apply_rules_kicad_pro_json(self, tmp_path, capsys):
        before = _authored_project()
        pro = _write(tmp_path / "board.kicad_pro", before)
        mfr_main(["apply-rules", str(pro), PROFILE, "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        kept = {item["path"] for item in payload["rule_merge"]["kept_stricter"]}
        assert "rules.min_clearance" in kept

    def test_fab_rules_apply(self, tmp_path):
        before = _authored_project()
        pro = _write(tmp_path / "board.kicad_pro", before)
        assert fab_rules_main(["apply", PROFILE, str(pro)]) == 0
        _assert_authored_rules_kept(before, _read(pro))

    @pytest.mark.parametrize("design_type", [None, "digital"])
    def test_init_existing_project(self, tmp_path, design_type):
        before = _authored_project()
        pro = _write(tmp_path / "board.kicad_pro", before)
        assert init_project(str(pro), PROFILE, design_type=design_type) == 0
        _assert_authored_rules_kept(before, _read(pro))

    def test_mfr_json_reports_policy_relaxation(self, tmp_path, capsys):
        before = _authored_project()
        _default(before).update(clearance=0.2, track_width=0.2, via_diameter=0.6, via_drill=0.3)
        pro = _write(tmp_path / "board.kicad_pro", before)
        mfr_main(["apply-rules", str(pro), PROFILE, "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        relaxed = {item["path"] for item in payload["rule_merge"]["relaxed_by_policy"]}
        assert "netclasses.Default.clearance" in relaxed
        severities = _read(pro)["board"]["design_settings"]["rule_severities"]
        assert severities["isolated_copper"] == "error"
        assert severities["lib_footprint_mismatch"] == "error"

    def test_opt_out_still_overwrites(self, tmp_path, capsys):
        before = _authored_project()
        before["text_variables"]["KCT_PRESERVE_BOARD_RULES"] = "0"
        pro = _write(tmp_path / "board.kicad_pro", before)
        mfr_main(["apply-rules", str(pro), PROFILE])
        out = capsys.readouterr().out
        assert "because the project sets KCT_PRESERVE_BOARD_RULES=0" in out
        assert "rules.min_clearance: lowered 0.2 -> 0.127" in out
        after = _read(pro)
        assert after["board"]["design_settings"]["rules"]["min_clearance"] == pytest.approx(0.127)
        assert _default(after)["clearance"] == pytest.approx(0.127)
        # Keys merge_project_rules does not own are never touched, even then.
        assert _default(after)["diff_pair_gap"] == pytest.approx(0.12)

    def test_mfr_header_names_project_annular_floor(self, tmp_path, capsys):
        pro = _write(tmp_path / "board.kicad_pro", _authored_project())
        mfr_main(["apply-rules", str(pro), PROFILE])
        out = capsys.readouterr().out
        written = _read(pro)["board"]["design_settings"]["rules"]["min_via_annular_width"]
        assert f".kicad_pro floor {written:.3f} mm for micro vias" in out


# ---------------------------------------------------------------------------
# Refusal path: a merge that would loosen writes nothing
# ---------------------------------------------------------------------------


def _loosening_merge(real_merge):
    """Wrap ``merge_project_rules`` so it loosens two authored rules."""

    def merge(project_data, rules, **kwargs):
        result = real_merge(project_data, rules, **kwargs)
        settings = result["board"]["design_settings"]
        settings["rules"]["min_clearance"] = 0.05
        settings.setdefault("drc_exclusions", []).append("clearance|1|2|3|4")
        return result

    return merge


def _run_mfr_text(pro: Path) -> int:
    try:
        mfr_main(["apply-rules", str(pro), PROFILE])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


def _run_mfr_json(pro: Path) -> int:
    try:
        mfr_main(["apply-rules", str(pro), PROFILE, "--format", "json"])
    except SystemExit as exc:
        return int(exc.code or 0)
    return 0


_WRITERS = {
    "mfr-text": _run_mfr_text,
    "mfr-json": _run_mfr_json,
    "fab-rules": lambda pro: fab_rules_main(["apply", PROFILE, str(pro)]),
    "init": lambda pro: init_project(str(pro), PROFILE),
    "init-design-type": lambda pro: init_project(str(pro), PROFILE, design_type="digital"),
}


@pytest.mark.parametrize("writer", sorted(_WRITERS))
def test_writer_refuses_a_loosening_merge(writer, tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(
        project_generator,
        "merge_project_rules",
        _loosening_merge(project_generator.merge_project_rules),
    )
    pro = _write(tmp_path / "board.kicad_pro", _authored_project())
    original = pro.read_bytes()

    assert _WRITERS[writer](pro) != 0

    assert pro.read_bytes() == original
    assert not (tmp_path / "board.kicad_dru").exists()
    captured = capsys.readouterr()
    if writer == "mfr-json":
        message = json.loads(captured.out)["error"]
    else:
        message = captured.out + captured.err
    message = " ".join(message.split())  # rich wraps long lines
    assert "refusing to write" in message
    assert "rules.min_clearance: lowered 0.2 -> 0.05" in message
    assert "drc_exclusions.clearance|1|2|3|4: new DRC exclusion" in message
    assert "KCT_PRESERVE_BOARD_RULES=0" in message


# ---------------------------------------------------------------------------
# Guard: regenerating every committed board's .kicad_pro never loosens it
# ---------------------------------------------------------------------------

_BOARD_PROJECTS = sorted(
    p
    for p in (REPO_ROOT / "boards").glob("**/*.kicad_pro")
    if ".loom" not in p.relative_to(REPO_ROOT).parts
)


def _board_profile(data: dict) -> tuple[str, int]:
    meta = data.get("meta") or {}
    manufacturer = meta.get("manufacturer")
    if manufacturer not in get_manufacturer_ids():
        manufacturer = PROFILE
    layers = meta.get("layers")
    return manufacturer, layers if isinstance(layers, int) else 2


def test_committed_board_projects_found():
    assert len(_BOARD_PROJECTS) >= 20


@pytest.mark.parametrize(
    "project", _BOARD_PROJECTS, ids=[str(p.relative_to(REPO_ROOT)) for p in _BOARD_PROJECTS]
)
def test_regenerating_committed_board_never_loosens(project, tmp_path):
    original = _read(project)
    allow = merge_policy_allowances(original)
    # The CLI writers may not downgrade a severity at all (#7385 review).
    writer_allow = merge_policy_allowances(original, apply_severities=False)
    manufacturer, layers = _board_profile(original)
    rules = get_profile(manufacturer).get_design_rules(layers, 1.0)

    # merge_project_rules: the kct route / kct check --emit-drc-constraints path.
    merged = merge_project_rules(copy.deepcopy(original), rules)
    assert rule_loosenings(original, merged, allow=allow) == []

    # kct mfr apply-rules <x.kicad_pro>
    mfr_copy = tmp_path / "mfr" / project.name
    mfr_copy.parent.mkdir()
    shutil.copy(project, mfr_copy)
    mfr_main(["apply-rules", str(mfr_copy), manufacturer, "--layers", str(layers)])
    assert rule_loosenings(original, _read(mfr_copy), allow=writer_allow) == []

    # kicad-fab-rules apply
    fab_copy = tmp_path / "fab" / project.name
    fab_copy.parent.mkdir()
    shutil.copy(project, fab_copy)
    assert fab_rules_main(["apply", manufacturer, str(fab_copy), "--layers", str(layers)]) == 0
    assert rule_loosenings(original, _read(fab_copy), allow=writer_allow) == []

    # kct init on the existing project, with a netclass design template.
    init_copy = tmp_path / "init" / project.name
    init_copy.parent.mkdir()
    shutil.copy(project, init_copy)
    assert (
        init_project(str(init_copy), manufacturer, layers=layers, design_type="mixed_signal") == 0
    )
    assert rule_loosenings(original, _read(init_copy), allow=writer_allow) == []
