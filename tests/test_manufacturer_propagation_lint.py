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
2. The call sits on an allowlisted line in ``_ALLOWLIST`` below with a
   documented reason.

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
# Allowlist: files known to legitimately omit ``manufacturer=``.
#
# Each entry maps a file path (relative to ``src/kicad_tools``) to a set of
# 1-based line numbers where ``DesignRules(...)`` is intentionally
# constructed without ``manufacturer=``.  Add a brief reason next to the
# tuple to document why omission is safe.
# ----------------------------------------------------------------------------

# Format: {relative_path: {line_number: reason}}
_ALLOWLIST: dict[str, dict[int, str]] = {
    # Router core: default-constructed router (line ~352 is the
    # ``rules_dict``-spread default in _route_with_seed; line ~763 is
    # the constructor default).  Reaching either means the caller did
    # not pass rules.  (Line numbers refreshed for issue #3436 burn-down.)
    "router/core.py": {
        352: "worker-process rules_dict-spread default (both calls on this line)",
        763: "Autorouter.__init__ rules-arg default fallback",
    },
    # AdaptiveRouter default fallback — same pattern as Autorouter.
    "router/adaptive.py": {
        108: "AdaptiveAutorouter.__init__ default fallback",
    },
    # Library API: ``route_pcb()`` synthesises default rules only when
    # the caller explicitly passes ``rules=None`` (no PCB rules either).
    # The CLI always supplies its own rules with manufacturer wired in.
    "router/io.py": {
        2691: "route() fallback when caller passes rules=None",
        3292: "route_pcb() inner fallback when neither rules nor pcb_rules supplied",
    },
    # Benchmark/synthetic fixture generators — no real CLI context.
    "benchmark/runner.py": {
        112: "Benchmark harness fixture; no manufacturer context",
    },
    "benchmark/generators.py": {
        52: "Synthetic benchmark generator fixture",
        147: "Synthetic benchmark generator fixture",
    },
    # Evolutionary algorithm: constructs DesignRules from a serialised
    # rules_dict to recreate router state across worker processes.  The
    # ``manufacturer`` field flows through ``rules_dict`` if present.
    "router/algorithms/evolutionary.py": {
        222: "Evolutionary worker rules_dict-spread; manufacturer flows via dict",
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
    """

    def __init__(self, source_tree: ast.AST | None = None) -> None:
        self.sites: list[tuple[int, list[str]]] = []
        # Aliases that resolve to router.rules.DesignRules.
        self.router_aliases: set[str] = {"DesignRules"}
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

    def visit_Call(self, node: ast.Call) -> None:  # noqa: N802
        # Only match bare-name calls.  Attribute-access calls like
        # ``router_cpp.DesignRules()`` refer to a different class (the
        # C++ binding) and must not be flagged by this lint.
        if isinstance(node.func, ast.Name) and node.func.id in self.router_aliases:
            keywords = [kw.arg for kw in node.keywords if kw.arg is not None]
            self.sites.append((node.lineno, keywords))
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
    return [
        p
        for p in _SRC_ROOT.rglob("*.py")
        if "__pycache__" not in p.parts
    ]


_NOQA_RE = re.compile(r"#\s*noqa\s*:\s*MFR001\b", re.IGNORECASE)


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
        if not (
            _imports_router_design_rules(tree)
            or "DesignRules(" in source
        ):
            continue

        # If the file imports ONLY placement.DesignRules, skip it.
        if (
            _imports_placement_design_rules(tree)
            and not _imports_router_design_rules(tree)
        ):
            continue

        finder = _DesignRulesCallFinder(tree)
        finder.visit(tree)

        source_lines = source.splitlines()
        allowlist = _ALLOWLIST.get(rel, {})

        for lineno, keywords in finder.sites:
            if "manufacturer" in keywords:
                continue
            if lineno in allowlist:
                continue

            # In-line noqa pragma escape hatch — search a small window
            # around the opening paren, since the call may span lines.
            window_start = max(0, lineno - 1)
            window_end = min(len(source_lines), lineno + 1)
            window = "\n".join(source_lines[window_start:window_end])
            if _NOQA_RE.search(window):
                continue

            violations.append(
                f"{rel}:{lineno}: DesignRules(...) constructed without "
                f"manufacturer= and not in allowlist. Either thread "
                f"manufacturer through, add the line to _ALLOWLIST in "
                f"this test file with a reason, or add `# noqa: MFR001 "
                f'reason="..."` on the same line.'
            )

    assert not violations, (
        "manufacturer propagation regression (Issue #2708):\n  "
        + "\n  ".join(violations)
    )


# ----------------------------------------------------------------------------
# Sanity tests for the lint itself
# ----------------------------------------------------------------------------


def test_allowlist_entries_resolve_to_actual_call_sites():
    """Each allowlist entry must point to a real ``DesignRules(`` call."""
    stale: list[str] = []
    for rel, lines in _ALLOWLIST.items():
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
        site_lines = {site[0] for site in finder.sites}
        for lineno in lines:
            if lineno not in site_lines:
                stale.append(
                    f"{rel}:{lineno}: no DesignRules() call at this line "
                    f"(allowlist out of date)"
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

    sites_with_mfr = [
        site for site in finder.sites if "manufacturer" in site[1]
    ]
    assert sites_with_mfr, (
        f"{rel} ({description}): expected at least one DesignRules(...) "
        f"call passing manufacturer=, found none. Issue #2708 audit must "
        f"have regressed."
    )
