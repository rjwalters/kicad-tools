/*
 * Router C++ Core - exact-geometry clearance kernel (Epic #5509, Phase 1b)
 *
 * See clearance_kernel.hpp for the design contract.  This file also carries
 * the nanobind registration (``register_clearance_kernel``), following the
 * ``mesh.cpp`` precedent of keeping a self-contained binding in its own
 * translation unit so bindings.cpp grows by three lines.
 *
 * The three geometry primitives below are ``double`` re-implementations of
 * ``core/geometry.py``'s ``point_to_segment_distance`` / ``segments_intersect``
 * / ``segment_to_segment_distance``, statement-for-statement.  The Python port
 * calls the shared ``core.geometry`` versions directly; keeping the operation
 * order identical here is what makes the 1e-7 mm parity bound hold.
 */

#include "clearance_kernel.hpp"

#include <algorithm>
#include <cmath>
#include <string>

#include <nanobind/nanobind.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/variant.h>
#include <nanobind/stl/vector.h>

namespace nb = nanobind;

namespace router {
namespace clearance {

namespace {

// --- geometry primitives (double twins of core/geometry.py) ---------------

// Minimum distance from a point to a line segment.
//
// Rebased to the segment's start point so the computation uses only
// coordinate *differences* -- exactly invariant under a rigid translation of
// all three points (issue #3714).  Matches
// ``core/geometry.py:point_to_segment_distance`` statement for statement.
double point_to_segment_distance(double px, double py,
                                 double x1, double y1,
                                 double x2, double y2)
{
    const double dx = x2 - x1;
    const double dy = y2 - y1;
    const double seg_len_sq = dx * dx + dy * dy;

    const double apx = px - x1;
    const double apy = py - y1;

    if (seg_len_sq == 0.0) {
        return std::sqrt(apx * apx + apy * apy);
    }

    double t = (apx * dx + apy * dy) / seg_len_sq;
    t = std::max(0.0, std::min(1.0, t));

    const double rx = apx - t * dx;
    const double ry = apy - t * dy;

    return std::sqrt(rx * rx + ry * ry);
}

double cross(double ox, double oy, double px, double py, double qx, double qy)
{
    return (px - ox) * (qy - oy) - (py - oy) * (qx - ox);
}

// Proper segment intersection.  Shared endpoints and collinear overlap are
// NOT intersections (``core/geometry.py:segments_intersect`` convention).
bool segments_intersect(double ax1, double ay1, double ax2, double ay2,
                        double bx1, double by1, double bx2, double by2)
{
    const double d1 = cross(bx1, by1, bx2, by2, ax1, ay1);
    const double d2 = cross(bx1, by1, bx2, by2, ax2, ay2);
    const double d3 = cross(ax1, ay1, ax2, ay2, bx1, by1);
    const double d4 = cross(ax1, ay1, ax2, ay2, bx2, by2);

    return (((d1 > 0.0 && d2 < 0.0) || (d1 < 0.0 && d2 > 0.0)) &&
            ((d3 > 0.0 && d4 < 0.0) || (d3 < 0.0 && d4 > 0.0)));
}

// Minimum distance between two line segments.
double segment_to_segment_distance(double x1, double y1, double x2, double y2,
                                   double x3, double y3, double x4, double y4)
{
    if (segments_intersect(x1, y1, x2, y2, x3, y3, x4, y4)) {
        return 0.0;
    }

    const double d1 = point_to_segment_distance(x1, y1, x3, y3, x4, y4);
    const double d2 = point_to_segment_distance(x2, y2, x3, y3, x4, y4);
    const double d3 = point_to_segment_distance(x3, y3, x1, y1, x2, y2);
    const double d4 = point_to_segment_distance(x4, y4, x1, y1, x2, y2);

    return std::min(std::min(d1, d2), std::min(d3, d4));
}

// --- polyline helpers -----------------------------------------------------

// Minimum distance from a point to an open polyline.  A polyline with a
// single vertex degenerates to that point; an empty one cannot interact.
double point_to_polyline_distance(double px, double py, const KRing& points)
{
    const std::size_t n = points.size();
    if (n == 0) {
        return NO_INTERACTION;
    }
    if (n == 1) {
        const double dx = px - points[0].first;
        const double dy = py - points[0].second;
        return std::sqrt(dx * dx + dy * dy);
    }

    double best = NO_INTERACTION;
    for (std::size_t i = 0; i + 1 < n; ++i) {
        const double d = point_to_segment_distance(
            px, py,
            points[i].first, points[i].second,
            points[i + 1].first, points[i + 1].second);
        best = std::min(best, d);
    }
    return best;
}

// Minimum distance from a line segment to an open polyline.
double segment_to_polyline_distance(double x1, double y1, double x2, double y2,
                                   const KRing& points)
{
    const std::size_t n = points.size();
    if (n == 0) {
        return NO_INTERACTION;
    }
    if (n == 1) {
        return point_to_segment_distance(
            points[0].first, points[0].second, x1, y1, x2, y2);
    }

    double best = NO_INTERACTION;
    for (std::size_t i = 0; i + 1 < n; ++i) {
        const double d = segment_to_segment_distance(
            x1, y1, x2, y2,
            points[i].first, points[i].second,
            points[i + 1].first, points[i + 1].second);
        best = std::min(best, d);
    }
    return best;
}

// Minimum distance between two open polylines.
double polyline_to_polyline_distance(const KRing& a, const KRing& b)
{
    if (a.empty() || b.empty()) {
        return NO_INTERACTION;
    }
    if (a.size() == 1) {
        return point_to_polyline_distance(a[0].first, a[0].second, b);
    }

    double best = NO_INTERACTION;
    for (std::size_t i = 0; i + 1 < a.size(); ++i) {
        const double d = segment_to_polyline_distance(
            a[i].first, a[i].second, a[i + 1].first, a[i + 1].second, b);
        best = std::min(best, d);
    }
    return best;
}

// --- ring-set (region) helpers --------------------------------------------
//
// A *region* is a list of closed rings: ring 0 the outer boundary, the rest
// interior holes.  Containment is even-odd across every ring, so a point in a
// hole is outside the copper -- the same walk ``grid.cpp:fixed_fill_clear``
// performs, with its 1 mm binning dropped (the kernel answers one pair at a
// time; there is nothing to accelerate).

bool point_in_rings(const std::vector<KRing>& rings, double px, double py)
{
    bool inside = false;
    for (const KRing& ring : rings) {
        for (std::size_t i = 1; i < ring.size(); ++i) {
            // One shared crossing test with the indexed consumers (Phase 3f):
            // ``Grid3D::fixed_fill_clear`` walks the same parity over the
            // edges a row index selects instead of over whole rings.
            if (ring_edge_crosses_ray(px, py,
                                      ring[i - 1].first, ring[i - 1].second,
                                      ring[i].first, ring[i].second)) {
                inside = !inside;
            }
        }
    }
    return inside;
}

bool rings_empty(const std::vector<KRing>& rings)
{
    for (const KRing& ring : rings) {
        if (!ring.empty()) {
            return false;
        }
    }
    return true;
}

// Distance from a point to the region *boundary* (every ring).
double point_to_rings_distance(const std::vector<KRing>& rings,
                               double px, double py)
{
    double best = NO_INTERACTION;
    for (const KRing& ring : rings) {
        best = std::min(best, point_to_polyline_distance(px, py, ring));
    }
    return best;
}

// Distance from a segment to the region boundary.
double segment_to_rings_distance(const std::vector<KRing>& rings,
                                 double x1, double y1, double x2, double y2)
{
    double best = NO_INTERACTION;
    for (const KRing& ring : rings) {
        best = std::min(best, segment_to_polyline_distance(x1, y1, x2, y2, ring));
    }
    return best;
}

// Distance from an open polyline to the region boundary.
double polyline_to_rings_distance(const std::vector<KRing>& rings,
                                  const KRing& poly)
{
    double best = NO_INTERACTION;
    for (const KRing& ring : rings) {
        best = std::min(best, polyline_to_polyline_distance(poly, ring));
    }
    return best;
}

// Distance between two region boundaries.
double rings_to_rings_distance(const std::vector<KRing>& a,
                               const std::vector<KRing>& b)
{
    double best = NO_INTERACTION;
    for (const KRing& ring : a) {
        best = std::min(best, polyline_to_rings_distance(b, ring));
    }
    return best;
}

// --- region distances (0 inside the copper, like shapely's ``distance``) ---

double region_point_distance(const std::vector<KRing>& rings,
                             double px, double py)
{
    if (rings_empty(rings)) {
        return NO_INTERACTION;
    }
    if (point_in_rings(rings, px, py)) {
        return 0.0;
    }
    return point_to_rings_distance(rings, px, py);
}

double region_segment_distance(const std::vector<KRing>& rings,
                               double x1, double y1, double x2, double y2)
{
    if (rings_empty(rings)) {
        return NO_INTERACTION;
    }
    if (point_in_rings(rings, x1, y1) || point_in_rings(rings, x2, y2)) {
        return 0.0;
    }
    return segment_to_rings_distance(rings, x1, y1, x2, y2);
}

double region_polyline_distance(const std::vector<KRing>& rings,
                                const KRing& poly)
{
    if (rings_empty(rings) || poly.empty()) {
        return NO_INTERACTION;
    }
    for (const auto& pt : poly) {
        if (point_in_rings(rings, pt.first, pt.second)) {
            return 0.0;
        }
    }
    return polyline_to_rings_distance(rings, poly);
}

double region_region_distance(const std::vector<KRing>& a,
                              const std::vector<KRing>& b)
{
    if (rings_empty(a) || rings_empty(b)) {
        return NO_INTERACTION;
    }
    for (const KRing& ring : a) {
        for (const auto& pt : ring) {
            if (point_in_rings(b, pt.first, pt.second)) {
                return 0.0;
            }
        }
    }
    for (const KRing& ring : b) {
        for (const auto& pt : ring) {
            if (point_in_rings(a, pt.first, pt.second)) {
                return 0.0;
            }
        }
    }
    return rings_to_rings_distance(a, b);
}

// --- pad core as a region -------------------------------------------------
//
// The Minkowski core is carried as 1, 2 or 4 board-frame vertices; closing it
// into a ring lets every region helper above serve pads unchanged.  A 1- or
// 2-vertex core closes to a degenerate ring with no interior, which is exactly
// right: a point and a segment enclose nothing, and the even-odd walk counts
// each crossing twice and cancels.
std::vector<KRing> pad_region(const KPad& pad)
{
    if (pad.core.empty()) {
        return {};
    }
    KRing ring = pad.core;
    ring.push_back(pad.core.front());
    return {std::move(ring)};
}

// --- layer interaction ----------------------------------------------------

// The layer a shape lives on, or ALL_LAYERS when it spans every copper layer
// (vias, the board outline).
int shape_layer(const KShape& shape)
{
    if (const auto* seg = std::get_if<KSegment>(&shape)) {
        return seg->layer;
    }
    if (const auto* pad = std::get_if<KPad>(&shape)) {
        return pad->layer;
    }
    if (const auto* zone = std::get_if<KZonePoly>(&shape)) {
        return zone->layer;
    }
    return ALL_LAYERS;
}

bool layers_interact(const KShape& a, const KShape& b)
{
    const int la = shape_layer(a);
    const int lb = shape_layer(b);
    if (la == ALL_LAYERS || lb == ALL_LAYERS) {
        return true;
    }
    return la == lb;
}

// --- pair dispatch --------------------------------------------------------
//
// Canonical shape ordering, so each of the fifteen unordered pair kinds is
// implemented exactly once and argument order provably cannot matter.  Must
// match ``KShape``'s variant alternative order and the Python port's
// ``_SHAPE_RANK``.

constexpr int RANK_SEGMENT = 0;
constexpr int RANK_VIA = 1;
constexpr int RANK_EDGE = 2;
constexpr int RANK_PAD = 3;
constexpr int RANK_ZONE = 4;

int shape_rank(const KShape& shape)
{
    return static_cast<int>(shape.index());
}

constexpr int pair_key_of(int lo, int hi)
{
    return lo * 8 + hi;
}

int pair_key(const KShape& lo, const KShape& hi)
{
    return pair_key_of(shape_rank(lo), shape_rank(hi));
}

// --- per-pair copper distances --------------------------------------------

double copper_gap_seg_seg(const KSegment& a, const KSegment& b)
{
    const double centre = segment_to_segment_distance(
        a.x1, a.y1, a.x2, a.y2, b.x1, b.y1, b.x2, b.y2);
    return centre - a.width / 2.0 - b.width / 2.0;
}

double copper_gap_seg_via(const KSegment& s, const KVia& v)
{
    const double centre = point_to_segment_distance(
        v.x, v.y, s.x1, s.y1, s.x2, s.y2);
    return centre - s.width / 2.0 - v.diameter / 2.0;
}

double copper_gap_via_via(const KVia& a, const KVia& b)
{
    const double dx = b.x - a.x;
    const double dy = b.y - a.y;
    const double centre = std::sqrt(dx * dx + dy * dy);
    return centre - a.diameter / 2.0 - b.diameter / 2.0;
}

double copper_gap_seg_edge(const KSegment& s, const KEdge& e)
{
    const double centre = segment_to_polyline_distance(s.x1, s.y1, s.x2, s.y2, e.points);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - s.width / 2.0;
}

double copper_gap_via_edge(const KVia& v, const KEdge& e)
{
    const double centre = point_to_polyline_distance(v.x, v.y, e.points);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - v.diameter / 2.0;
}

// --- pad copper distances -------------------------------------------------
//
// Every one of these is ``dist(core, other) - corner_radius - other's own
// half-extent``: the Minkowski identity, so no pad tessellation is involved
// and the answer is exact.

double copper_gap_pad_seg(const KPad& p, const KSegment& s)
{
    const double centre = region_segment_distance(pad_region(p), s.x1, s.y1, s.x2, s.y2);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - p.corner_radius - s.width / 2.0;
}

double copper_gap_pad_via(const KPad& p, const KVia& v)
{
    const double centre = region_point_distance(pad_region(p), v.x, v.y);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - p.corner_radius - v.diameter / 2.0;
}

double copper_gap_pad_pad(const KPad& a, const KPad& b)
{
    const double centre = region_region_distance(pad_region(a), pad_region(b));
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - a.corner_radius - b.corner_radius;
}

double copper_gap_pad_edge(const KPad& p, const KEdge& e)
{
    const double centre = region_polyline_distance(pad_region(p), e.points);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - p.corner_radius;
}

double copper_gap_pad_zone(const KPad& p, const KZonePoly& z)
{
    const double centre = region_region_distance(pad_region(p), z.rings);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - p.corner_radius;
}

// --- zone copper distances ------------------------------------------------
//
// A pour carries no width of its own -- the filled polygon *is* the copper --
// so only the partner's half-extent is subtracted.  This is the
// ``SegmentZoneClearanceRule`` reading (``line.distance(poly)`` minus the
// half width) with shapely replaced by the even-odd walk.

double copper_gap_zone_seg(const KZonePoly& z, const KSegment& s)
{
    const double centre = region_segment_distance(z.rings, s.x1, s.y1, s.x2, s.y2);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - s.width / 2.0;
}

double copper_gap_zone_via(const KZonePoly& z, const KVia& v)
{
    const double centre = region_point_distance(z.rings, v.x, v.y);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - v.diameter / 2.0;
}

double copper_gap_zone_edge(const KZonePoly& z, const KEdge& e)
{
    return region_polyline_distance(z.rings, e.points);
}

double copper_gap_zone_zone(const KZonePoly& a, const KZonePoly& b)
{
    return region_region_distance(a.rings, b.rings);
}

// --- per-pair hole distances ----------------------------------------------

double hole_gap_via_via(const KVia& a, const KVia& b)
{
    if (a.drill <= 0.0 || b.drill <= 0.0) {
        // Only one hole (or none): fall back to the drill-to-copper reading
        // for whichever side is drilled.
        if (a.drill > 0.0) {
            const double dx = b.x - a.x;
            const double dy = b.y - a.y;
            return std::sqrt(dx * dx + dy * dy) - a.drill / 2.0 - b.diameter / 2.0;
        }
        if (b.drill > 0.0) {
            const double dx = b.x - a.x;
            const double dy = b.y - a.y;
            return std::sqrt(dx * dx + dy * dy) - b.drill / 2.0 - a.diameter / 2.0;
        }
        return NO_INTERACTION;
    }
    const double dx = b.x - a.x;
    const double dy = b.y - a.y;
    return std::sqrt(dx * dx + dy * dy) - a.drill / 2.0 - b.drill / 2.0;
}

double hole_gap_via_seg(const KVia& v, const KSegment& s)
{
    if (v.drill <= 0.0) {
        return NO_INTERACTION;
    }
    const double centre = point_to_segment_distance(
        v.x, v.y, s.x1, s.y1, s.x2, s.y2);
    return centre - v.drill / 2.0 - s.width / 2.0;
}

double hole_gap_via_edge(const KVia& v, const KEdge& e)
{
    if (v.drill <= 0.0) {
        return NO_INTERACTION;
    }
    const double centre = point_to_polyline_distance(v.x, v.y, e.points);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - v.drill / 2.0;
}

// --- pad / zone hole distances --------------------------------------------
//
// A pad's hole is a disc of ``drill`` at the pad centre; a zone is never
// drilled.  Where only one side is drilled the reading is drill-to-copper --
// the *hole* against the other object's copper -- exactly the one-sided branch
// ``hole_gap_via_via`` already takes.  There is no in-repo reference model for
// drill-to-copper (``git grep hole_to_copper`` finds nothing in
// ``validate/``), so the kernel measures the geometry and leaves the threshold
// entirely to the caller.

double hole_gap_pad_via(const KPad& p, const KVia& v)
{
    if (p.drill > 0.0 && v.drill > 0.0) {
        const double dx = v.x - p.cx;
        const double dy = v.y - p.cy;
        return std::sqrt(dx * dx + dy * dy) - p.drill / 2.0 - v.drill / 2.0;
    }
    if (p.drill > 0.0) {
        const double dx = v.x - p.cx;
        const double dy = v.y - p.cy;
        return std::sqrt(dx * dx + dy * dy) - p.drill / 2.0 - v.diameter / 2.0;
    }
    if (v.drill > 0.0) {
        const double centre = region_point_distance(pad_region(p), v.x, v.y);
        if (centre == NO_INTERACTION) {
            return NO_INTERACTION;
        }
        return centre - p.corner_radius - v.drill / 2.0;
    }
    return NO_INTERACTION;
}

double hole_gap_pad_pad(const KPad& a, const KPad& b)
{
    if (a.drill > 0.0 && b.drill > 0.0) {
        const double dx = b.cx - a.cx;
        const double dy = b.cy - a.cy;
        return std::sqrt(dx * dx + dy * dy) - a.drill / 2.0 - b.drill / 2.0;
    }
    if (a.drill > 0.0) {
        const double centre = region_point_distance(pad_region(b), a.cx, a.cy);
        if (centre == NO_INTERACTION) {
            return NO_INTERACTION;
        }
        return centre - b.corner_radius - a.drill / 2.0;
    }
    if (b.drill > 0.0) {
        const double centre = region_point_distance(pad_region(a), b.cx, b.cy);
        if (centre == NO_INTERACTION) {
            return NO_INTERACTION;
        }
        return centre - a.corner_radius - b.drill / 2.0;
    }
    return NO_INTERACTION;
}

double hole_gap_pad_seg(const KPad& p, const KSegment& s)
{
    if (p.drill <= 0.0) {
        return NO_INTERACTION;
    }
    const double centre = point_to_segment_distance(p.cx, p.cy, s.x1, s.y1, s.x2, s.y2);
    return centre - p.drill / 2.0 - s.width / 2.0;
}

double hole_gap_pad_edge(const KPad& p, const KEdge& e)
{
    if (p.drill <= 0.0) {
        return NO_INTERACTION;
    }
    const double centre = point_to_polyline_distance(p.cx, p.cy, e.points);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - p.drill / 2.0;
}

double hole_gap_pad_zone(const KPad& p, const KZonePoly& z)
{
    if (p.drill <= 0.0) {
        return NO_INTERACTION;
    }
    const double centre = region_point_distance(z.rings, p.cx, p.cy);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - p.drill / 2.0;
}

double hole_gap_via_zone(const KVia& v, const KZonePoly& z)
{
    if (v.drill <= 0.0) {
        return NO_INTERACTION;
    }
    const double centre = region_point_distance(z.rings, v.x, v.y);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - v.drill / 2.0;
}

// --- pad core construction (the ``_pad_polygon`` port) --------------------

constexpr double PI = 3.14159265358979323846;

// ``math.radians`` is ``x * (pi / 180)`` against the same double pi, so this
// constant is bit-identical to CPython's -- the Python port calls
// ``core.geometry.rotate_pad_offset`` and both must agree to the last ulp.
constexpr double DEG_TO_RAD = PI / 180.0;

// ``core/geometry.py:rotate_pad_offset``, statement for statement: KiCad
// applies a footprint/pad orientation as a **negated** angle relative to the
// standard CCW convention (pcbnew-verified, #3739; sign error #5227).
std::pair<double, double> rotate_pad_offset(double local_x, double local_y,
                                            double rotation_deg)
{
    const double angle_rad = (-rotation_deg) * DEG_TO_RAD;
    const double cos_a = std::cos(angle_rad);
    const double sin_a = std::sin(angle_rad);
    return {local_x * cos_a - local_y * sin_a,
            local_x * sin_a + local_y * cos_a};
}

// An axis-aligned rectangle core centred on the origin, counter-clockwise,
// collapsing a degenerate dimension to a segment or a point.
//
// The collapse matters for ``roundrect`` with ``rratio`` at KiCad's maximum
// 0.5, where the inner box is exactly ``0 x 0`` (``2 * 0.5 * min`` is exact in
// binary) and the pad is a true disc.  Keeping four coincident vertices would
// give the right *distance* but a degenerate ``pad_outline``, whose zero-length
// edges have no normal to rotate an arc between.
KRing local_rect(double w, double h)
{
    if (w <= 0.0 && h <= 0.0) {
        return {{0.0, 0.0}};
    }
    if (h <= 0.0) {
        return {{-w / 2.0, 0.0}, {w / 2.0, 0.0}};
    }
    if (w <= 0.0) {
        return {{0.0, -h / 2.0}, {0.0, h / 2.0}};
    }
    return {{-w / 2.0, -h / 2.0},
            {w / 2.0, -h / 2.0},
            {w / 2.0, h / 2.0},
            {-w / 2.0, h / 2.0}};
}

// The Minkowski decomposition of ``validate/rules/clearance.py:_pad_polygon``,
// in the pad's own local frame.  Returns (core vertices, dilation radius).
//
// Branch for branch with the reference model, including its fallback: ``rect``
// **and any unknown shape** -- ``custom``, ``trapezoid``, ``chamfered`` -- is
// the exact ``w x h`` rectangle with no over-approximation.  That is
// deliberate, not an oversight: the kernel must not invent primitives the
// reference model does not have.  A custom pad whose copper reaches outside
// its ``size`` box is therefore under-modelled here exactly as it is in
// ``kct check`` today; Epic #5509 does not change that model, it unifies it.
std::pair<KRing, double> pad_core_local(const std::string& shape,
                                        double w, double h, double rratio)
{
    if (w <= 0.0 || h <= 0.0) {
        return {KRing{}, 0.0};
    }

    const bool oval = (shape == "oval" || shape == "obround");

    if (shape == "circle" || (oval && std::fabs(w - h) < 1e-6)) {
        return {KRing{{0.0, 0.0}}, std::min(w, h) / 2.0};
    }

    if (oval) {
        const double r = std::min(w, h) / 2.0;
        if (w >= h) {
            return {KRing{{-(w / 2.0 - r), 0.0}, {w / 2.0 - r, 0.0}}, r};
        }
        return {KRing{{0.0, -(h / 2.0 - r)}, {0.0, h / 2.0 - r}}, r};
    }

    if (shape == "roundrect") {
        const double r = rratio * std::min(w, h);
        if (r <= 0.0) {
            return {local_rect(w, h), 0.0};
        }
        return {local_rect(w - 2.0 * r, h - 2.0 * r), r};
    }

    // "rect" and every other shape keyword.
    return {local_rect(w, h), 0.0};
}

// Segments needed for a circular arc of ``radius`` sweeping ``sweep`` radians
// so the sagitta stays within ARC_CHORD_ERROR_MM.
//
// A chord subtending ``a`` radians on radius ``r`` has sagitta
// ``r * (1 - cos(a / 2))``; inverting for ``a`` gives the largest step, and
// the count is its ceiling.  An *integer* count computed identically on both
// sides is the whole point: letting each port pick its own resolution is how a
// tessellated outline drifts.
int arc_segment_count(double radius, double sweep)
{
    if (radius <= ARC_CHORD_ERROR_MM || sweep <= 0.0) {
        return 1;
    }
    const double max_step = 2.0 * std::acos(1.0 - ARC_CHORD_ERROR_MM / radius);
    if (!(max_step > 0.0)) {
        return 1;
    }
    const int n = static_cast<int>(std::ceil(sweep / max_step));
    return n < 1 ? 1 : n;
}

// Outward normal (unit) of the directed edge a->b of a CCW polygon: the
// right-hand normal, since a CCW polygon keeps its interior to the left.
std::pair<double, double> outward_normal(const std::pair<double, double>& a,
                                        const std::pair<double, double>& b)
{
    const double dx = b.first - a.first;
    const double dy = b.second - a.second;
    const double len = std::sqrt(dx * dx + dy * dy);
    if (len == 0.0) {
        return {0.0, 0.0};
    }
    return {dy / len, -dx / len};
}

}  // namespace

// ---------------------------------------------------------------------------
// Pad construction
// ---------------------------------------------------------------------------

KPad make_pad(const std::string& shape, double w, double h, double rratio,
              double rotation_deg, double cx, double cy, int layer, double drill)
{
    const auto [core_local, radius] = pad_core_local(shape, w, h, rratio);

    KPad pad;
    pad.corner_radius = radius;
    pad.cx = cx;
    pad.cy = cy;
    pad.drill = drill;
    pad.layer = layer;
    pad.core.reserve(core_local.size());
    for (const auto& v : core_local) {
        const auto [rx, ry] = rotate_pad_offset(v.first, v.second, rotation_deg);
        pad.core.emplace_back(cx + rx, cy + ry);
    }
    return pad;
}

KRing pad_outline(const std::string& shape, double w, double h, double rratio,
                  double rotation_deg, double cx, double cy)
{
    const KPad pad = make_pad(shape, w, h, rratio, rotation_deg, cx, cy);
    const KRing& core = pad.core;
    const double r = pad.corner_radius;

    if (core.empty()) {
        return {};
    }

    // Radius 0 -- a bare rectangle.  Return the core itself, closed.
    if (r <= 0.0) {
        KRing ring = core;
        ring.push_back(core.front());
        return ring;
    }

    // A point core dilates to a full circle.
    if (core.size() == 1) {
        const int n = arc_segment_count(r, 2.0 * PI);
        KRing ring;
        ring.reserve(static_cast<std::size_t>(n) + 1);
        for (int k = 0; k < n; ++k) {
            const double ang = 2.0 * PI * static_cast<double>(k) / static_cast<double>(n);
            ring.emplace_back(core[0].first + r * std::cos(ang),
                              core[0].second + r * std::sin(ang));
        }
        ring.push_back(ring.front());
        return ring;
    }

    // Convex core (a segment traversed both ways, or a CCW quad): walk the
    // directed edges and emit a rounding arc at each vertex, between the
    // outward normals of the incoming and outgoing edges.
    const std::size_t m = core.size();
    std::vector<std::pair<std::size_t, std::size_t>> edges;
    if (m == 2) {
        edges = {{0, 1}, {1, 0}};
    } else {
        for (std::size_t i = 0; i < m; ++i) {
            edges.emplace_back(i, (i + 1) % m);
        }
    }

    KRing ring;
    for (std::size_t k = 0; k < edges.size(); ++k) {
        const auto& prev = edges[(k + edges.size() - 1) % edges.size()];
        const auto& curr = edges[k];
        const auto n_in = outward_normal(core[prev.first], core[prev.second]);
        const auto n_out = outward_normal(core[curr.first], core[curr.second]);
        const double a0 = std::atan2(n_in.second, n_in.first);
        const double a1 = std::atan2(n_out.second, n_out.first);
        double sweep = a1 - a0;
        while (sweep < 0.0) {
            sweep += 2.0 * PI;
        }
        const int n = arc_segment_count(r, sweep);
        const auto& pivot = core[curr.first];
        for (int t = 0; t <= n; ++t) {
            const double ang = a0 + sweep * static_cast<double>(t) / static_cast<double>(n);
            ring.emplace_back(pivot.first + r * std::cos(ang),
                              pivot.second + r * std::sin(ang));
        }
    }
    ring.push_back(ring.front());
    return ring;
}

// ---------------------------------------------------------------------------
// Public predicates
// ---------------------------------------------------------------------------

double copper_gap(const KShape& a, const KShape& b)
{
    if (!layers_interact(a, b)) {
        return NO_INTERACTION;
    }

    // Normalise the pair into canonical rank order (segment, via, edge, pad,
    // zone) so each of the fifteen unordered kinds is written exactly once.
    // The kernel has no notion of "which existed first" -- that absence is the
    // fix for #5398 -- so sorting the arguments cannot change the answer.
    const KShape& p = (shape_rank(a) <= shape_rank(b)) ? a : b;
    const KShape& q = (shape_rank(a) <= shape_rank(b)) ? b : a;

    switch (pair_key(p, q)) {
        case pair_key_of(RANK_SEGMENT, RANK_SEGMENT):
            return copper_gap_seg_seg(std::get<KSegment>(p), std::get<KSegment>(q));
        case pair_key_of(RANK_SEGMENT, RANK_VIA):
            return copper_gap_seg_via(std::get<KSegment>(p), std::get<KVia>(q));
        case pair_key_of(RANK_SEGMENT, RANK_EDGE):
            return copper_gap_seg_edge(std::get<KSegment>(p), std::get<KEdge>(q));
        case pair_key_of(RANK_SEGMENT, RANK_PAD):
            return copper_gap_pad_seg(std::get<KPad>(q), std::get<KSegment>(p));
        case pair_key_of(RANK_SEGMENT, RANK_ZONE):
            return copper_gap_zone_seg(std::get<KZonePoly>(q), std::get<KSegment>(p));
        case pair_key_of(RANK_VIA, RANK_VIA):
            return copper_gap_via_via(std::get<KVia>(p), std::get<KVia>(q));
        case pair_key_of(RANK_VIA, RANK_EDGE):
            return copper_gap_via_edge(std::get<KVia>(p), std::get<KEdge>(q));
        case pair_key_of(RANK_VIA, RANK_PAD):
            return copper_gap_pad_via(std::get<KPad>(q), std::get<KVia>(p));
        case pair_key_of(RANK_VIA, RANK_ZONE):
            return copper_gap_zone_via(std::get<KZonePoly>(q), std::get<KVia>(p));
        case pair_key_of(RANK_EDGE, RANK_PAD):
            return copper_gap_pad_edge(std::get<KPad>(q), std::get<KEdge>(p));
        case pair_key_of(RANK_EDGE, RANK_ZONE):
            return copper_gap_zone_edge(std::get<KZonePoly>(q), std::get<KEdge>(p));
        case pair_key_of(RANK_PAD, RANK_PAD):
            return copper_gap_pad_pad(std::get<KPad>(p), std::get<KPad>(q));
        case pair_key_of(RANK_PAD, RANK_ZONE):
            return copper_gap_pad_zone(std::get<KPad>(p), std::get<KZonePoly>(q));
        case pair_key_of(RANK_ZONE, RANK_ZONE):
            return copper_gap_zone_zone(std::get<KZonePoly>(p), std::get<KZonePoly>(q));
        case pair_key_of(RANK_EDGE, RANK_EDGE):
        default:
            // Board outline vs. board outline: not a copper pair.
            return NO_INTERACTION;
    }
}

double hole_gap(const KShape& a, const KShape& b)
{
    // Holes pass through the whole board, so -- unlike ``copper_gap`` -- there
    // is deliberately no layer gate here.
    const KShape& p = (shape_rank(a) <= shape_rank(b)) ? a : b;
    const KShape& q = (shape_rank(a) <= shape_rank(b)) ? b : a;

    switch (pair_key(p, q)) {
        case pair_key_of(RANK_SEGMENT, RANK_VIA):
            return hole_gap_via_seg(std::get<KVia>(q), std::get<KSegment>(p));
        case pair_key_of(RANK_SEGMENT, RANK_PAD):
            return hole_gap_pad_seg(std::get<KPad>(q), std::get<KSegment>(p));
        case pair_key_of(RANK_VIA, RANK_VIA):
            return hole_gap_via_via(std::get<KVia>(p), std::get<KVia>(q));
        case pair_key_of(RANK_VIA, RANK_EDGE):
            return hole_gap_via_edge(std::get<KVia>(p), std::get<KEdge>(q));
        case pair_key_of(RANK_VIA, RANK_PAD):
            return hole_gap_pad_via(std::get<KPad>(q), std::get<KVia>(p));
        case pair_key_of(RANK_VIA, RANK_ZONE):
            return hole_gap_via_zone(std::get<KVia>(p), std::get<KZonePoly>(q));
        case pair_key_of(RANK_EDGE, RANK_PAD):
            return hole_gap_pad_edge(std::get<KPad>(q), std::get<KEdge>(p));
        case pair_key_of(RANK_PAD, RANK_PAD):
            return hole_gap_pad_pad(std::get<KPad>(p), std::get<KPad>(q));
        case pair_key_of(RANK_PAD, RANK_ZONE):
            return hole_gap_pad_zone(std::get<KPad>(p), std::get<KZonePoly>(q));
        default:
            // Neither side is drilled (or can be).
            return NO_INTERACTION;
    }
}

bool clear(const KShape& a, const KShape& b, double required_mm)
{
    return copper_gap(a, b) >= required_mm - CLEARANCE_EPSILON_MM;
}

// ---------------------------------------------------------------------------
// Indexed-consumer primitives (Epic #5509, Phase 3f)
// ---------------------------------------------------------------------------
//
// The two steps ``copper_gap(KSegment, KZonePoly)`` decomposes into, for a
// consumer that owns a spatial index over the pour's edges.  See the header
// for why the decomposition exists and for the equivalence it must preserve.

double copper_gap_ring_edge(const KSegment& s,
                            double ax, double ay, double bx, double by)
{
    // ``segment_to_rings_distance`` restricted to one edge, minus the half
    // width ``copper_gap_zone_seg`` subtracts.
    return segment_to_segment_distance(s.x1, s.y1, s.x2, s.y2, ax, ay, bx, by)
           - s.width / 2.0;
}

bool ring_edge_crosses_ray(double px, double py,
                           double ax, double ay, double bx, double by)
{
    return (ay > py) != (by > py) &&
           px < (bx - ax) * (py - ay) / (by - ay) + ax;
}

}  // namespace clearance
}  // namespace router

