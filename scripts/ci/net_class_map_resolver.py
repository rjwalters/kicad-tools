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
