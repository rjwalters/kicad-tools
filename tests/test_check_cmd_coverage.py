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
            assert callable(getattr(DRCChecker, name)), (
                f"DRCChecker.{name} is not callable"
            )

    def test_names_are_unique(self) -> None:
        names = list(DRCChecker.CHECK_ALL_METHODS)
        assert len(names) == len(set(names)), (
            f"CHECK_ALL_METHODS contains duplicates: {names}"
        )


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
