/*
 * Router C++ Core - poly2tri constrained-Delaunay mesh binding
 *
 * Part of the mesh-router P1 vertical slice (issue #4268, epic #4267).
 *
 * The mesh-router navigation model (ADR on #4267) is a triangle-dual
 * navmesh: a plain *constrained Delaunay triangulation with holes* of the
 * board (outline as the outer boundary, pad keep-outs as interior holes),
 * with the two net endpoints inserted as Steiner points so A* can start and
 * end on real mesh vertices.  No quality refinement is needed -- the funnel
 * string-pull straightens the corridor regardless of triangle size (P0.5
 * spike), which is exactly why the license-simplest BSD-3 poly2tri suffices.
 *
 * poly2tri is a polygon-with-holes triangulator, so the "constraint edges"
 * are precisely the outer-boundary loop and each hole loop -- there is no
 * general arbitrary-segment constraint facility.  That matches the P1 need:
 * the only constraints we require are the board outline and the pad keep-out
 * boundaries.
 */

#include "poly2tri.h"

#include <nanobind/nanobind.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/tuple.h>

#include <cmath>
#include <memory>
#include <tuple>
#include <unordered_map>
#include <vector>

namespace nb = nanobind;

namespace {

using Coord = std::pair<double, double>;
using Tri = std::tuple<int, int, int>;
using MeshResult = std::pair<std::vector<Coord>, std::vector<Tri>>;

// Constrained Delaunay triangulation of a polygon with holes (poly2tri).
//
//   outer   : the board-outline boundary as a closed loop of (x, y) with NO
//             repeated first/last vertex (poly2tri requirement).
//   holes   : each an interior keep-out loop (same no-repeat rule); triangles
//             inside a hole are excluded from the output.
//   steiner : interior points forced to become mesh vertices (net endpoints).
//
// Returns (vertices, triangles) where each triangle is an index triple into
// vertices.  On any poly2tri failure (collinear/duplicate/degenerate input)
// the result is empty rather than a crash -- callers treat empty as "meshing
// failed, fall back".
MeshResult constrained_delaunay(const std::vector<Coord>& outer,
                                const std::vector<std::vector<Coord>>& holes,
                                const std::vector<Coord>& steiner)
{
    MeshResult result;
    if (outer.size() < 3) {
        return result;  // not a polygon
    }

    // We own every p2t::Point for the lifetime of the triangulation; the CDT
    // stores raw pointers and the output triangles reference these same
    // pointers, so pointer identity maps a triangle vertex back to its coord.
    std::vector<std::unique_ptr<p2t::Point>> storage;
    storage.reserve(outer.size() + steiner.size() + 16);

    auto make_point = [&](double x, double y) -> p2t::Point* {
        storage.push_back(std::make_unique<p2t::Point>(x, y));
        return storage.back().get();
    };

    std::vector<p2t::Point*> polyline;
    polyline.reserve(outer.size());
    for (const auto& c : outer) {
        polyline.push_back(make_point(c.first, c.second));
    }

    // hole_lines must outlive Triangulate(): CDT keeps the pointers.
    std::vector<std::vector<p2t::Point*>> hole_lines;
    hole_lines.reserve(holes.size());

    std::unique_ptr<p2t::CDT> cdt;
    try {
        cdt = std::make_unique<p2t::CDT>(polyline);
        for (const auto& h : holes) {
            if (h.size() < 3) {
                continue;  // a degenerate hole cannot be a polygon
            }
            std::vector<p2t::Point*> hl;
            hl.reserve(h.size());
            for (const auto& c : h) {
                hl.push_back(make_point(c.first, c.second));
            }
            hole_lines.push_back(std::move(hl));
            cdt->AddHole(hole_lines.back());
        }
        for (const auto& c : steiner) {
            cdt->AddPoint(make_point(c.first, c.second));
        }
        cdt->Triangulate();
    } catch (...) {
        return MeshResult();  // empty => caller falls back
    }

    std::vector<p2t::Triangle*> tris = cdt->GetTriangles();

    std::vector<Coord> verts;
    std::vector<Tri> out_tris;
    out_tris.reserve(tris.size());
    std::unordered_map<p2t::Point*, int> index_of;
    index_of.reserve(tris.size() * 2);

    auto vidx = [&](p2t::Point* p) -> int {
        auto it = index_of.find(p);
        if (it != index_of.end()) {
            return it->second;
        }
        int idx = static_cast<int>(verts.size());
        index_of.emplace(p, idx);
        verts.emplace_back(p->x, p->y);
        return idx;
    };

    for (p2t::Triangle* t : tris) {
        const int a = vidx(t->GetPoint(0));
        const int b = vidx(t->GetPoint(1));
        const int c = vidx(t->GetPoint(2));
        out_tris.emplace_back(a, b, c);
    }

    result.first = std::move(verts);
    result.second = std::move(out_tris);
    return result;
}

}  // namespace

// Declared in bindings.cpp; called from NB_MODULE(router_cpp, ...).
void register_mesh(nb::module_& m)
{
    m.def("constrained_delaunay", &constrained_delaunay, nb::arg("outer"),
          nb::arg("holes"), nb::arg("steiner"),
          "Constrained Delaunay triangulation (poly2tri) of a polygon with "
          "holes plus interior Steiner points. Returns (vertices, triangles) "
          "where each triangle is an index triple into vertices. Empty result "
          "signals a meshing failure (degenerate input).");
}
