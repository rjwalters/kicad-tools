"""``kct route`` resolves ``via_clearance`` from the board's own rules (Issue #6280).

Before #6280 ``kct route`` set ``DesignRules.trace_clearance`` from the
board / fab rules but left ``via_clearance`` at the 0.20mm dataclass default.
#6272 (PR #6279) then made every trace/via gate resolve
``max(trace_clearance, via_clearance)`` in both insertion orders, so that
default held every trace 0.20mm off every foreign via even on boards whose
declared rule -- the one ``kicad-cli`` measures -- is 0.15mm.  Board 02 went
from 27 to 52 vias for no DRC benefit.

These cases pin:

(a) ``via_clearance`` is read from the ``.kicad_pro`` board minimum, a
    ``.kicad_dru`` via-scoped (or unconditional) clearance rule and the
    netclass, is floored by the fab profile, and falls back to 0.20mm only
    when nothing is declared;
(b) a board whose ``.kicad_dru`` via rule is ABOVE its trace clearance still
    routes with traces at least the via rule away from foreign vias (the
    issue's third acceptance item), end to end through ``kct route``;
(c) the ``--via-clearance`` override, on both parsers.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.router.clearance_resolver import (
    DEFAULT_VIA_CLEARANCE_MM,
    DeclaredClearanceRules,
    RuleSource,
    _is_via_scoped_condition,
    read_declared_clearance_rules,
    resolve_via_clearance,
    trace_via_clearance_mm,
)

JLCPCB_FLOOR_MM = 0.127

VIA_RULE_DRU = (
    "(version 1)\n"
    '(rule "Via clearance"\n'
    "  (condition \"A.Type == 'Via' || B.Type == 'Via'\")\n"
    "  (constraint clearance (min {mm}mm)))\n"
)


def _board(tmp_path: Path, *, project: dict | None = None, dru: str | None = None) -> Path:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")
    if project is not None:
        pcb.with_suffix(".kicad_pro").write_text(json.dumps(project))
    if dru is not None:
        pcb.with_suffix(".kicad_dru").write_text(dru)
    return pcb


def _netclass_project(clearance: float, min_clearance: float | None = None) -> dict:
    project: dict = {
        "net_settings": {"classes": [{"name": "Default", "clearance": clearance}]},
    }
    if min_clearance is not None:
        project["board"] = {"design_settings": {"rules": {"min_clearance": min_clearance}}}
    return project


# ---------------------------------------------------------------------------
# (a) Reading and resolving the via rule
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "condition",
    [
        "A.Type == 'Via'",
        "A.Type == 'Via' || B.Type == 'Via'",
        "A.Type == 'via' && B.Type == 'track'",
        'B.Type == "Via"',
        "(A.Type == 'Via') || A.NetClass == 'HV'",
    ],
)
def test_via_scoped_conditions_are_recognised(condition: str) -> None:
    assert _is_via_scoped_condition(condition)


@pytest.mark.parametrize(
    "condition",
    [
        "A.NetClass == 'HV'",
        "A.Type == 'Pad' && B.Type == 'Via'",
        "A.Type == 'Track'",
        "A.Type != 'Via'",
        "A.Type == 'Via' && A.NetClass == 'HV'",
        "A.Via_Type != 'Micro'",
    ],
)
def test_other_conditions_are_not_read_as_via_rules(condition: str) -> None:
    """Anything needing a real DRC-expression evaluator is ignored, not guessed."""
    assert not _is_via_scoped_condition(condition)


def test_a_via_scoped_dru_rule_feeds_the_via_layer_only(tmp_path) -> None:
    pcb = _board(tmp_path, dru=VIA_RULE_DRU.format(mm=0.3))

    declared = read_declared_clearance_rules(pcb)

    assert declared.dru_via_mm == pytest.approx(0.3)
    # It must not leak into the board-wide trace base.
    assert declared.dru_mm is None
    assert declared.project_requirement() is None
    assert declared.via_requirement() == (0.3, RuleSource.PROJECT_DRU_VIA, None)
    assert not declared.is_empty()


@pytest.mark.parametrize(
    "project,dru,expected,source",
    [
        # Netclass only: the shipped-fleet shape (board 02 declares 0.15).
        (_netclass_project(0.15), None, 0.15, RuleSource.PROJECT_NET_CLASS),
        # The .kicad_pro board minimum, stricter than the netclass.
        (_netclass_project(0.15, min_clearance=0.18), None, 0.18, RuleSource.PROJECT_MIN_CLEARANCE),
        # A .kicad_dru via rule above everything else wins.
        (_netclass_project(0.15), VIA_RULE_DRU.format(mm=0.25), 0.25, RuleSource.PROJECT_DRU_VIA),
        # ...but never loosens a stricter generic rule: KiCad enforces both.
        (_netclass_project(0.22), VIA_RULE_DRU.format(mm=0.16), 0.22, RuleSource.PROJECT_NET_CLASS),
        # An unconditional DRU clearance applies to vias as well.
        (
            None,
            "(version 1)\n(rule c (constraint clearance (min 0.17mm)))\n",
            0.17,
            RuleSource.PROJECT_DRU,
        ),
    ],
)
def test_via_clearance_is_resolved_from_declared_rules(
    tmp_path, project, dru, expected: float, source: RuleSource
) -> None:
    declared = read_declared_clearance_rules(_board(tmp_path, project=project, dru=dru))

    resolved = resolve_via_clearance(
        declared=declared, fab_floor_mm=JLCPCB_FLOOR_MM, manufacturer="jlcpcb"
    )

    assert resolved.required_mm == pytest.approx(expected)
    assert resolved.source is source
    assert resolved.warning is None


def test_a_declared_value_replaces_the_default_in_both_directions() -> None:
    """The 0.20mm default is "nothing declared", not a margin to floor."""
    looser = resolve_via_clearance(declared=DeclaredClearanceRules(net_class_clearance_mm=0.15))
    stricter = resolve_via_clearance(declared=DeclaredClearanceRules(net_class_clearance_mm=0.3))

    assert looser.required_mm == pytest.approx(0.15)
    assert stricter.required_mm == pytest.approx(0.3)


def test_the_fab_floor_raises_a_sub_floor_declaration_and_warns() -> None:
    """The #6191 ``max(designer value, fab floor)`` policy, never looser."""
    resolved = resolve_via_clearance(
        declared=DeclaredClearanceRules(dru_via_mm=0.1),
        fab_floor_mm=JLCPCB_FLOOR_MM,
        manufacturer="jlcpcb",
    )

    assert resolved.required_mm == pytest.approx(JLCPCB_FLOOR_MM)
    assert resolved.source is RuleSource.FAB_FLOOR
    assert resolved.warning is not None
    assert "below the jlcpcb minimum clearance" in resolved.warning
    assert "--via-clearance 0.1" in resolved.warning


