"""Shared spec precondition for board recipes (issue #4539).

:mod:`kicad_tools.recipes.gate` is the **post**-condition half of the recipe
contract: after route / DRC / LVS have run, ``evaluate_pipeline_gate`` turns
their verdicts into ONE :class:`~kicad_tools.recipes.gate.PipelineGateResult`
that drives both the printed ``SUMMARY`` and the process exit code (#3912).

This module is the **pre**-condition half.  Before a recipe mutates anything
it asks a single question:

    *Does this board carry captured intent?*

Concretely: is there a ``project.kct`` next to the recipe (or in a bounded
set of its ancestors), is it non-empty, and does it parse?  A board that is
about to have copper written for it with no captured intent at all is the
condition this refuses to let pass silently.

Gate-strength ladder (issue #4539)
----------------------------------
=====  =========================================================  ==========
Level  Check                                                      Fleet
=====  =========================================================  ==========
L0     artifact exists and is non-empty                           8/8 pass
L1     L0 + :func:`~kicad_tools.spec.parser.load_spec` parses     8/8 pass
L2     L1 + :func:`~kicad_tools.spec.parser.validate_spec` clean  7/8 pass
=====  =========================================================  ==========

**L1 is the default.**  **L2 is strict-mode only** (opt in with
``KCT_REQUIRE_SPEC=1`` or ``strict=True``): as of ``origin/main`` @
``83d7625e`` ``boards/01-voltage-divider/project.kct`` is semantically
``Invalid`` (``Progress: current phase 'complete' not found in phases``), so
a default-on L2 would fail that board's CI job for a bookkeeping defect that
has nothing to do with this gate.  Repairing that spec is deliberately NOT
this module's job -- the gate reports, it does not repair.

**There is deliberately no L3.**  A hypothetical L3 ("the spec must *model*
the board's requirements", i.e. no unrecognized / unmodelled ``.kct`` keys)
is the schema-design question PR #4619's review explicitly deferred -- after
#4619, ``kct spec validate`` reports 8 unrecognized keys on board 00 alone,
so an L3 gate would reject 8/8 boards.  Deciding what ``.kct`` *should*
model is a separate issue.  **Do not add an unrecognized-key check here.**

Posture
-------
* **Read-only.**  This module never calls ``save_spec`` / ``append_decision``
  / ``kct spec decide``, and never writes to the ``.kct`` file in any way.
* **Advisory and fail-soft by default** -- a missing / empty / unparseable
  artifact prints a labelled warning to stderr and returns a falsy verdict.
  It does not raise and does not change the recipe's exit code.
* **Never crashes a recipe.**  An unexpected exception inside the helper is
  swallowed and reported as "could not check", mirroring
  ``_load_project_kct_for_escalation``'s bare ``except Exception: return``
  (``src/kicad_tools/cli/route_cmd.py``).
* **Strict mode is opt-in** and terminates the process with
  ``SystemExit(1)`` *before* the recipe's first write.  ``SystemExit``
  derives from ``BaseException``, so it deliberately passes straight through
  the recipes' broad ``except Exception`` handlers instead of being
  downgraded into their generic error path.

Discovery anchor (the trap)
---------------------------
Discovery walks **up from the recipe script's own directory**
(``Path(__file__).parent``), mirroring the bounded-ancestor pattern in
``_load_project_kct_for_escalation``.  It must NOT anchor on
:func:`pathlib.Path.cwd` or on the output directory: CI invokes every recipe
from the repo root with an out-of-tree output dir (e.g. ``uv run python
boards/00-simple-led/generate_design.py /tmp/board00-ci``), so a cwd- or
output-anchored search would find nothing and the gate would fire spuriously
on all 8 boards.

Usage
-----
.. code-block:: python

    from kicad_tools.recipes.precondition import require_spec

    def main() -> int:
        require_spec(__file__)   # advisory; strict via KCT_REQUIRE_SPEC=1
        ...
"""

from __future__ import annotations

import inspect
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TextIO

__all__ = [
    "MAX_ANCESTOR_DEPTH",
    "SPEC_FILENAME",
    "STRICT_ENV_VAR",
    "SpecPreconditionResult",
    "discover_spec",
    "require_spec",
]

#: The canonical intent artifact in this repository.  Consumer repos with a
#: differently-named artifact pass ``spec_path=`` explicitly.
SPEC_FILENAME = "project.kct"

#: Environment variable that opts into the hard (L2) gate.  ``KCT_*`` is the
#: repo convention (``KCT_ROUTE_GRID``, ``KCT_COUPLED_CPP``, ...).
STRICT_ENV_VAR = "KCT_REQUIRE_SPEC"

#: Bounded ancestor walk depth, matching ``_load_project_kct_for_escalation``.
MAX_ANCESTOR_DEPTH = 6

#: Levels, weakest to strongest.  ``"none"`` means "not even L0".
_LEVEL_ORDER = ("none", "L0", "L1", "L2")

