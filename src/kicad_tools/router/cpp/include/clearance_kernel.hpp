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
 * Consumers arrive one epic phase at a time.  Phase 1b added the kernel with
 * nothing wired to it; Phase 3a (#5660) switched the first -- `Grid3D`'s
 * route-copper halo marking in src/grid.cpp, and its Python sibling in
 * router/grid.py.  tests/router/test_clearance_kernel_parity.py keeps the
 * ledger of who is on the kernel and fails on an include that appears without
 * an entry, so each phase's before/after measurement stays attributable.
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
 * SCOPE (Phase 1b, complete)
 * --------------------------
 * All five shape classes: ``KSegment``, ``KVia``, ``KEdge`` (PR A) plus
 * ``KPad`` and ``KZonePoly`` (PR B).  Fifteen unordered pair kinds, every one
 * of them exercised by the parity suite.
 */

#pragma once

#include <limits>
#include <string>
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

// A polygon ring: a *closed* vertex list (first vertex repeated at the end),
// the convention ``Grid3D::add_fixed_fill`` already uses for zone fills.
using KRing = std::vector<std::pair<double, double>>;

// A footprint pad, as a Minkowski sum: a convex ``core`` (1, 2 or 4 vertices,
// already rotated and translated into the board frame) dilated by
// ``corner_radius``.
//
// Every branch of the reference model
// ``validate/rules/clearance.py:_pad_polygon`` is exactly such a sum:
//
//   * ``circle`` (and ``oval``/``obround`` with w == h) -> core is the single
//     centre point, radius ``min(w, h) / 2``;
//   * ``oval``/``obround`` -> core is a segment of length
//     ``max(w, h) - min(w, h)`` along the long axis, radius ``min(w, h) / 2``;
//   * ``roundrect`` -> core is the inner box ``(w - 2r) x (h - 2r)``, radius
//     ``r = rratio * min(w, h)``;
//   * ``rect`` **and every other shape** (``custom``, ``trapezoid``,
//     ``chamfered``) -> core is the exact ``w x h`` rectangle, radius 0.
//
// Carrying the core rather than a tessellated outline is what makes pad
// distances *exact*: ``copper_gap(pad, X) = dist(core, X) - corner_radius``
// has no vertex list for the two ports to disagree about, and no chord error
// to leak into a verdict.  ``pad_outline`` still produces the tessellated
// polygon for export and for the Phase 1c oracle adapter.
//
// ``cx``/``cy`` is the pad centre, which is where the drilled hole sits.
// ``drill <= 0`` models an SMD pad (hole queries return NO_INTERACTION).
// ``layer`` is ALL_LAYERS for a through-hole pad.
struct KPad {
    KRing core;
    double corner_radius = 0.0;
    double cx = 0.0;
    double cy = 0.0;
    double drill = 0.0;
    int layer = ALL_LAYERS;
};

// A filled copper pour on one layer: an outer ring plus zero or more interior
// rings (holes).  Mirrors ``grid.hpp``'s ``FixedFill`` and the ``_ZoneFill``
// model at ``validate/rules/clearance.py:1485``, where one zone fill is a
// shapely polygon **with holes** and ``SegmentZoneClearanceRule`` measures
// ``line.distance(poly)`` minus the half width.
//
// Containment is even-odd across every ring (the
// ``grid.cpp:fixed_fill_clear`` walk, without its binning), so a point inside
// a hole is correctly *outside* the copper.  A zone carries no width: the pour
// *is* the copper.
struct KZonePoly {
    std::vector<KRing> rings;
    int layer = ALL_LAYERS;
};

using KShape = std::variant<KSegment, KVia, KEdge, KPad, KZonePoly>;

// ---------------------------------------------------------------------------
// Pad construction (the ``_pad_polygon`` port)
// ---------------------------------------------------------------------------

// Build a pad's Minkowski core + radius from KiCad pad parameters.
//
// ``shape`` is the KiCad pad-shape keyword; ``w``/``h`` are the pad's *local*
// size; ``rratio`` the roundrect corner ratio (KiCad default 0.25);
// ``rotation_deg`` the pad's ABSOLUTE board-frame angle (``Pad.rotation``
// already includes the footprint rotation per KiCad's file convention, issue
// #3902); ``cx``/``cy`` the absolute pad centre.
//
// The core is rotated by the **negated** angle, matching KiCad's forward
// transform (``core/geometry.py:rotate_pad_offset``, pcbnew-verified in
// #3739; the sign error #5227 fixed).  Non-positive sizes yield an empty core,
// which cannot interact.
KPad make_pad(const std::string& shape, double w, double h, double rratio,
              double rotation_deg, double cx, double cy,
              int layer = ALL_LAYERS, double drill = 0.0);

// The pad's true copper outline as a closed polygon, for export and for the
// Phase 1c oracle adapter.  Arcs are tessellated with a fixed segment *count*
// per arc derived from the <= 0.5 um chord-error rule, computed identically in
// both ports -- never a per-side choice.
//
// Not used by ``copper_gap``: pad distances go through the exact core (see
// ``KPad``), so tessellation can never move a verdict.
KRing pad_outline(const std::string& shape, double w, double h, double rratio,
                  double rotation_deg, double cx, double cy);

// Maximum sagitta (chord error) allowed when tessellating a pad arc, in mm.
constexpr double ARC_CHORD_ERROR_MM = 0.0005;

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
