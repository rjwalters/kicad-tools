#!/usr/bin/env python3
"""Net-class-map resolution shared by the routed-PCB CI gates (Issue #3151).

Background
----------

Three DRC rule families -- ``diffpair_length_skew``,
``diffpair_routing_continuity`` and ``match_group_length_skew`` -- re-derive
their working state from ``(pcb, net_class_map)`` and **short-circuit to a
no-op when ``net_class_map is None``**.  This is the documented
graceful-degradation contract for external-router boards (#2684, #2652,
#2675, #2710): a bare ``kct check`` on a Freerouting board must not fire
diff-pair / match-group rules it cannot meaningfully evaluate.

``kct check`` only populates the map when the user passes
``--net-class-map <sidecar.json>``.  The strict CI error-count gate
(:mod:`scripts.ci.check_routed_drc`) historically shelled out to ``kct
check`` with NO sidecar, so it never counted those three families on
routed boards -- a real CI-gate gap (Issue #3151).

This module centralises the logic both gates need to close that gap
WITHOUT changing the standalone-CLI no-op contract:

* :func:`build_net_class_map_for_board` -- import a board's
  ``generate_design.build_net_class_map()`` to derive the map in-process.
  Promoted here from ``check_diffpair_coverage.py`` so both gates share one
  implementation.
* :func:`resolve_net_class_map_sidecar` -- a context manager that yields a
  filesystem path suitable for ``--net-class-map``.  It prefers a committed
  ``net_class_map.json`` sidecar next to the routed PCB (board 07 has one);
  when none exists (board 06 does NOT commit one) it falls back to
  in-process derivation, serialising the derived map to a temporary file
  that is cleaned up on exit.

The standalone ``kct check`` behaviour is unchanged: the no-op contract
lives in the DRC rules, and this module only affects what the CI gates
pass on the command line.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

# Canonical name of the committed sidecar a board's generate_design.py may
# emit next to its routed PCB (Phase 3M pattern; board 07 emits one).
SIDECAR_FILENAME = "net_class_map.json"


def count_blocking_errors(data: dict[str, Any]) -> tuple[int, dict[str, int]]:
    """Filter advisory rules out of a ``kct check --format json`` payload.

    Issue #4008: this is the single shared implementation of the
    blocking-vs-advisory error count used by BOTH routed-PCB CI gates
    (``check_routed_drc.py`` and ``check_matchgroup_coverage.py``).  Before
    it was hoisted here, ``check_routed_drc.py`` filtered advisory rules but
    ``check_matchgroup_coverage.py`` read the raw ``summary.errors`` integer,
    so the two gates compared *different* counts (9 blocking vs 14 raw on
    board 07) against the *same* ``.github/routed-drc-tolerance.yml`` floor.
    That forced the shared floor up to the raw count, silently granting the
    blocking gate dead slack (a 9->13 blocking regression could pass CI).
    Centralising the counter lets both gates agree and lets the floor drop
    to the blocking-only count.

    The gating verdict mirrors the audit pipeline's classifier
    (``DRCChecker.is_advisory_rule``): rules in
    :attr:`DRCChecker.ADVISORY_RULE_IDS` (currently just ``connectivity``)
    surface to consumers but do not block manufacturability.  PR #3060 added
    the ``connectivity`` rule and PR #3064 introduced the central classifier;
    this helper makes the CI gates honour the same severity model as
    ``ManufacturingAudit._check_drc``.

    Args:
        data: Parsed JSON object emitted by ``kct check --format json``.
            Expected to contain ``violations`` (a list with per-violation
            ``rule_id`` and ``severity`` fields) and a ``summary.errors``
            integer (the unfiltered count, used as a fall-back when no
            ``violations`` array is present).

    Returns:
        Tuple of ``(blocking_errors, advisory_by_rule)``:

        * ``blocking_errors`` -- number of error-severity violations whose
          ``rule_id`` is NOT in ``ADVISORY_RULE_IDS``.  This is what the
          gates compare to the allowlist.
        * ``advisory_by_rule`` -- mapping of advisory ``rule_id`` to count
          of error-severity violations of that rule, so callers can still
          print the connectivity findings for diagnostic visibility per
          the issue #3074 AC ("connectivity rule still appears in
          violation reports").

    Raises:
        RuntimeError: If the JSON lacks both a ``violations`` array and a
            ``summary.errors`` integer (the payload is malformed).
    """
    # Import lazily so the module stays importable in contexts where
    # ``kicad_tools`` is not yet on the path (the CI scripts run under
    # ``uv run`` where it always is).  A missing/renamed classifier surfaces
    # here, at the first count, rather than at module import time.
    from kicad_tools.validate.checker import DRCChecker

    violations = data.get("violations")
    if isinstance(violations, list):
        blocking = 0
        advisory_by_rule: dict[str, int] = {}
        for v in violations:
            if not isinstance(v, dict):
                continue
            # Only error-severity violations count toward the gate; warnings
            # are filtered upstream by ``--errors-only`` but we re-check
            # defensively in case a future flag change loosens that.
            severity = v.get("severity", "error")
            if severity != "error":
                continue
            rule_id = v.get("rule_id", "")
            if not isinstance(rule_id, str):
                continue
            if DRCChecker.is_advisory_rule(rule_id):
                advisory_by_rule[rule_id] = advisory_by_rule.get(rule_id, 0) + 1
            else:
                blocking += 1
        return blocking, advisory_by_rule

    # Fall-back: no per-violation array (legacy format).  Trust
    # ``summary.errors``.  Advisory awareness degrades gracefully -- the
    # gates behave exactly as they did before this change in that path.
    summary = data.get("summary", {})
    errors = summary.get("errors")
    if not isinstance(errors, int):
        raise RuntimeError(f"kct check JSON missing both violations and summary.errors: {data!r}")
    return errors, {}


def _import_module_from_path(module_name: str, path: Path) -> Any:
    """Import a module by file path without permanently polluting sys.path."""
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create import spec for {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def load_board_recipe_module(board_dir: Path) -> Any | None:
    """Import a board's ``generate_design.py`` module by path.

    Returns ``None`` when the board has no ``generate_design.py``.  The
    imported module exposes the board's declarative routing contract:
    ``build_net_class_map()`` (boards 06/07), and -- Issue #3413 phase 5
    -- the optional ``POUR_NETS`` / ``REQUIRED_SIGNAL_REACH`` constants
    the diff-pair coverage gate uses for its reach assertion.

    Args:
        board_dir: Path to the board directory (e.g.
            ``boards/06-diffpair-test``).
    """
    script = board_dir / "generate_design.py"
    if not script.is_file():
        return None

    # The board's generate_design.py imports its sibling modules via a
    # ``sys.path.insert(0, str(Path(__file__).parent))`` -- replicate that
    # so the import resolves correctly.
    saved_path = list(sys.path)
    sys.path.insert(0, str(board_dir))
    try:
        return _import_module_from_path(
            f"_ncm_resolver_generate_design_{board_dir.name.replace('-', '_')}",
            script,
        )
    finally:
        sys.path[:] = saved_path


def build_net_class_map_for_board(board_dir: Path) -> dict | None:
    """Import ``generate_design.build_net_class_map()`` from a board dir.

    Returns ``None`` if the board's ``generate_design.py`` does not exist
    or does not expose a ``build_net_class_map`` function (e.g. boards
    01-05).  In that case the diff-pair / match-group rules degrade to a
    no-op, which is correct for non-diff-pair / non-match-group boards.

    Args:
        board_dir: Path to the board directory (e.g.
            ``boards/06-diffpair-test``).

    Returns:
        The ``{net_name: NetClassRouting}`` map, or ``None``.
    """
    mod = load_board_recipe_module(board_dir)
    if mod is None:
        return None
    builder = getattr(mod, "build_net_class_map", None)
    if builder is None:
        return None
    return builder()


def _board_dir_for_pcb(pcb_path: Path) -> Path:
    """Return the board directory that owns a routed PCB.

    The repo convention is ``boards/<name>/output/<board>_routed.kicad_pcb``,
    so the board dir is the PCB's grandparent (``output/`` is the parent).
    """
    return pcb_path.resolve().parent.parent


@contextlib.contextmanager
def resolve_net_class_map_sidecar(pcb_path: Path) -> Iterator[Path | None]:
    """Yield a ``--net-class-map`` sidecar path for a routed PCB.

    Resolution order (Issue #3151, Option B):

    1. **Committed sidecar** -- if ``<pcb_dir>/net_class_map.json`` exists
       (board 07's ``generate_design.py`` emits one), yield it directly.
       This is the same sidecar the board's in-pipeline ``run_drc`` uses,
       so the CI gate counts exactly what ``generate_design.py`` counts.
    2. **In-process derivation** -- otherwise import the board's
       ``generate_design.build_net_class_map()`` and serialise the result
       to a temporary JSON file (board 06 has no committed sidecar).  The
       temp file is removed on context exit.
    3. **No map** -- if neither path yields a map (non-diff-pair boards
       like 01-05), yield ``None``; the caller runs ``kct check`` with no
       sidecar and the rules correctly no-op.

    Args:
        pcb_path: Path to a ``*_routed.kicad_pcb`` file.

    Yields:
        A ``Path`` to a sidecar JSON usable with ``--net-class-map``, or
        ``None`` when no net_class_map is available for this board.
    """
    pcb_path = Path(pcb_path)

    # (1) Prefer a committed sidecar adjacent to the routed PCB.
    committed = pcb_path.resolve().parent / SIDECAR_FILENAME
    if committed.is_file():
        yield committed
        return

    # (2) Fall back to in-process derivation from the board recipe.
    board_dir = _board_dir_for_pcb(pcb_path)
    try:
        net_class_map = build_net_class_map_for_board(board_dir)
    except Exception:
        # A board whose recipe cannot be imported (or which raises while
        # building the map) is treated as "no map available" -- the gate
        # then runs bare, preserving the pre-3151 behaviour for that board
        # rather than crashing the whole CI run.
        net_class_map = None

    if not net_class_map:
        yield None
        return

    # Serialise the derived map to a temp sidecar for ``--net-class-map``.
    # ``mkstemp`` returns an OS-level fd we close immediately after writing;
    # the file outlives the open handle (the subprocess reads it by path)
    # and is unlinked on context exit.
    from kicad_tools.router.rules import net_class_map_to_dict

    fd, tmp_name = tempfile.mkstemp(suffix=f"_{board_dir.name}_net_class_map.json")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w") as fh:
            json.dump(net_class_map_to_dict(net_class_map), fh, indent=2)
        yield tmp_path
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp_path.unlink()
