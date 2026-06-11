"""Pipeline-level connectivity invariant helpers (issue #2596).

The trace optimiser and DRC nudge passes occasionally drop pads from
multi-pad signal nets despite the per-route guards inside
:class:`TraceOptimizer`.  Issue #2596 documents an example on the
chorus-test board where ``AUDIO_R`` regressed from 5/6 to 3/6 connected
pads across the optimise step.

This module provides two complementary primitives:

* :func:`snapshot_connectivity` -- captures a deep copy of the current
  routes for every multi-pad signal net plus a per-net connectivity
  status dict (``connected`` / ``connected_pads`` / ``total_pads``).
  The snapshot is taken **before** a phase that mutates ``router.routes``.

* :func:`enforce_connectivity_invariant` -- compares post-phase
  connectivity against the snapshot, identifies regressed nets (nets
  that were fully connected before but are not after), reverts those
  nets to their pre-phase state in ``router.routes``, and returns a
  result object describing what happened.  In ``strict`` mode a
  ``ConnectivityRegressionError`` is raised instead of reverting so
  the calling CLI path can exit with a non-zero status.

The check is intentionally cheap: it reuses the existing
:func:`router.observability.validate_net_connectivity` and only deep
copies the routes that belong to the multi-pad signal nets being
tracked.
"""

from __future__ import annotations

import copy
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .observability import validate_net_connectivity

if TYPE_CHECKING:
    from .core import Autorouter
    from .primitives import Pad, Route

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------


@dataclass
class ConnectivitySnapshot:
    """Pre-phase connectivity snapshot for revert-on-regression.

    Attributes:
        net_pads: Mapping of net ID to the list of pads (as supplied at
            snapshot time) used for the connectivity check.  This is the
            authoritative pad set for the invariant -- it is reused for
            the post-phase check so before/after totals are comparable.
        pre_connectivity: Per-net connectivity dict returned by
            :func:`validate_net_connectivity` at snapshot time.
        pre_routes_by_net: Deep copies of every ``Route`` keyed by net
            ID.  Routes for multi-pad signal nets only.
        net_names: Mapping of net ID to net name for logging.
    """

    net_pads: dict[int, list[Pad]]
    pre_connectivity: dict[int, dict]
    pre_routes_by_net: dict[int, list[Route]]
    net_names: dict[int, str] = field(default_factory=dict)


@dataclass
class ConnectivityInvariantResult:
    """Outcome of an :func:`enforce_connectivity_invariant` call.

    Attributes:
        regressed_nets: Set of net IDs that were fully connected
            pre-phase and are not after the phase.  Empty when no
            regression occurred.
        reverted: ``True`` when at least one net was reverted to its
            pre-phase routes.  Always ``False`` in strict mode (the
            error is raised before revert).
        per_net_diff: Mapping of net ID to a tuple
            ``(pre_connected_pads, post_connected_pads, total_pads,
            net_name, regressed_bool)`` for every multi-pad signal
            net.  Used by ``--verbose`` reporting.
    """

    regressed_nets: set[int] = field(default_factory=set)
    reverted: bool = False
    per_net_diff: dict[int, tuple[int, int, int, str, bool]] = field(default_factory=dict)


class ConnectivityRegressionError(RuntimeError):
    """Raised in strict mode when a phase reduces fully-connected nets.

    Carries the underlying :class:`ConnectivityInvariantResult` so the
    CLI can format a useful exit message.
    """

    def __init__(self, phase: str, result: ConnectivityInvariantResult):
        regressed_names = sorted({result.per_net_diff[nid][3] for nid in result.regressed_nets})
        super().__init__(
            f"Connectivity invariant violated by phase '{phase}': "
            f"{len(result.regressed_nets)} net(s) regressed: "
            f"{', '.join(regressed_names) if regressed_names else '(unknown)'}"
        )
        self.phase = phase
        self.result = result


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def build_multi_pad_net_pads(
    router: Autorouter,
    multi_pad_net_ids: set[int] | None = None,
) -> dict[int, list[Pad]]:
    """Build the ``{net_id: [Pad, ...]}`` mapping used by the invariant.

    Mirrors the construction used by the post-save
    ``verify_output_connectivity`` block in ``route_cmd.py`` so the
    pre-phase totals are directly comparable.

    Args:
        router: The :class:`Autorouter` instance.
        multi_pad_net_ids: Optional pre-computed set of net IDs that
            should be tracked.  When ``None`` the function derives the
            set from ``router.nets`` (every net with at least 2 pads).

    Returns:
        Mapping of net ID to a list of :class:`Pad` objects.  Only
        nets with at least 2 pads are included.
    """
    if multi_pad_net_ids is None:
        multi_pad_net_ids = {nid for nid, keys in router.nets.items() if nid > 0 and len(keys) >= 2}
    net_pads: dict[int, list[Pad]] = {}
    for net_id, pad_keys in router.nets.items():
        if net_id not in multi_pad_net_ids:
            continue
        pad_list = [router.pads[k] for k in pad_keys if k in router.pads]
        if len(pad_list) >= 2:
            net_pads[net_id] = pad_list
    return net_pads


