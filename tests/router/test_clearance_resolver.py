"""Unified clearance-rule resolver (Epic #5509 Phase 2, issue #5645).

Phase 1 shipped the exact-geometry kernel and measured every consumer against
``kicad-cli``; it deliberately changed no threshold.  Phase 2 is the first
phase that changes *which* threshold a consumer resolves, so these cases pin
the four things the phase promised:

* the ``#5398`` 0.15-vs-0.20 case resolves to **0.20** through every entry
  point (the resolver API, ``DesignRules``, and ``kct route``'s CLI path);
* ``resolve(a, b) == resolve(b, a)`` -- the order-asymmetry criterion;
* ``#4507``'s Python/C++ pairwise *scope* agrees with the resolver's output;
* the precedence order itself -- project DRU, project netclass, legacy board
  netclass, board minimum, fab-tier floor, pair matrix, trace/via symmetry.

The distinction from ``tests/router/test_clearance_kernel_fixtures.py``'s
``test_issue5398_is_rejected_in_both_insertion_orders`` matters: that case is
about **geometry** (does the kernel measure the same gap either way round),
which Phase 1 settled.  These cases are about **rule selection** -- whether
``required_mm`` is 0.15 or 0.20 in the first place.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.router.clearance_resolver import (
    ClearanceQuery,
    ClearanceResolver,
    ClearanceRuleSet,
    CopperKind,
    DeclaredClearanceRules,
    ResolvedClearance,
    RuleSource,
    read_declared_clearance_rules,
    resolve_base_clearance,
)
from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table
from kicad_tools.router.rules import DesignRules, NetClassRouting

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The named Phase 1 fixture for #5398: a board whose ``.kicad_pro`` board
#: minimum -- the value ``kicad-cli pcb drc`` measures against -- requires
#: 0.20mm, against a router target of 0.15mm.
ISSUE5398_PCB = (
    REPO_ROOT / "tests/fixtures/conformance/issue5398-seg-via-0p18-order-seed5398.kicad_pcb"
)

#: ``kct route``'s flat target (``cli/route_cmd.py: DEFAULT_ROUTE_CLEARANCE_MM``).
ROUTE_TARGET_MM = 0.15

#: The value #5398's board declares and native DRC enforces.
ISSUE5398_REQUIRED_MM = 0.20

ALL_KINDS = (CopperKind.TRACE, CopperKind.VIA, CopperKind.PAD, CopperKind.ZONE)


# ---------------------------------------------------------------------------
# Acceptance criterion 1: the #5398 0.15/0.20 case resolves to 0.20 everywhere
# ---------------------------------------------------------------------------


def test_issue5398_board_declares_the_stricter_requirement() -> None:
    """The fixture's rules really do say 0.20mm -- the premise, read from disk."""
    assert ISSUE5398_PCB.exists(), f"Phase 1 fixture missing: {ISSUE5398_PCB}"

    declared = read_declared_clearance_rules(ISSUE5398_PCB)

    assert declared.project_min_clearance_mm == pytest.approx(ISSUE5398_REQUIRED_MM)
    # Nothing legacy and no sidecar rule file: a PCB-only reader sees NOTHING
    # here, which is exactly why the pre-Phase-2 resolver returned 0.15.
    assert declared.board_net_class_mm is None
    assert declared.dru_mm is None


def test_issue5398_base_resolves_to_the_declared_requirement() -> None:
    """The defect itself: 0.15 in, 0.20 out, because the board says 0.20."""
    declared = read_declared_clearance_rules(ISSUE5398_PCB)

    resolved = resolve_base_clearance(
        target_mm=ROUTE_TARGET_MM,
        declared=declared,
        fab_floor_mm=0.1016,
        manufacturer="jlcpcb",
    )

    assert resolved.required_mm == pytest.approx(ISSUE5398_REQUIRED_MM)
    assert resolved.source is RuleSource.PROJECT_MIN_CLEARANCE
    assert resolved.warning is None
    # Provenance keeps the losing target term, so a banner can explain itself.
    assert (RuleSource.TARGET_DEFAULT, ROUTE_TARGET_MM) in resolved.terms


@pytest.mark.parametrize("kind_a", ALL_KINDS)
@pytest.mark.parametrize("kind_b", ALL_KINDS)
def test_issue5398_resolves_to_020_for_every_pair_kind(kind_a, kind_b) -> None:
    """ "Everywhere" means every copper pair kind, not just trace-vs-trace.

    A consumer that only ever asks about segments and a consumer that only
    ever asks about pads must get the same number out of the same resolver --
    that is what "no per-consumer divergence" has to mean operationally.
    """
    declared = read_declared_clearance_rules(ISSUE5398_PCB)
    base = resolve_base_clearance(target_mm=ROUTE_TARGET_MM, declared=declared)
    # via_clearance is the DesignRules default (0.20) -- the value #5398
    # recorded against the CLI's 0.15mm trace target, so no kind can drop
    # below it.
    resolver = ClearanceResolver(ClearanceRuleSet(base=base, via_mm=0.20))

    required = resolver.required_mm(net_a="/SENSE_A", net_b="/GND", kind_a=kind_a, kind_b=kind_b)

    assert required == pytest.approx(ISSUE5398_REQUIRED_MM)


