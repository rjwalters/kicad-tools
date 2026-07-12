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
 * LIFO-seq tie-break.  The ``allow_swap_via`` polarity-swap move (#2473),
 * the ``manhattan_sum`` legacy heuristic and exact ``last_rejections``
 * string-keyed parity are intentionally DEFERRED to the pure-Python
 * fallback; the Python wrapper routes those cases to Python.
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
//   Secondary: higher g_score pops first on an f tie (greedy-on-ties, the
//              same #3199 rule the single-ended node uses -- pushes toward
//              the goal frontier).  This matches the Python coupled loop's
//              behavior: the Python heap orders CoupledNode by (f_score, seq)
//              with a NEGATED seq, and among equal (f, g) the negated seq is
//              the LIFO discriminator.  We fold in g here to stay consistent
//              with the single-ended greedy tie-break already validated by
//              the determinism suite; equal-(f,g) ties then fall to LIFO seq.
//   Tertiary:  higher seq pops first (LIFO; see the ``seq`` note above).
struct CoupledNodeGreater {
    bool operator()(const CoupledAStarNode& a, const CoupledAStarNode& b) const {
        if (a.f_score != b.f_score) return a.f_score > b.f_score;
        if (a.g_score != b.g_score) return a.g_score < b.g_score;  // higher g first
        return a.seq < b.seq;  // higher seq first (LIFO)
    }
};

class CoupledPathfinder {
public:
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

private:
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
    inline bool is_trace_blocked(int gx, int gy, int layer, int net) const {
        return is_cell_blocked(gx, gy, layer, net);
    }
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