def snapshot_connectivity(
    router: Autorouter,
    multi_pad_net_ids: set[int] | None = None,
) -> ConnectivitySnapshot:
    """Capture pre-phase connectivity and a deep copy of routes per net.

    Args:
        router: The :class:`Autorouter` whose routes will be mutated
            by an upcoming phase (optimize / nudge / cleanup).
        multi_pad_net_ids: Optional set of net IDs to track.  When
            ``None`` it is derived from ``router.nets``.

    Returns:
        A :class:`ConnectivitySnapshot` to pass to
        :func:`enforce_connectivity_invariant` after the phase runs.
    """
    net_pads = build_multi_pad_net_pads(router, multi_pad_net_ids)
    tracked_nets = set(net_pads.keys())

    # Deep copy the routes that belong to tracked nets.  We deliberately
    # deep copy rather than relying on the caller to keep references
    # because optimize_route() returns a *new* Route object whose
    # segments may share identity with the input -- holding the
    # original Route reference is not enough.
    pre_routes_by_net: dict[int, list[Route]] = {}
    for r in router.routes:
        if r.net in tracked_nets:
            pre_routes_by_net.setdefault(r.net, []).append(copy.deepcopy(r))

    pre_connectivity = validate_net_connectivity(router.routes, net_pads)

    net_names = {nid: router.net_names.get(nid, f"Net {nid}") for nid in tracked_nets}

    return ConnectivitySnapshot(
        net_pads=net_pads,
        pre_connectivity=pre_connectivity,
        pre_routes_by_net=pre_routes_by_net,
        net_names=net_names,
    )


