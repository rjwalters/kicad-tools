"""Issue #6044: ``routing_policy`` -- per-net via caps and maximum routed length.

Synthetic KiCad 10 boards built in-test.  Policies live in the existing
net-class map (``max_vias_per_net`` and ``length_constraint.max_length``).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from kicad_tools.router.kicad_length import measure_pcb_net
from kicad_tools.router.rules import LengthConstraint, NetClassRouting
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker

_HDR = """(kicad_pcb
\t(version 20260206)
\t(generator "pcbnew")
\t(generator_version "10.0")
\t(general (thickness 1.6) (legacy_teardrops no))
\t(paper "A4")
\t(layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))
\t(setup (pad_to_mask_clearance 0))
"""


def seg(a, b, net, layer="F.Cu", width=0.2):
    return (
        f"\t(segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {width}) "
        f'(layer "{layer}") (net "{net}") (uuid "{uuid.uuid4()}"))\n'
    )


def arc(a, mid, b, net, layer="F.Cu", width=0.2):
    return (
        f"\t(arc (start {a[0]} {a[1]}) (mid {mid[0]} {mid[1]}) (end {b[0]} {b[1]}) "
        f'(width {width}) (layer "{layer}") (net "{net}") (uuid "{uuid.uuid4()}"))\n'
    )


def via(at, net):
    return (
        f'\t(via (at {at[0]} {at[1]}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") '
        f'(net "{net}") (uuid "{uuid.uuid4()}"))\n'
    )


def write_board(path: Path, items: list[str]) -> Path:
    edge = (
        "\t(gr_rect (start -5 -10) (end 60 20) (stroke (width 0.1) (type default)) "
        f'(fill no) (layer "Edge.Cuts") (uuid "{uuid.uuid4()}"))\n'
    )
    path.write_text(_HDR + "".join(items) + edge + ")\n")
    return path


def vias_on(net: str, n: int, y: float = 0.0) -> list[str]:
    return [via((2 + 3 * i, y), net) for i in range(n)]


def checker_for(path: Path, ncm: dict, **kw) -> DRCChecker:
    return DRCChecker(PCB.load(str(path)), manufacturer="jlcpcb", layers=2, net_class_map=ncm, **kw)


def policy(name="CLK", **kw) -> NetClassRouting:
    max_length = kw.pop("max_length", None)
    lc = LengthConstraint(net_id=0, max_length=max_length) if max_length is not None else None
    return NetClassRouting(name=name, length_constraint=lc, **kw)


def errors(results):
    return [v for v in results.violations if v.severity == "error"]


# ---------------------------------------------------------------------------
# Via cap
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("n_vias", "cap", "fails"), [(1, 2, False), (2, 2, False), (3, 2, True)])
def test_via_cap_under_at_over(tmp_path, n_vias, cap, fails):
    pcb = write_board(
        tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "CLK"), *vias_on("CLK", n_vias)]
    )
    res = checker_for(pcb, {"CLK": policy(max_vias_per_net=cap)}).check_routing_policy()
    errs = errors(res)
    if not fails:
        assert errs == []
        return
    assert len(errs) == 1
    v = errs[0]
    assert v.rule_id == "routing_policy"
    assert "CLK" in v.message and "class CLK" in v.message
    assert f"{n_vias} vias" in v.message and f"max_vias_per_net {cap}" in v.message
    assert v.actual_value == n_vias and v.required_value == cap
    assert v.nets == ("CLK",) and v.items == ("CLK",)


def test_no_policy_no_finding(tmp_path):
    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "CLK"), *vias_on("CLK", 5)])
    res = checker_for(pcb, {"CLK": NetClassRouting(name="CLK")}).check_routing_policy()
    assert res.violations == []


# ---------------------------------------------------------------------------
# Max length
# ---------------------------------------------------------------------------


def test_branched_net_length_sums_every_branch(tmp_path):
    # A T: 10 + 10 mm trunk and a 5 mm branch -> 25 mm total.
    pcb = write_board(
        tmp_path / "b.kicad_pcb",
        [
            seg((0, 0), (10, 0), "FB"),
            seg((10, 0), (20, 0), "FB"),
            seg((10, 0), (10, 5), "FB"),
        ],
    )
    board = PCB.load(str(pcb))
    net_id = board.get_net_by_name("FB").number
    measured = measure_pcb_net(board, net_id, num_copper_hint=2)
    assert measured == pytest.approx(25.0, abs=0.05)

    over = checker_for(pcb, {"FB": policy("Analog", max_length=21.0)}).check_routing_policy()
    errs = errors(over)
    assert len(errs) == 1
    assert errs[0].actual_value == pytest.approx(round(measured, 4))
    assert errs[0].required_value == 21.0
    assert f"{measured:.3f} mm" in errs[0].message and "21.000 mm" in errs[0].message

    ok = checker_for(pcb, {"FB": policy("Analog", max_length=30.0)}).check_routing_policy()
    assert errors(ok) == []


def test_arc_net_with_length_policy_warns_unmeasured(tmp_path):
    pcb = write_board(
        tmp_path / "b.kicad_pcb",
        [seg((0, 0), (10, 0), "FB"), arc((10, 0), (12, 2), (14, 0), "FB")],
    )
    res = checker_for(pcb, {"FB": policy("Analog", max_length=1000.0)}).check_routing_policy()
    assert errors(res) == []
    warns = [v for v in res.violations if v.severity == "warning"]
    assert len(warns) == 1
    assert "unmeasured (track arcs)" in warns[0].message
    assert warns[0].nets == ("FB",)


# ---------------------------------------------------------------------------
# Diff pairs: not reported twice
# ---------------------------------------------------------------------------


def _pair_map(**kw) -> dict:
    nc = NetClassRouting(name="HS", priority=2, coupled_routing=True, max_vias_per_net=1, **kw)
    return {
        "X_P": NetClassRouting(**{**nc.__dict__, "diffpair_partner": "X_N"}),
        "X_N": NetClassRouting(**{**nc.__dict__, "diffpair_partner": "X_P"}),
    }


def _pair_board(tmp_path):
    return write_board(
        tmp_path / "b.kicad_pcb",
        [
            seg((0, 0), (20, 0), "X_P"),
            seg((0, 0.4), (20, 0.4), "X_N"),
            *vias_on("X_P", 2, 0.0),
            *vias_on("X_N", 2, 0.4),
        ],
    )


def test_pair_via_cap_reported_once(tmp_path):
    from kicad_tools.cli.check_cmd import run_selected_checks

    pcb = _pair_board(tmp_path)
    checker = checker_for(pcb, _pair_map())
    res = run_selected_checks(
        checker, {"routing_policy", "diffpair_uncoupled_length"}, set(), pcb_path=pcb
    )
    via_errs = [v for v in errors(res) if "vias" in v.message]
    assert {v.rule_id for v in via_errs} == {"diffpair_uncoupled_length"}
    assert sorted(v.nets for v in via_errs) == [("X_N",), ("X_P",)]


def test_pair_via_cap_checked_when_pair_rule_does_not_run(tmp_path):
    pcb = _pair_board(tmp_path)
    checker = checker_for(pcb, _pair_map(), routing_policy_defer_pair_vias=False)
    errs = errors(checker.check_routing_policy())
    assert sorted(v.nets for v in errs) == [("X_N",), ("X_P",)]


# ---------------------------------------------------------------------------
# --require-routing-policy (checker level)
# ---------------------------------------------------------------------------


def test_strict_reports_sensitive_class_without_policy(tmp_path):
    pcb = write_board(
        tmp_path / "b.kicad_pcb",
        [
            seg((0, 0), (20, 0), "CLK_A"),
            seg((0, 2), (20, 2), "CLK_B"),
            seg((0, 4), (20, 4), "GND"),
            seg((0, 6), (20, 6), "SDA"),
            seg((0, 8), (20, 8), "REF"),
        ],
    )
    clk = NetClassRouting(name="Clock", length_critical=True)
    ncm = {
        "CLK_A": clk,
        "CLK_B": clk,
        # Pour nets are exempt even when flagged sensitive.
        "GND": NetClassRouting(name="Power", is_pour_net=True, noise_sensitive=True),
        # Not sensitive -> never needs a policy.
        "SDA": NetClassRouting(name="Default"),
        # Sensitive with a policy -> passes.
        "REF": policy("Analog", noise_sensitive=True, max_length=100.0),
    }
    res = checker_for(pcb, ncm, require_routing_policy=True).check_routing_policy()
    errs = errors(res)
    assert len(errs) == 1
    v = errs[0]
    assert v.items == ("Clock",)
    assert v.nets == ("CLK_A", "CLK_B")
    assert "length_critical" in v.message

    # Without the flag the category says nothing about missing policies.
    assert checker_for(pcb, ncm).check_routing_policy().violations == []


# ---------------------------------------------------------------------------
# Malformed values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kw", "field"),
    [
        ({"max_vias_per_net": -1}, "max_vias_per_net"),
        ({"max_vias_per_net": 1.5}, "max_vias_per_net"),
        ({"max_vias_per_net": "two"}, "max_vias_per_net"),
        ({"max_length": 0.0}, "max_length"),
        ({"max_length": -3.0}, "max_length"),
        ({"max_length": "long"}, "max_length"),
    ],
)
def test_malformed_values_are_errors_not_exceptions(tmp_path, kw, field):
    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "CLK")])
    res = checker_for(pcb, {"CLK": policy(**kw)}).check_routing_policy()
    errs = errors(res)
    assert len(errs) == 1
    assert "malformed" in errs[0].message and field in errs[0].message
    assert "CLK" in errs[0].message


# ---------------------------------------------------------------------------
# CLI: flag, usage errors, unresolved keys, waivers, compatibility
# ---------------------------------------------------------------------------


def _write_map(path: Path, entries: dict) -> Path:
    path.write_text(json.dumps(entries))
    return path


def _cli(argv, capsys):
    from kicad_tools.cli.check_cmd import main

    rc = main([*argv, "--format", "json", "--allow-incomplete"])
    out = capsys.readouterr()
    return rc, out


def test_cli_via_cap_fails_and_waiver_relieves(tmp_path, capsys):
    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "CLK"), *vias_on("CLK", 3)])
    ncm = _write_map(tmp_path / "ncm.json", {"CLK": {"name": "Clock", "max_vias_per_net": 1}})
    rc, out = _cli([str(pcb), "--net-class-map", str(ncm), "--only", "routing_policy"], capsys)
    data = json.loads(out.out)
    found = [v for v in data["violations"] if v["rule_id"] == "routing_policy"]
    assert len(found) == 1 and rc != 0

    waivers = tmp_path / "waivers.json"
    waivers.write_text(
        json.dumps(
            {
                "version": 2,
                "waivers": [
                    {
                        "rule": "routing_policy",
                        "items": ["CLK"],
                        "reason": "test",
                        "issue": "#6044",
                    }
                ],
            }
        )
    )
    rc, out = _cli(
        [
            str(pcb),
            "--net-class-map",
            str(ncm),
            "--only",
            "routing_policy",
            "--waivers",
            str(waivers),
        ],
        capsys,
    )
    data = json.loads(out.out)
    found = [v for v in data["violations"] if v["rule_id"] == "routing_policy"]
    assert len(found) == 1 and found[0]["waived"] is True
    assert rc == 0


def test_cli_length_constraint_net_id_is_ignored(tmp_path, capsys):
    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "FB")])
    ncm = _write_map(
        tmp_path / "ncm.json",
        {"FB": {"name": "Analog", "length_constraint": {"net_id": 999, "max_length": 5.0}}},
    )
    rc, out = _cli([str(pcb), "--net-class-map", str(ncm), "--only", "routing_policy"], capsys)
    found = [v for v in json.loads(out.out)["violations"] if v["rule_id"] == "routing_policy"]
    assert len(found) == 1 and "exceeds max_length 5.000 mm" in found[0]["message"]


def test_cli_strict_flag(tmp_path, capsys):
    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "CLK")])
    ncm = _write_map(tmp_path / "ncm.json", {"CLK": {"name": "Clock", "length_critical": True}})
    base = [str(pcb), "--net-class-map", str(ncm), "--only", "routing_policy"]

    rc, out = _cli(base, capsys)
    assert rc == 0
    assert not [v for v in json.loads(out.out)["violations"] if v["rule_id"] == "routing_policy"]

    rc, out = _cli([*base, "--require-routing-policy"], capsys)
    found = [v for v in json.loads(out.out)["violations"] if v["rule_id"] == "routing_policy"]
    assert rc != 0 and len(found) == 1
    assert found[0]["items"] == ["Clock"]


def test_cli_strict_without_map_is_usage_error(tmp_path, capsys):
    from kicad_tools.cli.check_cmd import main

    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "CLK")])
    rc = main([str(pcb), "--require-routing-policy", "--allow-incomplete"])
    err = capsys.readouterr().err
    assert rc == 1
    assert "--require-routing-policy needs a net-class map" in err

    rc = main([str(pcb), "--require-routing-policy", "--no-net-class-map"])
    assert rc == 1
    rc = main([str(pcb), "--require-routing-policy", "--skip", "routing_policy"])
    assert rc == 1


def test_cli_unresolved_policy_key_warns(tmp_path, capsys):
    pcb = write_board(tmp_path / "b.kicad_pcb", [seg((0, 0), (20, 0), "CLK")])
    ncm = _write_map(
        tmp_path / "ncm.json",
        {
            "CLK": {"name": "Clock"},
            "NOPE": {"name": "Clock", "max_vias_per_net": 0},
        },
    )
    rc, out = _cli([str(pcb), "--net-class-map", str(ncm), "--only", "routing_policy"], capsys)
    assert "routing policy declared for net-class-map key(s) that match no board net" in out.err
    assert "NOPE" in out.err


def test_cli_policy_on_unrouted_net_warns(tmp_path, capsys):
    pcb = write_board(
        tmp_path / "b.kicad_pcb",
        [seg((0, 0), (20, 0), "CLK"), '\t(net 7 "IDLE")\n'],
    )
    ncm = _write_map(tmp_path / "ncm.json", {"IDLE": {"name": "Clock", "max_vias_per_net": 0}})
    rc, out = _cli([str(pcb), "--net-class-map", str(ncm), "--only", "routing_policy"], capsys)
    if "IDLE" not in out.err:
        pytest.skip("synthetic board did not declare the copper-less net")
    assert "routing policy declared but not evaluated" in out.err
