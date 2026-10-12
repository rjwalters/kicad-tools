"""Routed sidecars declare the router's net classes (Issue #6390).

``kct route`` routes ``GND`` / ``+5V`` at the ``Power`` class's 0.2 mm, but
the routed ``.kicad_pro`` used to declare ``Default`` alone, so neither DRC
engine held the copper to 0.2 mm.  These tests pin the export, its schema
shapes, authored-intent precedence, idempotency, the ``KCT_PRESERVE_BOARD_RULES=0``
DRU block, the renamed-output conflict escape and -- with ``kicad-cli`` -- that
both engines now flag a 0.175 mm gap between two 0.2 mm-class nets.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.core.project_clearance import resolve_project_clearances
from kicad_tools.manufacturers import get_profile, write_drc_constraints
from kicad_tools.manufacturers.netclass_dru import (
    DRU_NETCLASS_BLOCK_BEGIN,
    NETCLASS_RULE_PREFIX,
)
from kicad_tools.manufacturers.project_generator import (
    ROUTER_NETCLASSES_META_KEY,
    SIDECAR_DIGEST_META_KEY,
    apply_router_netclasses,
    is_kct_owned_project,
)
from kicad_tools.router.rules import DEFAULT_NET_CLASS_MAP, NetClassRouting

RULES = get_profile("jlcpcb").get_design_rules(layers=2)
POWER_RULE = (
    "(condition \"(A.NetClass == 'Power' || B.NetClass == 'Power') && "
    "!(A.Type == 'Pad' && B.Type == 'Pad' && A.Reference == B.Reference)\")"
)


def _board_text(gap_mm: float = 0.175) -> str:
    """Two parallel 0.2 mm F.Cu tracks, ``GND`` and ``+5V``, ``gap_mm`` apart."""
    y2 = round(5 + 0.2 + gap_mm, 4)
    return f"""(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "GND") (net 2 "+5V") (net 3 "SIG")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (segment (start 5 5) (end 10 5) (width 0.2) (layer "F.Cu") (net 1))
  (segment (start 5 {y2}) (end 10 {y2}) (width 0.2) (layer "F.Cu") (net 2)))