def test_a_declaration_at_the_floor_keeps_its_provenance() -> None:
    """Board 05 declares exactly the jlcpcb floor: the tie stays the board's."""
    resolved = resolve_via_clearance(
        declared=DeclaredClearanceRules(project_min_clearance_mm=JLCPCB_FLOOR_MM),
        fab_floor_mm=JLCPCB_FLOOR_MM,
        manufacturer="jlcpcb",
    )

    assert resolved.required_mm == pytest.approx(JLCPCB_FLOOR_MM)
    assert resolved.source is RuleSource.PROJECT_MIN_CLEARANCE
    assert resolved.warning is None


def test_the_via_value_never_resolves_below_the_trace_base() -> None:
    """Step 9 widens the base; it never narrows it.

    Board 05 declares 0.127mm while the router's trace target is 0.15mm.
    ``ClearanceResolver.resolve`` answers a via pair with ``max(base, via)``,
    so ``DesignRules.via_clearance`` -- read alone by via-to-pad and
    via-to-via halos -- must not sit below the trace base either.
    """
    declared = DeclaredClearanceRules(project_min_clearance_mm=JLCPCB_FLOOR_MM)

    resolved = resolve_via_clearance(
        declared=declared, fab_floor_mm=JLCPCB_FLOOR_MM, trace_clearance_mm=0.15
    )

    assert resolved.required_mm == pytest.approx(0.15)
    assert resolved.source is RuleSource.TRACE_BASE
    # A tie with the trace base keeps the board's own provenance.
    tie = resolve_via_clearance(
        declared=DeclaredClearanceRules(net_class_clearance_mm=0.15),
        fab_floor_mm=JLCPCB_FLOOR_MM,
        trace_clearance_mm=0.15,
    )
    assert tie.source is RuleSource.PROJECT_NET_CLASS


