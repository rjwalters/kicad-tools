/*
 * Placement C++ Core - AABB overlap/clearance operations
 *
 * Provides high-performance pairwise AABB computations for placement
 * cost evaluation. These functions mirror the pure Python implementations
 * in cost.py and must produce numerically identical results.
 */

#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <vector>

namespace placement {

/// Axis-Aligned Bounding Box represented as (min_x, min_y, max_x, max_y).
struct AABB {
    double min_x;
    double min_y;
    double max_x;
    double max_y;
};

/// Compute total pairwise overlap area between AABBs.
///
/// Mirrors cost.py:compute_overlap(). For each pair (i, j) where i < j,
/// computes the intersection area of the two AABBs and sums them.
///
/// @param boxes  Vector of AABBs (min_x, min_y, max_x, max_y).
/// @return Sum of pairwise overlap areas (mm^2). Zero means no overlaps.
inline double compute_overlap(const std::vector<AABB>& boxes) {
    double total = 0.0;
    const size_t n = boxes.size();
    for (size_t i = 0; i < n; ++i) {
        for (size_t j = i + 1; j < n; ++j) {
            double x_overlap = std::max(
                0.0,
                std::min(boxes[i].max_x, boxes[j].max_x) -
                    std::max(boxes[i].min_x, boxes[j].min_x));
            double y_overlap = std::max(
                0.0,
                std::min(boxes[i].max_y, boxes[j].max_y) -
                    std::max(boxes[i].min_y, boxes[j].min_y));
            total += x_overlap * y_overlap;
        }
    }
    return total;
}

/// Compute total boundary violation depth.
///
/// Mirrors cost.py:compute_boundary_violation(). For each box that extends
/// beyond the board outline, sums the depth of violation on each edge.
///
/// @param boxes   Vector of component AABBs.
/// @param board   Board outline AABB.
/// @return Sum of boundary violation depths across all components (mm).
inline double compute_boundary_violation(
    const std::vector<AABB>& boxes,
    const AABB& board) {
    double total = 0.0;
    for (const auto& box : boxes) {
        total += std::max(0.0, board.min_x - box.min_x);
        total += std::max(0.0, box.max_x - board.max_x);
        total += std::max(0.0, board.min_y - box.min_y);
        total += std::max(0.0, box.max_y - board.max_y);
    }
    return total;
}

/// Compute count of DRC clearance violations.
///
/// Mirrors cost.py:compute_drc_violations(). Checks pairwise clearance
/// between component bounding boxes against the minimum clearance rule.
///
/// @param boxes    Vector of component AABBs.
/// @param min_gap  Minimum clearance distance (mm).
/// @return Number of pairwise clearance violations.
inline double compute_drc_violations(
    const std::vector<AABB>& boxes,
    double min_gap) {
    double violations = 0.0;
    const size_t n = boxes.size();
    for (size_t i = 0; i < n; ++i) {
        for (size_t j = i + 1; j < n; ++j) {
            // Edge-to-edge gap (negative means overlap)
            double gap_x = std::max(boxes[i].min_x, boxes[j].min_x) -
                           std::min(boxes[i].max_x, boxes[j].max_x);
            double gap_y = std::max(boxes[i].min_y, boxes[j].min_y) -
                           std::min(boxes[i].max_y, boxes[j].max_y);

            double gap;
            if (gap_x <= 0 && gap_y <= 0) {
                // Overlapping on both axes
                gap = 0.0;
            } else if (gap_x > 0 && gap_y > 0) {
                // Corner-to-corner distance
                gap = std::sqrt(gap_x * gap_x + gap_y * gap_y);
            } else {
                // Edge-to-edge on one axis
                gap = std::max(gap_x, gap_y);
            }

            if (gap < min_gap) {
                violations += 1.0;
            }
        }
    }
    return violations;
}

}  // namespace placement
