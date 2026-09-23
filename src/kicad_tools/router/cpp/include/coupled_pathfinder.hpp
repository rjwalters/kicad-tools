/*
 * Router C++ Core - Coupled differential-pair A* pathfinder (Issue #4065)
 *
 * C++ port of the pure-Python ``CoupledPathfinder.route_coupled`` joint-state
 * A* loop (``src/kicad_tools/router/diffpair_routing.py``).  Routes the P and
 * N traces of a differential pair simultaneously through the joint
 * ``(p_pos, n_pos, direction)`` product state space, maintaining within-pair
 * spacing and clearance.
 *
 * Scope (v1, Board 07 Phase 2b): symmetric moves + both asymmetric
 * "converge" moves + layer-change (via) moves, the #3012 spacing floor, the
 * #2473/#3508 approach/departure tolerance relaxation, the #3078/#3508
 * path-history / trail-proximity guard, the #3439 corridor bitset, the
 * ``partner_aware`` heuristic (#3115), weighted A* (#3508) and the #3508
 * LIFO-seq tie-break.  The ``allow_swap_via`` polarity-swap move (#2473)
 * and the ``manhattan_sum`` legacy heuristic are intentionally DEFERRED to
 * the pure-Python fallback; the Python wrapper routes those cases to Python.
 * Issue #4459 wired the ``last_rejections`` string-keyed histogram out of
 * the C++ search (surfaced on ``CoupledRouteResult::rejections``) so a
 * C++-path budget-exit reports which guard pruned the frontier.
 *
 * The search consumes the SAME ``Grid3D`` the single-ended ``Pathfinder``
 * uses (marshalled once via ``CppGrid.from_routing_grid``); it is a new
 * consumer of existing grid data, not a new grid representation.
 */

#pragma once

#include "types.hpp"
#include "grid.hpp"
#include <vector>
#include <cstdint>
#include <optional>
#include <unordered_map>

namespace router {

// A joint-state A* node held in a contiguous pool (mirrors the single-ended
// pathfinder's index-based parent chain).  ``parent_idx == -1`` is the root.
struct CoupledAStarNode {
    // P head.
    int p_x, p_y, p_layer;
    // N head.
    int n_x, n_y, n_layer;
    // Current routing direction (dx, dy); (0, 0) at the root.
    int dir_dx, dir_dy;
    // A* scores.
    float f_score;
    float g_score;
    // Parent pool index (-1 = root) and whether the edge from the parent was
    // a via (both heads changed layer together).
    int parent_idx;
    bool via_from_parent;
    // Issue #3508 LIFO tie-break: ``seq`` is a monotonically INCREASING
    // push counter, but the comparator prefers the HIGHER seq on an
    // f/g tie (LIFO -- newest equal-f node pops first), the negated-counter
    // convention the Python coupled loop uses at diffpair_routing.py:1652.
    // This is deliberately DIFFERENT from the single-ended ``AStarNode``
    // (types.hpp), which is FIFO (lower seq pops first).  Copying the
    // single-ended convention here would reintroduce the plateau-flooding
    // pathology this port exists to test.
    uint64_t seq;
};

// Min-heap comparator (std::priority_queue is a MAX-heap, so ``operator()``
// returns true when ``a`` should sort AFTER ``b`` = pop later).
//   Primary:   lower f_score pops first.
//   Secondary: higher seq pops first (LIFO; see the ``seq`` note above).
//
// Issue #4065 reach-parity fix (2026-07-12): this comparator now orders
// EXACTLY on ``(f_score, seq)`` -- the same key set as the Python
// ``CoupledNode`` dataclass, which declares ``g_score`` as
// ``field(compare=False)`` (diffpair_routing.py:440) and orders strictly on
// ``(f_score, seq)``.  The earlier port folded in a ``g_score`` secondary
// tie level ("greedy-on-ties", borrowed from the single-ended ``AStarNode``)
// between f and seq.  On the board-07 pairs this was empirically harmless
// (identical ``best_progress`` on all 7 pairs), but on CONVERGING searches
// -- notably board-06's USB3 pairs -- the extra g level reorders the frontier
// pop sequence relative to Python and drives the coupled A* down a different,
// worse route (dropping USB3_RX1-, 20/21).  Removing the g level restores
// bit-for-bit frontier-ordering parity with the Python coupled loop and its
// reach (21/21).  The single-ended greedy-on-ties rule intentionally stays in
// ``AStarNode`` (types.hpp); the coupled loop does not use it because Python's
// coupled loop never did.
struct CoupledNodeGreater {
    bool operator()(const CoupledAStarNode& a, const CoupledAStarNode& b) const {
        if (a.f_score != b.f_score) return a.f_score > b.f_score;
        return a.seq < b.seq;  // higher seq first (LIFO)
    }
};

class CoupledPathfinder {
public:
    void set_fill_rail_dimensions(double ph, double pg, double nh, double ng) {
        p_fill_half_ = ph; p_fill_gap_ = pg; n_fill_half_ = nh; n_fill_gap_ = ng;
    }