def test_an_explicit_via_clearance_is_not_raised_to_the_trace_base() -> None:
    resolved = resolve_via_clearance(explicit_via_mm=0.1, trace_clearance_mm=0.15)

    assert resolved.required_mm == pytest.approx(0.1)
    assert resolved.source is RuleSource.EXPLICIT_TARGET


def test_nothing_declared_keeps_the_default() -> None:
    resolved = resolve_via_clearance(declared=None, fab_floor_mm=JLCPCB_FLOOR_MM)

    assert resolved.required_mm == pytest.approx(DEFAULT_VIA_CLEARANCE_MM)
    assert resolved.source is RuleSource.TARGET_DEFAULT
    assert resolved.warning is None


def test_nothing_declared_is_still_floored_by_a_coarse_fab() -> None:
    resolved = resolve_via_clearance(declared=None, fab_floor_mm=0.25, explicit_manufacturer=True)

    assert resolved.required_mm == pytest.approx(0.25)
    assert resolved.source is RuleSource.MANUFACTURER_OVERRIDE


def test_an_explicit_via_clearance_wins_and_warns_when_it_undercuts() -> None:
    declared = DeclaredClearanceRules(dru_via_mm=0.3)

    under = resolve_via_clearance(declared=declared, explicit_via_mm=0.2, fab_floor_mm=0.127)
    over = resolve_via_clearance(declared=declared, explicit_via_mm=0.35, fab_floor_mm=0.127)

    assert under.required_mm == pytest.approx(0.2)
    assert under.source is RuleSource.EXPLICIT_TARGET
    assert under.warning is not None and "--via-clearance 0.2mm" in under.warning
    assert over.required_mm == pytest.approx(0.35)
    assert over.warning is None


def test_the_trace_via_floor_still_takes_the_max() -> None:
    """#6272's both-orders floor is unchanged: a low via rule never loosens a trace."""
    assert trace_via_clearance_mm(0.15, 0.127) == pytest.approx(0.15)
    assert trace_via_clearance_mm(0.15, 0.3) == pytest.approx(0.3)


# ---------------------------------------------------------------------------
# (a) through the ``kct route`` resolution entry point
# ---------------------------------------------------------------------------


def _resolve_cli(pcb: Path, argv: list[str], **overrides):
    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    args = SimpleNamespace(clearance=0.15, manufacturer="jlcpcb", via_clearance=None)
    for key, value in overrides.items():
        setattr(args, key, value)
    _resolve_route_clearance(args, pcb, argv)
    return args


def test_route_cli_resolves_board02s_declared_via_rule() -> None:
    """Board 02's input declares a 0.15mm netclass: via clearance is 0.15, not 0.20."""
    repo = Path(__file__).resolve().parents[2]
    pcb = repo / "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"
    if not pcb.exists():
        pytest.skip("board 02 input not committed")

    args = _resolve_cli(pcb, ["--manufacturer", "jlcpcb"])

    assert args.clearance == pytest.approx(0.15)
    assert args.via_clearance == pytest.approx(0.15)
    assert args._via_clearance_rule_source == RuleSource.PROJECT_NET_CLASS.value


