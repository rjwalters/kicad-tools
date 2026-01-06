/*
 * Router C++ Core - 3D Grid Implementation
 * Part of kicad-tools router performance optimization (Phase 4)
 */

#include "grid.hpp"
#include <cmath>
#include <algorithm>

namespace router {

Grid3D::Grid3D(int cols, int rows, int layers, float resolution,
               float origin_x, float origin_y)
    : cols_(cols), rows_(rows), layers_(layers),
      resolution_(resolution), origin_x_(origin_x), origin_y_(origin_y) {

    // Allocate contiguous cell storage
    size_t total = static_cast<size_t>(cols) * rows * layers;
    cells_.resize(total);

    // Initialize congestion grid
    congestion_cols_ = std::max(1, cols / congestion_size_);
    congestion_rows_ = std::max(1, rows / congestion_size_);
    congestion_.resize(static_cast<size_t>(layers) * congestion_rows_ * congestion_cols_, 0);
}

void Grid3D::mark_blocked(int x, int y, int layer, int net, bool is_obstacle) {
    if (!is_valid(x, y, layer)) return;
    auto& cell = at(x, y, layer);
    cell.blocked = true;
    cell.net = net;
    cell.is_obstacle = is_obstacle;
}

void Grid3D::mark_rect_blocked(int x1, int y1, int x2, int y2, int layer, int net,
                               bool is_obstacle) {
    x1 = std::clamp(x1, 0, cols_ - 1);
    y1 = std::clamp(y1, 0, rows_ - 1);
    x2 = std::clamp(x2, 0, cols_ - 1);
    y2 = std::clamp(y2, 0, rows_ - 1);

    for (int y = y1; y <= y2; ++y) {
        for (int x = x1; x <= x2; ++x) {
            mark_blocked(x, y, layer, net, is_obstacle);
        }
    }
}

void Grid3D::mark_segment(int x1, int y1, int x2, int y2, int layer, int net,
                          int clearance_cells) {
    auto mark_with_clearance = [&](int gx, int gy) {
        for (int dy = -clearance_cells; dy <= clearance_cells; ++dy) {
            for (int dx = -clearance_cells; dx <= clearance_cells; ++dx) {
                int nx = gx + dx, ny = gy + dy;
                if (is_valid(nx, ny, layer)) {
                    auto& cell = at(nx, ny, layer);
                    if (!cell.blocked) {
                        cell.net = net;
                        update_congestion(nx, ny, layer, 1);
                    }
                    cell.blocked = true;
                }
            }
        }
    };

    // Bresenham's line algorithm
    int dx = std::abs(x2 - x1);
    int dy = std::abs(y2 - y1);
    int sx = (x1 < x2) ? 1 : -1;
    int sy = (y1 < y2) ? 1 : -1;
    int err = dx - dy;

    int x = x1, y = y1;
    while (true) {
        mark_with_clearance(x, y);
        if (x == x2 && y == y2) break;

        int e2 = 2 * err;
        if (e2 > -dy) {
            err -= dy;
            x += sx;
        }
        if (e2 < dx) {
            err += dx;
            y += sy;
        }
    }
}

void Grid3D::mark_via(int x, int y, int net, int radius_cells) {
    for (int layer = 0; layer < layers_; ++layer) {
        for (int dy = -radius_cells; dy <= radius_cells; ++dy) {
            for (int dx = -radius_cells; dx <= radius_cells; ++dx) {
                int nx = x + dx, ny = y + dy;
                if (is_valid(nx, ny, layer)) {
                    auto& cell = at(nx, ny, layer);
                    if (!cell.blocked) {
                        update_congestion(nx, ny, layer, 1);
                        cell.net = net;
                    }
                    cell.blocked = true;
                }
            }
        }
    }
}

void Grid3D::unmark_segment(int x1, int y1, int x2, int y2, int layer, int net,
                            int clearance_cells) {
    auto unmark_with_clearance = [&](int gx, int gy) {
        for (int dy = -clearance_cells; dy <= clearance_cells; ++dy) {
            for (int dx = -clearance_cells; dx <= clearance_cells; ++dx) {
                int nx = gx + dx, ny = gy + dy;
                if (is_valid(nx, ny, layer)) {
                    auto& cell = at(nx, ny, layer);
                    if (cell.pad_blocked) {
                        cell.net = cell.original_net;
                    } else if (cell.net == net) {
                        cell.blocked = false;
                        cell.net = 0;
                    }
                }
            }
        }
    };

    // Bresenham's line algorithm
    int dx = std::abs(x2 - x1);
    int dy = std::abs(y2 - y1);
    int sx = (x1 < x2) ? 1 : -1;
    int sy = (y1 < y2) ? 1 : -1;
    int err = dx - dy;

    int x = x1, y = y1;
    while (true) {
        unmark_with_clearance(x, y);
        if (x == x2 && y == y2) break;

        int e2 = 2 * err;
        if (e2 > -dy) {
            err -= dy;
            x += sx;
        }
        if (e2 < dx) {
            err += dx;
            y += sy;
        }
    }
}

void Grid3D::unmark_via(int x, int y, int net, int radius_cells) {
    for (int layer = 0; layer < layers_; ++layer) {
        for (int dy = -radius_cells; dy <= radius_cells; ++dy) {
            for (int dx = -radius_cells; dx <= radius_cells; ++dx) {
                int nx = x + dx, ny = y + dy;
                if (is_valid(nx, ny, layer)) {
                    auto& cell = at(nx, ny, layer);
                    if (cell.pad_blocked) {
                        cell.net = cell.original_net;
                    } else if (cell.net == net) {
                        cell.blocked = false;
                        cell.net = 0;
                    }
                }
            }
        }
    }
}

float Grid3D::get_congestion(int x, int y, int layer) const {
    int cx = std::min(x / congestion_size_, congestion_cols_ - 1);
    int cy = std::min(y / congestion_size_, congestion_rows_ - 1);
    size_t idx = static_cast<size_t>(layer) * congestion_rows_ * congestion_cols_ +
                 static_cast<size_t>(cy) * congestion_cols_ + cx;
    int count = congestion_[idx];
    int max_cells = congestion_size_ * congestion_size_;
    return std::min(1.0f, static_cast<float>(count) / max_cells);
}

void Grid3D::update_congestion(int x, int y, int layer, int delta) {
    int cx = std::min(x / congestion_size_, congestion_cols_ - 1);
    int cy = std::min(y / congestion_size_, congestion_rows_ - 1);
    size_t idx = static_cast<size_t>(layer) * congestion_rows_ * congestion_cols_ +
                 static_cast<size_t>(cy) * congestion_cols_ + cx;
    congestion_[idx] += delta;
}

void Grid3D::reset_usage() {
    for (auto& cell : cells_) {
        cell.usage_count = 0;
    }
}

void Grid3D::increment_usage(int x, int y, int layer) {
    if (is_valid(x, y, layer)) {
        at(x, y, layer).usage_count++;
    }
}

float Grid3D::get_negotiated_cost(int x, int y, int layer, float present_factor) const {
    if (!is_valid(x, y, layer)) {
        return std::numeric_limits<float>::infinity();
    }

    const auto& cell = at(x, y, layer);
    if (cell.is_obstacle) {
        return std::numeric_limits<float>::infinity();
    }

    float present_cost = present_factor * cell.usage_count;
    return present_cost + cell.history_cost;
}

void Grid3D::update_history_costs(float increment) {
    for (auto& cell : cells_) {
        if (cell.usage_count > 1) {
            cell.history_cost += increment * (cell.usage_count - 1);
        }
    }
}

int Grid3D::get_total_overflow() const {
    int overflow = 0;
    for (const auto& cell : cells_) {
        if (cell.usage_count > 1) {
            overflow += cell.usage_count - 1;
        }
    }
    return overflow;
}

int Grid3D::count_blocked() const {
    int count = 0;
    for (const auto& cell : cells_) {
        if (cell.blocked) count++;
    }
    return count;
}

float Grid3D::memory_mb() const {
    size_t bytes = cells_.size() * sizeof(GridCell) +
                   congestion_.size() * sizeof(int);
    return static_cast<float>(bytes) / (1024 * 1024);
}

}  // namespace router
