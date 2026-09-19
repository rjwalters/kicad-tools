/*
 * Router C++ Core - exact-geometry clearance kernel (Epic #5509, Phase 1b)
 *
 * The single exact-geometry clearance kernel that later phases of Epic #5509
 * move every clearance consumer onto.  This translation unit deliberately
 * knows *nothing* about rules, net classes, net-pair exemptions, grids or
 * routing state: it answers "how far apart is this copper, edge to edge" and
 * "is that at least `required_mm`".  Rule resolution (which number is
 * `required_mm` for a given pair) belongs to the Phase 2 resolver.
 *
 * Nothing in the shipped router calls this yet -- Phase 1b adds the kernel and
 * its parity/fixture evidence only (see the "no consumer switched" test in
 * tests/router/test_clearance_kernel_parity.py).
 *
 * PORT CONTRACT
 * -------------
 * ``src/kicad_tools/router/clearance_kernel.py`` is a line-for-line Python
 * port of this file.  ``tests/router/test_clearance_kernel_parity.py`` drives
 * both and asserts identical verdicts and |gap_py - gap_cpp| <= 1e-7 mm.  A
 * divergence is a bug on whichever side disagrees with kicad-cli -- never
 * something to paper over by loosening the bound.  Edit both sides together.
 *
 * PRECISION
 * ---------
 * Everything here is ``double``.  ``geometry.hpp``'s primitives are ``float``
 * (~1e-7 *relative*, i.e. ~1e-5 mm absolute on a 100 mm segment), which cannot
 * meet the parity bound against Python's float64, so the three primitives are
 * re-implemented in double inside ``clearance_kernel.cpp`` rather than reused.
 *
 * SCOPE (Phase 1b, PR A)
 * ----------------------
 * Segment, via and board-edge shapes.  ``KPad`` (with the ``pad_outline``
 * port of ``validate/rules/clearance.py:_pad_polygon``) and ``KZonePoly``
 * (rings with holes) arrive in the follow-up PR; this header is laid out so
 * adding them is an extra variant alternative, not a re-design.
 */

#pragma once

#include <limits>
#include <utility>
#include <variant>
#include <vector>

namespace router {
namespace clearance {

// Tolerance for "is this gap at least the requirement" comparisons.
//
// Deliberately a private copy of ``grid.cpp``'s file-local
// ``CLEARANCE_EPSILON_MM`` (and of ``router/io.py``'s
// ``_CLEARANCE_EPSILON_MM``): that one is ``constexpr`` inside a .cpp and is
// not includable.  Phase 4 of Epic #5509 collapses the copies once every
// consumer is on this kernel.
constexpr double CLEARANCE_EPSILON_MM = 1e-4;

// Sentinel layer meaning "present on every copper layer".  Vias and the board
// outline are modelled that way implicitly (neither carries a layer field);
// a segment may use it for a caller that has no layer information.
constexpr int ALL_LAYERS = -1;

// "These two shapes cannot interact" -- returned instead of a distance when a
// query is not applicable (different layers for copper, no drilled hole on
// either side for holes).  Callers compare against a requirement, and
// +infinity is always clear.
constexpr double NO_INTERACTION = std::numeric_limits<double>::infinity();

// ---------------------------------------------------------------------------
// Shape value types
// ---------------------------------------------------------------------------
//
// Plain structs: no net, no rule, no grid coordinates.  Coordinates and sizes
// are millimetres in the board frame.

// A routed track segment: a centreline with a copper width, on one layer.
struct KSegment {
    double x1 = 0.0;
    double y1 = 0.0;
    double x2 = 0.0;
    double y2 = 0.0;
    double width = 0.0;
    int layer = ALL_LAYERS;
};

// A via: a copper annulus of ``diameter`` around a drilled hole of ``drill``,
// present on every copper layer.  ``drill <= 0`` models a via with no hole
// (hole queries against it return NO_INTERACTION).
struct KVia {
    double x = 0.0;
    double y = 0.0;
    double diameter = 0.0;
    double drill = 0.0;
};

// The board outline, as an open polyline of vertices: the copper-to-edge
// reference geometry.  Consecutive vertices form the outline segments, the
// same model ``validate/rules/edge.py:_min_distance_to_outline`` consumes
// (arcs are flattened before they reach it).  Repeat the first vertex at the
// end to close a loop.  Applies to every copper layer.
struct KEdge {
    std::vector<std::pair<double, double>> points;
};

using KShape = std::variant<KSegment, KVia, KEdge>;

// ---------------------------------------------------------------------------
// Predicates
// ---------------------------------------------------------------------------

// Edge-to-edge copper distance in mm; negative when the two coppers overlap.
//
// Returns NO_INTERACTION when the pair cannot interact at all -- two segments
// on different layers, or two board outlines (a meaningless pair).
double copper_gap(const KShape& a, const KShape& b);

// Edge-to-edge distance in mm involving at least one drilled hole:
// drill-to-drill for a via pair, drill-to-copper when only one side is
// drilled.  Returns NO_INTERACTION when neither side has a hole.
//
// Holes pass through the whole board, so this is layer-independent by
// construction.
double hole_gap(const KShape& a, const KShape& b);

// Copper clearance verdict: ``copper_gap(a, b) >= required_mm - EPSILON``.
//
// Hole requirements (hole-to-hole, hole-to-copper) use a *different* rule
// value, so they are not folded in here -- compare ``hole_gap`` against the
// hole requirement with the same epsilon.
bool clear(const KShape& a, const KShape& b, double required_mm);

}  // namespace clearance
}  // namespace router