def test_issue5398_resolves_to_020_through_the_route_cli() -> None:
    """The shipped entry point: ``kct route``'s own resolution path.

    ``args.clearance`` is what ``cli/route_cmd.py`` hands to
    ``DesignRules(trace_clearance=…)``, so this is the one assertion that
    covers every downstream consumer at once without touching any of them
    (their own arithmetic is Phase 3 / Phase 4).
    """
    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    args = SimpleNamespace(clearance=ROUTE_TARGET_MM, manufacturer="jlcpcb", quiet=True)
    resolved = _resolve_route_clearance(args, ISSUE5398_PCB, [str(ISSUE5398_PCB)], quiet=True)

    assert resolved == pytest.approx(ISSUE5398_REQUIRED_MM)
    assert args.clearance == pytest.approx(ISSUE5398_REQUIRED_MM)
    assert args._clearance_rule_source == "project-min-clearance"


def test_issue5398_explicit_manufacturer_does_not_waive_the_requirement() -> None:
    """A named fab tier selects the floor; it does not waive the board's rule.

    #4875 made an explicit ``--manufacturer`` suppress board-declared rules.
    On this board that is #5398 exactly: 0.15mm copper on a 0.20mm board.
    """
    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    args = SimpleNamespace(clearance=ROUTE_TARGET_MM, manufacturer="jlcpcb", quiet=True)
    resolved = _resolve_route_clearance(
        args,
        ISSUE5398_PCB,
        [str(ISSUE5398_PCB), "--manufacturer", "jlcpcb"],
        quiet=True,
    )

    assert resolved == pytest.approx(ISSUE5398_REQUIRED_MM)


def test_issue5398_explicit_clearance_still_wins_but_warns() -> None:
    """``--clearance`` remains the one deliberate waiver -- and says so."""
    resolved = resolve_base_clearance(
        target_mm=ROUTE_TARGET_MM,
        declared=read_declared_clearance_rules(ISSUE5398_PCB),
        explicit_target=True,
    )

    assert resolved.required_mm == pytest.approx(ROUTE_TARGET_MM)
    assert resolved.source is RuleSource.EXPLICIT_TARGET
    assert resolved.warning is not None
    assert "0.2mm this board declares" in resolved.warning


def test_issue5398_explicit_clearance_warns_through_the_route_cli(capsys) -> None:
    """Issue #5656: the advisory above must reach the shipped CLI's stderr.

    The case one line up exercises :func:`resolve_base_clearance` directly.
    ``kct route`` used to skip reading the board's declared rules whenever
    ``--clearance`` was passed, so ``declared`` reached the resolver as
    ``None`` and the warning branch could never fire on the path an operator
    actually runs -- the value was right and the advisory was silently lost.
    """
    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    args = SimpleNamespace(clearance=ROUTE_TARGET_MM, manufacturer="jlcpcb", quiet=True)
    resolved = _resolve_route_clearance(
        args,
        ISSUE5398_PCB,
        [str(ISSUE5398_PCB), "--clearance", str(ROUTE_TARGET_MM)],
        quiet=True,
    )

    # The value is untouched: ``--clearance`` is still the one deliberate waiver.
    assert resolved == pytest.approx(ROUTE_TARGET_MM)
    assert args.clearance == pytest.approx(ROUTE_TARGET_MM)
    assert args._clearance_rule_source == "flag"

    captured = capsys.readouterr()
    assert "WARNING: explicit --clearance" in captured.err
    assert "0.2mm this board declares" in captured.err


def test_explicit_clearance_at_or_above_the_declared_rule_stays_silent(capsys) -> None:
    """No false positive: nothing is undercut, so nothing is warned about."""
    from kicad_tools.cli.route_cmd import _resolve_route_clearance

    above = ISSUE5398_REQUIRED_MM + 0.05
    args = SimpleNamespace(clearance=above, manufacturer="jlcpcb", quiet=True)
    resolved = _resolve_route_clearance(
        args, ISSUE5398_PCB, [str(ISSUE5398_PCB), "--clearance", str(above)], quiet=True
    )

    assert resolved == pytest.approx(above)
    assert capsys.readouterr().err == ""

    # And on the exact tie, where the operator matched the board's own rule.
    args = SimpleNamespace(clearance=ISSUE5398_REQUIRED_MM, manufacturer="jlcpcb", quiet=True)
    resolved = _resolve_route_clearance(
        args,
        ISSUE5398_PCB,
        [str(ISSUE5398_PCB), "--clearance", str(ISSUE5398_REQUIRED_MM)],
        quiet=True,
    )

    assert resolved == pytest.approx(ISSUE5398_REQUIRED_MM)
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Acceptance criterion 2: order asymmetry
# ---------------------------------------------------------------------------


