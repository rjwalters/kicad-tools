"""Regression test for bidirectional drift between ``kct check`` CLI
dispatcher and :meth:`DRCChecker.check_all` (Issue #3046).

Before the fix, the two code paths each silently omitted a check the
other ran:

* ``check_all`` invoked ``check_via_in_pad`` but the CLI dispatcher's
  ``check_methods`` dict omitted ``via_in_pad`` -- so ``kct check``
  could not surface in-pad-via errors that the audit/export path
  flagged.  This is the primary leak the issue is about.
* The CLI dispatcher invoked ``check_pad_grid_alignment`` but
  ``check_all`` omitted it -- so the library API ran a different set
  of checks than the CLI.

The fix closes both gaps and adds the invariant test below, which fails
on ``main`` (pre-fix) and passes after the dispatcher dict gains
``via_in_pad`` AND ``check_all`` gains ``check_pad_grid_alignment``.

To enable the introspection, ``check_all`` now drives its method calls
from :attr:`DRCChecker.CHECK_ALL_METHODS` (a class-level tuple).  This
test asserts:

1. The CLI dispatcher's ``check_methods`` dict references every name
   in ``CHECK_ALL_METHODS`` (the issue's main concern).
2. Conversely, every method in ``CHECK_ALL_METHODS`` is a real
   ``DRCChecker`` instance method (catches typos in the tuple).
3. The CLI's category list and dispatcher dict stay in sync (a
   sibling drift hazard).
4. Specific spot-checks that ``via_in_pad`` is exposed via the CLI
   (the exact regression #3046 reports).

Out of scope: the actual ViaInPadRule logic -- that is tested in
``tests/test_validate_via_in_pad.py`` and exercised end-to-end against
board 02 via ``tests/test_audit.py``.
"""

from __future__ import annotations

from kicad_tools.cli import check_cmd
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.violations import DRCResults


def _build_minimal_checker() -> DRCChecker:
    """Construct a ``DRCChecker`` against an in-memory empty PCB.

    The check_methods dict only stores bound methods, so we just need a
    valid ``DRCChecker`` instance to introspect against.  We do NOT
    invoke any of the real check implementations (every check method is
    stubbed before any of them runs).
    """
    pcb = PCB.create(width=10.0, height=10.0, layers=2)
    return DRCChecker(pcb, manufacturer="jlcpcb", layers=2)


def _extract_dispatcher_methods(checker: DRCChecker) -> dict[str, str]:
    """Return the dispatcher's ``{category: underlying-method-name}`` map.

    Works by stubbing every ``check_*`` method on the ``checker``
    instance with a recording stub, then driving each known category
    through :func:`run_selected_checks` once.  Each stub records the
    name of the method it replaced under the active category, so the
    returned dict tells us which method each CLI category actually
    invokes.

    We do NOT introspect the dispatcher dict directly because it lives
    inside the function body.  Behavioural introspection is more robust
    than syntactic introspection (no AST parsing, no source-file
    coupling).
    """
    recorded: dict[str, str] = {}
    active_category: list[str] = [""]

    # All known check_* attributes on the checker instance.  We stub
    # every one so accidentally invoking a real check (e.g. due to a
    # category not in the dispatcher dict) is impossible.
    check_attrs = [
        name
        for name in dir(checker)
        if name.startswith("check_") and callable(getattr(checker, name))
    ]

    def make_stub(method_name: str):
        def stub(*_args, **_kwargs) -> DRCResults:
            # Record the FIRST method invoked for this category (the
            # dispatcher dict only maps each category to one method).
            recorded.setdefault(active_category[0], method_name)
            return DRCResults()

        return stub

    for name in check_attrs:
        setattr(checker, name, make_stub(name))

    for category in check_cmd.CHECK_CATEGORIES:
        active_category[0] = category
        check_cmd.run_selected_checks(checker, only_set={category}, skip_set=set())

    return recorded


