"""Group 19 -- the incremental placement DRC (``drc_cpp``).

``drc/cpp_backend.py check_pair_clearance_cpp`` (``:121``) is the pad-to-pad
clearance check the placer runs on every candidate move, backed by
``drc/cpp/src/drc_clearance.cpp check_pair_clearance`` (``:18``).  It takes
**two whole footprints** and returns the minimum edge-to-edge clearance over
the ``O(P1 x P2)`` pad-pair loop, so the only pair shape it can answer is
``pad-pad`` -- which is why the generator grew that kind (see
``generator.PairKind``): with only routing pairs on the board there would
have been nothing in this consumer's scope and its row could only ever have
read ``not measured``.

**Every pad is a disc of ``max(w, h) / 2``, by construction.**
``_extract_pad_arrays`` (``:96``) feeds one radius per pad into the kernel:
``radius = max(pad.size[0], pad.size[1]) / 2``.  There is no shape keyword, no
pad-level rotation (only the *footprint* angle rotates pad offsets), and no
polygon.  A 1.2 x 0.8 rect pad is therefore a 0.6 mm disc circumscribing its
long side; a 1.6 x 0.8 oval is a 0.8 mm disc.  Such an envelope can only ever
make copper look *closer* than it is, so this model can over-reject but never
under-reject on geometry.

**This corpus cannot see that over-rejection, and saying so is part of the
measurement.**  The generator realises a pad-pad gap along the first pad's
local ``+X`` axis, because that is the one direction where every shape in
``PAD_SHAPES`` has support exactly ``w / 2``
(``generator.PadShape.half_extent_x``) and the placement is analytically
exact for any rotation.  For all four probe shapes ``max(w, h) / 2`` *is*
``w / 2`` (the long side is the local X side in each), so along precisely the
axis this corpus probes, the circumscribing disc reproduces the true copper
extent and the envelope is tight.  Any non-zero over-reject cell in this row
would mean the *arithmetic* disagrees, not the shape model.

Consequence for later phases: the disc envelope's real cost shows up on
pads approached **off** their long axis (a rect pad probed along its short
side is over-approximated by ``(w - h) / 2``), and the corpus would need a
pair kind that probes that direction to quantify it.  That is a generator
extension for the phase that owns this consumer (Phase 4), not something to
bolt on while measuring; it is recorded as a ``not measured`` sub-note on the
row rather than left implicit in a flattering zero.

**Threshold.**  The kernel returns a distance, not a verdict, and the placer
compares it against the design rule's minimum clearance.  This row compares it
against the router-side ``trace_clearance`` with the same
``CLEARANCE_EPSILON_MM`` slack every other consumer in the tree applies, so
the comparison is the placer's and the value is the case's own (never
"fixed" towards the project netclass -- see ``_support``).

Requires the compiled DRC extension, probed through the module's own
``is_cpp_available`` (``drc/cpp_backend.py:36``) rather than a local import
guess.  ``uv run kct build-native`` builds it alongside ``router_cpp``.
"""

from __future__ import annotations

from tests.conformance.adapters import KIND_CLEARANCE, Verdict
from tests.conformance.adapters._support import net_ids, pad_pad_pairs, router_rules
from tests.conformance.generator import CopperCase, PadSpec, PairKind

__all__ = ["DrcCppAdapter"]

#: The only pair shape ``check_pair_clearance`` can answer.
PAD_PAIR_KINDS = frozenset({PairKind.PAD_PAD})

#: The same slack every clearance comparison in the tree uses
#: (``router/clearance_kernel.py`` ``CLEARANCE_EPSILON_MM``,
#: ``grid.cpp``'s file-local copy, ``router/io.py``'s
#: ``_CLEARANCE_EPSILON_MM``).  Phase 4 of the epic collapses the copies.
CLEARANCE_EPSILON_MM = 1e-4


class DrcCppAdapter:
    """Drives ``check_pair_clearance_cpp`` on the case's pad-pad pairs."""

    name = "drc_cpp"
    group = 19
    pair_kinds = PAD_PAIR_KINDS

    def available(self) -> bool:
        from kicad_tools.drc.cpp_backend import is_cpp_available

        return bool(is_cpp_available())

    def verdicts(self, case: CopperCase) -> set[Verdict]:
        from kicad_tools.drc.cpp_backend import check_pair_clearance_cpp, is_cpp_available

        if not is_cpp_available():  # pragma: no cover - guarded by available()
            return set()

        nets = net_ids(case)
        rules = router_rules(case)
        found: set[Verdict] = set()

        for pair, pad_a, pad_b in pad_pad_pairs(case):
            result = check_pair_clearance_cpp(
                _footprint_for(pad_a, nets),
                _footprint_for(pad_b, nets),
                pad_a.reference,
                pad_b.reference,
            )
            if result is None:  # pragma: no cover - probe footprints always have a pad
                continue
            min_clearance = result[0]
            if min_clearance < rules.trace_clearance - CLEARANCE_EPSILON_MM:
                found.add(
                    Verdict.pair(
                        KIND_CLEARANCE,
                        pair.net_a,
                        pair.net_b,
                        gap_mm=float(min_clearance),
                        required_mm=rules.trace_clearance,
                    )
                )
        return found


def _footprint_for(spec: PadSpec, nets: dict[str, int]):
    """A single-pad ``Footprint`` matching what the board writer emitted.

    The probe footprints under ``tests/fixtures/conformance/Conformance.pretty``
    each hold one pad at the footprint origin, so a zero-offset pad plus the
    footprint's own position and angle reproduces the board exactly.  The
    angle goes on the *footprint*, which is what ``check_pair_clearance_cpp``
    rotates offsets by (``cpp_backend.py:157`` negates it, matching KiCad's
    convention verified against pcbnew in issue #3739).
    """
    from kicad_tools.schema.pcb import Footprint, Pad

    shape = spec.shape
    return Footprint(
        name=shape.name,
        layer=spec.layer,
        position=(spec.x, spec.y),
        rotation=spec.rotation,
        reference=spec.reference,
        value=shape.name,
        pads=[
            Pad(
                number="1",
                type="smd",
                shape=shape.shape,
                position=(0.0, 0.0),
                size=shape.size,
                layers=[spec.layer],
                net_number=nets[spec.net],
                net_name=spec.net,
                rotation=spec.rotation,
                roundrect_rratio=(
                    shape.roundrect_rratio if shape.roundrect_rratio is not None else 0.25
                ),
            )
        ],
    )
