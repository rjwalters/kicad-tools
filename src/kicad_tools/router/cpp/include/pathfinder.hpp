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
        const PadBounds& end_pad_bounds = {}     // End pad metal/approach bounds
    );

    // Resumable A* routing: initializes search state and runs to first goal.
    // Returns a RouteResult; if success=true, state is preserved for resume().
    // Caller must call clear_search_state() when done (success or failure).
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
        const PadBounds& end_pad_bounds = {}
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

private:
    // Check if trace placement is blocked (accounts for trace width)
    // radius_override: if > 0, use this instead of trace_half_width_cells_
    bool is_trace_blocked(int x, int y, int layer, int net, bool allow_sharing,
                          int radius_override = 0) const;

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
};

}  // namespace router