"""


def _pcb(tmp_path: Path, name: str = "board", gap_mm: float = 0.175) -> Path:
    pcb = tmp_path / f"{name}.kicad_pcb"
    pcb.write_text(_board_text(gap_mm))
    return pcb


def _router_map() -> dict[str, NetClassRouting]:
    return dict(DEFAULT_NET_CLASS_MAP)


def _project(pcb: Path) -> dict:
    return json.loads(pcb.with_suffix(".kicad_pro").read_text())


def _sidecars(pcb: Path) -> tuple[bytes, bytes]:
    return pcb.with_suffix(".kicad_pro").read_bytes(), pcb.with_suffix(".kicad_dru").read_bytes()


# --------------------------------------------------------------------- AC1


@pytest.mark.parametrize("mode", [None, "1", "0"])
def test_v3_project_declares_power_class_with_string_assignments(tmp_path, mode):
    pcb = _pcb(tmp_path)
    write_drc_constraints(
        pcb,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        preserve_board_rules=mode,
    )
    data = _project(pcb)
    settings = data["net_settings"]
    assert settings["meta"] == {"version": 3}
    power = next(c for c in settings["classes"] if c["name"] == "Power")
    assert power["clearance"] == pytest.approx(0.2)
    # Cloned from Default: every other field is the fab-floor Default's.
    default = next(c for c in settings["classes"] if c["name"] == "Default")
    assert {k: v for k, v in power.items() if k not in ("name", "clearance")} == {
        k: v for k, v in default.items() if k not in ("name", "clearance")
    }
    assert settings["netclass_assignments"] == {"+5V": "Power", "GND": "Power"}
    assert data["meta"][ROUTER_NETCLASSES_META_KEY] == {
        "classes": ["Power"],
        "assignments": ["+5V", "GND"],
    }
    resolved = resolve_project_clearances(data, ["GND", "+5V", "SIG"])
    assert resolved["GND"].clearance == pytest.approx(0.2)
    assert resolved["SIG"].source_class == "Default"

    dru = pcb.with_suffix(".kicad_dru").read_text()
    assert POWER_RULE in dru and "(min 0.2mm)" in dru
    # Exactly one Power rule, in the block the mode selects.
    assert dru.count(POWER_RULE) == 1
    assert (DRU_NETCLASS_BLOCK_BEGIN in dru) is (mode == "0")
    # The Power rule follows the unconditional fab-floor clearance.
    assert dru.index(POWER_RULE) > dru.index('(rule "Clearance - jlcpcb"')


@pytest.mark.parametrize("meta", [None, {"version": 5}])
def test_unversioned_or_v5_project_gets_array_assignments(tmp_path, meta):
    pcb = _pcb(tmp_path)
    settings: dict = {"classes": [{"name": "Default", "clearance": 0.1, "priority": 2**31 - 1}]}
    if meta is not None:
        settings["meta"] = meta
    pcb.with_suffix(".kicad_pro").write_text(json.dumps({"net_settings": settings}))
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    data = _project(pcb)
    assert data["net_settings"]["netclass_assignments"] == {"+5V": ["Power"], "GND": ["Power"]}
    assert "meta" not in data["net_settings"] or data["net_settings"]["meta"] == meta
    power = next(c for c in data["net_settings"]["classes"] if c["name"] == "Power")
    assert "priority" not in power
    assert resolve_project_clearances(data, ["GND", "+5V"])["+5V"].clearance == pytest.approx(0.2)


def test_classes_at_or_below_default_and_absent_nets_are_not_declared(tmp_path):
    pcb = _pcb(tmp_path)
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    data = _project(pcb)
    # DEFAULT_NET_CLASS_MAP also maps +3.3V, VCC, CLK, ... -- none is on the board.
    assert set(data["net_settings"]["netclass_assignments"]) == {"GND", "+5V"}
    weak = {"SIG": NetClassRouting(name="Weak", clearance=RULES.min_clearance_mm)}
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=weak)
    data = _project(pcb)
    assert [c["name"] for c in data["net_settings"]["classes"]] == ["Default"]
    assert data["net_settings"]["netclass_assignments"] == {}
    assert ROUTER_NETCLASSES_META_KEY not in data["meta"]
    assert "Power" not in pcb.with_suffix(".kicad_dru").read_text()


def test_none_keeps_classes_a_previous_route_declared(tmp_path):
    """Non-route writers (``kct check --emit-drc-constraints``, board scripts)."""
    pcb = _pcb(tmp_path)
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    before = _sidecars(pcb)
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb")
    assert _sidecars(pcb) == before
    # Mode 0 from a later reviewed-process script keeps the rule (own block).
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", preserve_board_rules="0")
    dru = pcb.with_suffix(".kicad_dru").read_text()
    assert DRU_NETCLASS_BLOCK_BEGIN in dru and NETCLASS_RULE_PREFIX + "Power" in dru


# --------------------------------------------------------------------- AC3


def test_authored_assignment_and_pattern_win(tmp_path, capsys):
    pcb = _pcb(tmp_path)
    pcb.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "meta": {"version": 3},
                    "classes": [
                        {"name": "Default", "clearance": 0.1},
                        {"name": "Loose", "clearance": 0.15},
                    ],
                    "netclass_assignments": {"GND": "Loose"},
                    "netclass_patterns": [],
                }
            }
        )
    )
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    data = _project(pcb)
    assert data["net_settings"]["netclass_assignments"] == {"+5V": "Power", "GND": "Loose"}
    loose = next(c for c in data["net_settings"]["classes"] if c["name"] == "Loose")
    assert loose["clearance"] == 0.15
    assert "not applied to GND" in capsys.readouterr().err


def test_authored_pattern_member_keeps_its_class(tmp_path, capsys):
    pcb = _pcb(tmp_path)
    pcb.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "classes": [
                        {"name": "Default", "clearance": 0.1, "priority": 2**31 - 1},
                        {"name": "Ground", "clearance": 0.12, "priority": 0},
                    ],
                    "netclass_patterns": [{"netclass": "Ground", "pattern": "GND"}],
                }
            }
        )
    )
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    assert _project(pcb)["net_settings"]["netclass_assignments"] == {"+5V": ["Power"]}
    assert "not applied to GND" in capsys.readouterr().err


def test_colliding_authored_class_is_never_replaced(tmp_path, capsys):
    pcb = _pcb(tmp_path)
    authored = {"name": "Power", "clearance": 0.15}
    pcb.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "meta": {"version": 3},
                    "classes": [{"name": "Default", "clearance": 0.1}, dict(authored)],
                }
            }
        )
    )
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    data = _project(pcb)
    assert [c for c in data["net_settings"]["classes"] if c["name"] == "Power"] == [authored]
    assert not data["net_settings"].get("netclass_assignments")
    assert "already defines 'Power'" in capsys.readouterr().err


def test_identical_authored_class_is_reused(tmp_path):
    pcb = _pcb(tmp_path)
    pcb.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "meta": {"version": 3},
                    "classes": [
                        {"name": "Default", "clearance": 0.1},
                        {"name": "Power", "clearance": 0.2, "track_width": 0.5},
                    ],
                }
            }
        )
    )
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    data = _project(pcb)
    powers = [c for c in data["net_settings"]["classes"] if c["name"] == "Power"]
    assert powers == [{"name": "Power", "clearance": 0.2, "track_width": 0.5}]
    assert data["meta"][ROUTER_NETCLASSES_META_KEY]["classes"] == []
    # The authored class survives a later route that no longer needs it.
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes={})
    assert any(c["name"] == "Power" for c in _project(pcb)["net_settings"]["classes"])


def test_unsupported_authored_pattern_blocks_router_classes(tmp_path, capsys):
    pcb = _pcb(tmp_path)
    pcb.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "classes": [
                        {"name": "Default", "clearance": 0.1, "priority": 2**31 - 1},
                        {"name": "Rails", "clearance": 0.3, "priority": 0},
                    ],
                    "netclass_patterns": [{"netclass": "Rails", "pattern": "+*V"}],
                }
            }
        )
    )
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    data = _project(pcb)
    assert [c["name"] for c in data["net_settings"]["classes"]] == ["Default", "Rails"]
    assert "cannot be evaluated safely" in capsys.readouterr().err


def test_previous_router_classes_are_replaced_not_accumulated():
    project: dict = {
        "net_settings": {"meta": {"version": 3}, "classes": [{"name": "Default", "clearance": 0.1}]}
    }
    apply_router_netclasses(project, {"A": NetClassRouting(name="Hot", clearance=0.3)}, ["A", "B"])
    apply_router_netclasses(project, {"B": NetClassRouting(name="Warm", clearance=0.2)}, ["A", "B"])
    assert [c["name"] for c in project["net_settings"]["classes"]] == ["Default", "Warm"]
    assert project["net_settings"]["netclass_assignments"] == {"B": "Warm"}


# --------------------------------------------------------------------- AC4


def test_routing_twice_gives_byte_identical_sidecars(tmp_path):
    pcb = _pcb(tmp_path)
    for mode in (None, "0"):
        write_drc_constraints(
            pcb,
            RULES,
            manufacturer_id="jlcpcb",
            router_net_classes=_router_map(),
            preserve_board_rules=mode,
        )
        first = _sidecars(pcb)
        write_drc_constraints(
            pcb,
            RULES,
            manufacturer_id="jlcpcb",
            router_net_classes=_router_map(),
            preserve_board_rules=mode,
        )
        assert _sidecars(pcb) == first


def test_rerouting_a_routed_board_reproduces_its_sidecars(tmp_path):
    """Source -> routed, then routed -> re-routed: the classes read back as-is."""
    source = _pcb(tmp_path, "src")
    routed = tmp_path / "src_routed.kicad_pcb"
    shutil.copyfile(source, routed)
    write_drc_constraints(
        routed,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        source_pcb_path=source,
    )
    rerouted = tmp_path / "src_routed_routed.kicad_pcb"
    shutil.copyfile(routed, rerouted)
    write_drc_constraints(
        rerouted,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        source_pcb_path=routed,
    )
    first = _project(routed)
    second = _project(rerouted)
    assert second["net_settings"] == first["net_settings"]
    assert (
        routed.with_suffix(".kicad_dru").read_bytes()
        == rerouted.with_suffix(".kicad_dru").read_bytes()
    )
    # And the routed sidecars themselves are stable across a repeat route.
    before = _sidecars(routed)
    write_drc_constraints(
        routed,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        source_pcb_path=source,
    )
    assert _sidecars(routed) == before


def test_router_class_conditions_are_not_via_scoped():
    """#6294/#6280: the via-clearance reader must keep ignoring these rules."""
    from kicad_tools.manufacturers.netclass_dru import netclass_condition
    from kicad_tools.router.clearance_resolver import _is_via_scoped_condition

    for router in (False, True):
        assert not _is_via_scoped_condition(netclass_condition("Power", router=router))


