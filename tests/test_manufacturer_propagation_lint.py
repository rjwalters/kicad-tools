"""Lint-style regression test for Issue #2708.

Background
----------
PR #2608 (Issue #2605) added the ``manufacturer`` field to
``kicad_tools.router.rules.DesignRules`` so the escape router can opt in
to in-pad via escape on fine-pitch packages when the target manufacturer
supports via-in-pad processing.

PR #2704 (#2695) discovered that PR #2608 had wired ``manufacturer`` into
only 1 of 4 ``DesignRules`` constructor sites in ``cli/route_cmd.py``.
The other 3 sites — escalation/tier-fallback paths — silently dropped
the CLI flag, disabling in-pad escape for any board that escalated past
the initial routing pass.

Issue #2708 audited the full codebase and found additional sites with
the same omission:

* ``router/core.py`` fine-grid and clearance-relaxation derived rules
* ``router/io.py`` ``PCBDesignRules.to_design_rules``
* ``reasoning/interpreter.py`` ``_get_design_rules``
* ``optim/place_route.py`` ``router_factory``

This file guards against future regressions by statically scanning every
``router.rules.DesignRules(...)`` constructor call in ``src/`` and
asserting that:

1. The call passes ``manufacturer=`` explicitly, OR
2. The call sits in an allowlisted *scope* in ``_ALLOWLIST`` below
   (keyed by enclosing function/method, line-stable) with a documented
   reason and expected call count.

The placement-side ``placement.analyzer.DesignRules`` is a different
dataclass with no manufacturer field and is not at risk; the lint
distinguishes the two by import-trace heuristics described below.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

# Repository root (parent of "tests")
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC_ROOT = _REPO_ROOT / "src" / "kicad_tools"

# ----------------------------------------------------------------------------
# Allowlist: sites known to legitimately omit ``manufacturer=``.
#
# Historically this allowlist was keyed by 1-based line numbers, which
# broke on *every* unrelated insertion above an audited site (issue #3993
# tracked three such breakages — PRs #3945, #3966, #3992 — each costing a
# full Doctor->Judge round-trip).  It is now keyed by the **enclosing
# scope** (module-level function or ``Class.method`` qualified name), which
# is stable against line shifts: inserting no-op lines above an allowlisted
# call no longer invalidates it, while a *new* ``DesignRules()`` fallback in
# an un-audited scope is still caught.
#
# To also catch a new bad call added *inside* an already-allowlisted scope,
# each entry records the exact number of manufacturer-less ``DesignRules()``
# calls expected in that scope.  Adding an extra call in an allowlisted
# scope raises the observed count above the allowed count and fails the
# lint (the extra call must be threaded through or explicitly re-audited).
#
# Format: {relative_path: {scope_qualname: (expected_count, reason)}}
#   - ``scope_qualname`` is the dotted path of enclosing FunctionDef /
#     AsyncFunctionDef / ClassDef names (e.g. ``Autorouter.__init__``).
#   - Module-level calls (no enclosing scope) use the key ``"<module>"``.
# ----------------------------------------------------------------------------

_MODULE_SCOPE = "<module>"

_ALLOWLIST: dict[str, dict[str, tuple[int, str]]] = {
    # Router core: default-constructed router.  ``_run_monte_carlo_trial``
    # holds the ``rules_dict``-spread default (a ternary that emits *two*
    # bare ``DesignRules()`` calls on one line); ``Autorouter.__init__``
    # holds the constructor ``rules or DesignRules()`` fallback.  Reaching
    # either means the caller did not pass rules.
    "router/core.py": {
        "_run_monte_carlo_trial": (
            2,
            "worker-process rules_dict-spread default (ternary: two calls)",
        ),
        "Autorouter.__init__": (1, "Autorouter.__init__ rules-arg default fallback"),
    },
    # AdaptiveRouter default fallback — same pattern as Autorouter.
    "router/adaptive.py": {
        "AdaptiveAutorouter.__init__": (1, "AdaptiveAutorouter.__init__ default fallback"),
    },
    # Mesh-router single-net pathfinder (#4268) — same constructor rules-arg
    # default fallback shape as Autorouter.__init__.  The sole caller,
    # ``Autorouter._route_net_mesh`` in router/core.py, always passes
    # ``self.rules`` (manufacturer already propagated from the Autorouter),
    # so this bare default is only reached when a caller constructs the
    # pathfinder with no rules (direct unit-test construction or
    # ``MeshPathfinder.from_board(rules=None)``), where no manufacturer
    # context exists to thread.
    "router/mesh/pathfinder.py": {
        "MeshPathfinder.__init__": (1, "MeshPathfinder.__init__ rules-arg default fallback"),
    },
    # Lattice-engine pathfinder (#4278) -- identical constructor rules-arg
    # default fallback shape as MeshPathfinder/Autorouter.  The dispatch
    # caller, ``Autorouter._ensure_lattice_pathfinder`` in router/core.py,
    # always passes ``self.rules`` (manufacturer already propagated); the
    # bare default is only reached by direct unit-test construction or
    # ``LatticePathfinder.from_board(rules=None)``, where no manufacturer
    # context exists to thread.  Via-in-pad is additionally moot for this
    # engine (vias land only on free-space lattice nodes, never in pads).
    "router/lattice/pathfinder.py": {
        "LatticePathfinder.__init__": (
            1,
            "LatticePathfinder.__init__ rules-arg default fallback",
        ),
    },
    # Library API: ``route_pcb()`` synthesises default rules only when
    # the caller explicitly passes ``rules=None`` (no PCB rules either).
    # The CLI always supplies its own rules with manufacturer wired in.
    "router/io.py": {
        "route_pcb": (1, "route_pcb() fallback when caller passes rules=None"),
        "load_pcb_for_routing": (
            1,
            "load_pcb_for_routing() inner fallback when neither rules nor pcb_rules supplied",
        ),
    },
    # Benchmark/synthetic fixture generators — no real CLI context.
    "benchmark/runner.py": {
        "BenchmarkRunner.run_single": (1, "Benchmark harness fixture; no manufacturer context"),
    },
    "benchmark/generators.py": {
        "generate_bga_breakout": (1, "Synthetic benchmark generator fixture"),
        "generate_random_board": (1, "Synthetic benchmark generator fixture"),
    },
    # Evolutionary algorithm: constructs DesignRules from a serialised
    # rules_dict to recreate router state across worker processes.  The
    # ``manufacturer`` field flows through ``rules_dict`` if present.
    # ``_run_evolutionary_trial`` also uses the two-call ternary form.
    "router/algorithms/evolutionary.py": {
        "_run_evolutionary_trial": (
            2,
            "Evolutionary worker rules_dict-spread; manufacturer flows via dict (ternary: two calls)",
        ),
    },
}


# ----------------------------------------------------------------------------
# AST visitor
# ----------------------------------------------------------------------------


class _DesignRulesCallFinder(ast.NodeVisitor):
    """Walk an AST and collect every ``DesignRules(...)`` call site.

    The visitor resolves ``from X import DesignRules as Y`` aliases by
    scanning ``ImportFrom`` nodes whose source module references the
    router package, then matching call-site names against that alias
    set.  Bare ``DesignRules`` and attribute access (``mod.DesignRules``)
    are also matched.

    Each recorded site carries its **enclosing scope** — the dotted path
    of enclosing function/method/class names (e.g. ``Autorouter.__init__``
    or ``_run_monte_carlo_trial``).  This lets the allowlist be keyed by a
    line-stable identifier instead of a raw line number (issue #3993).
    Module-level calls report the scope ``"<module>"``.
    """

    def __init__(self, source_tree: ast.AST | None = None) -> None:
        # Each site: (lineno, keywords, scope_qualname).
        self.sites: list[tuple[int, list[str], str]] = []
        # Aliases that resolve to router.rules.DesignRules.
        self.router_aliases: set[str] = {"DesignRules"}
        # Stack of enclosing scope names as we descend the tree.
        self._scope_stack: list[str] = []
        if source_tree is not None:
            self._collect_router_aliases(source_tree)

    def _collect_router_aliases(self, tree: ast.AST) -> None:
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            module = node.module or ""
            # Heuristic: only treat the import as a router alias when the
            # source module is in the router package.  This excludes
            # placement.analyzer.DesignRules.
            if "router" not in module and not module.endswith("rules"):
                continue
            if module.startswith("kicad_tools.placement"):
                continue
            for alias in node.names:
                if alias.name == "DesignRules":
                    self.router_aliases.add(alias.asname or "DesignRules")

    @property
    def _current_scope(self) -> str:
        return ".".join(self._scope_stack) if self._scope_stack else _MODULE_SCOPE

    def _visit_scope(self, node: ast.AST) -> None:
        self._scope_stack.append(getattr(node, "name", "?"))
        self.generic_visit(node)
        self._scope_stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:  # noqa: N802
        self._visit_scope(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:  # noqa: N802
        self._visit_scope(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:  # noqa: N802
        self._visit_scope(node)

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # Only match bare-name calls.  Attribute-access calls like
        # ``router_cpp.DesignRules()`` refer to a different class (the
        # C++ binding) and must not be flagged by this lint.
        if isinstance(node.func, ast.Name) and node.func.id in self.router_aliases:
            keywords = [kw.arg for kw in node.keywords if kw.arg is not None]
            self.sites.append((node.lineno, keywords, self._current_scope))
        self.generic_visit(node)


# ----------------------------------------------------------------------------
# File-level filters
# ----------------------------------------------------------------------------


# Files where ``DesignRules`` is a *different* dataclass (different
# fields, not at risk for the manufacturer-propagation bug class).
# Skipped entirely by the lint.
_NON_ROUTER_DESIGN_RULES_FILES = {
    # placement.analyzer.DesignRules — placement-conflict rules, no
    # manufacturer field.
    "placement/analyzer.py",
    "placement/fixer.py",
    # manufacturers.base.DesignRules — manufacturer profile dataclass
    # with ``min_trace_width_mm`` etc., independent class hierarchy.
    "manufacturers/base.py",
}


def _imports_router_design_rules(tree: ast.AST) -> bool:
    """Return True if the module imports ``DesignRules`` from the router.

    Recognises both ``from kicad_tools.router.rules import DesignRules``
    and ``from kicad_tools.router import DesignRules`` plus relative
    forms within the package (``.rules``, ``.router.rules``, etc.).
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name in ("DesignRules", "*"):
                    if "router" in module or module.endswith("rules"):
                        return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if "router" in alias.name:
                    return True
    return False


def _imports_placement_design_rules(tree: ast.AST) -> bool:
    """Return True if the module imports the *placement* DesignRules."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            module = node.module or ""
            for alias in node.names:
                if alias.name == "DesignRules" and "placement" in module:
                    return True
    return False


# ----------------------------------------------------------------------------
# Test
# ----------------------------------------------------------------------------


def _iter_python_files() -> list[Path]:
    return [p for p in _SRC_ROOT.rglob("*.py") if "__pycache__" not in p.parts]


_NOQA_RE = re.compile(r"#\s*noqa\s*:\s*MFR001\b", re.IGNORECASE)


def _scan_source_for_violations(
    rel: str,
    source: str,
    allowlist: dict[str, tuple[int, str]],
) -> list[str]:
    """Scan one module's source for manufacturer-less ``DesignRules()`` calls.

    Returns a list of human-readable violation strings for every bare
    ``DesignRules()`` construction whose enclosing scope is not covered by
    ``allowlist`` (keyed by scope qualname -> (expected_count, reason)) and
    not suppressed by an inline ``# noqa: MFR001`` pragma.

    Sharing this helper between the canonical lint and the acceptance tests
    guarantees the tests exercise the *actual* line-stable scope matching,
    not a re-implementation of it.
    """
    tree = ast.parse(source)
    finder = _DesignRulesCallFinder(tree)
    finder.visit(tree)

    source_lines = source.splitlines()

    # Count how many manufacturer-less calls each scope is allowed to
    # host, then decrement as we account for them.  A scope that hosts
    # *more* bare calls than its allowlisted budget still reports the
    # surplus as a violation (catches a new bad call added inside an
    # already-allowlisted scope).
    scope_budget: dict[str, int] = {scope: count for scope, (count, _) in allowlist.items()}

    violations: list[str] = []
    for lineno, keywords, scope in finder.sites:
        if "manufacturer" in keywords:
            continue
        if scope_budget.get(scope, 0) > 0:
            scope_budget[scope] -= 1
            continue

        # In-line noqa pragma escape hatch — search a small window
        # around the opening paren, since the call may span lines.
        window_start = max(0, lineno - 1)
        window_end = min(len(source_lines), lineno + 1)
        window = "\n".join(source_lines[window_start:window_end])
        if _NOQA_RE.search(window):
            continue

        violations.append(
            f"{rel}:{lineno} (scope {scope!r}): DesignRules(...) "
            f"constructed without manufacturer= and not covered by "
            f"_ALLOWLIST. Either thread manufacturer through, add the "
            f"enclosing scope to _ALLOWLIST in this test file with a "
            f'reason, or add `# noqa: MFR001 reason="..."` on the same '
            f"line."
        )
    return violations


def test_manufacturer_propagation_in_router_design_rules():
    """Every ``router.rules.DesignRules(...)`` call must pass ``manufacturer=``.

    Allowlisted sites (e.g., library-API default constructors, benchmark
    fixtures) are exempt — see ``_ALLOWLIST`` above.

    To suppress an individual call site without editing this test, add a
    ``# noqa: MFR001 reason="..."`` comment on the same line as the
    ``DesignRules(`` opening.
    """
    violations: list[str] = []

    for path in _iter_python_files():
        rel = path.relative_to(_SRC_ROOT).as_posix()
        if rel in _NON_ROUTER_DESIGN_RULES_FILES:
            continue

        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(path))
        except SyntaxError:
            # Skip files we can't parse; they have bigger problems.
            continue

        # Skip files that don't import DesignRules at all.
        if not (_imports_router_design_rules(tree) or "DesignRules(" in source):
            continue

        # If the file imports ONLY placement.DesignRules, skip it.
        if _imports_placement_design_rules(tree) and not _imports_router_design_rules(tree):
            continue

        violations.extend(_scan_source_for_violations(rel, source, _ALLOWLIST.get(rel, {})))

    assert not violations, "manufacturer propagation regression (Issue #2708):\n  " + "\n  ".join(
        violations
    )


# ----------------------------------------------------------------------------
# Sanity tests for the lint itself
# ----------------------------------------------------------------------------


def test_allowlist_entries_resolve_to_actual_call_sites():
    """Each allowlist entry must resolve to the expected bare-call scope.

    A scope-keyed entry is *stale* when its enclosing scope no longer
    hosts any manufacturer-less ``DesignRules()`` call, or when the number
    of such calls differs from the recorded ``expected_count`` (either a
    site was removed/threaded through, or a new one was added and must be
    re-audited).
    """
    from collections import Counter

    stale: list[str] = []
    for rel, scopes in _ALLOWLIST.items():
        path = _SRC_ROOT / rel
        if not path.exists():
            stale.append(f"{rel}: file does not exist")
            continue
        source = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source)
        except SyntaxError:
            continue
        finder = _DesignRulesCallFinder(tree)
        finder.visit(tree)

        # Count manufacturer-less calls per enclosing scope.
        observed: Counter[str] = Counter(
            site[2] for site in finder.sites if "manufacturer" not in site[1]
        )

        for scope, (expected_count, _reason) in scopes.items():
            actual = observed.get(scope, 0)
            if actual == 0:
                stale.append(
                    f"{rel}: scope {scope!r} hosts no manufacturer-less "
                    f"DesignRules() call (allowlist out of date)"
                )
            elif actual != expected_count:
                stale.append(
                    f"{rel}: scope {scope!r} hosts {actual} manufacturer-less "
                    f"DesignRules() call(s), allowlist expects {expected_count} "
                    f"(update the expected count if the change is intentional)"
                )

    assert not stale, "Stale entries in _ALLOWLIST:\n  " + "\n  ".join(stale)


@pytest.mark.parametrize(
    "site_description",
    [
        ("router/core.py", "_route_fine_grid fine_rules"),
        ("router/core.py", "clearance-relaxation relaxed_rules"),
        ("router/io.py", "PCBDesignRules.to_design_rules"),
        ("reasoning/interpreter.py", "_get_design_rules"),
        ("optim/place_route.py", "router_factory"),
    ],
)
def test_fixed_sites_pass_manufacturer(site_description):
    """Spot-check each curator-identified site for Issue #2708.

    The lint test above is the canonical guard; this parametrised test
    documents *which specific* sites the curator audited and is intended
    to fail loudly if any of them regress.
    """
    rel, description = site_description
    source = (_SRC_ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(source)
    finder = _DesignRulesCallFinder(tree)
    finder.visit(tree)

    sites_with_mfr = [site for site in finder.sites if "manufacturer" in site[1]]
    assert sites_with_mfr, (
        f"{rel} ({description}): expected at least one DesignRules(...) "
        f"call passing manufacturer=, found none. Issue #2708 audit must "
        f"have regressed."
    )


# ----------------------------------------------------------------------------
# Line-stability regression tests (Issue #3993)
#
# The allowlist was previously keyed by absolute line number, so inserting
# unrelated code above an audited site broke the lint (observed three times:
# PRs #3945, #3966, #3992).  These synthetic-source tests pin the two
# acceptance criteria: (1) no-op insertions above an allowlisted site do not
# fail; (2) a genuinely new fallback site still fails.  They run through the
# real ``_scan_source_for_violations`` helper so they track the production
# matching logic.
# ----------------------------------------------------------------------------


# Synthetic module mirroring the ``Autorouter.__init__`` fallback pattern:
# a single manufacturer-less ``DesignRules()`` inside an allowlisted scope.
_SYNTHETIC_SOURCE = """\
from kicad_tools.router.rules import DesignRules


class Autorouter:
    def __init__(self, rules=None):
        self.rules = rules or DesignRules()
"""

# Allowlist covering exactly the audited scope with a budget of one call.
_SYNTHETIC_ALLOWLIST = {"Autorouter.__init__": (1, "synthetic audited fallback")}


def test_noop_insertion_above_allowlisted_site_does_not_fail():
    """Acceptance: inserting no-op lines above an audited site stays green.

    This is the exact failure mode from issue #3993 — a line-number-keyed
    allowlist would flag the shifted site.  The scope-keyed allowlist must
    not.
    """
    # Baseline: the audited site is covered, so no violations.
    baseline = _scan_source_for_violations("synthetic.py", _SYNTHETIC_SOURCE, _SYNTHETIC_ALLOWLIST)
    assert baseline == [], f"baseline synthetic source unexpectedly flagged: {baseline}"

    # Insert 25 unrelated no-op lines above the class, shifting the audited
    # call far from its original line number.
    noop_block = "\n".join(f"_UNUSED_{i} = {i}  # unrelated insertion" for i in range(25))
    shifted_source = _SYNTHETIC_SOURCE.replace(
        "class Autorouter:", f"{noop_block}\n\n\nclass Autorouter:"
    )

    shifted = _scan_source_for_violations("synthetic.py", shifted_source, _SYNTHETIC_ALLOWLIST)
    assert shifted == [], (
        "Inserting no-op lines above an allowlisted DesignRules() site must "
        f"NOT fail the lint (issue #3993), but got violations: {shifted}"
    )


def test_new_fallback_site_still_fails():
    """Acceptance: a genuinely new manufacturer-less fallback still fails.

    The lint's intent must survive the re-keying: an un-audited scope, and
    an extra bare call added *inside* an audited scope, both remain
    violations.
    """
    # (a) New fallback in a brand-new, un-allowlisted scope.
    new_scope_source = _SYNTHETIC_SOURCE + (
        "\n\n"
        "def brand_new_factory():\n"
        "    return DesignRules()  # newly-added, un-audited fallback\n"
    )
    new_scope = _scan_source_for_violations("synthetic.py", new_scope_source, _SYNTHETIC_ALLOWLIST)
    assert new_scope, (
        "A new DesignRules() fallback in an un-allowlisted scope must fail "
        "the lint, but no violation was reported."
    )
    assert any("brand_new_factory" in v for v in new_scope), new_scope

    # (b) A second bare call added *inside* an already-allowlisted scope
    #     exceeds the recorded budget of 1 and must still fail.
    extra_call_source = _SYNTHETIC_SOURCE.replace(
        "        self.rules = rules or DesignRules()\n",
        "        self.rules = rules or DesignRules()\n"
        "        self.backup_rules = DesignRules()  # extra, un-audited\n",
    )
    extra_call = _scan_source_for_violations(
        "synthetic.py", extra_call_source, _SYNTHETIC_ALLOWLIST
    )
    assert extra_call, (
        "A second manufacturer-less DesignRules() added inside an "
        "allowlisted scope must exceed the recorded budget and fail the "
        "lint, but no violation was reported."
    )
