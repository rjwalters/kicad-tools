/*
 * Router C++ Core - Common Types
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#pragma once

#include <cstdint>
#include <vector>
#include <tuple>
#include <optional>

namespace router {

// Grid cell state
struct GridCell {
    bool blocked = false;
    int32_t net = 0;
    int16_t usage_count = 0;
    float history_cost = 0.0f;
    bool is_obstacle = false;
    bool is_zone = false;
    bool pad_blocked = false;
    int32_t original_net = 0;
};

// A* node for priority queue
struct AStarNode {
    float f_score;
    float g_score;
    int x;
    int y;
    int layer;
    int parent_idx;  // Index in closed set, -1 if no parent
    bool via_from_parent;
    int dx;  // Direction from parent
    int dy;

    // Comparison for min-heap (lower f_score first)
    bool operator>(const AStarNode& other) const {
        return f_score > other.f_score;
    }
};

// Route segment
struct Segment {
    float x1, y1, x2, y2;
    float width;
    int layer;
    int net;
};

// Via
struct Via {
    float x, y;
    float drill;
    float diameter;
    int layer_from;
    int layer_to;
    int net;
};

// Complete route result
struct RouteResult {
    std::vector<Segment> segments;
    std::vector<Via> vias;
    int net;
    bool success;
};

// Neighbor direction: dx, dy, dlayer, cost_multiplier
struct Neighbor {
    int dx;
    int dy;
    int dlayer;
    float cost_mult;
};

// Design rules (simplified for C++ core)
struct DesignRules {
    float trace_width = 0.127f;
    float trace_clearance = 0.127f;
    float via_drill = 0.3f;
    float via_diameter = 0.6f;
    float via_clearance = 0.127f;
    float grid_resolution = 0.127f;
    float cost_straight = 1.0f;
    float cost_turn = 1.5f;
    float cost_via = 10.0f;
    float cost_congestion = 5.0f;
    float congestion_threshold = 0.5f;
};

}  // namespace router