def _asymmetry_resolver() -> ClearanceResolver:
    """A resolver with every pair-level layer armed at a *different* value.

    Each layer contributes a distinct number, so a term that silently read
    only one side of the pair would show up as an order-dependent answer
    rather than being masked by a tie.
    """
    table = build_pairwise_clearance_table({"/AC_LINE": 150.0, "/GND": 0.0, "/SENSE": 0.0}, dru=0.2)
    net_classes = {
        "/AC_LINE": NetClassRouting(name="HV", clearance=0.9),
        "/GND": NetClassRouting(name="Power", clearance=0.3),
        # /SENSE deliberately unclassified -- the None branch must be
        # order-independent too.
    }
    base = ResolvedClearance(0.2, RuleSource.TARGET_DEFAULT, ((RuleSource.TARGET_DEFAULT, 0.2),))
    return ClearanceResolver(
        ClearanceRuleSet(
            base=base,
            via_mm=0.45,
            net_classes=net_classes,
            pair_table=table,
        )
    )


@pytest.mark.parametrize(
    "net_a,net_b",
    [
        ("/AC_LINE", "/GND"),
        ("/AC_LINE", "/SENSE"),
        ("/GND", "/SENSE"),
        ("/AC_LINE", "/AC_LINE"),
        ("/SENSE", ""),
        ("", ""),
    ],
)
@pytest.mark.parametrize("kind_a", ALL_KINDS)
@pytest.mark.parametrize("kind_b", ALL_KINDS)
def test_resolution_is_independent_of_argument_order(net_a, net_b, kind_a, kind_b) -> None:
    """The order-asymmetry criterion, over the full pair matrix.

    #5398's Python commit gate accepted a 0.18mm gap when the via existed
    first and rejected it when the segment did; the resolver must not be able
    to reproduce that class of bug at the *rule* layer.
    """
    resolver = _asymmetry_resolver()

    forward = resolver.resolve(
        ClearanceQuery(net_a=net_a, net_b=net_b, kind_a=kind_a, kind_b=kind_b)
    )
    reverse = resolver.resolve(
        ClearanceQuery(net_a=net_b, net_b=net_a, kind_a=kind_b, kind_b=kind_a)
    )

    assert forward.required_mm == pytest.approx(reverse.required_mm)
    assert forward.source is reverse.source


def test_canonical_form_is_idempotent_and_order_free() -> None:
    """``canonical()`` maps both orders of a pair onto the same query."""
    a = ClearanceQuery(net_a="/GND", net_b="/AC_LINE", kind_a=CopperKind.VIA)
    b = ClearanceQuery(net_a="/AC_LINE", net_b="/GND", kind_b=CopperKind.VIA)

    assert a.canonical() == b.canonical()
    assert a.canonical().canonical() == a.canonical()


# ---------------------------------------------------------------------------
# Acceptance criterion 3: #4507's Python/C++ pairwise scope
# ---------------------------------------------------------------------------

HV_NET_ID = 1
LV_NET_ID = 2
HV_TAP_NET_ID = 3
NET_IDS = {"/AC_LINE": HV_NET_ID, "/GND": LV_NET_ID, "/AC_LINE_TAP": HV_TAP_NET_ID}
HV_VOLTAGES = {"/AC_LINE": 150.0, "/GND": 0.0, "/AC_LINE_TAP": 150.0}
#: IEC 60664-1, PD2, material group IIIa @ 150 V, as pinned by
#: ``tests/router/test_pairwise_cpp_parity.py``.
IEC_150V_PD2_IIIA_MM = 1.6
HV_DRU_MM = 0.2


def _hv_resolver(dru: float = HV_DRU_MM) -> ClearanceResolver:
    base = ResolvedClearance(dru, RuleSource.TARGET_DEFAULT, ((RuleSource.TARGET_DEFAULT, dru),))
    return ClearanceResolver(
        ClearanceRuleSet(
            base=base,
            via_mm=dru,
            pair_table=build_pairwise_clearance_table(HV_VOLTAGES, dru=dru),
        )
    )


@pytest.mark.parametrize("kind_a", ALL_KINDS)
@pytest.mark.parametrize("kind_b", ALL_KINDS)
def test_pair_matrix_scope_is_copper_kind_independent(kind_a, kind_b) -> None:
    """#4507's scope gap, at the rule layer.

    The C++ pairwise path walks **all** copper (segments, vias, pads) while
    Phase 1's Python predicate was trace-vs-trace only, so "the copper this
    board fails on is invisible to both by construction".  The unified
    resolver's pairwise term is a property of the *net pair*, so it cannot
    be narrower for one copper kind than another.
    """
    resolver = _hv_resolver()

    required = resolver.required_mm(net_a="/AC_LINE", net_b="/GND", kind_a=kind_a, kind_b=kind_b)

    assert required == pytest.approx(IEC_150V_PD2_IIIA_MM)


def test_resolver_agrees_with_the_python_pairwise_table() -> None:
    """The resolver does not re-derive the matrix -- it composes it."""
    table = build_pairwise_clearance_table(HV_VOLTAGES, dru=HV_DRU_MM)
    resolver = _hv_resolver()

    for net_a in HV_VOLTAGES:
        for net_b in HV_VOLTAGES:
            assert resolver.required_mm(net_a=net_a, net_b=net_b) == pytest.approx(
                table.required_clearance(net_a, net_b)
            )