class TestCheckAllMethodsConstant:
    """The introspection seam ``DRCChecker.CHECK_ALL_METHODS`` must be a
    well-formed tuple of real method names; otherwise the rest of the
    invariant tests can't run."""

    def test_check_all_methods_is_tuple(self) -> None:
        assert isinstance(DRCChecker.CHECK_ALL_METHODS, tuple)
        assert DRCChecker.CHECK_ALL_METHODS, "CHECK_ALL_METHODS must not be empty"

    def test_every_name_is_a_real_method(self) -> None:
        for name in DRCChecker.CHECK_ALL_METHODS:
            assert hasattr(DRCChecker, name), (
                f"CHECK_ALL_METHODS lists {name!r} but DRCChecker has no such method"
            )
            assert callable(getattr(DRCChecker, name)), f"DRCChecker.{name} is not callable"

    def test_names_are_unique(self) -> None:
        names = list(DRCChecker.CHECK_ALL_METHODS)
        assert len(names) == len(set(names)), f"CHECK_ALL_METHODS contains duplicates: {names}"


class TestDispatcherIsSupersetOfCheckAll:
    """The core regression test for Issue #3046."""

    def test_cli_dispatcher_covers_every_check_all_method(self) -> None:
        """Every method invoked by ``check_all`` MUST be reachable from
        the CLI dispatcher.  Otherwise ``kct check`` silently skips
        rules that the library API runs (the #3046 leak).
        """
        checker = _build_minimal_checker()
        category_to_method = _extract_dispatcher_methods(checker)

        dispatched_method_names = set(category_to_method.values())
        check_all_method_names = set(DRCChecker.CHECK_ALL_METHODS)

        missing = check_all_method_names - dispatched_method_names
        assert not missing, (
            f"CLI dispatcher omits checks that DRCChecker.check_all runs: "
            f"{sorted(missing)}.  Add them to check_methods in "
            f"src/kicad_tools/cli/check_cmd.py::run_selected_checks."
        )

    def test_via_in_pad_is_exposed_via_cli(self) -> None:
        """Spot-check for the exact regression #3046 reports."""
        checker = _build_minimal_checker()
        category_to_method = _extract_dispatcher_methods(checker)
        assert "via_in_pad" in category_to_method, (
            "CLI dispatcher must expose via_in_pad so ``kct check`` "
            "surfaces in-pad-via errors that audit/export flag.  See "
            "Issue #3046 / board 02 (24.3, 9.9) on D2.1."
        )
        assert category_to_method["via_in_pad"] == "check_via_in_pad"

    def test_pad_grid_alignment_in_check_all(self) -> None:
        """The reciprocal of #3046: ``check_all`` must run the pad-grid
        check the CLI runs.  Both paths now invoke the same set."""
        assert "check_pad_grid_alignment" in DRCChecker.CHECK_ALL_METHODS


class TestCategoryListMatchesDispatcher:
    """``CHECK_CATEGORIES`` (the ``--only/--skip`` argparse choices) and
    the dispatcher dict are two declarations of the same set; drift
    between them produces silently-ignored CLI flags."""

    def test_categories_list_equals_dispatcher_keys(self) -> None:
        checker = _build_minimal_checker()
        category_to_method = _extract_dispatcher_methods(checker)
        assert set(check_cmd.CHECK_CATEGORIES) == set(category_to_method.keys()), (
            "CHECK_CATEGORIES must equal the keys of run_selected_checks's "
            "check_methods dict.  Drift means --only/--skip silently "
            "accepts/rejects unknown categories."
        )


