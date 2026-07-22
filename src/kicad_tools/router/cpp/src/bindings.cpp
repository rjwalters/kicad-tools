/*
 * Router C++ Core - nanobind Python bindings
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#include "grid.hpp"
#include "geometry.hpp"
#include "pathfinder.hpp"
#include "coupled_pathfinder.hpp"
#include "types.hpp"
#include <nanobind/nanobind.h>
#include <nanobind/stl/vector.h>
#include <nanobind/stl/pair.h>
#include <nanobind/stl/tuple.h>
#include <nanobind/stl/optional.h>
#include <nanobind/stl/string.h>
#include <nanobind/stl/unordered_map.h>

namespace nb = nanobind;
using namespace nb::literals;
using namespace router;

// Defined in mesh.cpp (issue #4268): registers the poly2tri constrained-
// Delaunay mesh binding onto the module. Kept in its own translation unit so
// the vendored poly2tri headers stay out of the main bindings compile.
void register_mesh(nb::module_& m);

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
        // Issue #3545: static-blockage flag -- set by ``mark_blocked``
        // for board geometry (pads, halos, keepouts); consulted by
        // ``unmark_segment`` / ``unmark_via`` so rip-up restores static
        // blockage instead of erasing it.
        .def_rw("static_blocked", &GridCell::static_blocked)
        .def_rw("avoidance_cost", &GridCell::avoidance_cost)
        // Issue #4071: per-cell corridor-reservation owner set count
        // (read-only surface for parity tests; the owner nets are written
        // via ``Grid3D::reserve_cell`` / read via ``is_reserved_for``).
        .def_ro("reserved_count", &GridCell::reserved_count);

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
        .def_rw("min_drill_clearance", &DesignRules::min_drill_clearance)
        // Issue #4071: soft corridor-attractor bonus (Python default 3.0).
        .def_rw("cost_corridor_attractor", &DesignRules::cost_corridor_attractor);

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

    // PadChannelBudget struct (Issue #3143)
    //
    // Soft per-cell penalty rectangle used to nudge the A* search toward
    // less-contested escape paths in the lateral channels adjacent to
    // dense-package pad rows.  See types.hpp::PadChannelBudget for the
    // per-field contract and the cost-shaping rationale.  Python callers
    // (cpp_backend.py / core.py) construct these once per net before
    // dispatching to route_resumable().
    nb::class_<PadChannelBudget>(m, "PadChannelBudget")
        .def(nb::init<>())
        .def_rw("gx1", &PadChannelBudget::gx1)
        .def_rw("gy1", &PadChannelBudget::gy1)
        .def_rw("gx2", &PadChannelBudget::gx2)
        .def_rw("gy2", &PadChannelBudget::gy2)
        .def_rw("layer", &PadChannelBudget::layer)
        .def_rw("capacity", &PadChannelBudget::capacity)
        .def_rw("overflow_penalty", &PadChannelBudget::overflow_penalty)
        .def_rw("origin_pad_ref_hash", &PadChannelBudget::origin_pad_ref_hash)
        // Issue #3143: ``source_net`` (origin net of the escape pad) lets
        // the Python adapter filter out the current net's own budget
        // entries before forwarding to the C++ pathfinder.
        .def_rw("source_net", &PadChannelBudget::source_net);

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
    // Issue #2610: wall-clock deadline (--per-net-timeout) was hit.
    m.attr("FAILURE_TIMEOUT") = static_cast<int>(FAILURE_TIMEOUT);
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
        // Blocking operations.
        //
        // Issue #3224: ``pad_blocked`` (default ``false``) marks the cell as
        // foreign pad copper.  The A* clearance branch in
        // ``pathfinder.cpp`` (lines 680 and 1173 -- the one-shot and
        // resumable / negotiated paths) reads ``cell.pad_blocked`` to
        // distinguish foreign-pad metal from foreign-pad clearance halo;
        // only halo cells participate in the pad-exit exemption.
        // Without the bit, the C++ A* accepted traces stepping through
        // foreign pad copper.  Defaults to ``false`` so existing callers
        // that mark obstacle cells (board outline, copper-pour clearance
        // halos) preserve their pre-#3224 behavior.
        .def("mark_blocked", &Grid3D::mark_blocked,
             "x"_a, "y"_a, "layer"_a, "net"_a, "is_obstacle"_a = false,
             "pad_blocked"_a = false)
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
        // Issue #4071: corridor-reservation write/read API.  Mirrors
        // Python ``RoutingGrid.reserve_corridor_cells`` (per-cell),
        // ``is_reserved_for``, ``clear_corridor_reservations``, and
        // ``reserved_cell_count``.  ``mark_via`` and the A* cost loop
        // honour these reservations (keep-out + attractor).
        // Issue #4079: ``soft`` (default False) selects HARD (via + lateral
        // keep-out) vs SOFT (attractor-only) reservation strength.
        .def("reserve_cell", &Grid3D::reserve_cell,
             "x"_a, "y"_a, "layer"_a, "net_ids"_a, "soft"_a = false)
        .def("clear_reservations", &Grid3D::clear_reservations)
        .def("reserved_cell_count", &Grid3D::reserved_cell_count)
        .def("is_reserved_for", &Grid3D::is_reserved_for,
             "x"_a, "y"_a, "layer"_a, "net"_a)
        .def("has_reservations", &Grid3D::has_reservations)
        .def("corridor_attractor_bonus", &Grid3D::corridor_attractor_bonus,
             "x"_a, "y"_a, "layer"_a, "net"_a, "bonus"_a)
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
        .def("decrement_usage", &Grid3D::decrement_usage, "x"_a, "y"_a, "layer"_a,
             "Issue #3438: rip-up parity -- mirrors "
             "RoutingGrid.unmark_route_usage so the C++ sharing-mode "
             "clauses see the same usage counts as the Python grid.")
        .def("get_negotiated_cost", &Grid3D::get_negotiated_cost,
             "x"_a, "y"_a, "layer"_a, "present_factor"_a, "net"_a = 0)
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
             "net"_a, "layer_idx"_a, "ref_hash"_a, "clearance_override"_a,
             "is_plane_net"_a = false)
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
             "via_clearance"_a, "min_drill_clearance"_a,
             "partner_net"_a = -1,
             "intra_pair_clearance"_a = 0.0f,
             "Validate a candidate route against stored geometry.  Issue #2559 "
             "/ Phase 1C: when partner_net >= 0 and intra_pair_clearance >= 0, "
             "comparisons against partner_net use intra_pair_clearance instead "
             "of trace_clearance (defaults preserve pre-#2559 behavior).")
        .def_prop_ro("pad_count", &Grid3D::pad_count)
        .def_prop_ro("stored_segment_count", &Grid3D::stored_segment_count)
        .def_prop_ro("stored_via_count", &Grid3D::stored_via_count);

    // Pathfinder class
    nb::class_<Pathfinder>(m, "Pathfinder")
        // Issue #4485: ``Pathfinder`` stores ``Grid3D& grid_`` -- a bare C++
        // reference to the ``Grid3D`` argument.  Without a keep-alive policy
        // nanobind is free to garbage-collect the Python ``Grid3D`` wrapper as
        // soon as the caller drops its last reference (e.g. the common
        // ``Pathfinder(Grid3D(...), rules).route(...)`` idiom, where the grid
        // is an unnamed temporary).  That frees the underlying ``cells_``
        // storage and leaves ``grid_`` dangling; the next ``route()`` reads
        // freed heap, producing the nondeterministic segment-count instability
        // reported in #4485 (confirmed as a heap-use-after-free by
        // AddressSanitizer at ``pathfinder.cpp`` ``grid_.at(...)``).
        // ``keep_alive<1, 2>`` ties the grid's lifetime (patient, arg index 2)
        // to the newly-constructed Pathfinder (nurse, index 1 = the implicit
        // ``self``/instance slot of the ``__init__`` binding).
        .def(nb::init<Grid3D&, const DesignRules&, bool>(),
             "grid"_a, "rules"_a, "diagonal_routing"_a = true,
             nb::keep_alive<1, 2>())
        // Issue #4346: ``start_pad_bounds`` / ``end_pad_bounds`` are bound via a
        // thin lambda taking ``std::optional<PadBounds>`` defaulting to
        // ``nb::none()`` instead of a materialized ``PadBounds{}`` default arg.
        // A *bound-type* default argument makes nanobind cast the sentinel into a
        // persistent Python object held for the module's lifetime; because CPython
        // does not deallocate extension modules at interpreter finalization, those
        // objects are still live when nanobind's teardown leak checker runs -- the
        // exact source of the "leaked 4 instances of PadBounds" report on
        // ``kct build-native``. Defaulting to None keeps zero tracked instances;
        // the omitted case substitutes an all-zero ``PadBounds{}`` inside the
        // lambda, preserving pre-#4346 Python-visible behavior identically.
        .def("route",
             [](Pathfinder& self,
                float start_x, float start_y, int start_layer,
                float end_x, float end_y, int end_layer,
                int net,
                const std::vector<int>& start_layers,
                const std::vector<int>& end_layers,
                bool negotiated_mode,
                float present_cost_factor,
                float weight,
                int trace_radius_cells,
                int via_radius_cells,
                std::optional<PadBounds> start_pad_bounds,
                std::optional<PadBounds> end_pad_bounds,
                int partner_net,
                int intra_pair_radius_cells,
                double per_net_timeout_seconds,
                int max_search_iterations,
                float emit_trace_width,
                float emit_via_diameter,
                float emit_via_drill,
                const std::vector<PadChannelBudget>& pad_channel_budgets) {
                 return self.route(
                     start_x, start_y, start_layer,
                     end_x, end_y, end_layer,
                     net,
                     start_layers, end_layers,
                     negotiated_mode, present_cost_factor, weight,
                     trace_radius_cells, via_radius_cells,
                     start_pad_bounds.value_or(PadBounds{}),
                     end_pad_bounds.value_or(PadBounds{}),
                     partner_net, intra_pair_radius_cells,
                     per_net_timeout_seconds, max_search_iterations,
                     emit_trace_width, emit_via_diameter, emit_via_drill,
                     pad_channel_budgets);
             },
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
             "start_pad_bounds"_a = nb::none(),
             "end_pad_bounds"_a = nb::none(),
             // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
             "partner_net"_a = -1,
             "intra_pair_radius_cells"_a = 0,
             // Issue #2610: per-net wall-clock deadline (seconds; <= 0 disables)
             // and override for the iteration backstop (<= 0 = cols*rows*4).
             "per_net_timeout_seconds"_a = 0.0,
             "max_search_iterations"_a = 0,
             // Issue #3130: per-net emit widths/diameters (0 = use rules_ defaults).
             // Defaults preserve pre-#3130 emit behavior identically.
             "emit_trace_width"_a = 0.0f,
             "emit_via_diameter"_a = 0.0f,
             "emit_via_drill"_a = 0.0f,
             // Issue #3143: per-pad lateral-channel budget (empty = inert).
             "pad_channel_budgets"_a = std::vector<PadChannelBudget>{})
        .def("route_resumable",
             [](Pathfinder& self,
                float start_x, float start_y, int start_layer,
                float end_x, float end_y, int end_layer,
                int net,
                const std::vector<int>& start_layers,
                const std::vector<int>& end_layers,
                bool negotiated_mode,
                float present_cost_factor,
                float weight,
                int trace_radius_cells,
                int via_radius_cells,
                std::optional<PadBounds> start_pad_bounds,
                std::optional<PadBounds> end_pad_bounds,
                int partner_net,
                int intra_pair_radius_cells,
                double per_net_timeout_seconds,
                int max_search_iterations,
                float emit_trace_width,
                float emit_via_diameter,
                float emit_via_drill,
                const std::vector<PadChannelBudget>& pad_channel_budgets) {
                 return self.route_resumable(
                     start_x, start_y, start_layer,
                     end_x, end_y, end_layer,
                     net,
                     start_layers, end_layers,
                     negotiated_mode, present_cost_factor, weight,
                     trace_radius_cells, via_radius_cells,
                     start_pad_bounds.value_or(PadBounds{}),
                     end_pad_bounds.value_or(PadBounds{}),
                     partner_net, intra_pair_radius_cells,
                     per_net_timeout_seconds, max_search_iterations,
                     emit_trace_width, emit_via_diameter, emit_via_drill,
                     pad_channel_budgets);
             },
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
             "start_pad_bounds"_a = nb::none(),
             "end_pad_bounds"_a = nb::none(),
             // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
             "partner_net"_a = -1,
             "intra_pair_radius_cells"_a = 0,
             // Issue #2610: per-net wall-clock deadline (seconds; <= 0 disables)
             // and override for the iteration backstop (<= 0 = cols*rows*4).
             "per_net_timeout_seconds"_a = 0.0,
             "max_search_iterations"_a = 0,
             // Issue #3130: per-net emit widths/diameters (0 = use rules_ defaults).
             // Defaults preserve pre-#3130 emit behavior identically.
             "emit_trace_width"_a = 0.0f,
             "emit_via_diameter"_a = 0.0f,
             "emit_via_drill"_a = 0.0f,
             // Issue #3143: per-pad lateral-channel budget (empty = inert).
             "pad_channel_budgets"_a = std::vector<PadChannelBudget>{})
        .def("resume", &Pathfinder::resume,
             "reject_x"_a, "reject_y"_a, "reject_layer"_a)
        .def("clear_search_state", &Pathfinder::clear_search_state)
        .def("set_routable_layers", &Pathfinder::set_routable_layers, "layers"_a)
        .def("is_via_blocked", &Pathfinder::is_via_blocked,
             "x"_a, "y"_a, "net"_a, "allow_sharing"_a, "radius_override"_a = 0,
             "Check if a via placement at (x, y) is blocked. "
             "Includes both grid-cell blocking and geometric via-vs-via "
             "clearance against stored_vias_ (Issue #2466).")
        .def("is_diagonal_blocked", &Pathfinder::is_diagonal_blocked,
             "x"_a, "y"_a, "dx"_a, "dy"_a, "layer"_a, "net"_a,
             "allow_sharing"_a,
             "Check if a diagonal move from (x, y) toward (dx, dy) would "
             "cut through obstacle corners.  Exposed for the Issue #3456 "
             "regression tests: same-net cells must be passable in "
             "standard mode regardless of the obstacle flag (parity with "
             "the Python _is_diagonal_corner_blocked, Issue #864).")
        .def("is_trace_blocked", &Pathfinder::is_trace_blocked,
             "x"_a, "y"_a, "layer"_a, "net"_a, "allow_sharing"_a,
             "radius_override"_a = 0, "partner_net"_a = -1,
             "partner_radius"_a = 0,
             "Check if a trace placement at (x, y, layer) is blocked, "
             "accounting for trace width.  Exposed for the Issue #3456 "
             "regression tests: same-net cells must be passable in "
             "standard mode regardless of the obstacle flag (parity with "
             "the Python _is_trace_blocked, Issue #864).")
        .def("is_foreign_pad_metal_within_radius",
             &Pathfinder::is_foreign_pad_metal_within_radius,
             "x"_a, "y"_a, "layer"_a, "net"_a, "radius"_a,
             "Return True if any cell within Chebyshev ``radius`` of (x, y, layer) "
             "has pad_blocked=True and a foreign net.  Used by the pad-exit "
             "clearance guard (Issue #3226) to refuse a relaxation step into the "
             "inner part of an adjacent foreign pad's halo.")
        .def("set_relief_mode", &Pathfinder::set_relief_mode, "enabled"_a,
             "Issue #3438: enable/disable the relief-probe mode.  When "
             "enabled, sharing-mode foreign usage-0 non-obstacle cells "
             "(escape stubs, route clearance halos, via halo rings) become "
             "passable at a finite per-step penalty instead of hard, so a "
             "zero-overflow hard failure can produce a min-conflict probe "
             "path whose crossed owner nets feed the targeted rip-up.")
        .def_prop_ro("relief_mode", &Pathfinder::relief_mode)
        .def_prop_ro("iterations", &Pathfinder::get_iterations)
        .def_prop_ro("nodes_explored", &Pathfinder::get_nodes_explored);

    // CoupledPathNode struct (Issue #4065): one joint-state node on the
    // reconstructed coupled path (root->goal order).  The Python wrapper
    // unpacks these into the ``p_path`` / ``n_path`` lists that
    // ``_reconstruct_coupled_routes`` builds, then feeds them to the
    // unchanged Python ``_build_route_from_path``.
    nb::class_<CoupledPathNode>(m, "CoupledPathNode")
        .def(nb::init<>())
        .def_ro("p_x", &CoupledPathNode::p_x)
        .def_ro("p_y", &CoupledPathNode::p_y)
        .def_ro("p_layer", &CoupledPathNode::p_layer)
        .def_ro("n_x", &CoupledPathNode::n_x)
        .def_ro("n_y", &CoupledPathNode::n_y)
        .def_ro("n_layer", &CoupledPathNode::n_layer)
        .def_ro("via_from_parent", &CoupledPathNode::via_from_parent);

    // CoupledRouteResult struct (Issue #4065): the two-trace joint path plus
    // the #4052 budget-exit diagnostics (iterations / best_progress /
    // timeout_exceeded / iteration_limited) the Python caller reads back as
    // ``last_*`` attributes.
    nb::class_<CoupledRouteResult>(m, "CoupledRouteResult")
        .def(nb::init<>())
        .def_ro("path", &CoupledRouteResult::path)
        .def_ro("success", &CoupledRouteResult::success)
        .def_ro("iterations", &CoupledRouteResult::iterations)
        .def_ro("best_progress", &CoupledRouteResult::best_progress)
        .def_ro("timeout_exceeded", &CoupledRouteResult::timeout_exceeded)
        .def_ro("iteration_limited", &CoupledRouteResult::iteration_limited)
        // Issue #4459: per-reason move-rejection histogram (reason -> count).
        .def_ro("rejections", &CoupledRouteResult::rejections);

    // CoupledPathfinder class (Issue #4065): C++ port of the joint-state
    // diff-pair A* loop.  Consumes the SAME Grid3D as the single-ended
    // Pathfinder.  See coupled_pathfinder.hpp for the v1 scope / deferred
    // features (allow_swap_via, manhattan_sum heuristic).  Issue #4459 wired
    // the string-keyed rejection histogram out of the C++ search (previously
    // Python-only), surfaced on ``CoupledRouteResult::rejections``.
    nb::class_<CoupledPathfinder>(m, "CoupledPathfinder")
        // Issue #4485: like ``Pathfinder``, ``CoupledPathfinder`` holds a bare
        // ``Grid3D& grid_`` reference, so the grid argument must outlive the
        // pathfinder.  ``keep_alive<1, 2>`` ties the grid (patient, arg index
        // 2) to the constructed CoupledPathfinder (nurse, index 1) to prevent
        // the same dangling-reference use-after-free.
        .def(nb::init<Grid3D&, const DesignRules&, int, int, int, int, int,
                      double, double>(),
             "grid"_a, "rules"_a, "target_spacing_cells"_a, "min_spacing_cells"_a,
             "trace_half_width_cells"_a, "via_extra_cells"_a, "via_drill_cells"_a,
             "spacing_penalty_factor"_a, "heuristic_weight"_a,
             nb::keep_alive<1, 2>())
        .def("route", &CoupledPathfinder::route,
             "p_start_x"_a, "p_start_y"_a, "n_start_x"_a, "n_start_y"_a,
             "start_layer"_a,
             "p_goal_x"_a, "p_goal_y"_a, "n_goal_x"_a, "n_goal_y"_a,
             "end_layer"_a,
             "p_net"_a, "n_net"_a,
             "effective_target_spacing"_a, "effective_approach_radius"_a,
             "effective_departure_radius"_a,
             "routable_layers"_a, "corridor_bitset"_a,
             "max_iterations_budget"_a, "timeout_seconds"_a);

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

    // Issue #4268: poly2tri constrained-Delaunay mesh binding for the
    // mesh-router navigation substrate.
    register_mesh(m);
}