def test_resolver_matches_the_cpp_pairwise_scope() -> None:
    """Parity with ``Grid3D::pairwise_required_clearance``'s composition.

    The C++ gates apply ``max(effective_scalar, matrix[dom_a][dom_b])`` over
    every copper pair they walk (``grid.cpp``'s ``required`` lambdas).  The
    resolver must produce that same number from net *names*, so a Phase 3
    consumer can stop computing it locally.
    """
    from kicad_tools.router.cpp_backend import is_cpp_available
    from kicad_tools.router.pairwise_clearance import build_cpp_domain_matrix

    if not is_cpp_available():
        pytest.skip("C++ router backend not available")

    from kicad_tools.router import router_cpp

    table = build_pairwise_clearance_table(HV_VOLTAGES, dru=HV_DRU_MM)
    domains = build_cpp_domain_matrix(table, NET_IDS)
    assert domains is not None

    grid = router_cpp.Grid3D(200, 200, 2, 0.1, 0.0, 0.0)
    grid.set_pairwise_domains(domains.net_to_domain, domains.matrix)
    assert grid.pairwise_active is True

    resolver = _hv_resolver()
    for name_a, id_a in NET_IDS.items():
        for name_b, id_b in NET_IDS.items():
            cpp_pair = grid.pairwise_required_clearance(id_a, id_b)
            cpp_effective = max(HV_DRU_MM, cpp_pair)
            assert resolver.required_mm(net_a=name_a, net_b=name_b) == pytest.approx(
                cpp_effective, abs=1e-6
            ), f"{name_a} vs {name_b}"


# ---------------------------------------------------------------------------
# Trace/via symmetry
# ---------------------------------------------------------------------------


def _via_symmetry_resolver() -> ClearanceResolver:
    base = ResolvedClearance(0.15, RuleSource.TARGET_DEFAULT, ((RuleSource.TARGET_DEFAULT, 0.15),))
    return ClearanceResolver(ClearanceRuleSet(base=base, via_mm=0.20))


@pytest.mark.parametrize("other", [CopperKind.TRACE, CopperKind.PAD, CopperKind.ZONE])
def test_a_via_on_either_side_raises_the_requirement(other) -> None:
    """#5398's trace-vs-via asymmetry, resolved once for the unordered pair.

    ``kct route`` builds its ``DesignRules`` with ``trace_clearance`` set from
    ``route_cmd.DEFAULT_ROUTE_CLEARANCE_MM`` (0.15mm) while ``via_clearance``
    keeps the dataclass default of 0.20mm -- ``router/rules.py`` ships *both*
    fields at 0.2, so the 0.15 is the CLI's target, never a ``DesignRules``
    default.  Consumers disagreed about which of the two applied depending on
    which object they were called *about*.
    """
    resolver = _via_symmetry_resolver()

    assert resolver.required_mm(kind_a=CopperKind.VIA, kind_b=other) == pytest.approx(0.20)
    assert resolver.required_mm(kind_a=other, kind_b=CopperKind.VIA) == pytest.approx(0.20)
    assert resolver.required_mm(kind_a=other, kind_b=other) == pytest.approx(0.15)


def test_via_clearance_below_the_base_cannot_tighten_a_pair() -> None:
    """Every pair-level layer is a widening ``max``, never a relaxation."""
    base = ResolvedClearance(0.3, RuleSource.PROJECT_MIN_CLEARANCE, ())
    resolver = ClearanceResolver(ClearanceRuleSet(base=base, via_mm=0.1))

    assert resolver.required_mm(kind_a=CopperKind.VIA) == pytest.approx(0.3)


def test_edge_pairs_are_answered_with_the_base_alone() -> None:
    """Copper-to-edge is a different KiCad constraint, not folded in here."""
    resolver = _via_symmetry_resolver()

    assert resolver.required_mm(kind_a=CopperKind.VIA, kind_b=CopperKind.EDGE) == pytest.approx(
        0.15
    )


# ---------------------------------------------------------------------------
# Per-net-class overrides
# ---------------------------------------------------------------------------


def test_net_class_override_takes_the_stricter_of_the_two_nets() -> None:
    base = ResolvedClearance(0.2, RuleSource.TARGET_DEFAULT, ())
    resolver = ClearanceResolver(
        ClearanceRuleSet(
            base=base,
            net_classes={
                "/HV": NetClassRouting(name="HV", clearance=0.8),
                "/GND": NetClassRouting(name="Power", clearance=0.25),
            },
        )
    )

    assert resolver.required_mm(net_a="/HV", net_b="/GND") == pytest.approx(0.8)
    assert resolver.required_mm(net_a="/GND", net_b="/HV") == pytest.approx(0.8)
    assert resolver.required_mm(net_a="/GND", net_b="/SIG") == pytest.approx(0.25)
    assert resolver.required_mm(net_a="/SIG", net_b="/OTHER") == pytest.approx(0.2)