    // Issue #5410 (B1): per-net-class dimensions for the dynamic route-halo
    // refinement, mirroring what ``RouteHaloRefiner`` reads off the net class
    // on the Python side (``nc.trace_width``, ``nc.clearance``,
    // ``nc.via_size``).
    //
    // WHY this exists.  The halo refinement WAIVES a raster rejection, so the
    // scalar it re-measures with is a real design rule, not a search radius.
    // The raster halo it overrides was dilated with the candidate's NET-CLASS
    // clearance; re-measuring with the global ``DesignRules`` scalar therefore
    // admits candidates the net class forbids whenever the class is wider than
    // the global rule -- the under-blocking direction.  The single-ended
    // ``Pathfinder`` already threads its effective values in
    // (``search_trace_half_width_mm_`` / ``search_fill_trace_clearance_``);
    // this is the coupled equivalent, keyed by net id because the coupled
    // predicates are called for both rails.
    //
    // Nets with no entry fall back to the global rules, so a caller that
    // installs nothing keeps exactly the pre-#5410 global-rule behaviour.
    //
    // Issue #5711: ``partner_net`` / ``partner_clearance`` carry the diff-pair
    // intra-pair waiver ``RouteHaloRefiner.trace_clear`` passes into
    // ``RouteHaloGeometry.clear`` (``nc.diffpair_partner`` resolved to a net
    // id, and ``nc.effective_intra_pair_clearance()``).  Omitting them made
    // partner copper demand the ordinary, wider class clearance -- safe, but
    // 212 cells more conservative than the Python arm on a class whose
    // intra-pair gap is narrower than its clearance, which is the normal
    // reason to author one.  ``partner_net < 0`` or ``partner_clearance < 0``
    // means "no waiver", matching the Python ``partner is None`` branch.
    void set_halo_net_dimensions(int net, double trace_width,
                                 double trace_clearance, double via_diameter,
                                 int partner_net = -1,
                                 double partner_clearance = -1.0) {
        halo_net_dims_[net] = HaloNetDims{trace_width, trace_clearance, via_diameter,
                                          partner_net, partner_clearance};
    }
    void clear_halo_net_dimensions() { halo_net_dims_.clear(); }
    // All construction-time scalars mirror the Python
    // ``CoupledPathfinder.__init__`` derived radii and rule constants.  The
    // Python side pre-computes the trace/via clearance radii (identical
    // formulas) and passes them in, so C++ does no rule-string parsing.
    CoupledPathfinder(Grid3D& grid,
                      const DesignRules& rules,
                      int target_spacing_cells,
                      int min_spacing_cells,
                      int trace_half_width_cells,
                      int via_extra_cells,
                      int via_drill_cells,
                      double spacing_penalty_factor,
                      double heuristic_weight);

    // Route a coupled pair.  All positions are GRID coordinates (the Python
    // wrapper does world_to_grid + layer_to_index before calling, exactly as
    // ``route_coupled`` does).  ``routable_layers`` is the grid's routable
    // layer index list.  ``corridor_bitset`` is a flat ``cols*rows`` bool
    // mask (empty vector = no corridor); ``corridor_exempt`` are the 4
    // endpoint (x,y) cells exempt from corridor pruning.  Budgets mirror the
    // Python kwargs.  Returns a ``CoupledRouteResult`` with the joint path
    // (root->goal) and the #4052 diagnostics.
    CoupledRouteResult route(
        int p_start_x, int p_start_y,
        int n_start_x, int n_start_y,
        int start_layer,
        int p_goal_x, int p_goal_y,
        int n_goal_x, int n_goal_y,
        int end_layer,
        int p_net, int n_net,
        int effective_target_spacing,
        int effective_approach_radius,
        int effective_departure_radius,
        const std::vector<int>& routable_layers,
        const std::vector<uint8_t>& corridor_bitset,
        int max_iterations_budget,
        double timeout_seconds);

    // Issue #5410: public probes over the two blocked predicates the dynamic-
    // halo refinement changed.  The joint-state search is a single opaque
    // ``route`` call, so without these a backend-parity test could only infer
    // the branch's verdict from whether a whole pair happened to route.
    bool trace_blocked(int gx, int gy, int layer, int net,
                       int from_x = -1, int from_y = -1) const {
        return is_trace_blocked(gx, gy, layer, net, from_x, from_y);
    }
    bool via_blocked(int gx, int gy, int net) const {
        return is_via_blocked(gx, gy, net);
    }
    // Epic #5509 Phase 3c (#5662): the coupled search's rail clearance gate,
    // promoted out of the ``route()`` loop into a named, bindable method.
    //
    // Two things changed when it was promoted.  It is now **public and
    // reachable from Python** (``bindings.cpp`` exposes it), which is what
    // retires the epic's one genuinely unexposed consumer group -- group 7
    // used to be a lambda no oracle adapter could drive.  And it now consults
    // the shared exact-geometry clearance kernel
    // (``clearance_kernel.hpp``) against the grid's **stored route
    // geometry**, not just its fixed fills: committed copper that the C++
    // blocked plane has not been re-synced with was invisible to the coupled
    // search by construction, which is the #4507 defect.
    //
    // ``ax``/``ay`` -> ``bx``/``by`` is the candidate rail step in GRID
    // coordinates; ``layer`` the layer it is traced on (ignored for a via
    // candidate, which is copper on every layer); ``net`` the rail's own net;
    // ``partner_net`` the other rail's net (pass ``-1`` for none), whose
    // copper is deliberately exempt here -- within-pair spacing is the
    // search's own spacing constraint plus the commit-time intra-pair gate,
    // not this foreign-copper check; ``rail_half`` / ``rail_gap`` the
    // per-rail copper half-width and clearance (negative = fall back to the
    // ``DesignRules`` scalars), matching ``set_fill_rail_dimensions``.
    bool rail_clear(int ax, int ay, int bx, int by, int layer, int net,
                    int partner_net, double rail_half, double rail_gap,
                    bool is_via) const;

