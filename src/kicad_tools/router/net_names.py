"""Hierarchical net-name normalization and matching (Issue #4149).

KiCad names label-derived nets with the schematic's hierarchical sheet
path prefix (``/FUSED_LINE`` on the root sheet, ``/PWR/VBUS`` inside a
sub-sheet), while power-symbol nets stay global and bare (``GND``,
``+3.3V``).  User-supplied selectors (``--net-class-map`` keys, and in
future ``--skip-nets`` / ``--power-nets`` / ``--analog-nets``) are almost
always written with bare names, so a bare key silently matches zero board
nets whenever the board net carries a ``/`` prefix.

This module provides a single, well-tested normalizer/matcher so those
selectors resolve a bare user key against the *sheet-local suffix* of each
board net name (the segment after the last ``/``), while refusing to
silently guess when a bare key would match more than one distinct board
net.

Only ``--net-class-map`` adopts this helper today (see
``cli/route_cmd.py::_apply_net_class_map_sidecar``); the sibling selectors
are a documented follow-up.  The helper is designed so adopting them later
is an import-and-swap of the membership test.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field


def net_name_suffix(name: str) -> str:
    """Return the sheet-local suffix after the last ``/``.

    Root-sheet and bare names are returned unchanged (``rsplit`` on a name
    with no ``/`` yields the name itself); nested sheet paths collapse to
    their final segment regardless of nesting depth.

    Examples::

        net_name_suffix("/FUSED_LINE")     -> "FUSED_LINE"
        net_name_suffix("/A/B/FUSED_LINE") -> "FUSED_LINE"
        net_name_suffix("GND")             -> "GND"
    """
    return name.rsplit("/", 1)[-1]


def build_net_name_index(board_net_names: Iterable[str]) -> dict[str, list[str]]:
    """Map each sheet-local suffix to the board net names sharing it.

    The returned ``suffix -> [raw board net names]`` index is what lets the
    matcher (a) resolve a bare key to a prefixed board net and (b) detect
    the ambiguous case where more than one distinct board net shares a
    suffix (e.g. both ``/A`` and ``A``, or ``/X/A`` and ``/Y/A``).

    Duplicate raw names collapse (a board never has two nets with the exact
    same name), and insertion order is preserved so warning messages list
    candidates deterministically.
    """
    index: dict[str, list[str]] = {}
    for raw in board_net_names:
        suffix = net_name_suffix(raw)
        bucket = index.setdefault(suffix, [])
        if raw not in bucket:
            bucket.append(raw)
    return index


@dataclass(frozen=True)
class NetKeyResolution:
    """Outcome of resolving one user-supplied selector key.

    Exactly one of the following holds:

    * ``matched`` is a board net name  -> unambiguous match (exact or
      unique-suffix); ``ambiguous`` is empty.
    * ``matched`` is ``None`` and ``ambiguous`` is non-empty -> the bare
      key matches multiple distinct board nets; caller must NOT apply the
      override to any of them.
    * ``matched`` is ``None`` and ``ambiguous`` is empty -> no board net
      matches (typo / renamed net / wrong sheet prefix).
    """

    key: str
    matched: str | None = None
    ambiguous: tuple[str, ...] = ()

    @property
    def is_ambiguous(self) -> bool:
        return self.matched is None and bool(self.ambiguous)


def resolve_net_key(user_key: str, index: dict[str, list[str]]) -> NetKeyResolution:
    """Resolve a single user key against a board-net-name index.

    Resolution order:

    1. **Fully-qualified exact match** — the key contains a ``/`` and is
       itself a raw board net name (a user who wrote ``/FUSED_LINE`` or
       ``/X/A`` explicitly).  A qualified key is unambiguous by
       construction, so it wins outright and is never flagged ambiguous.
    2. **Unique suffix match** — the key's suffix matches exactly one board
       net.  This covers both the bare-key-matches-prefixed case
       (``FUSED_LINE`` -> ``/FUSED_LINE``) and the historical bare-vs-bare
       case (``GND`` -> ``GND``, the sole ``GND`` bucket entry).
    3. **Ambiguous suffix** — the key's suffix matches multiple distinct
       board nets (``A`` when the board has both ``/A`` and ``A``, or
       ``/X/A`` and ``/Y/A``); returns ``matched=None`` with the candidates
       listed.  A *bare* key is refused here even if one candidate is an
       exact string match — silently preferring the exact one would just
       relocate the misconfiguration this module exists to surface.
    4. **No match** — returns an empty resolution.
    """
    # 1. A fully-qualified key (contains '/') that names a real board net
    #    is unambiguous by construction — exact match wins.
    if "/" in user_key:
        for bucket in index.values():
            if user_key in bucket:
                return NetKeyResolution(key=user_key, matched=user_key)

    # 2/3. Suffix match: normalize the key the same way we normalize board
    #      names, then look it up in the suffix index.  A bare key that
    #      collides across multiple board nets is ambiguous even if one of
    #      them is an exact string match.
    candidates = index.get(net_name_suffix(user_key), [])
    if len(candidates) == 1:
        return NetKeyResolution(key=user_key, matched=candidates[0])
    if len(candidates) > 1:
        return NetKeyResolution(key=user_key, ambiguous=tuple(candidates))

    # 4. No board net matches.
    return NetKeyResolution(key=user_key)


def nearest_net_names(user_key: str, board_net_names: Iterable[str], limit: int = 3) -> list[str]:
    """Return board net names that look similar to an unmatched ``user_key``.

    A lightweight, dependency-free hint for the zero-match diagnostic: a
    board net is a candidate when the key is contained in it, it is
    contained in the key, or either shares the other's sheet-local suffix.
    No Levenshtein — the ``/``-prefix case (the actual bug) is handled
    structurally by :func:`resolve_net_key`; this is only advisory text for
    genuinely unresolved keys.

    Results preserve board-net iteration order and are capped at ``limit``.
    """
    key_suffix = net_name_suffix(user_key)
    hits: list[str] = []
    for raw in board_net_names:
        if raw in hits:
            continue
        raw_suffix = net_name_suffix(raw)
        if (
            user_key in raw
            or raw in user_key
            or key_suffix == raw_suffix
            or raw.endswith("/" + user_key)
        ):
            hits.append(raw)
        if len(hits) >= limit:
            break
    return hits


@dataclass
class NetClassMapResolution:
    """Aggregate result of resolving an entire ``--net-class-map`` sidecar.

    Attributes:
        resolved: ``{board_net_name: original_user_key}`` for keys that
            matched exactly one board net.  The board net name is what the
            router keys ``net_class_map`` by, so this is the rekeyed map the
            caller should apply.
        unmatched: user keys that matched no board net.
        ambiguous: ``{user_key: (candidate board nets, ...)}`` for keys
            that matched more than one distinct board net; the caller must
            apply the override to none of them.
    """

    resolved: dict[str, str] = field(default_factory=dict)
    unmatched: list[str] = field(default_factory=list)
    ambiguous: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def total(self) -> int:
        return len(self.resolved) + len(self.unmatched) + len(self.ambiguous)


def resolve_net_class_map_keys(
    user_keys: Iterable[str],
    board_net_names: Iterable[str],
) -> NetClassMapResolution:
    """Resolve every sidecar key against the board's net names.

    Partitions the keys into resolved / unmatched / ambiguous buckets (see
    :class:`NetClassMapResolution`).  The board net names are materialized
    once so the index and the nearest-name hints share a single pass-free
    view.
    """
    board = list(board_net_names)
    index = build_net_name_index(board)
    result = NetClassMapResolution()
    for key in user_keys:
        resolution = resolve_net_key(key, index)
        if resolution.matched is not None:
            result.resolved[resolution.matched] = key
        elif resolution.is_ambiguous:
            result.ambiguous[key] = resolution.ambiguous
        else:
            result.unmatched.append(key)
    return result