_TRUTHY = frozenset({"1", "true", "yes", "on", "strict", "hard"})
_FALSY = frozenset({"", "0", "false", "no", "off"})


@dataclass(frozen=True)
class SpecPreconditionResult:
    """Structured verdict from :func:`require_spec`.

    Returning a small frozen dataclass rather than a bare ``bool`` mirrors
    :class:`~kicad_tools.recipes.gate.PipelineGateResult` and lets tests
    assert *why* the gate fired instead of matching on printed text.

    Attributes:
        ok: ``True`` when the attained level meets the required level
            (L1 by default, L2 under strict mode).  ``__bool__`` delegates
            here, so ``if not require_spec(...):`` reads naturally.
        level: the strongest level actually attained -- one of ``"none"``,
            ``"L0"``, ``"L1"``, ``"L2"``.
        required_level: the level that had to be attained (``"L1"`` by
            default, ``"L2"`` under strict mode).
        found_path: the discovered intent artifact, or ``None`` when the
            bounded upward walk found nothing.
        searched: every candidate path the walk considered, in order.
        strict: whether the hard gate was engaged for this call.
        checked: ``False`` only when the helper itself failed unexpectedly
            (a bug in the helper, not a violation by the board).  Strict
            mode does NOT terminate the process in that case -- an internal
            error is indistinguishable from "unknown", and a helper bug must
            never take a recipe down.
        reasons: human-readable explanations for a falsy verdict.
    """

    ok: bool
    level: str
    required_level: str
    found_path: Path | None = None
    searched: tuple[Path, ...] = ()
    strict: bool = False
    checked: bool = True
    reasons: tuple[str, ...] = field(default_factory=tuple)

    def __bool__(self) -> bool:
        return self.ok


def _level_rank(level: str) -> int:
    try:
        return _LEVEL_ORDER.index(level)
    except ValueError:  # pragma: no cover - defensive
        return 0


def _env_strict() -> bool:
    """Read :data:`STRICT_ENV_VAR`; unset / falsy strings mean advisory."""
    raw = os.environ.get(STRICT_ENV_VAR)
    if raw is None:
        return False
    value = raw.strip().lower()
    if value in _FALSY:
        return False
    if value in _TRUTHY:
        return True
    # Any other non-empty value is treated as opt-in: a user who typed
    # something into KCT_REQUIRE_SPEC meant to turn it ON.
    return True


def discover_spec(
    anchor: Path | str,
    *,
    filename: str = SPEC_FILENAME,
    max_depth: int = MAX_ANCESTOR_DEPTH,
) -> tuple[Path | None, tuple[Path, ...]]:
    """Walk up from ``anchor`` looking for ``filename``.

    Mirrors the bounded-ancestor discovery in
    ``_load_project_kct_for_escalation``: at most ``max_depth`` directories
    are considered and the walk stops at the filesystem root, so it can
    never hang.

    Args:
        anchor: a directory, or a file whose directory is used.
        filename: artifact filename to look for.
        max_depth: maximum number of directories to consider.

    Returns:
        ``(found_path_or_None, tuple_of_candidates_considered)``.
    """
    start = Path(anchor)
    if start.is_file():
        start = start.parent
    elif start.suffix and not start.is_dir():
        # A non-existent path that looks like a file (e.g. a __file__ whose
        # directory was deleted): treat its parent as the anchor directory.
        start = start.parent

    candidates: list[Path] = []
    cur = start.resolve()
    for _ in range(max_depth):
        candidates.append(cur / filename)
        if cur.parent == cur:  # filesystem root -- stop, never loop
            break
        cur = cur.parent

    for candidate in candidates:
        if candidate.is_file():
            return candidate, tuple(candidates)
    return None, tuple(candidates)


def _caller_file() -> str | None:
    """Best-effort ``__file__`` of the immediate caller of :func:`require_spec`."""
    frame = inspect.currentframe()
    # _caller_file -> require_spec -> the recipe
    for _ in range(2):
        if frame is None:
            return None
        frame = frame.f_back
    if frame is None:
        return None
    value = frame.f_globals.get("__file__")
    return value if isinstance(value, str) else None


def _evaluate(spec_file: Path) -> tuple[str, list[str]]:
    """Return ``(attained_level, reasons)`` for an existing candidate path.

    Runs the L0 -> L1 -> L2 ladder and stops at the first failure.  This is
    where the ladder's ceiling lives: there is intentionally no L3
    unrecognized-key check (see the module docstring).
    """
    reasons: list[str] = []

    # --- L0: exists and is non-empty -------------------------------------
    if not spec_file.is_file():
        return "none", [f"no {spec_file.name} found"]
    try:
        text = spec_file.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return "none", [f"{spec_file} could not be read: {exc}"]
    if not text.strip():
        return "none", [f"{spec_file} is empty"]

    # --- L1: parses -------------------------------------------------------
    from kicad_tools.spec.parser import load_spec, validate_spec

    try:
        load_spec(spec_file)
    except Exception as exc:  # ValueError / ValidationError / FileNotFound
        return "L0", [f"{spec_file} does not parse: {exc}"]

    # --- L2: semantically validates (strict mode only) --------------------
    try:
        valid, errors = validate_spec(spec_file)
    except Exception as exc:  # pragma: no cover - validate_spec is total
        return "L1", [f"{spec_file} could not be validated: {exc}"]
    if not valid:
        reasons.extend(f"{spec_file}: {err}" for err in errors)
        return "L1", reasons

    return "L2", reasons