// ---------------------------------------------------------------------------
// nanobind registration
// ---------------------------------------------------------------------------

void register_clearance_kernel(nb::module_& m)
{
    using namespace nb::literals;
    using router::clearance::KEdge;
    using router::clearance::KPad;
    using router::clearance::KSegment;
    using router::clearance::KShape;
    using router::clearance::KVia;
    using router::clearance::KZonePoly;

    nb::class_<KSegment>(m, "KSegment",
                         "Track segment: centreline + copper width, one layer.")
        .def(nb::init<>())
        .def("__init__",
             [](KSegment* self, double x1, double y1, double x2, double y2,
                double width, int layer) {
                 new (self) KSegment{x1, y1, x2, y2, width, layer};
             },
             "x1"_a, "y1"_a, "x2"_a, "y2"_a, "width"_a,
             "layer"_a = router::clearance::ALL_LAYERS)
        .def_rw("x1", &KSegment::x1)
        .def_rw("y1", &KSegment::y1)
        .def_rw("x2", &KSegment::x2)
        .def_rw("y2", &KSegment::y2)
        .def_rw("width", &KSegment::width)
        .def_rw("layer", &KSegment::layer);

    nb::class_<KVia>(m, "KVia",
                     "Via: copper annulus + drilled hole, on every copper layer.")
        .def(nb::init<>())
        .def("__init__",
             [](KVia* self, double x, double y, double diameter, double drill) {
                 new (self) KVia{x, y, diameter, drill};
             },
             "x"_a, "y"_a, "diameter"_a, "drill"_a = 0.0)
        .def_rw("x", &KVia::x)
        .def_rw("y", &KVia::y)
        .def_rw("diameter", &KVia::diameter)
        .def_rw("drill", &KVia::drill);

    nb::class_<KEdge>(m, "KEdge",
                      "Board outline as an open polyline of flattened vertices.")
        .def(nb::init<>())
        .def("__init__",
             [](KEdge* self, std::vector<std::pair<double, double>> points) {
                 new (self) KEdge{std::move(points)};
             },
             "points"_a)
        .def_rw("points", &KEdge::points);

    nb::class_<KPad>(m, "KPad",
                     "Footprint pad: a convex Minkowski core dilated by "
                     "corner_radius, plus an optional drilled hole. Build one "
                     "with make_pad() rather than by hand.")
        .def(nb::init<>())
        .def("__init__",
             [](KPad* self, std::vector<std::pair<double, double>> core,
                double corner_radius, double cx, double cy, double drill, int layer) {
                 new (self) KPad{std::move(core), corner_radius, cx, cy, drill, layer};
             },
             "core"_a, "corner_radius"_a, "cx"_a, "cy"_a, "drill"_a = 0.0,
             "layer"_a = router::clearance::ALL_LAYERS)
        .def_rw("core", &KPad::core)
        .def_rw("corner_radius", &KPad::corner_radius)
        .def_rw("cx", &KPad::cx)
        .def_rw("cy", &KPad::cy)
        .def_rw("drill", &KPad::drill)
        .def_rw("layer", &KPad::layer);

    nb::class_<KZonePoly>(m, "KZonePoly",
                          "Filled copper pour: an outer ring plus interior "
                          "rings (holes), on one layer. Rings are closed "
                          "(first vertex repeated at the end).")
        .def(nb::init<>())
        .def("__init__",
             [](KZonePoly* self,
                std::vector<std::vector<std::pair<double, double>>> rings, int layer) {
                 new (self) KZonePoly{std::move(rings), layer};
             },
             "rings"_a, "layer"_a = router::clearance::ALL_LAYERS)
        .def_rw("rings", &KZonePoly::rings)
        .def_rw("layer", &KZonePoly::layer);

    m.def("make_pad",
          &router::clearance::make_pad,
          "shape"_a, "w"_a, "h"_a, "rratio"_a = 0.25, "rotation_deg"_a = 0.0,
          "cx"_a = 0.0, "cy"_a = 0.0, "layer"_a = router::clearance::ALL_LAYERS,
          "drill"_a = 0.0,
          "Build a KPad from KiCad pad parameters (the _pad_polygon model, as "
          "a Minkowski core + radius).");

    m.def("pad_outline",
          &router::clearance::pad_outline,
          "shape"_a, "w"_a, "h"_a, "rratio"_a = 0.25, "rotation_deg"_a = 0.0,
          "cx"_a = 0.0, "cy"_a = 0.0,
          "The pad's true copper outline as a closed polygon, arcs tessellated "
          "to <= ARC_CHORD_ERROR_MM sagitta with a per-arc segment count both "
          "ports compute identically.");

    m.def("copper_gap",
          [](const KShape& a, const KShape& b) {
              return router::clearance::copper_gap(a, b);
          },
          "a"_a, "b"_a,
          "Edge-to-edge copper distance in mm (negative on overlap; +inf when "
          "the pair cannot interact).");

    m.def("hole_gap",
          [](const KShape& a, const KShape& b) {
              return router::clearance::hole_gap(a, b);
          },
          "a"_a, "b"_a,
          "Drill-to-drill / drill-to-copper distance in mm (+inf when neither "
          "side is drilled).");

    m.def("clear",
          [](const KShape& a, const KShape& b, double required_mm) {
              return router::clearance::clear(a, b, required_mm);
          },
          "a"_a, "b"_a, "required_mm"_a,
          "copper_gap(a, b) >= required_mm - CLEARANCE_EPSILON_MM.");

    m.def("copper_gap_ring_edge",
          &router::clearance::copper_gap_ring_edge,
          "s"_a, "ax"_a, "ay"_a, "bx"_a, "by"_a,
          "The single-edge step of copper_gap(KSegment, KZonePoly), for a "
          "consumer with its own index over a pour's boundary edges.");

    m.def("ring_edge_crosses_ray",
          &router::clearance::ring_edge_crosses_ray,
          "px"_a, "py"_a, "ax"_a, "ay"_a, "bx"_a, "by"_a,
          "The single-edge step of the kernel's even-odd containment walk.");

    m.attr("CLEARANCE_EPSILON_MM") = router::clearance::CLEARANCE_EPSILON_MM;
    m.attr("CLEARANCE_ALL_LAYERS") = router::clearance::ALL_LAYERS;
    m.attr("CLEARANCE_ARC_CHORD_ERROR_MM") = router::clearance::ARC_CHORD_ERROR_MM;
}