def test_design_rules_bridge_carries_every_layer() -> None:
    """``DesignRules.clearance_resolver()`` is the consumer-facing entry point."""
    rules = DesignRules(
        trace_clearance=0.18,
        via_clearance=0.25,
        pairwise_clearance=build_pairwise_clearance_table(HV_VOLTAGES, dru=0.18),
    )
    resolver = rules.clearance_resolver({"/SIG": NetClassRouting(name="S", clearance=0.22)})

    assert resolver.required_mm() == pytest.approx(0.18)
    assert resolver.required_mm(kind_b=CopperKind.VIA) == pytest.approx(0.25)
    assert resolver.required_mm(net_a="/SIG", net_b="/OTHER") == pytest.approx(0.22)
    assert resolver.required_mm(net_a="/AC_LINE", net_b="/GND") == pytest.approx(
        IEC_150V_PD2_IIIA_MM
    )


# ---------------------------------------------------------------------------
# Precedence order
# ---------------------------------------------------------------------------


def test_dru_outranks_the_project_minimum() -> None:
    """A board ``.kicad_dru`` rule is more specific than the board minimum."""
    declared = DeclaredClearanceRules(dru_mm=0.35, project_min_clearance_mm=0.2)

    assert declared.project_requirement() == (0.35, RuleSource.PROJECT_DRU, None)


def test_board_minimum_clamps_a_looser_dru_rule_upward() -> None:
    """KiCad's board minimum clamps every rule upward -- so does this."""
    declared = DeclaredClearanceRules(dru_mm=0.1, project_min_clearance_mm=0.25)

    value, source, _name = declared.project_requirement()

    assert value == pytest.approx(0.25)
    assert source is RuleSource.PROJECT_MIN_CLEARANCE


def test_a_project_minimum_below_the_target_cannot_loosen_it() -> None:
    """The #5398 divergence in the other direction, and why these are floors.

    Every in-repo board whose project declares a minimum declares one at or
    below the 0.15mm route target (board 05 declares 0.127mm).  Treating a
    declared *minimum* as a replacement -- the shape #4875 gave the legacy
    ``.kicad_pcb`` layer -- would silently re-space those boards looser than
    the operator asked for, which is the same class of per-invocation
    divergence this phase exists to remove.
    """
    resolved = resolve_base_clearance(
        target_mm=ROUTE_TARGET_MM,
        declared=DeclaredClearanceRules(project_min_clearance_mm=0.127),
        fab_floor_mm=0.1016,
    )

    assert resolved.required_mm == pytest.approx(ROUTE_TARGET_MM)
    assert resolved.source is RuleSource.TARGET_DEFAULT
    assert resolved.warning is None


def test_the_project_netclass_is_a_floor_now_that_the_writer_means_it(tmp_path) -> None:
    """#5654: ``net_settings.classes[].clearance`` is read, as a **floor**.

    Phase 2 refused to read it because KiCad's stock template ships
    ``Default`` at 0.20mm and every project this repo wrote inherited that
    verbatim -- a template default, not a declaration (honouring it moved
    board 04's post-route violation count from 24 to 28 at an unchanged 9/9
    nets).  #5654 fixed the writer first
    (``core/project_file.py:DEFAULT_NETCLASS_CLEARANCE_MM``), so the field now
    states the clearance the board is actually routed at -- and it is the
    value KiCad's own ``clearance`` DRC test measures against.
    """
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")
    pcb.with_suffix(".kicad_pro").write_text(
        '{"net_settings": {"classes": [{"name": "Default", "clearance": 0.2}]}}'
    )

    declared = read_declared_clearance_rules(pcb)

    assert declared.net_class_clearance_mm == pytest.approx(0.2)
    assert declared.net_class_name == "Default"
    assert not declared.is_empty()
    assert declared.project_requirement() == (0.2, RuleSource.PROJECT_NET_CLASS, "Default")

    resolved = resolve_base_clearance(target_mm=ROUTE_TARGET_MM, declared=declared)
    assert resolved.required_mm == pytest.approx(0.2)
    assert resolved.source is RuleSource.PROJECT_NET_CLASS
    assert resolved.source_label == "Default"


def test_a_project_netclass_below_the_target_never_loosens_it(tmp_path) -> None:
    """Floor, not replacement -- the #5398 divergence in the other direction.

    A tier-relaxed project (``kct route --mfr jlcpcb-tier1`` rewrites the
    ``Default`` netclass to the 0.1016mm fab floor) declares *less* than the
    router's 0.15mm target.  Honouring that as a replacement would silently
    re-space the board downward; as a floor it changes nothing.
    """
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")
    pcb.with_suffix(".kicad_pro").write_text(
        '{"net_settings": {"classes": [{"name": "Default", "clearance": 0.1016}]}}'
    )

    declared = read_declared_clearance_rules(pcb)
    assert declared.net_class_clearance_mm == pytest.approx(0.1016)

    resolved = resolve_base_clearance(target_mm=ROUTE_TARGET_MM, declared=declared)
    assert resolved.required_mm == pytest.approx(ROUTE_TARGET_MM)
    assert resolved.source is RuleSource.TARGET_DEFAULT