def enforce_connectivity_invariant(
    router: Autorouter,
    snapshot: ConnectivitySnapshot,
    phase: str,
    *,
    strict: bool = False,
    verbose: bool = False,
    quiet: bool = False,
    detect_partial_regressions: bool = True,
) -> ConnectivityInvariantResult:
    """Verify the post-phase invariant and revert regressed nets.

    The invariant is: ``connected_pads(post) >= connected_pads(pre)``
    measured on the multi-pad signal nets captured by
    :func:`snapshot_connectivity`.  When violated, the routes for every
    regressed net are reverted in place (default mode) or a
    :class:`ConnectivityRegressionError` is raised (strict mode).

    **Issue #3124**: The original detector only fired on True->False
    transitions of the fully-connected boolean (``pre_connected and not
    post_connected``).  This missed the case where a net was already
    *partially* connected pre-phase (e.g. 3/4 pads reachable) and the
    phase degraded it further (e.g. 1/4 pads).  Board 05's optimize
    phase dropped 5 nets from the iter-1 best snapshot because 13 of the
    32 nets were only partially-connected when the invariant fired and
    those degradations were silently allowed.  The detector is now
    pad-count based: any decrease in ``connected_pads`` triggers a
    revert (when ``detect_partial_regressions=True``, the default).

    Args:
        router: The :class:`Autorouter` whose routes were just mutated.
            ``router.routes`` is updated in place when revert happens.
        snapshot: The snapshot taken before the phase.
        phase: Short label identifying the phase that ran (e.g.
            ``"optimize"`` or ``"nudge"``).  Used in log messages and
            in the strict-mode error.
        strict: When True, raise :class:`ConnectivityRegressionError`
            on regression instead of reverting.
        verbose: When True (and not ``quiet``) emit one ``print`` line
            per multi-pad signal net describing the before/after pad
            count and whether a revert happened.  When False, only
            regressed nets are logged via :mod:`logging` warnings.
        quiet: Suppress all ``print`` output even when ``verbose``.
        detect_partial_regressions: When True (the default, issue
            #3124), any net whose ``connected_pads`` strictly decreases
            is treated as a regression and reverted.  When False, the
            legacy behaviour is restored: only fully-connected
            (True->False) regressions trigger a revert.  Provided as a
            kill-switch in case a downstream caller has a reason to
            tolerate partial degradation.

    Returns:
        :class:`ConnectivityInvariantResult` describing what happened.
    """
    post_conn = validate_net_connectivity(router.routes, snapshot.net_pads)

    result = ConnectivityInvariantResult()

    # Build per-net diff for verbose reporting and identify regressions.
    for nid, post_info in post_conn.items():
        pre_info = snapshot.pre_connectivity.get(nid, {})
        pre_connected = bool(pre_info.get("connected", False))
        post_connected = bool(post_info.get("connected", False))
        pre_pads = int(pre_info.get("connected_pads", 0))
        post_pads = int(post_info.get("connected_pads", 0))
        total_pads = int(post_info.get("total_pads", 0))
        net_name = snapshot.net_names.get(nid, f"Net {nid}")

        # Issue #3124: detect partial regressions, not just True->False.
        # The original check (pre_connected and not post_connected) only
        # fired when a fully-connected net became disconnected; it
        # silently allowed a partial net (e.g. 3/4 pads) to be further
        # degraded by the optimize / nudge / finalize phases.
        if detect_partial_regressions:
            regressed = post_pads < pre_pads
        else:
            regressed = pre_connected and not post_connected
        if regressed:
            result.regressed_nets.add(nid)

        result.per_net_diff[nid] = (
            pre_pads,
            post_pads,
            total_pads,
            net_name,
            regressed,
        )

    if not result.regressed_nets:
        if verbose and not quiet:
            print(
                f"  Connectivity invariant ({phase}): no regressions "
                f"({len(post_conn)} multi-pad nets checked)"
            )
        return result

    # Strict mode: raise before reverting so the caller can fail fast.
    if strict:
        if not quiet:
            print(
                f"  ERROR: connectivity regression in phase '{phase}' "
                f"({len(result.regressed_nets)} net(s)):"
            )
            for nid in sorted(result.regressed_nets):
                pre_pads, post_pads, total_pads, net_name, _ = result.per_net_diff[nid]
                print(
                    f"    {net_name}: pre {pre_pads}/{total_pads} -> post {post_pads}/{total_pads}"
                )
        raise ConnectivityRegressionError(phase, result)

    # Default: revert regressed nets to pre-phase routes.
    reverted_nets = result.regressed_nets
    removed_routes = [r for r in router.routes if r.net in reverted_nets]
    restored_routes: list[Route] = []
    new_routes = [r for r in router.routes if r.net not in reverted_nets]
    for nid in reverted_nets:
        restored = snapshot.pre_routes_by_net.get(nid, [])
        new_routes.extend(restored)
        restored_routes.extend(restored)
    router.routes = new_routes
    result.reverted = True

    # Issue #3507: the revert just mutated ``router.routes`` -- re-mark
    # the grid so its occupancy reflects the restored pre-phase copper
    # instead of the regressed post-phase geometry.  Expressed as pure
    # removals + insertions because the per-net route counts may differ
    # between the two states.  Defensive getattr: unit tests drive this
    # with stub routers that carry routes but no grid.
    grid = getattr(router, "grid", None)
    if grid is not None:
        replacements: list[tuple[Route | None, Route | None]] = [
            (r, None) for r in removed_routes
        ] + [(None, r) for r in restored_routes]
        grid.resync_route_occupancy(replacements)

    # Always log a warning so the regression surfaces in CI even when
    # --verbose is not set.
    regressed_names = sorted({snapshot.net_names.get(nid, f"Net {nid}") for nid in reverted_nets})
    logger.warning(
        "Connectivity regression in phase '%s' for %d net(s) (reverted): %s",
        phase,
        len(reverted_nets),
        ", ".join(regressed_names),
    )

    if not quiet:
        print(
            f"  WARNING: phase '{phase}' regressed connectivity for "
            f"{len(reverted_nets)} net(s) (reverted):"
        )
        for nid in sorted(reverted_nets):
            pre_pads, post_pads, total_pads, net_name, _ = result.per_net_diff[nid]
            print(
                f"    {net_name}: pre {pre_pads}/{total_pads} "
                f"-> post {post_pads}/{total_pads} (reverted)"
            )
    elif verbose:
        # quiet=True overrides verbose for stdout, but we still want a
        # programmatic indication in the result.
        pass

    if verbose and not quiet:
        # Emit the full diff (regressed + non-regressed) for debugging.
        print(f"  Connectivity invariant ({phase}) per-net diff:")
        for nid in sorted(result.per_net_diff):
            pre_pads, post_pads, total_pads, net_name, regr = result.per_net_diff[nid]
            tag = " (reverted)" if regr else ""
            print(
                f"    {net_name}: pre {pre_pads}/{total_pads} -> post {post_pads}/{total_pads}{tag}"
            )

    return result
