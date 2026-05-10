/*
 * Router C++ Core - A* Pathfinder
 * Part of kicad-tools router performance optimization (Phase 4)
 *
 * High-performance A* pathfinding with:
 * - Multi-layer support with via transitions
 * - Diagonal routing (45-degree angles)
 * - Congestion-aware routing costs
 * - Negotiated routing support for rip-up and reroute
 */

#pragma once

#include "types.hpp"
#include "grid.hpp"
#include <vector>
#include <queue>
#include <unordered_set>
#include <unordered_map>
#include <chrono>

namespace router {

// Hash for 3D grid coordinates in unordered_set/unordered_map
struct GridPosHash {
    size_t operator()(const std::tuple<int, int, int>& pos) const {
        auto [x, y, layer] = pos;
        return std::hash<int>()(x) ^
               (std::hash<int>()(y) << 10) ^
               (std::hash<int>()(layer) << 20);
    }
};

class Pathfinder {
public:
    Pathfinder(Grid3D& grid, const DesignRules& rules, bool diagonal_routing = true);

    // Main routing function (non-resumable, backward compatible)
    //
    // Issue #2610: ``per_net_timeout_seconds`` is the wall-clock budget for
    // this A* invocation.  When <= 0 (default), no wall-clock deadline is
    // enforced and the search runs until success, open-set exhaustion, or
    // the memory-backstop iteration cap.  ``max_search_iterations`` is the
    // hard iteration ceiling; when <= 0 (default) it falls back to the
    // historical ``cols * rows * 4`` heuristic.  When the search aborts
    // because the deadline fired, ``RouteResult::failure_reason`` is set to
    // ``FAILURE_TIMEOUT``; iteration-cap aborts set ``FAILURE_ITERATION_LIMIT``.
    RouteResult route(
        float start_x, float start_y, int start_layer,
        float end_x, float end_y, int end_layer,
        int net,
        const std::vector<int>& start_layers = {},  // For PTH pads
        const std::vector<int>& end_layers = {},    // For PTH pads
        bool negotiated_mode = false,
        float present_cost_factor = 0.0f,
        float weight = 1.0f,  // A* weight (1.0 = optimal, >1.0 = faster)
        int trace_radius_cells = 0,  // Per-net trace clearance radius (0 = use default)
        int via_radius_cells = 0,    // Per-net via clearance radius (0 = use default)
        const PadBounds& start_pad_bounds = {},  // Start pad metal/approach bounds
        const PadBounds& end_pad_bounds = {},    // End pad metal/approach bounds
        // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
        // partner_net = -1 disables the partner branch (default; pre-#2559 behavior).
        // intra_pair_radius_cells = 0 means "no tighter radius" -- the partner
        // (when set) is treated with the wider trace_radius_cells.
        int partner_net = -1,
        int intra_pair_radius_cells = 0,
        // Issue #2610: per-net wall-clock deadline (seconds; <= 0 disables)
        // and override for the iteration backstop (<= 0 = use cols*rows*4).
        double per_net_timeout_seconds = 0.0,
        int max_search_iterations = 0
    );

    // Resumable A* routing: initializes search state and runs to first goal.
    // Returns a RouteResult; if success=true, state is preserved for resume().
    // Caller must call clear_search_state() when done (success or failure).
    //
    // Issue #2610: see ``route()`` for ``per_net_timeout_seconds`` and
    // ``max_search_iterations`` semantics.  The deadline is computed once
    // at ``route_resumable()`` entry and shared across the initial search
    // and any subsequent ``resume()`` calls, so a single per-net budget
    // covers all retry attempts.
    RouteResult route_resumable(
        float start_x, float start_y, int start_layer,
        float end_x, float end_y, int end_layer,
        int net,
        const std::vector<int>& start_layers = {},
        const std::vector<int>& end_layers = {},
        bool negotiated_mode = false,
        float present_cost_factor = 0.0f,
        float weight = 1.0f,
        int trace_radius_cells = 0,
        int via_radius_cells = 0,
        const PadBounds& start_pad_bounds = {},
        const PadBounds& end_pad_bounds = {},
        // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
        // See comment on route() above; defaults preserve pre-#2559 behavior.
        int partner_net = -1,
        int intra_pair_radius_cells = 0,
        // Issue #2610: deadline + iteration override; see ``route()`` above.
        double per_net_timeout_seconds = 0.0,
        int max_search_iterations = 0
    );

    // Resume A* search after rejecting a goal cell.
    // Adds (reject_x, reject_y, reject_layer) to skip set and continues
    // from the preserved open set. Returns next-best result.
    RouteResult resume(int reject_x, int reject_y, int reject_layer);

    // Clear all A* search state (open set, closed set, g scores, etc.).
    // Must be called after route_resumable()/resume() to release memory.
    void clear_search_state();

    // Configure routable layers (skip plane layers)
    void set_routable_layers(const std::vector<int>& layers);

    // Statistics from last route
    int get_iterations() const { return last_iterations_; }
    int get_nodes_explored() const { return last_nodes_explored_; }

    // Check if via placement is blocked on all layers.
    // radius_override: if > 0, use this instead of via_half_cells_.
    //
    // Public to allow direct exercise from regression tests.  See
    // tests/test_router_cpp_via_clearance.py (Issue #2466) which verifies
    // that the search refuses placements the post-route validator would
    // later reject.
    bool is_via_blocked(int x, int y, int net, bool allow_sharing,
                        int radius_override = 0) const;