def test_a_project_with_no_netclass_block_declares_nothing(tmp_path) -> None:
    """The field is optional: absent / malformed contributes nothing."""
    for project_json in (
        "{}",
        '{"net_settings": {}}',
        '{"net_settings": {"classes": []}}',
        '{"net_settings": {"classes": [{"name": "Default"}]}}',
        '{"net_settings": {"classes": "not-a-list"}}',
        '{"net_settings": {"classes": [{"clearance": 0.3}]}}',  # unnamed class
    ):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20221018))\n")
        pcb.with_suffix(".kicad_pro").write_text(project_json)

        declared = read_declared_clearance_rules(pcb)
        assert declared.net_class_clearance_mm is None, project_json
        assert declared.is_empty(), project_json


@pytest.mark.parametrize(
    "net_class_mm,other,expected_mm,expected_source",
    [
        # The netclass competes with the board minimum the same way the
        # board minimum competes with the DRU rule: highest wins.
        (0.25, {"project_min_clearance_mm": 0.20}, 0.25, RuleSource.PROJECT_NET_CLASS),
        (0.18, {"project_min_clearance_mm": 0.20}, 0.20, RuleSource.PROJECT_MIN_CLEARANCE),
        (0.25, {"dru_mm": 0.20}, 0.25, RuleSource.PROJECT_NET_CLASS),
        (0.18, {"dru_mm": 0.20}, 0.20, RuleSource.PROJECT_DRU),
        # Exact ties resolve by RuleSource declaration order, so provenance
        # never flips between runs.
        (0.20, {"project_min_clearance_mm": 0.20}, 0.20, RuleSource.PROJECT_MIN_CLEARANCE),
        (0.20, {"dru_mm": 0.20}, 0.20, RuleSource.PROJECT_DRU),
        (0.20, {}, 0.20, RuleSource.PROJECT_NET_CLASS),
    ],
)
def test_the_netclass_competes_with_the_other_project_minima(
    net_class_mm: float,
    other: dict,
    expected_mm: float,
    expected_source: RuleSource,
) -> None:
    declared = DeclaredClearanceRules(
        net_class_clearance_mm=net_class_mm, net_class_name="Default", **other
    )

    requirement = declared.project_requirement()
    assert requirement is not None
    assert requirement[0] == pytest.approx(expected_mm)
    assert requirement[1] is expected_source

    resolved = resolve_base_clearance(target_mm=ROUTE_TARGET_MM, declared=declared)
    assert resolved.required_mm == pytest.approx(expected_mm)


def test_a_nonpositive_project_netclass_is_ignored() -> None:
    """A zero/negative netclass clearance is not a requirement of any kind."""
    for value in (0.0, -0.1):
        declared = DeclaredClearanceRules(net_class_clearance_mm=value, net_class_name="Default")
        assert declared.project_requirement() is None
        assert declared.is_empty()


def test_the_fab_floor_still_outranks_a_looser_project_netclass() -> None:
    """The fab floor is the last ``max``; a netclass below it is warned about."""
    declared = DeclaredClearanceRules(net_class_clearance_mm=0.05, net_class_name="Default")

    resolved = resolve_base_clearance(
        target_mm=0.04,
        declared=declared,
        fab_floor_mm=0.127,
        manufacturer="jlcpcb",
    )

    assert resolved.required_mm == pytest.approx(0.127)
    assert resolved.warning is not None
    assert "below the jlcpcb minimum clearance" in resolved.warning


def test_an_explicit_target_still_overrides_the_project_netclass(tmp_path) -> None:
    """``--clearance`` remains the one deliberate waiver (precedence step 1)."""
    declared = DeclaredClearanceRules(net_class_clearance_mm=0.30, net_class_name="Default")

    resolved = resolve_base_clearance(target_mm=0.15, declared=declared, explicit_target=True)

    assert resolved.required_mm == pytest.approx(0.15)
    assert resolved.source is RuleSource.EXPLICIT_TARGET
    assert resolved.warning is not None
    assert "0.3mm this board declares" in resolved.warning


def test_legacy_board_netclass_replaces_the_target_in_both_directions() -> None:
    """#4875's layer keeps replacement semantics; Phase 2 does not revisit it."""
    for declared_mm in (0.13, 0.254):
        resolved = resolve_base_clearance(
            target_mm=ROUTE_TARGET_MM,
            declared=DeclaredClearanceRules(
                board_net_class_mm=declared_mm, board_net_class_name="Default"
            ),
            fab_floor_mm=0.127,
            manufacturer="jlcpcb",
        )

        assert resolved.required_mm == pytest.approx(declared_mm)
        assert resolved.source is RuleSource.BOARD_NET_CLASS
        assert resolved.source_label == "Default"
        assert resolved.warning is None