def test_route_cli_banner_names_the_via_rule(tmp_path, capsys) -> None:
    pcb = _board(tmp_path, project=_netclass_project(0.15), dru=VIA_RULE_DRU.format(mm=0.3))

    args = _resolve_cli(pcb, [])

    assert args.clearance == pytest.approx(0.15), "a via rule never moves the trace base"
    assert args.via_clearance == pytest.approx(0.3)
    out = capsys.readouterr().out
    assert "Via clearance: 0.3mm (from board .kicad_dru via clearance rule)" in out
    # The trace banner owns the "(rules:" marker; the via line must not add one.
    assert out.count("(rules:") == 1


def test_route_cli_prints_no_via_banner_for_the_default(tmp_path, capsys) -> None:
    """Nothing declared: 0.20mm, silently -- byte-identical to the pre-#6280 output."""
    args = _resolve_cli(_board(tmp_path), [])

    assert args.via_clearance == pytest.approx(DEFAULT_VIA_CLEARANCE_MM)
    assert "Via clearance" not in capsys.readouterr().out


def test_route_cli_quiet_suppresses_the_via_banner(tmp_path, capsys) -> None:
    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    pcb = _board(tmp_path, project=_netclass_project(0.15))
    args = SimpleNamespace(clearance=0.15, manufacturer="jlcpcb", via_clearance=None)
    _resolve_route_clearance(args, pcb, [], quiet=True)

    assert args.via_clearance == pytest.approx(0.15)
    assert "Via clearance" not in capsys.readouterr().out


def test_route_cli_sub_floor_project_rule_resolves_at_the_trace_base(tmp_path, capsys) -> None:
    """A project netclass below the fab floor: the 0.15mm trace base governs.

    The trace path floors that declaration at its target silently, so the via
    path does too -- no fab-floor warning about a value that decided nothing.
    """
    args = _resolve_cli(_board(tmp_path, project=_netclass_project(0.05)), [])

    assert args.clearance == pytest.approx(0.15)
    assert args.via_clearance == pytest.approx(0.15)
    assert args._via_clearance_rule_source == RuleSource.TRACE_BASE.value
    captured = capsys.readouterr()
    assert "below the jlcpcb minimum clearance" not in captured.err
    assert "Via clearance: 0.15mm (from the trace clearance" in captured.out


def test_a_via_scoped_rule_below_the_fab_floor_warns(tmp_path, capsys) -> None:
    """Only a via-specific sub-floor rule reaches the via warning on its own."""
    pcb = _board(tmp_path, dru=VIA_RULE_DRU.format(mm=0.05))

    args = _resolve_cli(pcb, [], clearance=0.1)

    assert args.via_clearance == pytest.approx(JLCPCB_FLOOR_MM)
    err = capsys.readouterr().err
    assert "declares via clearance 0.05mm" in err
    assert "--via-clearance 0.05" in err


def test_route_cli_sub_floor_warning_is_not_repeated(tmp_path, capsys) -> None:
    """A legacy board netclass below the floor warns once (trace path), not twice."""
    pcb = tmp_path / "legacy.kicad_pcb"
    pcb.write_text(
        "(kicad_pcb (version 20221018)\n"
        '  (net 0 "")\n  (net 1 "GND")\n'
        '  (net_class Default "default"\n'
        "    (clearance 0.05) (trace_width 0.25) (via_dia 0.8) (via_drill 0.4)\n"
        "    (add_net GND))\n"
        ")\n"
    )

    args = _resolve_cli(pcb, [])

    assert args.clearance == pytest.approx(JLCPCB_FLOOR_MM)
    assert args.via_clearance == pytest.approx(JLCPCB_FLOOR_MM)
    assert capsys.readouterr().err.count("below the jlcpcb minimum clearance") == 1