def test_router_class_rules_do_not_change_the_unconditional_dru_floor(tmp_path):
    from kicad_tools.router.clearance_resolver import _read_dru_clearances

    pcb = _pcb(tmp_path)
    write_drc_constraints(
        pcb,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        preserve_board_rules="0",
    )
    assert _read_dru_clearances(pcb.with_suffix(".kicad_dru")) == (RULES.min_clearance_mm, None)


# --------------------------------------------------------------------- AC5


@pytest.fixture
def stale_routed(tmp_path):
    """A source board plus a routed output kct wrote from an OLDER source."""
    source = _pcb(tmp_path, "src")
    source.with_suffix(".kicad_pro").write_text(
        json.dumps(
            {
                "net_settings": {
                    "meta": {"version": 3},
                    "classes": [{"name": "Default", "clearance": 0.2}],
                }
            }
        )
    )
    routed = tmp_path / "src_routed.kicad_pcb"
    shutil.copyfile(source, routed)
    write_drc_constraints(
        routed,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        source_pcb_path=source,
    )
    # The source moves on (e.g. a regenerated design adds a text variable).
    data = json.loads(source.with_suffix(".kicad_pro").read_text())
    data["text_variables"] = {"REV": "B"}
    source.with_suffix(".kicad_pro").write_text(json.dumps(data))
    return source, routed


