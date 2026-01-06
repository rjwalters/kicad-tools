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

class Pathfinder {
public:
    Pathfinder(Grid3D& grid, const DesignRules& rules, bool diagonal_routing = true);

    // Main routing function
    RouteResult route(
        float start_x, float start_y, int start_layer,
        float end_x, float end_y, int end_layer,
        int net,
        const std::vector<int>& start_layers = {},  // For PTH pads
        const std::vector<int>& end_layers = {},    // For PTH pads
        bool negotiated_mode = false,
        float present_cost_factor = 0.0f,
        float weight = 1.0f  // A* weight (1.0 = optimal, >1.0 = faster)
    );

    // Configure routable layers (skip plane layers)
    void set_routable_layers(const std::vector<int>& layers);

    // Statistics from last route
    int get_iterations() const { return last_iterations_; }
    int get_nodes_explored() const { return last_nodes_explored_; }

private:
    // Check if trace placement is blocked (accounts for trace width)
    bool is_trace_blocked(int x, int y, int layer, int net, bool allow_sharing) const;

    // Check if diagonal move cuts through obstacles
    bool is_diagonal_blocked(int x, int y, int dx, int dy, int layer, int net,
                             bool allow_sharing) const;

    // Check if via placement is blocked on all layers
    bool is_via_blocked(int x, int y, int net, bool allow_sharing) const;

    // Heuristic: Manhattan distance with layer change cost
    float heuristic(int x, int y, int layer, int goal_x, int goal_y, int goal_layer) const;

    // Get congestion cost for a cell
    float get_congestion_cost(int x, int y, int layer) const;

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
};

// Hash for 3D grid coordinates in unordered_set
struct GridPosHash {
    size_t operator()(const std::tuple<int, int, int>& pos) const {
        auto [x, y, layer] = pos;
        return std::hash<int>()(x) ^
               (std::hash<int>()(y) << 10) ^
               (std::hash<int>()(layer) << 20);
    }
};

}  // namespace router
