"""kicad-cli clearance conformance oracle (Epic #5509, Phase 1a).

This package is a **measurement harness**, not a routing change.  It exists to
answer one question with evidence rather than argument:

    For a given piece of copper, does kicad-tools' clearance arithmetic agree
    with ``kicad-cli pcb drc``?

The pipeline is:

1. :mod:`tests.conformance.generator` emits a seeded, deterministic
   :class:`~tests.conformance.generator.CopperCase` -- a small pile of
   segments, vias, pads and (optionally) one zone, each on its own net, with
   every close pair placed at an *analytically known* copper gap.
2. :mod:`tests.conformance.board` serialises that case into a real
   ``.kicad_pcb`` + sibling ``.kicad_pro`` through the ordinary
   ``kicad_tools`` writers.
3. :mod:`tests.conformance.oracle` runs ``kicad-cli pcb drc`` over the board
   and canonicalises its findings into a set of
   :class:`~tests.conformance.adapters.Verdict` values -- **ground truth**.
4. :mod:`tests.conformance.adapters` defines the
   :class:`~tests.conformance.adapters.ConsumerAdapter` protocol that a later
   phase (Phase 1b, the adapter PR) implements once per in-tree clearance
   consumer, so each consumer's answer can be compared against truth.
5. :mod:`tests.conformance.report` renders ``docs/clearance-conformance.md``:
   one row per consumer group from Epic #5509 section 1, with groups that have
   no adapter yet explicitly marked ``not measured`` rather than omitted.

**Report-only.**  Nothing here changes a consumer, a rule value, a heuristic
or a board.  A disagreement is a table row, not a patch -- it becomes a test
failure only when that consumer is switched to the shared kernel in its own
epic phase.
"""

from __future__ import annotations

__all__: list[str] = []
