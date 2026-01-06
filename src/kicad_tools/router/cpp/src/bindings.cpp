/*
 * Router C++ Core - nanobind Python bindings
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#include "grid.hpp"
#include "pathfinder.hpp"
#include "types.hpp"
#include <nanobind/nanobind.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/optional.h>

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
        .def_rw("original_net", &GridCell::original_net);

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
        .def_rw("congestion_threshold", &DesignRules::congestion_threshold);

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
        .def_ro("success", &RouteResult::success);

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
        .def("memory_mb", &Grid3D::memory_mb);

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
             "weight"_a = 1.0f)
        .def("set_routable_layers", &Pathfinder::set_routable_layers, "layers"_a)
        .def_prop_ro("iterations", &Pathfinder::get_iterations)
        .def_prop_ro("nodes_explored", &Pathfinder::get_nodes_explored);

    // Version info
    m.def("version", []() { return "1.0.0"; });
    m.def("is_available", []() { return true; });
}
