"""Structured diff-pair outcomes (issue #6837, slice 2c-ii).

A routed diff pair's result used to exist only as a string (``"coupled"``,
``"uncoupled-fallback:pair-no-path"``, ``"partially-coupled:1/2"`` ...).  Those
strings are a contract: the router's ``Diff pairs (lattice):`` summary prints
them, ``scripts/research/diffpair_bench.py`` parses them into its pair digest,
and the retry gates compare them.  This module gives them a structured form
without changing a single character of that text:

* :class:`SegmentOutcome` is one coupled segment's result;
  :attr:`SegmentOutcome.label` is the exact legacy string and
  :meth:`SegmentOutcome.from_label` parses it back.
* :class:`PairOutcome` is a whole pair (one or more chain segments);
  :attr:`PairOutcome.label` is the exact legacy aggregate.
* :class:`SegmentOutcomes` is the ``key -> label`` mapping the engines already
  keep, which also remembers the record behind every label.  It is a ``dict``
  of strings, so every existing comparison keeps working, and the label is
  always *derived* from the record, so the two cannot drift.

The decline-reason vocabulary the retry gates share also lives here
(:data:`EXHAUSTED_SEARCH_REASONS`, :data:`GATEWAY_RETRY_REASONS`).  Like the
rest of :mod:`kicad_tools.router.pairplan`, nothing here imports an engine.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

#: Decline reasons the lattice ``_route_impl`` returns when its A* actually RAN
#: and was exhausted (issues #6373, #4605, #4979).  The plain value and its two
#: "constrained" refinements differ only in which diagnostic label wins when a
#: keepout mask or a hard layer set pruned candidates on the way -- every one of
#: them still means "a search ran and found no path".  Consumers that gate on
#: exhaustion (the #6535 tap-chain retry, issue #6743) must use this set rather
#: than compare one literal, so a diagnostic refinement cannot silently disable
#: them.  ``no-egress-start``/``-end`` are deliberately NOT here: they name a
#: dead-end escape at an endpoint, not an exhausted corridor search.
REASON_NO_PATH = "no-path"
REASON_NO_PATH_KEEPOUT = "no-path-keepout-constrained"
REASON_NO_PATH_LAYER = "no-path-layer-constrained"
EXHAUSTED_SEARCH_REASONS: frozenset[str] = frozenset(
    {REASON_NO_PATH, REASON_NO_PATH_KEEPOUT, REASON_NO_PATH_LAYER}
)

#: Issue #6444: pair-segment decline reasons that earn ONE fan-via gateway
#: retry: an exhausted planar search (every :data:`EXHAUSTED_SEARCH_REASONS`
#: refinement, with the ``pair-`` prefix the pair search adds) or emitted legs
#: clipping a keepout.  A named set, like the #6535 tap-retry gate, so a
#: diagnostic refinement of a reason cannot silently disable the retry.
GATEWAY_RETRY_REASONS: frozenset[str] = frozenset(
    {f"pair-{r}" for r in EXHAUSTED_SEARCH_REASONS} | {"pair-leg-keepout"}
)

#: Issue #6446: gateway-retry declines that do NOT earn the one-ended hybrid
#: retry -- no other layer to couple on, the via budget, or the deadline.  Every
#: other ``pair-gateway-*`` decline (no candidate site, no path, a pass-under
#: body that could not attach ...) means the gateway search ran and the
#: segment still has no coupled body, which is when a hybrid may still help.
HYBRID_SKIP_REASONS: frozenset[str] = frozenset(
    {"pair-gateway-no-layer", "pair-gateway-via-limit", "pair-gateway-deadline"}
)


def hybrid_retry_reason(reason: str) -> bool:
    """Whether a gateway-retry decline ``reason`` earns the hybrid retry (#6446)."""
    return reason.startswith("pair-gateway-") and reason not in HYBRID_SKIP_REASONS


#: Prefix of a segment routed as two single-ended legs after its coupled run
#: declined (issue #6373).
FALLBACK_PREFIX = "uncoupled-fallback:"

#: Prefix of a segment routed as a coupled trunk plus single-ended legs at its
#: hard terminal (issue #6446): ``hybrid:<ref>.<pin>``.
HYBRID_PREFIX = "hybrid:"

SegmentStatus = Literal[
    "coupled", "uncoupled-fallback", "hybrid", "declined", "deadline-exceeded", "not-attempted"
]

#: Statuses whose label is the status itself.
_BARE_STATUSES: frozenset[str] = frozenset({"coupled", "deadline-exceeded", "not-attempted"})


def _label_is_ambiguous(reason: str | None) -> bool:
    """Whether a declined ``reason`` would parse back as another status."""
    return reason is not None and (
        reason in _BARE_STATUSES
        or reason.startswith(FALLBACK_PREFIX)
        or reason.startswith(HYBRID_PREFIX)
    )


@dataclass(frozen=True)
class SegmentOutcome:
    """One coupled segment's result.

    ``status`` is what happened:

    * ``"coupled"`` -- the segment routed as a coupled pair (``gateway`` names
      the fan-via gateway when it coupled through one, issue #6444);
    * ``"uncoupled-fallback"`` -- the coupled run declined for ``reason`` and
      the segment was routed as two single-ended legs (issue #6373);
    * ``"hybrid"`` -- a coupled trunk from one end to a trunk start outside
      the other (``reason`` names that hard terminal, ``"U4.B1"``), joined to
      the pads by two single-ended legs (issue #6446);
    * ``"declined"`` -- the coupled run declined for ``reason`` and nothing
      was emitted;
    * ``"deadline-exceeded"`` / ``"not-attempted"`` -- no attempt finished.
    """

    status: SegmentStatus
    reason: str | None = None
    gateway: str | None = None

    def __post_init__(self) -> None:
        needs_reason = self.status in ("uncoupled-fallback", "hybrid", "declined")
        if needs_reason and self.reason is None:
            raise ValueError(f"a {self.status!r} segment outcome needs a reason")
        if not needs_reason and self.reason is not None:
            raise ValueError(f"a {self.status!r} segment outcome takes no reason")
        if self.gateway is not None and self.status != "coupled":
            raise ValueError("only a coupled segment outcome names a gateway")
        if self.status == "declined" and _label_is_ambiguous(self.reason):
            # A declined label is its bare reason, so a reason that reads as
            # another status's label would parse back as that status
            # (Issue #6948).
            raise ValueError(
                f"declined reason {self.reason!r} would read back as a different "
                "status; use a reason that is not a bare status and does not start "
                f"with {FALLBACK_PREFIX!r} or {HYBRID_PREFIX!r}"
            )

    @classmethod
    def coupled(cls, gateway: str | None = None) -> SegmentOutcome:
        return cls("coupled", gateway=gateway)

    @classmethod
    def fallback(cls, reason: str) -> SegmentOutcome:
        return cls("uncoupled-fallback", reason)

    @classmethod
    def hybrid(cls, terminal: str) -> SegmentOutcome:
        return cls("hybrid", terminal)

    @classmethod
    def declined(cls, reason: str) -> SegmentOutcome:
        return cls("declined", reason)

    @property
    def label(self) -> str:
        """The exact legacy outcome string (what the router prints)."""
        if self.status in _BARE_STATUSES:
            return self.status
        if self.status == "uncoupled-fallback":
            return f"{FALLBACK_PREFIX}{self.reason}"
        if self.status == "hybrid":
            return f"{HYBRID_PREFIX}{self.reason}"
        assert self.reason is not None  # __post_init__
        return self.reason

    @classmethod
    def from_label(cls, label: str) -> SegmentOutcome:
        """Parse a legacy segment outcome string; ``from_label(s).label == s``.

        A gateway name is not part of the label, so it cannot be recovered.
        """
        if label in _BARE_STATUSES:
            return cls(label)  # type: ignore[arg-type]
        if label.startswith(FALLBACK_PREFIX):
            return cls.fallback(label[len(FALLBACK_PREFIX) :])
        if label.startswith(HYBRID_PREFIX):
            return cls.hybrid(label[len(HYBRID_PREFIX) :])
        return cls.declined(label)


def aggregate_label(segment_labels: list[str]) -> str:
    """One diff-pair outcome from its chain segments' labels (issue #6378).

    * no segment: ``"not-attempted"``;
    * a single segment (every pair without an interior part): its outcome,
      unchanged;
    * every segment ``coupled``: ``"coupled"``;
    * every segment in the #6373 fallback: ``"uncoupled-fallback:<reason>"``
      (the first segment's reason);
    * some segments coupled: ``"partially-coupled:<k>/<n>"`` (a ``hybrid:``
      segment (issue #6446) counts as not coupled here -- v1 does not give a
      chain its own hybrid label);
    * otherwise (no segment coupled, not all in fallback): the first
      non-coupled segment's outcome.
    """
    if not segment_labels:
        return "not-attempted"
    if len(segment_labels) == 1:
        return segment_labels[0]
    n = len(segment_labels)
    k = sum(1 for o in segment_labels if o == "coupled")
    if k == n:
        return "coupled"
    if k > 0:
        return f"partially-coupled:{k}/{n}"
    if all(o.startswith(FALLBACK_PREFIX) for o in segment_labels):
        return segment_labels[0]
    return next(o for o in segment_labels if not o.startswith(FALLBACK_PREFIX))


@dataclass(frozen=True)
class PairOutcome:
    """A whole diff pair's result.

    ``segments`` holds ``(span, outcome)`` per chain segment in chain order,
    where ``span`` is the segment's ``"J1.B6->U2.3"`` label.  ``pre_chain`` is
    set instead when the pair never got a segment (``"no-endpoint-couples"``,
    ``"no-net-class"``).
    """

    pair_name: str
    segments: tuple[tuple[str, SegmentOutcome], ...] = ()
    pre_chain: str | None = None

    @property
    def label(self) -> str:
        """The exact legacy pair outcome string (what the router prints)."""
        if self.pre_chain is not None:
            return self.pre_chain
        return aggregate_label([o.label for _span, o in self.segments])

    @property
    def coupled_segments(self) -> tuple[int, int]:
        """``(coupled, total)`` chain segments."""
        k = sum(1 for _span, o in self.segments if o.status == "coupled")
        return k, len(self.segments)


class SegmentOutcomes(dict[Any, str]):
    """``key -> outcome label``, remembering the record behind each label.

    Assign a :class:`SegmentOutcome` (``m[key] = SegmentOutcome.coupled()``)
    and the mapping stores its :attr:`~SegmentOutcome.label`; read the record
    back with :meth:`record`.  A plain string is accepted too and parsed with
    :meth:`SegmentOutcome.from_label`.  Being a ``dict`` of strings, it
    compares, ``.get``\\ s and prints exactly like the plain dict it replaces.
    """

    def __init__(self, items: Mapping[Any, str | SegmentOutcome] | None = None) -> None:
        super().__init__()
        self._records: dict[Any, SegmentOutcome] = {}
        for key, value in (items or {}).items():
            self[key] = value

    def __setitem__(self, key: Any, value: str | SegmentOutcome) -> None:
        if not isinstance(value, (str, SegmentOutcome)):
            # ``setdefault(key)`` / ``fromkeys(keys)`` default to None, which
            # has no label (Issue #6948).
            raise TypeError(
                "SegmentOutcomes values must be a SegmentOutcome or an outcome "
                f"label string, not {type(value).__name__}"
            )
        rec = value if isinstance(value, SegmentOutcome) else SegmentOutcome.from_label(value)
        self._records[key] = rec
        super().__setitem__(key, rec.label)

    def __delitem__(self, key: Any) -> None:
        super().__delitem__(key)
        del self._records[key]

    def pop(self, key: Any, *default: Any) -> Any:
        self._records.pop(key, None)
        return super().pop(key, *default)

    def popitem(self) -> tuple[Any, str]:
        key, value = super().popitem()
        del self._records[key]
        return key, value

    def clear(self) -> None:
        super().clear()
        self._records.clear()

    def setdefault(self, key: Any, default: Any = None) -> Any:
        if key not in self:
            self[key] = default
        return self[key]

    def update(self, *args: Any, **kwargs: Any) -> None:
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def __ior__(self, other: Any) -> SegmentOutcomes:  # type: ignore[override,misc]
        self.update(other)
        return self

    def copy(self) -> SegmentOutcomes:
        return SegmentOutcomes(self._records)

    def __reduce__(self) -> tuple[Any, ...]:
        # ``copy``/``deepcopy``/``pickle`` rebuild from the records, so the
        # parts a label cannot carry (a gateway name) survive.
        return (SegmentOutcomes, (dict(self._records),))

    def record(self, key: Any) -> SegmentOutcome | None:
        """The structured outcome stored under ``key`` (``None`` if absent)."""
        return self._records.get(key)


def segment_record(outcomes: Mapping[Any, str], key: Any) -> SegmentOutcome:
    """``key``'s outcome as a record, from either kind of outcome mapping.

    A :class:`SegmentOutcomes` returns the record it stored (gateway name
    included); a plain ``dict`` of labels (duck-typed stand-ins in tests) is
    parsed.  A missing key is ``"not-attempted"``.
    """
    if isinstance(outcomes, SegmentOutcomes):
        rec = outcomes.record(key)
        if rec is not None:
            return rec
    label = outcomes.get(key)
    return (
        SegmentOutcome.from_label(label) if label is not None else SegmentOutcome("not-attempted")
    )