class TestEntryPointRegistryParity:
    """Cross-pipeline parity across the 6 DRC entry points (Issue #3044).

    Every entry point listed below must resolve its rule universe through
    :meth:`DRCChecker.check_all` (which is now :attr:`CHECK_ALL_METHODS`-
    driven).  This guards against the failure mode behind #3044: the CLI
    used to maintain a hand-rolled dict that drifted from ``check_all``
    (Issue #3046 / PR #3055), and the audit pipeline hardcoded a literal
    ``rule_id == "connectivity"`` filter (PR #3060) as a one-off
    workaround.

    The 6 entry points, per the issue's curator notes, are:

    1. ``kct check`` CLI -- ``cli/check_cmd.py::run_selected_checks``.
    2. :meth:`DRCChecker.check_all` library API (drives entry points 3-5).
    3. ``kct fix-drc`` -- ``cli/fix_drc_cmd.py::_run_python_drc``.
    4. ``kct reason`` -- ``cli/reason_cmd.py::main`` (DRC bootstrap).
    5. ``kct route`` post-pass DRC -- ``cli/route_cmd.py::run_post_route_drc``.
    6. ``ManufacturingAudit`` (used by ``kct audit`` / ``kct export`` /
       ``kct fleet status``) -- ``audit/auditor.py::_check_drc``.

    Entry points 3, 4, 5, 6 all delegate to ``check_all`` via a fresh
    :class:`DRCChecker` instance, so the regression contract for them is
    "they MUST NOT hand-roll their own method list" -- enforced
    syntactically below.  Entry point 1 is already covered by the
    sibling :class:`TestDispatcherIsSupersetOfCheckAll` (the CLI must be
    a superset).

    The advisory-rule classifier introduced for this issue
    (:attr:`DRCChecker.ADVISORY_RULE_IDS`) is the documented escape
    hatch: entry points that gate manufacturability (``_check_drc`` and,
    indirectly, ``kct export``) MAY filter advisory rules out of their
    blocking tally, but they MUST do so via the classifier -- not via
    literal ``rule_id == "X"`` comparisons.
    """

    @staticmethod
    def _read_source(module_path: str) -> str:
        """Return the source text of a module file.

        Resolves the module relative to ``src/kicad_tools/`` so the test
        keeps working when run from any cwd inside the repo.
        """
        import importlib
        from pathlib import Path

        mod = importlib.import_module(module_path)
        assert mod.__file__ is not None, f"{module_path} has no __file__"
        return Path(mod.__file__).read_text()

    def test_fix_drc_resolves_via_check_all(self) -> None:
        """``kct fix-drc`` must use ``check_all`` -- not its own list."""
        src = self._read_source("kicad_tools.cli.fix_drc_cmd")
        assert "checker.check_all()" in src, (
            "kct fix-drc must invoke checker.check_all() so it inherits "
            "the unified rule registry.  See Issue #3044."
        )

    def test_reason_resolves_via_check_all(self) -> None:
        """``kct reason`` must use ``check_all`` -- not its own list."""
        src = self._read_source("kicad_tools.cli.reason_cmd")
        assert "checker.check_all()" in src, (
            "kct reason must invoke checker.check_all() so it inherits "
            "the unified rule registry.  See Issue #3044."
        )

    def test_route_post_pass_resolves_via_check_all(self) -> None:
        """``kct route`` post-pass DRC must use ``check_all``."""
        src = self._read_source("kicad_tools.cli.route_cmd")
        assert "checker.check_all()" in src, (
            "kct route post-pass DRC must invoke checker.check_all() so "
            "it inherits the unified rule registry.  See Issue #3044."
        )

    def test_audit_resolves_via_check_all(self) -> None:
        """``ManufacturingAudit._check_drc`` must use ``check_all``.

        The audit opts into the ``kct check`` CLI's pad_grid auto-derive
        tolerance policy (issues #3061 / #3497), so the call carries the
        ``pad_grid_auto_derive=True`` argument.
        """
        src = self._read_source("kicad_tools.audit.auditor")
        assert "checker.check_all(pad_grid_auto_derive=True)" in src, (
            "ManufacturingAudit._check_drc must invoke checker.check_all("
            "pad_grid_auto_derive=True) so it inherits the unified rule "
            "registry (Issue #3044) AND matches the kct-check pad_grid "
            "tolerance policy (Issues #3061 / #3497)."
        )

    def test_audit_uses_advisory_classifier_not_literal(self) -> None:
        """The audit MUST filter advisory rules via the classifier, not
        via literal ``rule_id == "X"`` comparisons.

        This is the regression test for PR #3060's one-off workaround
        (``v.rule_id != "connectivity"``).  When a future rule needs
        advisory classification, adding it to
        :attr:`DRCChecker.ADVISORY_RULE_IDS` should be sufficient; the
        audit must never need a code change.
        """
        src = self._read_source("kicad_tools.audit.auditor")
        # The classifier must be invoked from the audit.
        assert "is_advisory_rule" in src, (
            "ManufacturingAudit must invoke DRCChecker.is_advisory_rule "
            "to filter advisory rules out of the blocking tally.  See "
            "Issue #3044."
        )
        # And the literal-rule filter from PR #3060 must be gone.
        assert 'rule_id != "connectivity"' not in src, (
            "ManufacturingAudit must not filter advisory rules by "
            "literal rule_id comparison.  Use DRCChecker.is_advisory_rule "
            "instead.  See Issue #3044."
        )

    def test_advisory_rule_ids_contains_connectivity(self) -> None:
        """The ``connectivity`` rule is the seed advisory rule.

        It was reclassified from the literal audit-side filter (PR #3060)
        to the central :attr:`DRCChecker.ADVISORY_RULE_IDS` set as part
        of this issue.  Removing it from the set would silently change
        every gating-aware entry point (audit, export) to treat
        connectivity gaps as blocking -- which is exactly the
        double-counting regression PR #3060 originally fixed.
        """
        assert "connectivity" in DRCChecker.ADVISORY_RULE_IDS, (
            "connectivity must be classified as advisory; see PR #3060 "
            "for the audit-side rationale (zone-bridged incomplete nets "
            "are already classified by ConnectivityStatus, not DRC)."
        )

    def test_is_advisory_rule_classifier(self) -> None:
        """The :meth:`DRCChecker.is_advisory_rule` classifier returns
        True for advisory rule_ids and False for everything else."""
        assert DRCChecker.is_advisory_rule("connectivity") is True
        # Spot-check several blocking rules.
        for rule_id in (
            "clearance_pad_segment",
            "via_in_pad",
            "pad_grid",
            "dimension_min_trace_width",
            "edge_clearance",
            "single_pad_net",
        ):
            assert DRCChecker.is_advisory_rule(rule_id) is False, (
                f"{rule_id} must not be classified as advisory"
            )

    def test_entry_points_see_same_rule_universe_modulo_severity(self) -> None:
        """All 6 entry points must see the same rule UNIVERSE.

        ``check_all`` is the single source of truth.  Every entry point
        either invokes ``check_all`` directly (entry points 3-6) or is
        constrained by :class:`TestDispatcherIsSupersetOfCheckAll` to be
        a superset (entry point 1).  This test consolidates the
        invariant by asserting the ``check_all`` method names are
        precisely the set of rules the CLI dispatcher exposes (no
        accidental CLI-only or check_all-only methods).
        """
        checker = _build_minimal_checker()
        category_to_method = _extract_dispatcher_methods(checker)

        dispatcher_methods = set(category_to_method.values())
        check_all_methods = set(DRCChecker.CHECK_ALL_METHODS)

        # The CLI dispatcher is the entry point with the widest method
        # set (it has both the check_all union AND any CLI-only methods
        # like pad_grid had pre-#3055).  After #3055, the two must be
        # identical.
        assert dispatcher_methods == check_all_methods, (
            "Entry-point parity violation: CLI dispatcher and check_all "
            "must invoke the same set of check_* methods.  Differences: "
            f"CLI-only={dispatcher_methods - check_all_methods}, "
            f"check_all-only={check_all_methods - dispatcher_methods}.  "
            "See Issue #3044."
        )