    // Issue #2476: Diagnostic-aware variant of is_via_blocked.
    //
    // Identical semantics to the boolean overload above, but additionally
    // records the offending stored-via net (when the geometric via-vs-via
    // clearance rule is what caused the rejection).  When the function
    // returns true:
    //   * out_blocking_net != 0 indicates a geometric via-vs-via reject; the
    //     value is the net id of the stored via that triggered the
    //     clearance violation.  ``out_world_x``/``out_world_y`` carry the
    //     world-coordinate location of the rejected candidate.
    //   * out_blocking_net == 0 indicates the rejection came from the
    //     grid-cell blocking heuristic (out-of-bounds or another net's
    //     marked clearance), not the stored-via geometry.
    bool is_via_blocked_diag(int x, int y, int net, bool allow_sharing,
                             int radius_override,
                             int& out_blocking_net,
                             float& out_world_x,
                             float& out_world_y) const;

private:
    // Check if trace placement is blocked (accounts for trace width)
    // radius_override: if > 0, use this instead of trace_half_width_cells_
    //
    // Issue #2559 / Epic #2556 Phase 1C: when partner_net >= 0 and
    // partner_radius > 0 and partner_radius < radius, cells whose net
    // matches partner_net are checked against the smaller partner_radius
    // instead of the wider radius.  This implements within-pair clearance
    // for differential pairs.  Defaults preserve pre-#2559 behavior.
    bool is_trace_blocked(int x, int y, int layer, int net, bool allow_sharing,
                          int radius_override = 0,
                          int partner_net = -1,
                          int partner_radius = 0) const;

    // Check if diagonal move cuts through obstacles
    bool is_diagonal_blocked(int x, int y, int dx, int dy, int layer, int net,
                             bool allow_sharing) const;

    // Heuristic: Manhattan distance with layer change cost
    float heuristic(int x, int y, int layer, int goal_x, int goal_y, int goal_layer) const;

    // Get congestion cost for a cell
    float get_congestion_cost(int x, int y, int layer) const;

    // Core A* loop shared by route(), route_resumable(), and resume().
    // Returns RouteResult with success=true if goal reached, or success=false
    // if open set exhausted / iteration limit hit.
    RouteResult run_astar_loop();

    // Reconstruct path from A* result
    RouteResult reconstruct_path(
        const std::vector<AStarNode>& closed_list,
        int end_idx,
        float start_x, float start_y,
        float end_x, float end_y,
        int net
    );

    Grid3D& grid_;
    DesignRules rules_;
    bool diagonal_routing_;

    // Pre-computed neighbor offsets
    std::vector<Neighbor> neighbors_2d_;

    // Pre-computed radii in grid cells
    int trace_half_width_cells_;
    int via_half_cells_;

    // Routable layer indices
    std::vector<int> routable_layers_;

    // Statistics
    int last_iterations_ = 0;
    int last_nodes_explored_ = 0;

    // --- Resumable A* search state (promoted from route() locals) ---
    using PQ = std::priority_queue<AStarNode, std::vector<AStarNode>, std::greater<AStarNode>>;
    PQ search_open_set_;
    std::unordered_set<std::tuple<int, int, int>, GridPosHash> search_closed_set_;
    std::unordered_map<std::tuple<int, int, int>, float, GridPosHash> search_g_scores_;
    std::vector<AStarNode> search_closed_list_;

    // Set of rejected goal cells (skipped during goal test in resume())
    std::unordered_set<std::tuple<int, int, int>, GridPosHash> rejected_goals_;

    // Cached route parameters for resume()
    float search_start_x_ = 0, search_start_y_ = 0;
    float search_end_x_ = 0, search_end_y_ = 0;
    int search_end_gx_ = 0, search_end_gy_ = 0;
    int search_net_ = 0;
    int search_max_iterations_ = 0;
    std::vector<int> search_valid_start_layers_;
    std::vector<int> search_valid_end_layers_;
    bool search_negotiated_mode_ = false;
    float search_present_cost_factor_ = 0.0f;
    float search_weight_ = 1.0f;
    int search_trace_radius_cells_ = 0;
    int search_via_radius_cells_ = 0;
    PadBounds search_start_pad_bounds_{};
    PadBounds search_end_pad_bounds_{};
    bool search_state_active_ = false;  // True when resumable state is valid

    // Issue #2559 / Epic #2556 Phase 1C: diff-pair within-pair clearance.
    // Cached per-route-call so resume() and run_astar_loop() can apply
    // the partner-aware radius branch on every neighbor expansion.
    int search_partner_net_ = -1;
    int search_intra_pair_radius_cells_ = 0;

    // Issue #2476: Via-vs-via failure tracking for run_astar_loop().  Reset
    // by route_resumable() and accumulated across the search and any
    // subsequent resume() calls so that the failure reason surfaced to
    // Python reflects the blocker most recently observed before the open
    // set drained.
    int search_via_block_count_ = 0;
    int search_last_blocking_net_ = 0;
    float search_last_block_world_x_ = 0.0f;
    float search_last_block_world_y_ = 0.0f;

    // Issue #2610: Per-net wall-clock deadline for the resumable search.
    // ``search_has_deadline_`` is true when the caller supplied a positive
    // ``per_net_timeout_seconds``; in that case ``search_deadline_`` is the
    // absolute ``steady_clock`` instant at which the A* loop must abort with
    // ``FAILURE_TIMEOUT``.  ``search_timed_out_`` is set when the loop
    // observes the deadline has fired so the run_astar_loop() epilogue can
    // distinguish TIMEOUT from ITERATION_LIMIT / NO_PATH.  The deadline is
    // computed once in route_resumable() and shared with resume() so a
    // single per-net budget covers the whole sequence of (initial, resume*)
    // attempts -- matching the Python pathfinder's behavior at line 1784.
    std::chrono::steady_clock::time_point search_deadline_{};
    bool search_has_deadline_ = false;
    bool search_timed_out_ = false;
};

}  // namespace router
