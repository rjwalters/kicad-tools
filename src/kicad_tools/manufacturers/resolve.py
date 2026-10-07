"""Shared ``--mfr`` / ``--manufacturer`` default resolution (Issues #3920, #6155, #6169).

A ``.kicad_pcb`` carries no embedded fab-tier hint, so every command that takes
a manufacturer profile used to hard-default to the base ``jlcpcb`` tier and
disagreed with ``kct check`` on boards that declare ``target_fab:
jlcpcb-tier1`` in ``project.kct``.  This module is the single resolver they all
share:

* :func:`_resolve_effective_check_mfr` -- the precedence ladder (explicit flag >
  ``fab_profile.json`` sidecar > ``project.kct`` ``target_fab`` > default).
* :func:`resolve_cli_manufacturer` -- the CLI-facing wrapper that also prints
  the ``[INFO] auto-loaded fab profile ...`` line, for commands whose
  ``--mfr`` default is a ``None`` sentinel.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Literal, NamedTuple

from kicad_tools.manufacturers import get_manufacturer_ids
from kicad_tools.sync.discover import resolve_target_fab_for_pcb


def _discover_fab_profile_sidecar(pcb_path: Path) -> Path | None:
    """Probe conventional locations for a ``fab_profile.json`` sidecar (#3920).

    ``kct route`` writes a ``fab_profile.json`` sidecar next to the routed PCB
    recording the resolved manufacturer profile (``--manufacturer``).  ``kct
    check`` should auto-load it so the effective ``--mfr`` matches the tier the
    board was routed against -- without the user having to pass ``--mfr`` by
    hand.  Mirrors :func:`_discover_net_class_map_sidecar`'s probe locations
    exactly.

    Returns:
        The first existing candidate path, or ``None`` when no sidecar is
        found.
    """
    pcb_dir = pcb_path.parent
    candidates = [
        pcb_dir / "fab_profile.json",
        pcb_dir / "output" / "fab_profile.json",
        pcb_dir.parent / "output" / "fab_profile.json",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


MfrResolutionSource = Literal["cli", "sidecar", "project_kct", "default"]
"""Where an effective ``--mfr`` value came from (#4701).

Mirrors the four precedence tiers of :func:`_resolve_effective_check_mfr` 1:1,
so downstream consumers (e.g. the via-in-pad advisory) can word their output
without re-deriving how the tier was resolved.
"""


class ResolvedMfr(NamedTuple):
    """Result of :func:`_resolve_effective_check_mfr` (#4701).

    ``source`` records which precedence tier produced ``mfr`` so callers can
    report *how* the tier was resolved (declared vs. defaulted) instead of
    only *what* it resolved to.
    """

    mfr: str
    messages: list[str]
    source: MfrResolutionSource


def _resolve_effective_check_mfr(
    cli_mfr: str | None,
    pcb_path: Path,
    default: str = "jlcpcb",
    *,
    consult_sidecar: bool = True,
) -> ResolvedMfr:
    """Resolve the manufacturer profile ``kct check`` judges against (#3920).

    A routed ``.kicad_pcb`` carries no embedded fab-tier hint, so bare ``kct
    check`` used to hard-default to the base ``jlcpcb`` tier and report a false
    ``FAILED`` on boards that route legal, tier-gated geometry (e.g.
    via-in-pad, legal at ``jlcpcb-tier1``).  This resolves the effective
    profile from every available source with a documented precedence.

    Precedence (highest first):

    1. Explicit ``--mfr`` flag (``cli_mfr is not None``).  Always wins, mirroring
       ``build_cmd._resolve_effective_mfr``'s "explicit flag wins" contract.
    2. Auto-discovered ``fab_profile.json`` sidecar written by ``kct route``.
    3. Discovered ``project.kct`` ``target_fab``.
    4. The historical ``default`` (``"jlcpcb"``).

    ``consult_sidecar=False`` skips tier 2.  ``kct route`` resolves its
    ``--manufacturer`` through this same function (#6155) but must not read
    ``fab_profile.json``: that sidecar is route's own *output* record, so
    consuming it as an input would make a previous run's tier (including a
    ``--mfr-tier-ladder`` escalation result) sticky across re-routes and
    shadow a later ``project.kct`` edit.

    A malformed / empty sidecar, or an unknown profile id from either the
    sidecar or ``project.kct``, degrades gracefully: warn and fall back to the
    next precedence tier (mirroring the net-class-map malformed-sidecar
    handling).

    Returns:
        A :class:`ResolvedMfr` of ``(effective_mfr, messages, source)`` where
        ``messages`` are stderr lines (``[INFO] auto-loaded ...`` /
        ``WARNING: ignoring ...``) the caller should print, and ``source``
        names which precedence tier produced ``effective_mfr`` (#4701). Kept
        as return values (not printed here) so the resolution is
        unit-testable in isolation.
    """
    messages: list[str] = []

    # Precedence 1: an explicit flag always wins.
    if cli_mfr is not None:
        return ResolvedMfr(cli_mfr, messages, "cli")

    valid_ids = set(get_manufacturer_ids())

    # Precedence 2: fab_profile.json sidecar.
    sidecar = _discover_fab_profile_sidecar(pcb_path) if consult_sidecar else None
    if sidecar is not None:
        mfr: str | None = None
        try:
            data = json.loads(sidecar.read_text())
        except (OSError, json.JSONDecodeError) as e:
            messages.append(f"WARNING: ignoring malformed fab-profile sidecar {sidecar}: {e}")
        else:
            mfr = data.get("mfr") if isinstance(data, dict) else None
            if not mfr:
                messages.append(f"WARNING: ignoring fab-profile sidecar {sidecar}: no 'mfr' field")
            elif mfr not in valid_ids:
                messages.append(
                    f"WARNING: ignoring fab-profile sidecar {sidecar}: unknown profile {mfr!r}"
                )
            else:
                messages.append(f"[INFO] auto-loaded fab profile: {mfr} (from {sidecar})")
                return ResolvedMfr(mfr, messages, "sidecar")

    # Precedence 3: project.kct target_fab.
    target_fab = resolve_target_fab_for_pcb(pcb_path)
    if target_fab:
        if target_fab not in valid_ids:
            messages.append(
                f"WARNING: ignoring project.kct target_fab {target_fab!r}: unknown profile"
            )
        else:
            messages.append(
                f"[INFO] auto-loaded fab profile: {target_fab} (from project.kct target_fab)"
            )
            return ResolvedMfr(target_fab, messages, "project_kct")

    # Precedence 4: historical default.
    return ResolvedMfr(default, messages, "default")


def resolve_cli_manufacturer(
    cli_mfr: str | None,
    path: str | Path,
    default: str = "jlcpcb",
    *,
    consult_sidecar: bool = True,
) -> str:
    """Resolve a command's ``--mfr`` and print the resolver's notices (#6169).

    ``path`` is the board (``.kicad_pcb`` / ``.kicad_pro``) or a project
    directory.  For a directory, ``project.kct`` is looked up in that directory
    first (then one level up, as for a board next to it).  Precedence matches
    ``kct check``: explicit flag > (sidecar, when ``consult_sidecar``) >
    ``project.kct`` ``target_fab`` > ``default``.  Notices go to stderr.

    Commands that *write* a ``fab_profile.json`` (``route``) must pass
    ``consult_sidecar=False`` so they never read their own previous output;
    read-only commands keep the default.
    """
    p = Path(path)
    # resolve_target_fab_for_pcb / the sidecar probe look relative to
    # ``path.parent``; for a project directory use a synthetic child so the
    # directory itself is searched first.
    anchor = p / "_" if p.is_dir() else p
    resolved = _resolve_effective_check_mfr(
        cli_mfr, anchor, default, consult_sidecar=consult_sidecar
    )
    for line in resolved.messages:
        print(line, file=sys.stderr)
    return resolved.mfr
