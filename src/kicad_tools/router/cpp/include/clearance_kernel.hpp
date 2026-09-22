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

// ---------------------------------------------------------------------------
// Indexed-consumer primitives (Epic #5509, Phase 3f)
// ---------------------------------------------------------------------------
//
// ``copper_gap(seg, zone)`` walks every edge of every ring.  A consumer that
// owns a *spatial index* over a pour's boundary edges -- ``Grid3D``'s 1 mm
// bins, and their Python twin in ``router/fixed_copper_kernel.py`` -- cannot
// hand the whole ``KZonePoly`` over without throwing that index away: on a
// 2000-vertex pour the indexed walk is ~400x cheaper, and the fixed-copper
// predicate sits in the A* step loop.
//
// So the kernel exposes the two *steps* that walk decomposes into, and the
// consumer supplies the iteration order.  This is not a second model: taking
// the minimum of ``copper_gap_ring_edge`` over every edge of a ring set, with
// containment from ``ring_edge_crosses_ray`` parity over the same edges,
// reproduces ``copper_gap(seg, KZonePoly{rings})`` exactly -- asserted over
// random pours by ``tests/router/test_clearance_kernel_parity.py``.

// The single-edge step of ``copper_gap(KSegment, KZonePoly)``: the edge-to-edge
// copper gap between a track segment and ONE boundary edge of a ring set.
//
// A pour carries no width of its own -- the filled polygon *is* the copper --
// so only the segment's half width is subtracted.  A ring edge carries no
// layer either; the caller has already decided that this pour and this segment
// share one (``copper_gap`` would apply ``layers_interact`` here).
double copper_gap_ring_edge(const KSegment& s,
                            double ax, double ay, double bx, double by);

// The single-edge step of the kernel's even-odd containment walk: does the
// +x ray from ``(px, py)`` cross this ring edge?  A caller that toggles a
// parity flag across every edge of a ring set -- or across every edge that can
// possibly straddle ``py``, which is what a row index selects -- reproduces
// the kernel's own "is this point inside the copper" answer.
bool ring_edge_crosses_ray(double px, double py,
                           double ax, double ay, double bx, double by);

}  // namespace clearance
}  // namespace router