class TestRuleCategoryTaxonomy:
    """Issue #3803: the manufacturing-vs-advisory *reporting* taxonomy.

    ``DRCChecker.category_for_rule`` classifies every rule_id into one of
    two presentation buckets so ``kct check`` can render a per-bucket
    headline.  These tests pin:

    * the classifier is total (no rule_id resolves to an unexpected
      category) and covers every rule_id registered in the validate rules;
    * the load-bearing advisory rules (the ones that inflated the headline)
      land in ``advisory-quality`` and the fab-blocking rules land in
      ``manufacturing``;
    * the taxonomy is ORTHOGONAL to the gating ``ADVISORY_RULE_IDS`` set --
      classifying ``copper_sliver`` as advisory-quality for reporting must
      NOT change what :meth:`DRCChecker.is_advisory_rule` returns (that
      drives gate verdicts and must stay ``{"connectivity"}``).
    """

    import re
    from pathlib import Path as _Path

    _RULES_DIR = _Path(__file__).resolve().parent.parent / "src" / "kicad_tools" / "validate"
    _RULE_ID_RE = re.compile(r'rule_id\s*=\s*"([a-z0-9_]+)"')

    @classmethod
    def _registered_rule_ids(cls) -> set[str]:
        """Scrape every literal ``rule_id="..."`` from the validate engine."""
        found: set[str] = set()
        for path in cls._RULES_DIR.rglob("*.py"):
            found.update(cls._RULE_ID_RE.findall(path.read_text()))
        return found

    def test_every_registered_rule_id_is_categorized(self) -> None:
        """No uncategorized leak: each registered rule_id maps to exactly
        one of the two known categories."""
        valid = {DRCChecker.CATEGORY_MANUFACTURING, DRCChecker.CATEGORY_ADVISORY}
        rule_ids = self._registered_rule_ids()
        assert rule_ids, "expected to scrape at least one rule_id from validate/"
        for rule_id in rule_ids:
            category = DRCChecker.category_for_rule(rule_id)
            assert category in valid, f"{rule_id} -> {category!r} is not a valid category"

    def test_advisory_quality_rules_classified(self) -> None:
        """The routing-intent / quality rules that inflated the headline
        must land in the advisory bucket."""
        for rule_id in (
            "connectivity",
            "ampacity",
            "copper_sliver",
            "impedance",
            "diffpair_length_skew",
            "diffpair_routing_continuity",
            "diffpair_clearance_intra",
            "match_group_length_skew",
            "silk_over_copper",
        ):
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_ADVISORY, (
                f"{rule_id} must be advisory-quality"
            )

    def test_manufacturing_rules_classified(self) -> None:
        """Fab-blocking copper / clearance / hole / edge / mask / drill
        rules must land in the manufacturing bucket."""
        for rule_id in (
            "clearance",
            "clearance_segment_zone",
            "clearance_via_zone",
            "edge_clearance",
            "edge_clearance_via",
            "hole_to_hole_clearance",
            "via_in_pad",
            "solder_mask_pad",
            "dimension_via_drill",
            "single_pad_net",
            "footprint_outside_board",
            "zone_fill",
        ):
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_MANUFACTURING, (
                f"{rule_id} must be manufacturing"
            )

    def test_dynamic_clearance_subtypes_classified(self) -> None:
        """Dynamically-suffixed ``clearance_*`` subtypes (built via
        ``f"clearance_{suffix}"`` in rules/clearance.py, so not literal in
        the map) fall through to the manufacturing prefix rule."""
        for rule_id in (
            "clearance_pad_segment",
            "clearance_segment_via",
            "clearance_pad_via",
            "clearance_via_via",
            "clearance_segment_segment",
        ):
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_MANUFACTURING, (
                f"{rule_id} must be manufacturing"
            )

    def test_unknown_rule_defaults_to_manufacturing(self) -> None:
        """An unrecognized rule surfaces in the fab-blocking bucket rather
        than being silently hidden among advisory findings."""
        assert (
            DRCChecker.category_for_rule("some_brand_new_rule") == DRCChecker.CATEGORY_MANUFACTURING
        )

    def test_reporting_taxonomy_is_orthogonal_to_gating(self) -> None:
        """Reporting-category classification must NOT change the gating
        advisory set: ``is_advisory_rule`` stays ``{"connectivity"}`` even
        though copper_sliver/diffpair/ampacity are advisory *for reporting*.
        """
        assert frozenset({"connectivity"}) == DRCChecker.ADVISORY_RULE_IDS
        assert DRCChecker.is_advisory_rule("connectivity") is True
        for rule_id in (
            "copper_sliver",
            "diffpair_length_skew",
            "ampacity",
            "impedance",
            "silk_over_copper",
        ):
            # advisory-quality for REPORTING ...
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_ADVISORY
            # ... but NOT advisory for GATING (unchanged behavior).
            assert DRCChecker.is_advisory_rule(rule_id) is False
