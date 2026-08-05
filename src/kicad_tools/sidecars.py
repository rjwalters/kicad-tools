"""Shared sidecar-filename resolution (Issue #4634).

A *sidecar* is a small JSON file committed next to a routed PCB that
carries state the ``.kicad_pcb`` cannot express.  The net-class-map
sidecar is the important one: it gates three DRC rule families
(``match_group_length_skew``, ``diffpair_length_skew``,
``diffpair_routing_continuity``) which short-circuit to a no-op when no
map is supplied.

Four independent consumers probe for that file:

1. ``kicad_tools.cli.check_cmd`` -- ``kct check``'s auto-discovery.
2. ``scripts/ci/net_class_map_resolver.py`` -- the routed-DRC CI gate.
3. ``kicad_tools.report.net_class_map`` -- the ``kct export`` report
   surface.
4. ``scripts/ci/check_matchgroup_coverage.py`` -- the match-group CI gate.

Before this module each of them spelled the probe out separately, and
after PR #4629 (#4601) taught consumer 1 about the **stem-keyed**
``<pcb_stem>.net_class_map.json`` convention the four no longer agreed on
the filename set.  In a directory holding *both* forms, ``kct check``
would load the stem-keyed file while both CI merge gates and the report
loaded the bare one -- a different rule set locally than in the gate that
guards merge, with no warning on either side (Issue #4634).

This module is the single source of truth for **which names are probed
and in what order within a directory**.  It deliberately does NOT decide
*which directories* a consumer searches: the four consumers legitimately
differ there (``kct check`` walks three directories, the CI gates and the
report each search exactly one), and silently widening any of them would
let a board resolve a sidecar it does not resolve today.  Callers pass
their own directory list to :func:`first_existing_net_class_map_sidecar`.

Placement rationale (why a top-level leaf module):

* ``src/kicad_tools/`` must never import from ``scripts/ci/`` -- that
  path has no ``__init__.py`` and is excluded from the installed wheel,
  so importing it would break ``pip install kicad-tools``.  The shared
  helper therefore has to live inside the package, and ``scripts/ci/ ->
  kicad_tools`` is the already-established direction (the CI gates run
  under ``uv run``).
* It is NOT folded into ``kicad_tools.report.net_class_map`` because
  importing anything under ``kicad_tools.report`` executes
  ``report/__init__.py``, which pulls the optional ``[report]`` extra
  (jinja2).  A CI gate must not acquire a rendering dependency to
  resolve a filename.

Accordingly this module imports **stdlib only**.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from pathlib import Path

__all__ = [
    "NET_CLASS_MAP_SIDECAR_BASENAME",
    "first_existing_net_class_map_sidecar",
    "net_class_map_sidecar_candidates",
    "net_class_map_sidecar_names",
]

# The bare, board-agnostic sidecar name.  This is what ``kct route``
# writes next to the routed PCB (Issue #3917 Defect 1 / #4428) and what
# every board under ``boards/NN/output/`` commits today.
NET_CLASS_MAP_SIDECAR_BASENAME = "net_class_map.json"


def net_class_map_sidecar_names(pcb_stem: str) -> list[str]:
    """Return the sidecar filenames to probe in one directory, in order.

    Two filename conventions are accepted:

    - ``<pcb_stem>.net_class_map.json`` -- the stem-keyed sidecar
      convention used by hand-maintained board trees that keep several
      revisions side by side (``board_v24.kicad_pcb`` next to
      ``board_v24.net_class_map.json``).
    - ``net_class_map.json`` -- the bare name written by ``kct route``
      (Issue #3917 Defect 1 / #4428).

    Ordering is deliberate: within a directory the **stem-keyed** name
    wins, because it is evidence about *this* board rather than a
    generic file.

    The stem must match **exactly**: no globbing and no un-suffixing
    heuristics.  A directory holding ``board_v23.net_class_map.json``
    and ``board_v24.net_class_map.json`` must never apply v23's
    constraints to a v24 board, and ``board_routed.kicad_pcb`` looks
    only for ``board_routed.net_class_map.json``.

    Args:
        pcb_stem: The PCB filename stem (``Path.stem``), e.g.
            ``"board_routed"`` for ``board_routed.kicad_pcb``.

    Returns:
        Filenames in probe order: stem-keyed first, bare name second.
        A falsy/empty stem yields the bare name only (there is no
        meaningful stem-keyed candidate for it).
    """
    if not pcb_stem:
        return [NET_CLASS_MAP_SIDECAR_BASENAME]
    return [
        f"{pcb_stem}.{NET_CLASS_MAP_SIDECAR_BASENAME}",
        NET_CLASS_MAP_SIDECAR_BASENAME,
    ]


def net_class_map_sidecar_candidates(pcb_path: Path | str) -> list[Path]:
    """Enumerate the candidate sidecar paths ``kct check`` auto-probes.

    This is the widest of the four consumers' probes: three directories
    x the two names from :func:`net_class_map_sidecar_names`, with
    nearer directories winning over farther ones (the pre-#4601
    ``pcb_dir`` -> ``pcb_dir/output`` -> ``pcb_dir.parent/output`` order
    is preserved).

    Args:
        pcb_path: Path to the ``*.kicad_pcb`` being checked.

    Returns:
        Candidate paths in probe order, de-duplicated (the
        ``<board>/output/<pcb>`` layout makes ``pcb_dir`` and
        ``pcb_dir.parent/output`` the same directory).
    """
    pcb_path = Path(pcb_path)
    pcb_dir = pcb_path.parent
    directories = [pcb_dir, pcb_dir / "output", pcb_dir.parent / "output"]
    return _candidate_paths(directories, pcb_path.stem)


def first_existing_net_class_map_sidecar(
    directories: Sequence[Path] | Iterable[Path],
    pcb_stem: str,
) -> Path | None:
    """Return the first existing sidecar over caller-chosen directories.

    The **name** precedence is shared with every other consumer (see
    :func:`net_class_map_sidecar_names`); the **directory** scope is the
    caller's, deliberately.  Consumers that search a single directory
    today must keep passing a single-element sequence -- widening a
    consumer's directory scope changes which sidecar a board resolves and
    is out of scope for the name-unification this module exists to
    provide (Issue #4634 AC4).

    Args:
        directories: Directories to search, nearest-wins first.
        pcb_stem: The PCB filename stem used for the stem-keyed name.

    Returns:
        The first candidate that exists as a file, or ``None``.
    """
    for candidate in _candidate_paths(directories, pcb_stem):
        if candidate.is_file():
            return candidate
    return None


def _candidate_paths(
    directories: Sequence[Path] | Iterable[Path],
    pcb_stem: str,
) -> list[Path]:
    """Cross directories with names, preserving order and de-duplicating."""
    names = net_class_map_sidecar_names(pcb_stem)
    candidates: list[Path] = []
    seen: set[Path] = set()
    for directory in directories:
        for name in names:
            candidate = Path(directory) / name
            if candidate in seen:
                continue
            seen.add(candidate)
            candidates.append(candidate)
    return candidates