def test_kct_written_destination_is_regenerated(stale_routed):
    source, routed = stale_routed
    assert is_kct_owned_project(_project(routed))
    write_drc_constraints(
        routed,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        source_pcb_path=source,
    )
    assert _project(routed)["text_variables"] == {"REV": "B"}


def test_floor_only_edit_keeps_kct_ownership(stale_routed):
    """Board 04's ``apply_native_floors`` hand-sets a floor after kct wrote it."""
    source, routed = stale_routed
    write_drc_constraints(routed, RULES, manufacturer_id="jlcpcb", preserve_board_rules="0")
    data = _project(routed)
    data["board"]["design_settings"]["rules"]["min_via_annular_width"] = 0.075
    routed.with_suffix(".kicad_pro").write_text(json.dumps(data, indent=2) + "\n")
    assert is_kct_owned_project(data)
    write_drc_constraints(
        routed,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        source_pcb_path=source,
    )
    assert _project(routed)["text_variables"] == {"REV": "B"}


def test_hand_edited_destination_still_conflicts(stale_routed):
    source, routed = stale_routed
    data = _project(routed)
    data["net_settings"]["classes"].append({"name": "Mine", "clearance": 0.5})
    routed.with_suffix(".kicad_pro").write_text(json.dumps(data))
    before = _sidecars(routed)
    assert not is_kct_owned_project(data)
    with pytest.raises(ValueError, match="DRC sidecar conflict"):
        write_drc_constraints(
            routed,
            RULES,
            manufacturer_id="jlcpcb",
            router_net_classes=_router_map(),
            source_pcb_path=source,
        )
    assert _sidecars(routed) == before


def test_dru_differing_only_in_managed_blocks_is_regenerated(tmp_path):
    source = _pcb(tmp_path, "src")
    source.with_suffix(".kicad_dru").write_text(
        '(version 1)\n(rule "Custom width" (constraint track_width (min 0.2mm)))\n'
    )
    routed = tmp_path / "src_routed.kicad_pcb"
    shutil.copyfile(source, routed)
    write_drc_constraints(routed, RULES, manufacturer_id="jlcpcb", source_pcb_path=source)
    # A later pass under another mode rewrote the managed blocks only.
    write_drc_constraints(
        routed,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        preserve_board_rules="0",
    )
    write_drc_constraints(
        routed,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        source_pcb_path=source,
    )
    dru = routed.with_suffix(".kicad_dru").read_text()
    assert '"Custom width"' in dru and DRU_NETCLASS_BLOCK_BEGIN not in dru
    # A user rule added to the destination is still protected.
    routed.with_suffix(".kicad_dru").write_text(
        dru + '(rule "Mine" (constraint track_width (min 0.3mm)))\n'
    )
    with pytest.raises(ValueError, match="DRC sidecar conflict"):
        write_drc_constraints(routed, RULES, manufacturer_id="jlcpcb", source_pcb_path=source)