# ---------------------------------------------------------------------------
# (c) ``--via-clearance``
# ---------------------------------------------------------------------------


def test_explicit_via_clearance_flag_is_authoritative(tmp_path, capsys) -> None:
    pcb = _board(tmp_path, project=_netclass_project(0.15), dru=VIA_RULE_DRU.format(mm=0.3))

    args = _resolve_cli(pcb, ["--via-clearance", "0.2"], via_clearance=0.2)

    assert args.via_clearance == pytest.approx(0.2)
    captured = capsys.readouterr()
    assert "Via clearance: 0.2mm (from explicit --via-clearance flag)" in captured.out
    assert "explicit --via-clearance 0.2mm is below the 0.3mm" in captured.err


def test_via_clearance_flag_parses_on_both_parsers() -> None:
    from kicad_tools.cli.parser import create_parser
    from kicad_tools.cli.route_cmd import _route_parser

    inner = _route_parser().parse_args(["board.kicad_pcb", "--via-clearance", "0.18"])
    assert inner.via_clearance == pytest.approx(0.18)
    assert _route_parser().parse_args(["board.kicad_pcb"]).via_clearance is None

    outer = create_parser().parse_args(["route", "board.kicad_pcb", "--via-clearance", "0.18"])
    assert outer.via_clearance == pytest.approx(0.18)


def test_via_clearance_flag_is_forwarded_only_when_given(monkeypatch) -> None:
    """The shim forwards an explicit value and keeps flag-off argv unchanged."""
    from kicad_tools.cli import route_cmd
    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser

    seen: list[list[str]] = []
    monkeypatch.setattr(route_cmd, "main", lambda argv=None: seen.append(list(argv)) or 0)

    for extra in ([], ["--via-clearance", "0.18"]):
        monkeypatch.setattr("sys.argv", ["kct", "route", "board.kicad_pcb", *extra])
        run_route_command(create_parser().parse_args(["route", "board.kicad_pcb", *extra]))

    assert "--via-clearance" not in seen[0]
    flag = seen[1].index("--via-clearance")
    assert float(seen[1][flag + 1]) == pytest.approx(0.18)


# ---------------------------------------------------------------------------
# (b) End to end: a via rule above the trace rule still governs trace/via gaps
# ---------------------------------------------------------------------------

#: The foreign via sits this far off SIG's straight line, leaving a 0.17mm
#: edge-to-edge gap: legal under the board's 0.15mm netclass, illegal under
#: the old 0.20mm default and under a 0.30mm via rule.  Only the resolved via
#: rule decides whether the router may take the straight path.
_VIA_OFFSET_MM = 0.57
_VIA_DIAMETER = 0.6
_TRACE_WIDTH = 0.2


