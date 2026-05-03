/*
 * Router C++ Core - A* Pathfinder Implementation
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#include "pathfinder.hpp"
#include <queue>
#include <unordered_set>
#include <cmath>
#include <algorithm>

namespace router {

Pathfinder::Pathfinder(Grid3D& grid, const DesignRules& rules, bool diagonal_routing)
    : grid_(grid), rules_(rules), diagonal_routing_(diagonal_routing) {

    // Pre-compute neighbor offsets for 2D moves
    neighbors_2d_ = {
        {1, 0, 0, 1.0f},   // Right
        {-1, 0, 0, 1.0f},  // Left
        {0, 1, 0, 1.0f},   // Down
        {0, -1, 0, 1.0f},  // Up
    };

    if (diagonal_routing) {
        // Add 45-degree diagonal moves (cost = sqrt(2) ~= 1.414)
        neighbors_2d_.push_back({1, 1, 0, 1.414f});   // Down-Right
        neighbors_2d_.push_back({-1, 1, 0, 1.414f});  // Down-Left
        neighbors_2d_.push_back({1, -1, 0, 1.414f});  // Up-Right
        neighbors_2d_.push_back({-1, -1, 0, 1.414f}); // Up-Left
    }

    // Pre-compute trace clearance radius in grid cells
    // This is the total radius from trace centerline that must be clear:
    // - trace_width/2: half-width of the trace copper
    // - trace_clearance: required clearance from trace edge to obstacles
    // This enforces clearance as a hard constraint during routing.
    // Issue #553: Previously only checked trace_width/2, causing DRC violations
    // when traces were placed too close to obstacles.
    trace_half_width_cells_ = std::max(
        1, static_cast<int>(std::ceil(
            (rules.trace_width / 2 + rules.trace_clearance) / grid.resolution())));

    // Pre-compute via blocking radius in grid cells
    via_half_cells_ = std::max(
        1, static_cast<int>(std::ceil(
            (rules.via_diameter / 2 + rules.via_clearance) / grid.resolution())));

    // Default: all layers are routable
    for (int i = 0; i < grid.layers(); ++i) {
        routable_layers_.push_back(i);
    }
}

void Pathfinder::set_routable_layers(const std::vector<int>& layers) {
    routable_layers_ = layers;
}

bool Pathfinder::is_trace_blocked(int x, int y, int layer, int net,
                                  bool allow_sharing, int radius_override) const {
    int radius = (radius_override > 0) ? radius_override : trace_half_width_cells_;
    for (int dy = -radius; dy <= radius; ++dy) {
        for (int dx = -radius; dx <= radius; ++dx) {
            int cx = x + dx, cy = y + dy;
            if (!grid_.is_valid(cx, cy, layer)) {
                return true;  // Out of bounds
            }

            const auto& cell = grid_.at(cx, cy, layer);
            if (cell.blocked) {
                if (allow_sharing && !cell.is_obstacle) {
                    // Negotiated mode: allow sharing non-obstacle cells
                    if (cell.net == 0 && cell.usage_count == 0) {
                        return true;  // Static no-net obstacle
                    }
                    if (cell.net != net && cell.usage_count == 0) {
                        return true;  // Static obstacle from another net
                    }
                    // Allow with cost penalty for routed cells
                } else {
                    // Standard mode: block if obstacle or different net
                    if (cell.is_obstacle || cell.net != net) {
                        return true;
                    }
                }
            }
        }
    }
    return false;
}

bool Pathfinder::is_diagonal_blocked(int x, int y, int dx, int dy, int layer,
                                     int net, bool allow_sharing) const {
    // Only check for diagonal moves
    if (dx == 0 || dy == 0) return false;

    // Check the two adjacent orthogonal cells (prevent corner cutting)
    std::vector<std::pair<int, int>> adjacent = {
        {x, y + dy},  // Vertical neighbor
        {x + dx, y},  // Horizontal neighbor
    };

    for (const auto& [cx, cy] : adjacent) {
        if (!grid_.is_valid(cx, cy, layer)) {
            return true;  // Out of bounds
        }

        const auto& cell = grid_.at(cx, cy, layer);
        if (cell.blocked) {
            if (allow_sharing && !cell.is_obstacle) {
                if (cell.net == 0 && cell.usage_count == 0) {
                    return true;
                }
                if (cell.net != net && cell.usage_count == 0) {
                    return true;
                }
            } else {
                if (cell.is_obstacle || cell.net != net) {
                    return true;
                }
            }
        }
    }
    return false;
}

bool Pathfinder::is_via_blocked(int x, int y, int net, bool allow_sharing,
                                int radius_override) const {
    int radius = (radius_override > 0) ? radius_override : via_half_cells_;
    for (int layer = 0; layer < grid_.layers(); ++layer) {
        for (int dy = -radius; dy <= radius; ++dy) {
            for (int dx = -radius; dx <= radius; ++dx) {
                int cx = x + dx, cy = y + dy;
                if (!grid_.is_valid(cx, cy, layer)) {
                    return true;
                }

                const auto& cell = grid_.at(cx, cy, layer);
                if (cell.blocked) {
                    if (allow_sharing && !cell.is_obstacle) {
                        if (cell.net == 0 && cell.usage_count == 0) {
                            return true;
                        }
                        if (cell.net != net && cell.usage_count == 0) {
                            return true;
                        }
                    } else {
                        if (cell.is_obstacle || cell.net != net) {
                            return true;
                        }
                    }
                }
            }
        }
    }
    return false;
}

float Pathfinder::heuristic(int x, int y, int layer,
                            int goal_x, int goal_y, int goal_layer) const {
    float dx = static_cast<float>(std::abs(x - goal_x));
    float dy = static_cast<float>(std::abs(y - goal_y));

    // Octile distance for diagonal routing
    float h;
    if (diagonal_routing_) {
        h = std::max(dx, dy) + (1.414f - 1.0f) * std::min(dx, dy);
    } else {
        h = dx + dy;  // Manhattan distance
    }

    // Add layer change cost estimate
    if (layer != goal_layer) {
        h += rules_.cost_via;
    }

    return h * rules_.cost_straight;
}

float Pathfinder::get_congestion_cost(int x, int y, int layer) const {
    float congestion = grid_.get_congestion(x, y, layer);
    if (congestion > rules_.congestion_threshold) {
        float excess = congestion - rules_.congestion_threshold;
        return rules_.cost_congestion * (1.0f + excess * 2.0f);
    }
    return 0.0f;
}

RouteResult Pathfinder::route(
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
    const PadBounds& start_pad_bounds,
    const PadBounds& end_pad_bounds
) {
    RouteResult result;
    result.net = net;
    result.success = false;

    // Convert to grid coordinates
    auto [start_gx, start_gy] = grid_.world_to_grid(start_x, start_y);
    auto [end_gx, end_gy] = grid_.world_to_grid(end_x, end_y);

    // Determine valid start/end layers
    std::vector<int> valid_start_layers = start_layers.empty()
        ? std::vector<int>{start_layer} : start_layers;
    std::vector<int> valid_end_layers = end_layers.empty()
        ? std::vector<int>{end_layer} : end_layers;

    // Issue #2427: Use pad bounds if provided, otherwise fall back to single-cell
    // bounds matching the grid-snapped center (backward-compatible).
    PadBounds sp = start_pad_bounds;
    PadBounds ep = end_pad_bounds;
    bool has_start_bounds = (sp.metal_gx1 != sp.metal_gx2 || sp.metal_gy1 != sp.metal_gy2
                             || (sp.metal_gx1 == start_gx && sp.metal_gy1 == start_gy));
    bool has_end_bounds = (ep.metal_gx1 != ep.metal_gx2 || ep.metal_gy1 != ep.metal_gy2
                           || (ep.metal_gx1 == end_gx && ep.metal_gy1 == end_gy));
    // If no bounds were passed (all zeros but not matching grid coords), create
    // single-cell bounds at the grid-snapped position for uniform code paths.
    if (!has_start_bounds && sp.metal_gx1 == 0 && sp.metal_gy1 == 0
        && sp.metal_gx2 == 0 && sp.metal_gy2 == 0) {
        sp.metal_gx1 = sp.metal_gx2 = start_gx;
        sp.metal_gy1 = sp.metal_gy2 = start_gy;
        sp.approach_gx1 = start_gx - 2;
        sp.approach_gy1 = start_gy - 2;
        sp.approach_gx2 = start_gx + 2;
        sp.approach_gy2 = start_gy + 2;
    }
    if (!has_end_bounds && ep.metal_gx1 == 0 && ep.metal_gy1 == 0
        && ep.metal_gx2 == 0 && ep.metal_gy2 == 0) {
        ep.metal_gx1 = ep.metal_gx2 = end_gx;
        ep.metal_gy1 = ep.metal_gy2 = end_gy;
        ep.approach_gx1 = end_gx - 2;
        ep.approach_gy1 = end_gy - 2;
        ep.approach_gx2 = end_gx + 2;
        ep.approach_gy2 = end_gy + 2;
    }

    // A* data structures
    using PQ = std::priority_queue<AStarNode, std::vector<AStarNode>, std::greater<AStarNode>>;
    PQ open_set;
    std::unordered_set<std::tuple<int, int, int>, GridPosHash> closed_set;
    std::unordered_map<std::tuple<int, int, int>, float, GridPosHash> g_scores;
    std::vector<AStarNode> closed_list;  // For path reconstruction

    // Issue #2427 Phase 1: Seed start nodes from ALL cells within start pad's
    // metal area, not just the grid-snapped center. This handles off-grid pads
    // where the center cell may be blocked by another net's clearance zone.
    for (int sgx = sp.metal_gx1; sgx <= sp.metal_gx2; ++sgx) {
        for (int sgy = sp.metal_gy1; sgy <= sp.metal_gy2; ++sgy) {
            if (!grid_.is_valid(sgx, sgy, 0)) continue;
            for (int sl : valid_start_layers) {
                float h = heuristic(sgx, sgy, sl, end_gx, end_gy, valid_end_layers[0]);
                AStarNode start_node{h, 0.0f, sgx, sgy, sl, -1, false, 0, 0};
                auto key = std::make_tuple(sgx, sgy, sl);
                auto it = g_scores.find(key);
                if (it == g_scores.end() || 0.0f < it->second) {
                    g_scores[key] = 0.0f;
                    open_set.push(start_node);
                }
            }
        }
    }

    int max_iterations = grid_.cols() * grid_.rows() * 4;
    last_iterations_ = 0;
    last_nodes_explored_ = 0;

    while (!open_set.empty() && last_iterations_ < max_iterations) {
        last_iterations_++;

        AStarNode current = open_set.top();
        open_set.pop();

        auto current_key = std::make_tuple(current.x, current.y, current.layer);
        if (closed_set.count(current_key)) {
            continue;
        }
        closed_set.insert(current_key);

        // Store node for path reconstruction
        int current_idx = static_cast<int>(closed_list.size());
        closed_list.push_back(current);
        last_nodes_explored_++;

        // Issue #2427 Phase 1: Goal check - accept any cell within end pad's
        // metal area bounds, not just the exact grid-snapped center cell.
        // This handles off-grid pads where the center doesn't align with the
        // routing grid (mirrors Python pathfinder behavior from Issue #956).
        bool in_end_metal = (
            current.x >= ep.metal_gx1 && current.x <= ep.metal_gx2 &&
            current.y >= ep.metal_gy1 && current.y <= ep.metal_gy2
        );
        if (in_end_metal) {
            bool layer_ok = std::find(valid_end_layers.begin(), valid_end_layers.end(),
                                      current.layer) != valid_end_layers.end();
            if (layer_ok) {
                result = reconstruct_path(closed_list, current_idx,
                                          start_x, start_y, end_x, end_y, net);
                result.success = true;
                return result;
            }
        }

        // Issue #2427 Phase 2: Pre-compute whether current node is within a
        // pad's metal area (for pad exit relaxation, mirroring Issue #990).
        bool is_exiting_start_pad = (
            current.x >= sp.metal_gx1 && current.x <= sp.metal_gx2 &&
            current.y >= sp.metal_gy1 && current.y <= sp.metal_gy2 &&
            std::find(valid_start_layers.begin(), valid_start_layers.end(),
                      current.layer) != valid_start_layers.end()
        );
        bool is_exiting_end_pad = (
            current.x >= ep.metal_gx1 && current.x <= ep.metal_gx2 &&
            current.y >= ep.metal_gy1 && current.y <= ep.metal_gy2 &&
            std::find(valid_end_layers.begin(), valid_end_layers.end(),
                      current.layer) != valid_end_layers.end()
        );

        // Explore 2D neighbors
        for (const auto& [dx, dy, dlayer, cost_mult] : neighbors_2d_) {
            int nx = current.x + dx;
            int ny = current.y + dy;
            int nlayer = current.layer;

            if (!grid_.is_valid(nx, ny, nlayer)) {
                continue;
            }

            // Check diagonal corner blocking
            if (dx != 0 && dy != 0) {
                if (is_diagonal_blocked(current.x, current.y, dx, dy, nlayer, net,
                                        negotiated_mode)) {
                    continue;
                }
            }

            // Check cell blocking
            const auto& cell = grid_.at(nx, ny, nlayer);

            // Issue #2427 Phase 2: Geometry-derived approach zone and pad metal
            // area checks (mirrors Python pathfinder Issues #1618, #990, #1764).
            bool layer_in_start = std::find(valid_start_layers.begin(),
                valid_start_layers.end(), nlayer) != valid_start_layers.end();
            bool layer_in_end = std::find(valid_end_layers.begin(),
                valid_end_layers.end(), nlayer) != valid_end_layers.end();

            bool is_in_start_metal = (
                nx >= sp.metal_gx1 && nx <= sp.metal_gx2 &&
                ny >= sp.metal_gy1 && ny <= sp.metal_gy2 && layer_in_start
            );
            bool is_in_end_metal = (
                nx >= ep.metal_gx1 && nx <= ep.metal_gx2 &&
                ny >= ep.metal_gy1 && ny <= ep.metal_gy2 && layer_in_end
            );

            bool is_start_adjacent = (
                nx >= sp.approach_gx1 && nx <= sp.approach_gx2 &&
                ny >= sp.approach_gy1 && ny <= sp.approach_gy2 && layer_in_start
            );
            bool is_end_adjacent = (
                nx >= ep.approach_gx1 && nx <= ep.approach_gx2 &&
                ny >= ep.approach_gy1 && ny <= ep.approach_gy2 && layer_in_end
            );

            if (cell.blocked) {
                // Issue #1764: If neighbor is within pad metal area, always allow
                if (is_in_start_metal || is_in_end_metal) {
                    // Allow entry into own pad's metal area
                } else if (cell.net == net) {
                    // Same-net blocked cell (e.g., our THT pad area) - allow
                } else if (cell.net == 0) {
                    // No-net blocked cell - use full check
                    if (is_trace_blocked(nx, ny, nlayer, net, negotiated_mode,
                                         trace_radius_cells)) {
                        continue;
                    }
                } else {
                    // Different net's blocked cell
                    // Issue #996/#990: When exiting a pad, allow entering clearance
                    // zones (not actual pad copper) so A* can escape dense layouts.
                    bool is_clearance_only = !cell.pad_blocked;
                    bool is_pad_exit = is_exiting_start_pad || is_exiting_end_pad;
                    if (is_clearance_only && is_pad_exit) {
                        // Clearance zone cell while exiting pad - allow
                    } else {
                        continue;
                    }
                }
            } else {
                // Issue #1702 Gap 1: Even when center cell is unblocked, check
                // trace clearance. The trace has physical width and must not
                // violate clearance to other nets within its radius. Skip this
                // check near pads to allow pad approach/exit (Issue #990/#1618).
                bool is_pad_exit_or_approach = (
                    is_start_adjacent || is_end_adjacent ||
                    is_exiting_start_pad || is_exiting_end_pad
                );
                if (!is_pad_exit_or_approach) {
                    if (is_trace_blocked(nx, ny, nlayer, net, negotiated_mode,
                                         trace_radius_cells)) {
                        continue;
                    }
                }
            }

            auto neighbor_key = std::make_tuple(nx, ny, nlayer);
            if (closed_set.count(neighbor_key)) {
                continue;
            }

            // Calculate cost
            float turn_cost = 0.0f;
            if (current.dx != 0 || current.dy != 0) {
                if (current.dx != dx || current.dy != dy) {
                    turn_cost = rules_.cost_turn;
                }
            }

            float congestion_cost = get_congestion_cost(nx, ny, nlayer);
            float negotiated_cost = 0.0f;
            if (negotiated_mode) {
                negotiated_cost = grid_.get_negotiated_cost(nx, ny, nlayer, present_cost_factor);
            }

            float avoidance = grid_.at(nx, ny, nlayer).avoidance_cost;

            float new_g = current.g_score +
                          cost_mult * rules_.cost_straight +
                          turn_cost + congestion_cost + negotiated_cost +
                          avoidance;

            auto it = g_scores.find(neighbor_key);
            if (it == g_scores.end() || new_g < it->second) {
                g_scores[neighbor_key] = new_g;
                float h = heuristic(nx, ny, nlayer, end_gx, end_gy, valid_end_layers[0]);
                float f = new_g + weight * h;

                AStarNode neighbor{f, new_g, nx, ny, nlayer, current_idx, false, dx, dy};
                open_set.push(neighbor);
            }
        }

        // Try layer change (via)
        for (int new_layer : routable_layers_) {
            if (new_layer == current.layer) continue;

            if (is_via_blocked(current.x, current.y, net, negotiated_mode,
                               via_radius_cells)) {
                continue;
            }

            auto neighbor_key = std::make_tuple(current.x, current.y, new_layer);
            if (closed_set.count(neighbor_key)) {
                continue;
            }

            float congestion_cost = get_congestion_cost(current.x, current.y, new_layer);
            float negotiated_cost = 0.0f;
            if (negotiated_mode) {
                negotiated_cost = grid_.get_negotiated_cost(
                    current.x, current.y, new_layer, present_cost_factor);
            }

            float avoidance = grid_.at(current.x, current.y, new_layer).avoidance_cost;

            float new_g = current.g_score + rules_.cost_via + congestion_cost +
                          negotiated_cost + avoidance;

            auto it = g_scores.find(neighbor_key);
            if (it == g_scores.end() || new_g < it->second) {
                g_scores[neighbor_key] = new_g;
                float h = heuristic(current.x, current.y, new_layer,
                                    end_gx, end_gy, valid_end_layers[0]);
                float f = new_g + weight * h;

                AStarNode neighbor{f, new_g, current.x, current.y, new_layer,
                                   current_idx, true, current.dx, current.dy};
                open_set.push(neighbor);
            }
        }
    }

    // No path found
    return result;
}

RouteResult Pathfinder::reconstruct_path(
    const std::vector<AStarNode>& closed_list,
    int end_idx,
    float start_x, float start_y,
    float end_x, float end_y,
    int net
) {
    RouteResult result;
    result.net = net;
    result.success = true;

    // Build path from end to start
    std::vector<std::tuple<float, float, int, bool>> path;
    int idx = end_idx;
    while (idx >= 0 && idx < static_cast<int>(closed_list.size())) {
        const auto& node = closed_list[idx];
        auto [wx, wy] = grid_.grid_to_world(node.x, node.y);
        path.emplace_back(wx, wy, node.layer, node.via_from_parent);
        idx = node.parent_idx;
    }
    std::reverse(path.begin(), path.end());

    if (path.size() < 2) {
        return result;
    }

    // Convert path to segments and vias
    float current_x = start_x;
    float current_y = start_y;
    int current_layer = std::get<2>(path[0]);

    for (size_t i = 0; i < path.size(); ++i) {
        auto [wx, wy, layer, is_via] = path[i];

        if (is_via) {
            // Add via
            Via via;
            via.x = current_x;
            via.y = current_y;
            via.drill = rules_.via_drill;
            via.diameter = rules_.via_diameter;
            via.layer_from = current_layer;
            via.layer_to = layer;
            via.net = net;
            result.vias.push_back(via);
            current_layer = layer;
        } else {
            // Add segment if position changed
            if (std::abs(wx - current_x) > 0.01f || std::abs(wy - current_y) > 0.01f) {
                Segment seg;
                seg.x1 = current_x;
                seg.y1 = current_y;
                seg.x2 = wx;
                seg.y2 = wy;
                seg.width = rules_.trace_width;
                seg.layer = layer;
                seg.net = net;
                result.segments.push_back(seg);
                current_x = wx;
                current_y = wy;
                current_layer = layer;
            }
        }
    }

    // Final segment to end
    if (std::abs(end_x - current_x) > 0.01f || std::abs(end_y - current_y) > 0.01f) {
        Segment seg;
        seg.x1 = current_x;
        seg.y1 = current_y;
        seg.x2 = end_x;
        seg.y2 = end_y;
        seg.width = rules_.trace_width;
        seg.layer = current_layer;
        seg.net = net;
        result.segments.push_back(seg);
    }

    return result;
}

}  // namespace router
