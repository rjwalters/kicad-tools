"""Group 16 -- the match-group tuning insertion self-check.

``router/match_group_tuning.py`` inserts serpentine detours to equalise
matched-length nets, and before it commits one it runs its own DRC self-check
over the *new* segments.  ``_post_insertion_clearance_detail_group``
(``:1609``) is that check: a keyword-only pure function over ``Route`` dicts
and scalar clearances, returning ``None`` when safe and a description of the
first violation otherwise.  Its call precedent is
``tests/test_match_group_tuning.py:3106``.

The row asks the function exactly what the tuner asks it: *may this new
segment be added, given this foreign copper?*  ``group_net_ids`` holds both
nets (the check only measures pairs inside the declared group plus the
supplied foreign pads), ``intra_group_clearance_mm`` is the router's
``trace_clearance`` and ``via_clearance_mm`` its ``via_clearance``, so the
segment-vs-via branch is live rather than skipped.

**Pair kinds: the candidate must be a segment.**  ``new_segments`` is the
only candidate shape; the tuner never proposes a via (a serpentine is
in-layer by construction), so ``via-via`` and ``pad-via`` are out of scope
under this harness's via-first order.

**No diff-pair partner.**  ``diff_pair_partners`` /
``intra_pair_clearance_mm`` stay ``None``, which is the non-diff-pair path.
The paired sibling ``_post_insertion_clearance_detail_pair_group``
(``:2404``) needs a *pair* of mirrored candidate segments with declared P/N
net ids -- a diff-pair candidate the generator does not place -- so it is
recorded as a ``not measured`` sub-note on this row.  Note that group 8's
``diffpair`` adapter does **not** close it either: that row drives the coupled
*constructor's* gates over ordinary corpus pairs, and the missing ingredient
here is a declared P/N candidate in the **generator**, not a live
``DiffPairRouter``.
"""

from __future__ import annotations

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import (
    net_ids,
    pair_contexts,
    router_pad,
    router_rules,
    router_segment,
    single_object_route,
)
from tests.conformance.generator import CopperCase, PadSpec, PairKind, SegmentSpec

__all__ = ["MatchGroupAdapter"]

#: The kinds whose candidate is a segment -- ``new_segments`` is the only
#: candidate shape the self-check accepts.
SEGMENT_CANDIDATE_KINDS = frozenset({PairKind.SEG_SEG, PairKind.SEG_VIA, PairKind.PAD_SEG})


class MatchGroupAdapter:
    """Drives ``_post_insertion_clearance_detail_group`` on the pair."""

    name = "match_group"
    group = 16
    pair_kinds = SEGMENT_CANDIDATE_KINDS

    def available(self) -> bool:
        return True

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        from kicad_tools.router.match_group_tuning import (
            _post_insertion_clearance_detail_group,
        )

        nets = net_ids(case)
        rules = router_rules(case)
        found: set[Verdict] = set()

        for context in pair_contexts(case):
            if context.kind not in self.pair_kinds:
                continue
            candidate = context.candidate
            assert isinstance(candidate, SegmentSpec)
            existing = context.existing
            candidate_net = nets[candidate.net]
            existing_net = nets[existing.net]

            foreign_pads = None
            routes_by_net: dict[int, object] = {}
            if isinstance(existing, PadSpec):
                foreign_pads = [router_pad(existing, nets)]
            else:
                routes_by_net[existing_net] = single_object_route(existing, nets)

            detail = _post_insertion_clearance_detail_group(
                new_segments=[router_segment(candidate, nets)],
                candidate_net_id=candidate_net,
                group_net_ids={candidate_net, existing_net},
                routes_by_net=routes_by_net,  # type: ignore[arg-type]
                intra_group_clearance_mm=rules.trace_clearance,
                via_clearance_mm=rules.via_clearance,
                foreign_pads=foreign_pads,
                pad_clearance_mm=rules.trace_clearance,
            )

            if detail is not None:
                net_a, net_b = context.nets
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        net_a,
                        net_b,
                        gap_mm=None,  # the check returns a description, not a number
                        required_mm=rules.trace_clearance,
                    )
                )
        return found
