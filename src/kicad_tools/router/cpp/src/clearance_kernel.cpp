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
double point_to_polyline_distance(double px, double py, const KEdge& edge)
{
    const std::size_t n = edge.points.size();
    if (n == 0) {
        return NO_INTERACTION;
    }
    if (n == 1) {
        const double dx = px - edge.points[0].first;
        const double dy = py - edge.points[0].second;
        return std::sqrt(dx * dx + dy * dy);
    }

    double best = NO_INTERACTION;
    for (std::size_t i = 0; i + 1 < n; ++i) {
        const double d = point_to_segment_distance(
            px, py,
            edge.points[i].first, edge.points[i].second,
            edge.points[i + 1].first, edge.points[i + 1].second);
        best = std::min(best, d);
    }
    return best;
}

// Minimum distance from a segment centreline to an open polyline.
double segment_to_polyline_distance(const KSegment& seg, const KEdge& edge)
{
    const std::size_t n = edge.points.size();
    if (n == 0) {
        return NO_INTERACTION;
    }
    if (n == 1) {
        return point_to_segment_distance(
            edge.points[0].first, edge.points[0].second,
            seg.x1, seg.y1, seg.x2, seg.y2);
    }

    double best = NO_INTERACTION;
    for (std::size_t i = 0; i + 1 < n; ++i) {
        const double d = segment_to_segment_distance(
            seg.x1, seg.y1, seg.x2, seg.y2,
            edge.points[i].first, edge.points[i].second,
            edge.points[i + 1].first, edge.points[i + 1].second);
        best = std::min(best, d);
    }
    return best;
}

// --- layer interaction ----------------------------------------------------

// The layer a shape lives on, or ALL_LAYERS when it spans every copper layer
// (vias, the board outline).
int shape_layer(const KShape& shape)
{
    if (const auto* seg = std::get_if<KSegment>(&shape)) {
        return seg->layer;
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
    const double centre = segment_to_polyline_distance(s, e);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - s.width / 2.0;
}

double copper_gap_via_edge(const KVia& v, const KEdge& e)
{
    const double centre = point_to_polyline_distance(v.x, v.y, e);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - v.diameter / 2.0;
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
    const double centre = point_to_polyline_distance(v.x, v.y, e);
    if (centre == NO_INTERACTION) {
        return NO_INTERACTION;
    }
    return centre - v.drill / 2.0;
}

}  // namespace

// ---------------------------------------------------------------------------
// Public predicates
// ---------------------------------------------------------------------------

double copper_gap(const KShape& a, const KShape& b)
{
    if (!layers_interact(a, b)) {
        return NO_INTERACTION;
    }

    if (const auto* sa = std::get_if<KSegment>(&a)) {
        if (const auto* sb = std::get_if<KSegment>(&b)) {
            return copper_gap_seg_seg(*sa, *sb);
        }
        if (const auto* vb = std::get_if<KVia>(&b)) {
            return copper_gap_seg_via(*sa, *vb);
        }
        return copper_gap_seg_edge(*sa, std::get<KEdge>(b));
    }

    if (const auto* va = std::get_if<KVia>(&a)) {
        if (const auto* sb = std::get_if<KSegment>(&b)) {
            return copper_gap_seg_via(*sb, *va);
        }
        if (const auto* vb = std::get_if<KVia>(&b)) {
            return copper_gap_via_via(*va, *vb);
        }
        return copper_gap_via_edge(*va, std::get<KEdge>(b));
    }

    const auto& ea = std::get<KEdge>(a);
    if (const auto* sb = std::get_if<KSegment>(&b)) {
        return copper_gap_seg_edge(*sb, ea);
    }
    if (const auto* vb = std::get_if<KVia>(&b)) {
        return copper_gap_via_edge(*vb, ea);
    }
    // Board outline vs. board outline: not a copper pair.
    return NO_INTERACTION;
}

double hole_gap(const KShape& a, const KShape& b)
{
    if (const auto* va = std::get_if<KVia>(&a)) {
        if (const auto* vb = std::get_if<KVia>(&b)) {
            return hole_gap_via_via(*va, *vb);
        }
        if (const auto* sb = std::get_if<KSegment>(&b)) {
            return hole_gap_via_seg(*va, *sb);
        }
        return hole_gap_via_edge(*va, std::get<KEdge>(b));
    }

    if (const auto* vb = std::get_if<KVia>(&b)) {
        if (const auto* sa = std::get_if<KSegment>(&a)) {
            return hole_gap_via_seg(*vb, *sa);
        }
        return hole_gap_via_edge(*vb, std::get<KEdge>(a));
    }

    // Neither side is drilled.
    return NO_INTERACTION;
}

bool clear(const KShape& a, const KShape& b, double required_mm)
{
    return copper_gap(a, b) >= required_mm - CLEARANCE_EPSILON_MM;
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
    using router::clearance::KSegment;
    using router::clearance::KShape;
    using router::clearance::KVia;

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

    m.attr("CLEARANCE_EPSILON_MM") = router::clearance::CLEARANCE_EPSILON_MM;
    m.attr("CLEARANCE_ALL_LAYERS") = router::clearance::ALL_LAYERS;
}