def _via_board(tmp_path: Path, via_rule_mm: float | None) -> Path:
    """SIG runs west-east past a lone GND via ``_VIA_OFFSET_MM`` off its line."""

    def pad(ref: str, uid: int, x: float, y: float, net: int, name: str) -> str:
        return f"""  (footprint "TestPoint:TP"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "TP" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{name}"))
  )
"""

    pcb = tmp_path / "via_rule.kicad_pcb"
    pcb.write_text(
        f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG")
  (net 2 "GND")
  (gr_rect (start 100 100) (end 130 110)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
{pad("TP1", 10, 104.0, 105, 1, "SIG")}{pad("TP2", 11, 126.0, 105, 1, "SIG")}{pad("TP3", 12, 115.0, 108.5, 2, "GND")}  (via (at 115 {105 + _VIA_OFFSET_MM}) (size {_VIA_DIAMETER}) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) (uuid "00000000-0000-0000-0000-000000000099"))
)
"""
    )
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(_netclass_project(0.15)))
    if via_rule_mm is not None:
        pcb.with_suffix(".kicad_dru").write_text(VIA_RULE_DRU.format(mm=via_rule_mm))
    return pcb


def _sig_gap_to_foreign_via(routed: Path) -> float:
    """Smallest edge-to-edge gap between SIG copper and the GND via."""
    from kicad_tools.sexp import parse_file

    tree = parse_file(routed)
    nets = {}
    for net in tree.find_all("net"):
        atoms = net.get_atoms()
        if len(atoms) >= 2:
            nets[str(atoms[1])] = int(atoms[0])
    sig = nets["SIG"]

    via_xy = (115.0, 105.0 + _VIA_OFFSET_MM)
    best = math.inf
    segments = 0
    for seg in tree.find_all("segment"):
        if int(seg.find_child("net").get_first_atom()) != sig:
            continue
        segments += 1
        sx, sy = (float(v) for v in seg.find_child("start").get_atoms()[:2])
        ex, ey = (float(v) for v in seg.find_child("end").get_atoms()[:2])
        width = float(seg.find_child("width").get_first_atom())
        dx, dy = ex - sx, ey - sy
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else ((via_xy[0] - sx) * dx + (via_xy[1] - sy) * dy) / length2
        t = max(0.0, min(1.0, t))
        px, py = sx + t * dx, sy + t * dy
        centre = math.hypot(via_xy[0] - px, via_xy[1] - py)
        best = min(best, centre - width / 2 - _VIA_DIAMETER / 2)
    assert segments, "SIG was not routed"
    return best


def _route(pcb: Path, out: Path) -> int:
    from kicad_tools.cli.route_cmd import main as route_main

    return route_main(
        [
            str(pcb),
            "-o",
            str(out),
            "--strategy",
            "basic",
            "--grid",
            "0.05",
            "--trace-width",
            str(_TRACE_WIDTH),
            "--no-auto-layers",
            "--no-auto-pour",
            # The foreign via is pre-existing copper: without this flag
            # ``kct route`` does not load it as an obstacle at all.
            "--preserve-existing",
            "--skip-drc",
            "--quiet",
        ]
    )


def test_a_dru_via_rule_above_the_trace_rule_keeps_traces_off_foreign_vias(tmp_path) -> None:
    """Issue acceptance item 3: the via rule, not the trace rule, governs the gap."""
    pcb = _via_board(tmp_path, via_rule_mm=0.3)
    out = tmp_path / "routed.kicad_pcb"

    _route(pcb, out)

    gap = _sig_gap_to_foreign_via(out)
    assert gap >= 0.3 - 1e-6, f"SIG passes {gap:.4f}mm from a foreign via under a 0.30mm rule"


def test_without_a_via_rule_the_netclass_governs_the_via_gap(tmp_path) -> None:
    """Control: with only the 0.15mm netclass declared, via clearance is 0.15.

    Before #6280 the router held this trace at the 0.20mm dataclass default
    even though nothing on the board asks for more than 0.15mm.
    """
    pcb = _via_board(tmp_path, via_rule_mm=None)
    out = tmp_path / "routed.kicad_pcb"

    _route(pcb, out)

    gap = _sig_gap_to_foreign_via(out)
    assert gap >= 0.15 - 1e-6
    assert gap < DEFAULT_VIA_CLEARANCE_MM, (
        f"SIG detoured to {gap:.4f}mm: the 0.20mm default still governs a board "
        "that declares 0.15mm"
    )


def test_a_second_resolution_does_not_treat_the_resolved_value_as_explicit(tmp_path) -> None:
    """``args.via_clearance`` carries the resolved value after the first pass."""
    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    pcb = _board(tmp_path, project=_netclass_project(0.15))
    args = _resolve_cli(pcb, [])
    assert args.via_clearance == pytest.approx(0.15)

    args.manufacturer = "oshpark"  # a later pass with a coarser fab floor
    _resolve_route_clearance(args, pcb, [], quiet=True)

    assert args.via_clearance == pytest.approx(0.152), "re-floored, not frozen"
    assert args._via_clearance_rule_source == RuleSource.FAB_FLOOR.value
