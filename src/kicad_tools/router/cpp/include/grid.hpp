/*
 * Router C++ Core - 3D Grid
 * Part of kicad-tools router performance optimization (Phase 4)
 *
 * High-performance 3D grid with contiguous memory layout for cache efficiency.
 * Uses flat array indexing for O(1) cell access.
 */

#pragma once

#include "types.hpp"
#include <vector>
#include <cmath>
#include <algorithm>

namespace router {

class Grid3D {
public:
    Grid3D(int cols, int rows, int layers, float resolution,
           float origin_x, float origin_y);

    // Cell access - inline for performance
    inline GridCell& at(int x, int y, int layer) {
        return cells_[index(x, y, layer)];
    }

    inline const GridCell& at(int x, int y, int layer) const {
        return cells_[index(x, y, layer)];
    }

    inline bool is_valid(int x, int y, int layer) const {
        return x >= 0 && x < cols_ && y >= 0 && y < rows_ &&
               layer >= 0 && layer < layers_;
    }

    inline bool is_valid_and_free(int x, int y, int layer, int net) const {
        if (!is_valid(x, y, layer)) return false;
        const auto& cell = at(x, y, layer);
        if (!cell.blocked) return true;
        // Blocked cell is passable if it's the same net
        return cell.net == net && !cell.is_obstacle;
    }

    // Coordinate conversion
    inline std::pair<int, int> world_to_grid(float x, float y) const {
        int gx = static_cast<int>(std::round((x - origin_x_) / resolution_));
        int gy = static_cast<int>(std::round((y - origin_y_) / resolution_));
        return {std::clamp(gx, 0, cols_ - 1), std::clamp(gy, 0, rows_ - 1)};
    }

    inline std::pair<float, float> grid_to_world(int gx, int gy) const {
        return {origin_x_ + gx * resolution_, origin_y_ + gy * resolution_};
    }

    // Bulk operations for obstacle marking
    void mark_blocked(int x, int y, int layer, int net, bool is_obstacle = false);
    void mark_rect_blocked(int x1, int y1, int x2, int y2, int layer, int net,
                           bool is_obstacle = false);

    // Route marking with clearance buffer using Bresenham
    void mark_segment(int x1, int y1, int x2, int y2, int layer, int net,
                      int clearance_cells);
    void mark_via(int x, int y, int net, int radius_cells);

    // Unmark route (rip-up)
    void unmark_segment(int x1, int y1, int x2, int y2, int layer, int net,
                        int clearance_cells);
    void unmark_via(int x, int y, int net, int radius_cells);

    // Congestion tracking
    float get_congestion(int x, int y, int layer) const;
    void update_congestion(int x, int y, int layer, int delta = 1);

    // Negotiated routing support
    void reset_usage();
    void increment_usage(int x, int y, int layer);
    float get_negotiated_cost(int x, int y, int layer, float present_factor) const;
    void update_history_costs(float increment);
    int get_total_overflow() const;

    // Accessors
    int cols() const { return cols_; }
    int rows() const { return rows_; }
    int layers() const { return layers_; }
    float resolution() const { return resolution_; }
    size_t total_cells() const { return cells_.size(); }

    // Statistics
    int count_blocked() const;
    float memory_mb() const;

private:
    inline size_t index(int x, int y, int layer) const {
        return static_cast<size_t>(layer) * rows_ * cols_ +
               static_cast<size_t>(y) * cols_ +
               static_cast<size_t>(x);
    }

    std::vector<GridCell> cells_;  // Flat array for cache efficiency
    int cols_, rows_, layers_;
    float resolution_;
    float origin_x_, origin_y_;

    // Congestion grid (coarser)
    std::vector<int> congestion_;
    int congestion_cols_, congestion_rows_;
    int congestion_size_ = 8;  // Cells per congestion region
};

}  // namespace router