def test_a_project_minimum_still_floors_a_legacy_board_netclass() -> None:
    """The two layers compose: the legacy value replaces, the minimum floors."""
    resolved = resolve_base_clearance(
        target_mm=ROUTE_TARGET_MM,
        declared=DeclaredClearanceRules(
            board_net_class_mm=0.13,
            board_net_class_name="Default",
            project_min_clearance_mm=0.22,
        ),
    )

    assert resolved.required_mm == pytest.approx(0.22)
    assert resolved.source is RuleSource.PROJECT_MIN_CLEARANCE


def test_nothing_declared_leaves_the_target_untouched() -> None:
    """The fleet path: no declared rule, byte-identical to the flat default."""
    resolved = resolve_base_clearance(
        target_mm=ROUTE_TARGET_MM, declared=DeclaredClearanceRules(), fab_floor_mm=0.1016
    )

    assert resolved.required_mm == pytest.approx(ROUTE_TARGET_MM)
    assert resolved.source is RuleSource.TARGET_DEFAULT
    assert resolved.warning is None


def test_declared_value_below_the_fab_floor_is_raised_and_warned() -> None:
    """#4875's "ambiguity #2" decision, preserved by the unified resolver."""
    resolved = resolve_base_clearance(
        target_mm=ROUTE_TARGET_MM,
        declared=DeclaredClearanceRules(board_net_class_mm=0.05, board_net_class_name="Default"),
        fab_floor_mm=0.127,
        manufacturer="jlcpcb",
    )

    assert resolved.required_mm == pytest.approx(0.127)
    assert resolved.source is RuleSource.FAB_FLOOR
    assert resolved.warning is not None
    assert "below the jlcpcb minimum clearance" in resolved.warning
    assert "--clearance 0.05" in resolved.warning


def test_explicit_manufacturer_names_the_floor_it_contributed() -> None:
    """Provenance, not value: the number is identical either way."""
    kwargs = {
        "target_mm": 0.1,
        "declared": DeclaredClearanceRules(),
        "fab_floor_mm": 0.127,
    }

    named = resolve_base_clearance(explicit_manufacturer=True, **kwargs)
    unnamed = resolve_base_clearance(**kwargs)

    assert named.required_mm == unnamed.required_mm == pytest.approx(0.127)
    assert named.source is RuleSource.MANUFACTURER_OVERRIDE
    assert unnamed.source is RuleSource.FAB_FLOOR


def test_provenance_ties_break_deterministically() -> None:
    """Equal-valued terms must not make ``source`` depend on evaluation order."""
    base = ResolvedClearance(0.2, RuleSource.TARGET_DEFAULT, ())
    resolver = ClearanceResolver(
        ClearanceRuleSet(
            base=base,
            via_mm=0.2,
            net_classes={"/SIG": NetClassRouting(name="S", clearance=0.2)},
        )
    )

    resolved = resolver.resolve(ClearanceQuery(net_a="/SIG", net_b="/GND", kind_b=CopperKind.VIA))

    assert resolved.required_mm == pytest.approx(0.2)
    # TARGET_DEFAULT precedes both NET_CLASS_OVERRIDE and VIA_CLEARANCE in
    # RuleSource declaration order, so it keeps the attribution on a tie.
    assert resolved.source is RuleSource.TARGET_DEFAULT


# ---------------------------------------------------------------------------
# Reading the board's own files
# ---------------------------------------------------------------------------


