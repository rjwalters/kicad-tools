/*
 * Placement C++ Core - Batch cost evaluator
 *
 * Accepts flat arrays of component positions and sizes for efficient
 * batch evaluation without per-object marshaling overhead.
 */

#pragma once

#include "aabb.hpp"
#include <cstddef>
#include <vector>

namespace placement {

/// Result of a batch cost evaluation.
struct CostResult {
    double overlap;
    double boundary;
    double drc;
};

/// Batch cost evaluator for placement optimization.
///
/// Accepts flat arrays of positions (x, y) and sizes (w, h) and evaluates
/// overlap, boundary violation, and DRC violations in a single pass.
/// The flat-array interface avoids per-object marshaling overhead when
/// called from Python via nanobind.
class BatchCostEvaluator {
public:
    /// Construct evaluator with board outline and design rule clearance.
    ///
    /// @param board_min_x  Left edge of board (mm).
    /// @param board_min_y  Top edge of board (mm).
    /// @param board_max_x  Right edge of board (mm).
    /// @param board_max_y  Bottom edge of board (mm).
    /// @param min_clearance  Minimum copper-to-copper clearance (mm).
    BatchCostEvaluator(
        double board_min_x,
        double board_min_y,
        double board_max_x,
        double board_max_y,
        double min_clearance)
        : board_{board_min_x, board_min_y, board_max_x, board_max_y},
          min_clearance_(min_clearance) {}

    /// Evaluate all cost components for a set of components.
    ///
    /// @param xs      X positions of components (mm).
    /// @param ys      Y positions of components (mm).
    /// @param widths  Widths of components (mm).
    /// @param heights Heights of components (mm).
    /// @return CostResult with overlap, boundary, and drc fields.
    CostResult evaluate(
        const std::vector<double>& xs,
        const std::vector<double>& ys,
        const std::vector<double>& widths,
        const std::vector<double>& heights) const {

        const size_t n = xs.size();

        // Build AABBs from positions and sizes
        std::vector<AABB> boxes;
        boxes.reserve(n);
        for (size_t i = 0; i < n; ++i) {
            double half_w = widths[i] / 2.0;
            double half_h = heights[i] / 2.0;
            boxes.push_back({
                xs[i] - half_w,
                ys[i] - half_h,
                xs[i] + half_w,
                ys[i] + half_h,
            });
        }

        CostResult result;
        result.overlap = compute_overlap(boxes);
        result.boundary = compute_boundary_violation(boxes, board_);
        result.drc = compute_drc_violations(boxes, min_clearance_);
        return result;
    }

    /// Compute only pairwise overlap area.
    double evaluate_overlap(
        const std::vector<double>& xs,
        const std::vector<double>& ys,
        const std::vector<double>& widths,
        const std::vector<double>& heights) const {

        auto boxes = build_boxes(xs, ys, widths, heights);
        return compute_overlap(boxes);
    }

    /// Compute only boundary violations.
    double evaluate_boundary(
        const std::vector<double>& xs,
        const std::vector<double>& ys,
        const std::vector<double>& widths,
        const std::vector<double>& heights) const {

        auto boxes = build_boxes(xs, ys, widths, heights);
        return compute_boundary_violation(boxes, board_);
    }

    /// Compute only DRC violations.
    double evaluate_drc(
        const std::vector<double>& xs,
        const std::vector<double>& ys,
        const std::vector<double>& widths,
        const std::vector<double>& heights) const {

        auto boxes = build_boxes(xs, ys, widths, heights);
        return compute_drc_violations(boxes, min_clearance_);
    }

private:
    AABB board_;
    double min_clearance_;

    std::vector<AABB> build_boxes(
        const std::vector<double>& xs,
        const std::vector<double>& ys,
        const std::vector<double>& widths,
        const std::vector<double>& heights) const {

        const size_t n = xs.size();
        std::vector<AABB> boxes;
        boxes.reserve(n);
        for (size_t i = 0; i < n; ++i) {
            double half_w = widths[i] / 2.0;
            double half_h = heights[i] / 2.0;
            boxes.push_back({
                xs[i] - half_w,
                ys[i] - half_h,
                xs[i] + half_w,
                ys[i] + half_h,
            });
        }
        return boxes;
    }
};

}  // namespace placement