def test_stamp_digest_lives_in_meta(tmp_path):
    pcb = _pcb(tmp_path)
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb")
    meta = _project(pcb)["meta"]
    assert list(meta)[-1] == SIDECAR_DIGEST_META_KEY and len(meta[SIDECAR_DIGEST_META_KEY]) == 64


# ----------------------------------------------------- route_cmd threading


def test_route_sidecar_writer_threads_the_effective_map(tmp_path):
    from kicad_tools.cli import route_cmd

    pcb = _pcb(tmp_path)
    route_cmd._write_drc_constraint_sidecars(
        pcb, "jlcpcb", 2, quiet=True, net_class_map=_router_map()
    )
    assert _project(pcb)["net_settings"]["netclass_assignments"] == {
        "+5V": "Power",
        "GND": "Power",
    }


def test_post_route_drc_overlays_the_loaded_map(tmp_path, monkeypatch):
    """The sidecar gets the same router map + ``--net-class-map`` overlay as
    ``net_class_map.json``."""
    from kicad_tools.cli import route_cmd

    calls = {}

    def fake(output_path, manufacturer, layers, **kwargs):
        calls.update(kwargs)
        raise RuntimeError("stop")

    monkeypatch.setattr(route_cmd, "_write_drc_constraint_sidecars", fake)
    pcb = _pcb(tmp_path)
    loaded = {"SIG": NetClassRouting(name="HV", clearance=0.5)}
    with pytest.raises(RuntimeError, match="stop"):
        route_cmd.run_post_route_drc(
            pcb, "jlcpcb", 2, quiet=True, net_class_map=_router_map(), loaded_net_class_map=loaded
        )
    assert calls["net_class_map"]["SIG"].name == "HV"
    assert calls["net_class_map"]["GND"].name == "Power"


# ------------------------------------------------- AC2: both engines agree


def _kct_errors(pcb: Path, tmp_path: Path) -> list[dict]:
    from kicad_tools.cli.check_cmd import main

    out = tmp_path / f"{pcb.stem}-kct.json"
    main(
        [
            str(pcb),
            "--mfr",
            "jlcpcb",
            "--only",
            "netclass_clearance_copper",
            "--allow-incomplete",
            "--no-net-class-map",
            "--format",
            "json",
            "--output",
            str(out),
        ]
    )
    report = json.loads(out.read_text())
    return [
        v
        for v in report["violations"]
        if v["rule_id"] == "netclass_clearance_copper" and v["severity"] == "error"
    ]


def _kicad_cli_clearance(pcb: Path) -> list[dict]:
    from kicad_tools.cli.runner import find_kicad_cli

    cli = find_kicad_cli()
    report = pcb.with_suffix(".drc.json")
    proc = subprocess.run(
        [
            str(cli),
            "pcb",
            "drc",
            "--format",
            "json",
            "--severity-error",
            "-o",
            str(report),
            str(pcb),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    data = json.loads(report.read_text())
    return [v for v in data["violations"] if v["type"] == "clearance"]


@pytest.mark.parametrize("gap, expect", [(0.175, True), (0.205, False)])
def test_kct_check_enforces_router_class(tmp_path, capsys, gap, expect):
    pcb = _pcb(tmp_path, gap_mm=gap)
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map())
    errors = _kct_errors(pcb, tmp_path)
    capsys.readouterr()
    assert bool(errors) is expect, errors
    if expect:
        assert min(e["actual_value"] for e in errors) == pytest.approx(gap, abs=1e-3)
        assert all(e["required_value"] == pytest.approx(0.2) for e in errors)


def _has_kicad_cli() -> bool:
    from kicad_tools.cli.runner import find_kicad_cli

    return find_kicad_cli() is not None


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
@pytest.mark.parametrize("mode", [None, "0"])
@pytest.mark.parametrize("shape", ["v3", "unversioned"])
@pytest.mark.parametrize("gap, expect", [(0.175, True), (0.205, False)])
def test_kicad_cli_enforces_router_class(tmp_path, mode, shape, gap, expect):
    pcb = _pcb(tmp_path, gap_mm=gap)
    if shape == "unversioned":
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "net_settings": {
                        "classes": [{"name": "Default", "clearance": 0.1, "priority": 2**31 - 1}]
                    }
                }
            )
        )
    write_drc_constraints(
        pcb,
        RULES,
        manufacturer_id="jlcpcb",
        router_net_classes=_router_map(),
        preserve_board_rules=mode,
    )
    findings = _kicad_cli_clearance(pcb)
    assert bool(findings) is expect, findings


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
def test_kicad_cli_is_blind_without_the_export(tmp_path):
    """Control: the pre-#6390 Default-only sidecars pass the 0.175 mm gap."""
    pcb = _pcb(tmp_path, gap_mm=0.175)
    write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb")
    assert _kicad_cli_clearance(pcb) == []