def test_dru_reader_takes_the_strictest_unconditional_rule(tmp_path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")
    pcb.with_suffix(".kicad_dru").write_text(
        "(version 1)\n"
        '(rule "Clearance - loose"\n'
        "  (constraint clearance (min 0.1016mm)))\n"
        '(rule "Clearance - strict"\n'
        "  (constraint clearance (min 0.3mm)))\n"
    )

    assert read_declared_clearance_rules(pcb).dru_mm == pytest.approx(0.3)


def test_dru_reader_skips_conditional_rules(tmp_path) -> None:
    """A conditional rule needs a DRC-expression evaluator we do not have.

    Applying it board-wide would over-constrain every pair the condition
    excludes, which is a worse failure than naming the gap.
    """
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")
    pcb.with_suffix(".kicad_dru").write_text(
        "(version 1)\n"
        '(rule "HV only"\n'
        "  (condition \"A.NetClass == 'HV'\")\n"
        "  (constraint clearance (min 2.5mm)))\n"
        '(rule "Board wide"\n'
        "  (constraint clearance (min 0.2mm)))\n"
    )

    assert read_declared_clearance_rules(pcb).dru_mm == pytest.approx(0.2)


def test_dru_reader_ignores_other_constraint_kinds(tmp_path) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")
    pcb.with_suffix(".kicad_dru").write_text(
        "(version 1)\n"
        '(rule "Track Width"\n'
        "  (constraint track_width (min 0.8mm)))\n"
        '(rule "Edge"\n'
        "  (constraint edge_clearance (min 0.5mm)))\n"
    )

    assert read_declared_clearance_rules(pcb).dru_mm is None


def test_shipped_fab_rule_files_parse(tmp_path) -> None:
    """The reader must handle the real generated sidecars, not just fixtures."""
    from kicad_tools.router.clearance_resolver import _read_dru_clearance

    shipped = REPO_ROOT / "src/kicad_tools/manufacturers/rules/jlcpcb-4layer-1oz.kicad_dru"
    assert shipped.exists()

    # The JLCPCB sidecar's only unconditional clearance rule is 0.1016mm; its
    # hole_clearance rules are conditional and must not leak in.
    assert _read_dru_clearance(shipped) == pytest.approx(0.1016)


def test_missing_and_malformed_sidecars_declare_nothing(tmp_path) -> None:
    """Rule derivation is an enhancement, never a new failure mode."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")

    assert read_declared_clearance_rules(pcb).is_empty()

    pcb.with_suffix(".kicad_pro").write_text("{not json")
    pcb.with_suffix(".kicad_dru").write_text("(rule clearance (min")  # unbalanced

    assert read_declared_clearance_rules(pcb).is_empty()


@pytest.mark.parametrize(
    "project_json,expected",
    [
        ('{"board": {"design_settings": {"rules": {"min_clearance": 0.2}}}}', 0.2),
        # KiCad writes ``defaults.clearance_min`` alongside the rule block; a
        # project carrying only that half still declares a board minimum.
        ('{"board": {"design_settings": {"defaults": {"clearance_min": 0.18}}}}', 0.18),
        # Both present and disagreeing: KiCad enforces both, so take the max.
        (
            '{"board": {"design_settings": {"rules": {"min_clearance": 0.15},'
            ' "defaults": {"clearance_min": 0.22}}}}',
            0.22,
        ),
    ],
)
def test_project_board_minimum_is_read_from_either_block(
    tmp_path, project_json: str, expected: float
) -> None:
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")
    pcb.with_suffix(".kicad_pro").write_text(project_json)

    assert read_declared_clearance_rules(pcb).project_min_clearance_mm == pytest.approx(expected)


def test_shipped_boards_declare_only_minima_they_mean(tmp_path) -> None:
    """The in-repo evidence that no shipped board carries a template default.

    Before #5654 boards 00-04 shipped KiCad's stock ``Default`` 0.20mm
    netclass and no board minimum at all, which is exactly why Phase 2
    refused to read the netclass.  The writer now emits the clearance the
    board is actually routed at (0.15mm), so every shipped board declares a
    value it means -- and the fleet resolves to the same 0.15mm target it
    always did.  If a board ever reverts to the stock 0.20mm, this fails and
    the "read the netclass" decision needs revisiting.
    """
    from kicad_tools.core.project_file import DEFAULT_NETCLASS_CLEARANCE_MM

    writer_default = REPO_ROOT / "boards/00-simple-led/output/simple_led.kicad_pcb"
    declares_minimum = REPO_ROOT / "boards/06-diffpair-test/output/diffpair_test.kicad_pcb"
    for path in (writer_default, declares_minimum):
        if not path.exists():
            pytest.skip(f"board input not committed: {path}")

    stock = read_declared_clearance_rules(writer_default)
    assert stock.project_min_clearance_mm is None
    assert stock.net_class_clearance_mm == pytest.approx(DEFAULT_NETCLASS_CLEARANCE_MM)
    assert stock.net_class_clearance_mm == pytest.approx(ROUTE_TARGET_MM)
    assert resolve_base_clearance(
        target_mm=ROUTE_TARGET_MM, declared=stock
    ).required_mm == pytest.approx(ROUTE_TARGET_MM)

    declares = read_declared_clearance_rules(declares_minimum)
    assert declares.project_min_clearance_mm == pytest.approx(0.15)
    assert declares.net_class_clearance_mm == pytest.approx(0.15)


def test_legacy_board_netclasses_are_still_read(tmp_path) -> None:
    """#4875's layer survives, on its own footing (replace, not floor)."""
    pcb = tmp_path / "board.kicad_pcb"
    pcb.write_text("(kicad_pcb (version 20221018))\n")

    declared = read_declared_clearance_rules(
        pcb, board_net_classes={"Default": SimpleNamespace(clearance=0.254)}
    )

    assert declared.board_net_class_mm == pytest.approx(0.254)
    assert declared.board_requirement() == (0.254, RuleSource.BOARD_NET_CLASS, "Default")
    assert declared.project_requirement() is None


# ---------------------------------------------------------------------------
# Phase 2 is a resolver-unification phase, not a threshold-change phase
# ---------------------------------------------------------------------------


def test_phase2_changes_no_threshold_constant() -> None:
    """The epic-wide constraint, pinned where a reviewer can see it.

    Phase 2 may change *which* value is resolved; it may not change any of
    the values themselves.  These are the three constants the issue names.
    """
    from kicad_tools.cli.route_cmd import DEFAULT_ROUTE_CLEARANCE_MM
    from kicad_tools.router.mfr_limits import resolve_clearance

    assert DEFAULT_ROUTE_CLEARANCE_MM == 0.15
    assert DesignRules().trace_clearance == 0.2
    assert DesignRules().via_clearance == 0.2
    assert resolve_clearance("jlcpcb") == pytest.approx(0.127)
    assert resolve_clearance("oshpark") == pytest.approx(0.152)