def require_spec(
    recipe_file: Path | str | None = None,
    *,
    spec_path: Path | str | None = None,
    strict: bool | None = None,
    filename: str = SPEC_FILENAME,
    max_depth: int = MAX_ANCESTOR_DEPTH,
    stream: TextIO | None = None,
) -> SpecPreconditionResult:
    """Assert that a board recipe carries captured intent before it mutates.

    Call this as the first statement of a recipe's ``main()``::

        require_spec(__file__)

    Args:
        recipe_file: discovery anchor -- pass ``__file__``.  Defaults to the
            calling module's ``__file__``.  **Never** pass ``Path.cwd()`` or
            the output directory (see the module docstring).
        spec_path: explicit artifact path, bypassing discovery.  This is the
            escape hatch for a consumer repo whose intent artifact lives
            somewhere else or is named something else.
        strict: ``True`` engages the hard L2 gate, ``False`` forces advisory
            mode, ``None`` (default) reads :data:`STRICT_ENV_VAR`.
        filename: artifact filename for discovery (default ``project.kct``).
        max_depth: bounded ancestor-walk depth.
        stream: where the advisory is written (default ``sys.stderr``).

    Returns:
        A :class:`SpecPreconditionResult`.  Falsy when the precondition is
        not met.

    Raises:
        SystemExit: only in strict mode, and only when the check actually
            ran and failed.  Exit code is ``1``.
    """
    out = stream if stream is not None else sys.stderr
    is_strict = _env_strict() if strict is None else bool(strict)
    required = "L2" if is_strict else "L1"

    try:
        if spec_path is not None:
            candidate = Path(spec_path)
            searched: tuple[Path, ...] = (candidate,)
            found = candidate if candidate.is_file() else None
        else:
            anchor = recipe_file if recipe_file is not None else _caller_file()
            if anchor is None:
                anchor = Path.cwd()  # last resort; documented as unreliable
            found, searched = discover_spec(anchor, filename=filename, max_depth=max_depth)

        if found is None:
            level = "none"
            reasons = [f"no {filename} found near {Path(searched[0]).parent if searched else '?'}"]
        else:
            level, reasons = _evaluate(found)
    except Exception as exc:  # noqa: BLE001 - a helper bug must not kill a recipe
        result = SpecPreconditionResult(
            ok=False,
            level="none",
            required_level=required,
            strict=is_strict,
            checked=False,
            reasons=(f"spec precondition could not be checked: {exc!r}",),
        )
        _print_advisory(result, out, filename=filename)
        return result

    ok = _level_rank(level) >= _level_rank(required)
    result = SpecPreconditionResult(
        ok=ok,
        level=level,
        required_level=required,
        found_path=found,
        searched=searched,
        strict=is_strict,
        checked=True,
        reasons=tuple(reasons),
    )

    if ok:
        return result

    _print_advisory(result, out, filename=filename)
    if is_strict:
        # SystemExit (BaseException) intentionally bypasses the recipes'
        # broad ``except Exception`` handlers so a strict failure cannot be
        # downgraded into their generic error path -- and it happens before
        # the recipe's first write.
        raise SystemExit(1)
    return result


def _print_advisory(
    result: SpecPreconditionResult,
    out: TextIO,
    *,
    filename: str = SPEC_FILENAME,
) -> None:
    """Print the labelled advisory for a falsy verdict."""
    label = "SPEC PRECONDITION FAILED" if result.strict else "spec precondition"
    prefix = "❌" if result.strict else "⚠️"
    print(f"\n{prefix}  {label}: no usable {filename} for this recipe", file=out)
    for reason in result.reasons:
        print(f"   - {reason}", file=out)
    if result.found_path is None and result.searched:
        print("   searched (nearest first):", file=out)
        for candidate in result.searched:
            print(f"     {candidate}", file=out)
    if not result.checked:
        print(
            "   (the check itself failed; treating as UNKNOWN, not as a violation)",
            file=out,
        )
    elif result.strict:
        print(
            f"   strict mode ({STRICT_ENV_VAR}) requires {result.required_level}; "
            f"attained {result.level}. Aborting before any file is written.",
            file=out,
        )
    else:
        print(
            f"   advisory only (attained {result.level}, need {result.required_level}); "
            f"the recipe continues. Set {STRICT_ENV_VAR}=1 to make this fatal.",
            file=out,
        )
    print(
        "   This checks that captured intent EXISTS and parses -- it is NOT a "
        "schema-completeness check.",
        file=out,
    )
