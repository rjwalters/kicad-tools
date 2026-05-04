/*
 * Router C++ Core - nanobind Python bindings
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#include "grid.hpp"
#include "geometry.hpp"
#include "pathfinder.hpp"
#include "types.hpp"
#include <nanobind/nanobind.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>

namespace nb = nanobind;
using namespace nb::literals;
using namespace router;

NB_MODULE(router_cpp, m) {
    m.doc() = "C++ router core for high-performance PCB routing";

    // GridCell struct
    nb::class_<GridCell>(m, "GridCell")
        .def(nb::init<>())
        .def_rw("blocked", &GridCell::blocked)
        .def_rw("net", &GridCell::net)
        .def_rw("usage_count", &GridCell::usage_count)
        .def_rw("history_cost", &GridCell::history_cost)
        .def_rw("is_obstacle", &GridCell::is_obstacle)
        .def_rw("is_zone", &GridCell::is_zone)
        .def_rw("pad_blocked", &GridCell::pad_blocked)
        .def_rw("original_net", &GridCell::original_net)
        .def_rw("avoidance_cost", &GridCell::avoidance_cost);

    // DesignRules struct
    nb::class_<DesignRules>(m, "DesignRules")
        .def(nb::init<>())
        .def_rw("trace_width", &DesignRules::trace_width)
        .def_rw("trace_clearance", &DesignRules::trace_clearance)
        .def_rw("via_drill", &DesignRules::via_drill)
        .def_rw("via_diameter", &DesignRules::via_diameter)
        .def_rw("via_clearance", &DesignRules::via_clearance)
        .def_rw("grid_resolution", &DesignRules::grid_resolution)
        .def_rw("cost_straight", &DesignRules::cost_straight)
        .def_rw("cost_turn", &DesignRules::cost_turn)
        .def_rw("cost_via", &DesignRules::cost_via)
        .def_rw("cost_congestion", &DesignRules::cost_congestion)
        .def_rw("congestion_threshold", &DesignRules::congestion_threshold)
        .def_rw("min_drill_clearance", &DesignRules::min_drill_clearance);

    // ValidationResult struct (Issue #2439)
    nb::class_<ValidationResult>(m, "ValidationResult")
        .def(nb::init<>())
        .def_rw("valid", &ValidationResult::valid)
        .def_rw("min_clearance", &ValidationResult::min_clearance)
        .def_rw("violation_x", &ValidationResult::violation_x)
        .def_rw("violation_y", &ValidationResult::violation_y)
        .def_rw("violation_type", &ValidationResult::violation_type);

    // PadBounds struct
    nb::class_<PadBounds>(m, "PadBounds")
        .def(nb::init<>())
        .def_rw("metal_gx1", &PadBounds::metal_gx1)
        .def_rw("metal_gy1", &PadBounds::metal_gy1)
        .def_rw("metal_gx2", &PadBounds::metal_gx2)
        .def_rw("metal_gy2", &PadBounds::metal_gy2)
        .def_rw("approach_gx1", &PadBounds::approach_gx1)
        .def_rw("approach_gy1", &PadBounds::approach_gy1)
        .def_rw("approach_gx2", &PadBounds::approach_gx2)
        .def_rw("approach_gy2", &PadBounds::approach_gy2);

    // Segment struct
    nb::class_<Segment>(m, "Segment")
        .def(nb::init<>())
        .def_rw("x1", &Segment::x1)
        .def_rw("y1", &Segment::y1)
        .def_rw("x2", &Segment::x2)
        .def_rw("y2", &Segment::y2)
        .def_rw("width", &Segment::width)
        .def_rw("layer", &Segment::layer)
        .def_rw("net", &Segment::net);

    // Via struct
    nb::class_<Via>(m, "Via")
        .def(nb::init<>())
        .def_rw("x", &Via::x)
        .def_rw("y", &Via::y)
        .def_rw("drill", &Via::drill)
        .def_rw("diameter", &Via::diameter)
        .def_rw("layer_from", &Via::layer_from)
        .def_rw("layer_to", &Via::layer_to)
        .def_rw("net", &Via::net);

    // RouteResult struct
    nb::class_<RouteResult>(m, "RouteResult")
        .def(nb::init<>())
        .def_ro("segments", &RouteResult::segments)
        .def_ro("vias", &RouteResult::vias)
        .def_ro("net", &RouteResult::net)
        .def_ro("success", &RouteResult::success)
        // Issue #2476: structured failure diagnostics (mirrors
        // ValidationResult::violation_type vocabulary).
        .def_ro("failure_reason", &RouteResult::failure_reason)
        .def_ro("blocking_via_net", &RouteResult::blocking_via_net)
        .def_ro("failure_x", &RouteResult::failure_x)
        .def_ro("failure_y", &RouteResult::failure_y);

    // Issue #2476: FailureReason constants (exposed as module attributes
    // so Python tests/strategies can dispatch on the same vocabulary the
    // C++ search uses internally).
    m.attr("FAILURE_NONE") = static_cast<int>(FAILURE_NONE);
    m.attr("FAILURE_NO_PATH") = static_cast<int>(FAILURE_NO_PATH);
    m.attr("FAILURE_ITERATION_LIMIT") = static_cast<int>(FAILURE_ITERATION_LIMIT);
    m.attr("FAILURE_VIA_VIA_BLOCKED") = static_cast<int>(FAILURE_VIA_VIA_BLOCKED);

    // Grid3D class
    nb::class_<Grid3D>(m, "Grid3D")
        .def(nb::init<int, int, int, float, float, float>(),
             "cols"_a, "rows"_a, "layers"_a, "resolution"_a,
             "origin_x"_a = 0.0f, "origin_y"_a = 0.0f)
        // Cell access
        .def("at", nb::overload_cast<int, int, int>(&Grid3D::at),
             "x"_a, "y"_a, "layer"_a, nb::rv_policy::reference)
        .def("is_valid", &Grid3D::is_valid, "x"_a, "y"_a, "layer"_a)
        .def("is_valid_and_free", &Grid3D::is_valid_and_free,
             "x"_a, "y"_a, "layer"_a, "net"_a)
        // Coordinate conversion
        .def("world_to_grid", &Grid3D::world_to_grid, "x"_a, "y"_a)
        .def("grid_to_world", &Grid3D::grid_to_world, "gx"_a, "gy"_a)
        // Blocking operations
        .def("mark_blocked", &Grid3D::mark_blocked,
             "x"_a, "y"_a, "layer"_a, "net"_a, "is_obstacle"_a = false)
        .def("mark_rect_blocked", &Grid3D::mark_rect_blocked,
             "x1"_a, "y1"_a, "x2"_a, "y2"_a, "layer"_a, "net"_a, "is_obstacle"_a = false)
        // Route marking
        .def("mark_segment", &Grid3D::mark_segment,
             "x1"_a, "y1"_a, "x2"_a, "y2"_a, "layer"_a, "net"_a, "clearance_cells"_a)
        .def("mark_via", &Grid3D::mark_via,
             "x"_a, "y"_a, "net"_a, "radius_cells"_a)
        .def("unmark_segment", &Grid3D::unmark_segment,
             "x1"_a, "y1"_a, "x2"_a, "y2"_a, "layer"_a, "net"_a, "clearance_cells"_a)
        .def("unmark_via", &Grid3D::unmark_via,
             "x"_a, "y"_a, "net"_a, "radius_cells"_a)
        // DRC avoidance feedback
        .def("boost_region_cost", &Grid3D::boost_region_cost,
             "center_x"_a, "center_y"_a, "layer"_a, "radius_cells"_a, "amount"_a)
        .def("clear_avoidance_costs", &Grid3D::clear_avoidance_costs)
        // Congestion
        .def("get_congestion", &Grid3D::get_congestion, "x"_a, "y"_a, "layer"_a)
        .def("update_congestion", &Grid3D::update_congestion,
             "x"_a, "y"_a, "layer"_a, "delta"_a = 1)
        // Negotiated routing
        .def("reset_usage", &Grid3D::reset_usage)
        .def("increment_usage", &Grid3D::increment_usage, "x"_a, "y"_a, "layer"_a)
        .def("get_negotiated_cost", &Grid3D::get_negotiated_cost,
             "x"_a, "y"_a, "layer"_a, "present_factor"_a)
        .def("update_history_costs", &Grid3D::update_history_costs, "increment"_a)
        .def("get_total_overflow", &Grid3D::get_total_overflow)
        // Properties
        .def_prop_ro("cols", &Grid3D::cols)
        .def_prop_ro("rows", &Grid3D::rows)
        .def_prop_ro("layers", &Grid3D::layers)
        .def_prop_ro("resolution", &Grid3D::resolution)
        .def_prop_ro("total_cells", &Grid3D::total_cells)
        // Statistics
        .def("count_blocked", &Grid3D::count_blocked)
        .def("memory_mb", &Grid3D::memory_mb)
        // Geometric validation (Issue #2439)
        .def("add_pad", &Grid3D::add_pad,
             "x"_a, "y"_a, "width"_a, "height"_a,
             "net"_a, "layer_idx"_a, "ref_hash"_a, "clearance_override"_a)
        .def("add_stored_segment", &Grid3D::add_stored_segment,
             "x1"_a, "y1"_a, "x2"_a, "y2"_a,
             "width"_a, "layer_idx"_a, "net"_a)
        .def("add_stored_via", &Grid3D::add_stored_via,
             "x"_a, "y"_a, "drill"_a, "diameter"_a, "net"_a)
        .def("clear_validation_data", &Grid3D::clear_validation_data)
        .def("clear_stored_routes", &Grid3D::clear_stored_routes,
             "Issue #2481: Drop only stored route data (segments + vias), "
             "keeping pads.  Used after rip-up to invalidate the cached "
             "snapshot consulted by Pathfinder::is_via_blocked_diag.")
        .def("validate_route", &Grid3D::validate_route,
             "segments"_a, "vias"_a, "exclude_net"_a,
             "exclude_ref_hashes"_a, "trace_clearance"_a,
             "via_clearance"_a, "min_drill_clearance"_a)
        .def_prop_ro("pad_count", &Grid3D::pad_count)
        .def_prop_ro("stored_segment_count", &Grid3D::stored_segment_count)
        .def_prop_ro("stored_via_count", &Grid3D::stored_via_count);

    // Pathfinder class
    nb::class_<Pathfinder>(m, "Pathfinder")
        .def(nb::init<Grid3D&, const DesignRules&, bool>(),
             "grid"_a, "rules"_a, "diagonal_routing"_a = true)
        .def("route", &Pathfinder::route,
             "start_x"_a, "start_y"_a, "start_layer"_a,
             "end_x"_a, "end_y"_a, "end_layer"_a,
             "net"_a,
             "start_layers"_a = std::vector<int>{},
             "end_layers"_a = std::vector<int>{},
             "negotiated_mode"_a = false,
             "present_cost_factor"_a = 0.0f,
             "weight"_a = 1.0f,
             "trace_radius_cells"_a = 0,
             "via_radius_cells"_a = 0,
             "start_pad_bounds"_a = PadBounds{},
             "end_pad_bounds"_a = PadBounds{})
        .def("route_resumable", &Pathfinder::route_resumable,
             "start_x"_a, "start_y"_a, "start_layer"_a,
             "end_x"_a, "end_y"_a, "end_layer"_a,
             "net"_a,
             "start_layers"_a = std::vector<int>{},
             "end_layers"_a = std::vector<int>{},
             "negotiated_mode"_a = false,
             "present_cost_factor"_a = 0.0f,
             "weight"_a = 1.0f,
             "trace_radius_cells"_a = 0,
             "via_radius_cells"_a = 0,
             "start_pad_bounds"_a = PadBounds{},
             "end_pad_bounds"_a = PadBounds{})
        .def("resume", &Pathfinder::resume,
             "reject_x"_a, "reject_y"_a, "reject_layer"_a)
        .def("clear_search_state", &Pathfinder::clear_search_state)
        .def("set_routable_layers", &Pathfinder::set_routable_layers, "layers"_a)
        .def("is_via_blocked", &Pathfinder::is_via_blocked,
             "x"_a, "y"_a, "net"_a, "allow_sharing"_a, "radius_override"_a = 0,
             "Check if a via placement at (x, y) is blocked. "
             "Includes both grid-cell blocking and geometric via-vs-via "
             "clearance against stored_vias_ (Issue #2466).")
        .def_prop_ro("iterations", &Pathfinder::get_iterations)
        .def_prop_ro("nodes_explored", &Pathfinder::get_nodes_explored);

    // Geometry functions (Issue #2439)
    m.def("fnv1a_hash", [](const std::string& s) -> uint32_t {
        return router::fnv1a_hash(s.c_str(), s.size());
    }, "s"_a, "Compute FNV-1a hash of a string (deterministic)");

    m.def("point_to_segment_distance",
          &router::point_to_segment_distance,
          "px"_a, "py"_a, "x1"_a, "y1"_a, "x2"_a, "y2"_a,
          "Point-to-segment distance");

    m.def("segment_to_segment_distance",
          &router::segment_to_segment_distance,
          "x1"_a, "y1"_a, "x2"_a, "y2"_a,
          "x3"_a, "y3"_a, "x4"_a, "y4"_a,
          "Segment-to-segment distance");

    m.def("segments_intersect",
          &router::segments_intersect,
          "ax1"_a, "ay1"_a, "ax2"_a, "ay2"_a,
          "bx1"_a, "by1"_a, "bx2"_a, "by2"_a,
          "Test whether two segments properly intersect");

    // Version info
    m.def("version", []() { return "1.0.0"; });
    m.def("is_available", []() { return true; });

    // Build version (Issue #2501): exposed both as a module attribute (for cheap
    // identity checks at import time) and a callable (for parity with version()).
    // The Python side mirrors this as _REQUIRED_CPP_BUILD_VERSION in
    // cpp_backend.py; mismatch indicates the compiled .so is stale relative to
    // the cpp/ source tree and the C++ backend is disabled with an actionable
    // "kct build-native" hint.
    m.attr("BUILD_VERSION") = router::ROUTER_CPP_BUILD_VERSION;
    m.def("build_version", []() { return router::ROUTER_CPP_BUILD_VERSION; });
}