    // The same gate in WORLD millimetres, which is where the arithmetic
    // actually lives -- ``rail_clear`` is ``grid_to_world`` plus this call.
    //
    // Both are bound.  The grid-coordinate form is what the search uses; the
    // world form is what the Epic #5509 conformance adapter must use, because
    // snapping a corpus case's copper onto the routing grid first would
    // measure the raster's quantisation instead of this consumer's clearance
    // model.
    bool rail_clear_world(double ax, double ay, double bx, double by,
                          int layer, int net, int partner_net,
                          double rail_half, double rail_gap,
                          bool is_via) const;

private:
    // The stored-route half of ``rail_clear``.
    bool stored_route_clear(double ax, double ay, double bx, double by,
                            int layer, int net, int partner_net,
                            double half, double gap, bool is_via) const;

    // Issue #5410 (B1): net id -> effective net-class halo dimensions.
    struct HaloNetDims {
        double trace_width;
        double trace_clearance;
        double via_diameter;
        // Issue #5711: the diff-pair intra-pair waiver, or (-1, -1) for none.
        int partner_net = -1;
        double partner_clearance = -1.0;
    };
    std::unordered_map<int, HaloNetDims> halo_net_dims_;
    const HaloNetDims* halo_dims_for(int net) const {
        auto it = halo_net_dims_.find(net);
        return it == halo_net_dims_.end() ? nullptr : &it->second;
    }

    double p_fill_half_ = -1, p_fill_gap_ = -1, n_fill_half_ = -1, n_fill_gap_ = -1;
    Grid3D& grid_;
    DesignRules rules_;
    int target_spacing_cells_;
    int min_spacing_cells_;
    int trace_half_width_cells_;
    int via_extra_cells_;
    int via_drill_cells_;
    double spacing_penalty_factor_;
    double heuristic_weight_;
    int cols_, rows_, num_layers_;

    // Grid-cell predicates (inlined mirror of the Python helpers).
    inline bool is_cell_blocked(int gx, int gy, int layer, int net) const {
        if (gx < 0 || gx >= cols_ || gy < 0 || gy >= rows_) return true;
        if (layer < 0 || layer >= num_layers_) return true;
        const GridCell& cell = grid_.at(gx, gy, layer);
        return cell.blocked && cell.net != net;
    }
    // Issue #5410: a conservative dynamic route halo is an acceleration
    // structure, not a physical constraint.  ``mark_segment`` / ``mark_via``
    // dilate committed copper to whole grid cells, so a foreign route's halo
    // covers candidates whose ACTUAL copper and drill gaps satisfy the
    // effective rules.  PR #5425 taught the per-net ``Pathfinder`` to measure
    // that geometry before rejecting such a cell; these two helpers apply the
    // identical refinement to the coupled joint-state search, which until now
    // consulted the raster alone.
    //
    // ``route_cell_has_geometry`` is the provenance gate: it answers false for
    // out-of-bounds cells, pad metal, static halos, keepouts, reserved cells,
    // and for any cell whose covering marks lack registered physical geometry
    // -- so every hard or unverifiable blockage keeps its rejection and only
    // verified dynamic route copper is ever re-measured.
    bool trace_halo_cell_clear(int cx, int cy, int layer, int from_x, int from_y,
                               int to_x, int to_y, int net) const;
    bool via_route_geometry_clear(int x, int y, int net) const;
    // ``from_x`` / ``from_y`` (default -1) name the step's ORIGIN cell so the
    // refinement measures the swept segment, not just its endpoint.
    bool is_trace_blocked(int gx, int gy, int layer, int net,
                          int from_x = -1, int from_y = -1) const;
    bool is_via_blocked(int gx, int gy, int net) const;

    inline bool at_goal(int x, int y, int gx, int gy) const {
        return x == gx && y == gy;
    }

    double heuristic(int p_x, int p_y, int p_layer,
                     int n_x, int n_y, int n_layer,
                     int p_goal_x, int p_goal_y, int p_goal_layer,
                     int n_goal_x, int n_goal_y, int n_goal_layer) const;
};

}  // namespace router