# ------------------------------- router classes skip same-footprint pad pairs


_PADS_BOARD = """(kicad_pcb (version 20240108) (generator pcbnew)
  (general (thickness 1.6)) (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0)) (net 0 "") (net 1 "GND") (net 2 "SIG")
  (gr_rect (start 0 0) (end 20 20) (stroke (width 0.1) (type default))
    (fill none) (layer "Edge.Cuts"))
  (footprint "T:Two" (layer "F.Cu") (at 5 5)
    (property "Reference" "U1" (at 0 -2) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.3 1) (layers "F.Cu") (net 1 "GND"))
    (pad "2" smd rect (at 0.45 0) (size 0.3 1) (layers "F.Cu") (net 2 "SIG")))
  (footprint "T:One" (layer "F.Cu") (at 10 5)
    (property "Reference" "U2" (at 0 -2) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.3 1) (layers "F.Cu") (net 1 "GND")))
  (footprint "T:One" (layer "F.Cu") (at 10.45 5)
    (property "Reference" "U3" (at 0 -2) (layer "F.SilkS")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.3 1) (layers "F.Cu") (net 2 "SIG"))))
"""


def _pads_board(tmp_path: Path, *, authored: bool) -> Path:
    """GND/SIG pads 0.15 mm apart: inside U1, and between U2 and U3."""
    pcb = tmp_path / "pads.kicad_pcb"
    pcb.write_text(_PADS_BOARD)
    if authored:
        pcb.with_suffix(".kicad_pro").write_text(
            json.dumps(
                {
                    "net_settings": {
                        "meta": {"version": 3},
                        "classes": [
                            {"name": "Default", "clearance": 0.1},
                            {"name": "Power", "clearance": 0.2},
                        ],
                        "netclass_assignments": {"GND": "Power"},
                    }
                }
            )
        )
        write_drc_constraints(pcb, RULES, manufacturer_id="jlcpcb")
    else:
        write_drc_constraints(
            pcb, RULES, manufacturer_id="jlcpcb", router_net_classes=_router_map()
        )
    return pcb


def _pad_pairs(refs: list[list[str]]) -> set[frozenset[str]]:
    return {frozenset(pair) for pair in refs}


@pytest.mark.parametrize("authored", [False, True])
def test_kct_router_class_skips_same_footprint_pads(tmp_path, capsys, authored):
    pcb = _pads_board(tmp_path, authored=authored)
    errors = _kct_errors(pcb, tmp_path)
    capsys.readouterr()
    footprints = {frozenset(item.split("-")[0] for item in e["items"]) for e in errors}
    # Between U2 and U3 the class always holds.
    assert frozenset({"U2", "U3"}) in footprints, errors
    # Inside U1 only an authored class does.
    assert (frozenset({"U1"}) in footprints) is authored, errors


@pytest.mark.skipif(not _has_kicad_cli(), reason="kicad-cli not installed")
@pytest.mark.parametrize("authored", [False, True])
def test_kicad_cli_router_class_skips_same_footprint_pads(tmp_path, authored):
    pcb = _pads_board(tmp_path, authored=authored)
    findings = _kicad_cli_clearance(pcb)
    owners = [
        {item["description"].split(" of ")[1].split(" ")[0] for item in v["items"]}
        for v in findings
    ]
    assert {"U2", "U3"} in owners
    assert ({"U1"} in owners) is authored, owners
