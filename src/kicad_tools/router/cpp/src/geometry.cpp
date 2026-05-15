/*
 * Router C++ Core - Geometry Functions (Issue #2439)
 *
 * Faithful port of src/kicad_tools/core/geometry.py.
 * These functions are the canonical geometry primitives used by
 * the post-route geometric validation to compute clearances.
 */

#include "geometry.hpp"
#include <cmath>
#include <algorithm>
#include <cstdint>

namespace router {

float point_to_segment_distance(
    float px, float py,
    float x1, float y1,
    float x2, float y2)
{
    float dx = x2 - x1;
    float dy = y2 - y1;
    float seg_len_sq = dx * dx + dy * dy;

    if (seg_len_sq == 0.0f) {
        // Degenerate segment (a single point)
        float ex = px - x1;
        float ey = py - y1;
        return std::sqrt(ex * ex + ey * ey);
    }

    // Projection parameter, clamped to segment
    float t = std::clamp(((px - x1) * dx + (py - y1) * dy) / seg_len_sq, 0.0f, 1.0f);

    // Closest point on segment
    float cx = x1 + t * dx;
    float cy = y1 + t * dy;

    float ex = px - cx;
    float ey = py - cy;
    return std::sqrt(ex * ex + ey * ey);
}

bool segments_intersect(
    float ax1, float ay1, float ax2, float ay2,
    float bx1, float by1, float bx2, float by2)
{
    // Cross product helper: sign of (OP x OQ)
    auto cross = [](float ox, float oy, float px, float py, float qx, float qy) -> float {
        return (px - ox) * (qy - oy) - (py - oy) * (qx - ox);
    };

    float d1 = cross(bx1, by1, bx2, by2, ax1, ay1);
    float d2 = cross(bx1, by1, bx2, by2, ax2, ay2);
    float d3 = cross(ax1, ay1, ax2, ay2, bx1, by1);
    float d4 = cross(ax1, ay1, ax2, ay2, bx2, by2);

    // Proper intersection: each segment straddles the line of the other
    if (((d1 > 0 && d2 < 0) || (d1 < 0 && d2 > 0)) &&
        ((d3 > 0 && d4 < 0) || (d3 < 0 && d4 > 0))) {
        return true;
    }

    return false;
}

float segment_to_segment_distance(
    float x1, float y1, float x2, float y2,
    float x3, float y3, float x4, float y4)
{
    // If segments properly intersect, distance is zero
    if (segments_intersect(x1, y1, x2, y2, x3, y3, x4, y4)) {
        return 0.0f;
    }

    // Otherwise, the minimum distance is the smallest of the four
    // endpoint-to-segment distances
    float d1 = point_to_segment_distance(x1, y1, x3, y3, x4, y4);
    float d2 = point_to_segment_distance(x2, y2, x3, y3, x4, y4);
    float d3 = point_to_segment_distance(x3, y3, x1, y1, x2, y2);
    float d4 = point_to_segment_distance(x4, y4, x1, y1, x2, y2);

    return std::min({d1, d2, d3, d4});
}

float rect_segment_centerline_distance(
    float cx, float cy, float w, float h,
    float x1, float y1, float x2, float y2)
{
    // Issue #2908: signed centerline-to-rect distance.  Mirrors the
    // Python helper in router/grid.py and the validator-side helper
    // from PR #2787 (validate/rules/clearance.py).  Used by
    // ``Grid3D::validate_route`` to model rectangular SMD pad geometry
    // accurately for the segment-to-pad clearance check.
    const float half_w = w / 2.0f;
    const float half_h = h / 2.0f;
    const float left = cx - half_w;
    const float right = cx + half_w;
    const float bot = cy - half_h;
    const float top = cy + half_h;

    auto is_inside = [&](float px, float py) -> bool {
        return left <= px && px <= right && bot <= py && py <= top;
    };

    const bool p1_in = is_inside(x1, y1);
    const bool p2_in = is_inside(x2, y2);

    if (p1_in && p2_in) {
        // Whole centerline inside rect -- return deepest signed-depth.
        auto signed_depth = [&](float px, float py) -> float {
            float gap_x = std::max(px - right, left - px);
            float gap_y = std::max(py - top, bot - py);
            // Both gap_x and gap_y <= 0 when (px, py) is inside the rect.
            return std::max(gap_x, gap_y);
        };
        float deepest = std::min(signed_depth(x1, y1), signed_depth(x2, y2));
        constexpr int steps = 32;
        const float dx = x2 - x1;
        const float dy = y2 - y1;
        for (int i = 1; i < steps; ++i) {
            const float t = static_cast<float>(i) / static_cast<float>(steps);
            float d = signed_depth(x1 + t * dx, y1 + t * dy);
            if (d < deepest) {
                deepest = d;
            }
        }
        return deepest;
    }

    if (p1_in != p2_in) {
        // Endpoint straddles the boundary -- centerline crosses an edge.
        return 0.0f;
    }

    // Both endpoints outside.  Check whether the segment crosses any of
    // the four rectangle edges; if so the centerline touches the
    // boundary (distance 0).
    const float ex[4][4] = {
        {left, bot, right, bot},
        {right, bot, right, top},
        {right, top, left, top},
        {left, top, left, bot},
    };
    for (int e = 0; e < 4; ++e) {
        if (segments_intersect(x1, y1, x2, y2,
                               ex[e][0], ex[e][1], ex[e][2], ex[e][3])) {
            return 0.0f;
        }
    }

    // No crossing -- closest approach is min over:
    //   * each segment endpoint to nearest rect point
    //   * each rect corner to nearest point on segment.
    auto point_to_rect = [&](float px, float py) -> float {
        float closest_x = std::clamp(px, left, right);
        float closest_y = std::clamp(py, bot, top);
        float ex_ = px - closest_x;
        float ey_ = py - closest_y;
        return std::sqrt(ex_ * ex_ + ey_ * ey_);
    };

    float best = point_to_rect(x1, y1);
    best = std::min(best, point_to_rect(x2, y2));

    const float corners[4][2] = {
        {left, bot},
        {right, bot},
        {right, top},
        {left, top},
    };
    for (int c = 0; c < 4; ++c) {
        best = std::min(
            best,
            point_to_segment_distance(corners[c][0], corners[c][1], x1, y1, x2, y2)
        );
    }
    return best;
}

uint32_t fnv1a_hash(const char* str, size_t len) {
    uint32_t hash = 2166136261u;  // FNV offset basis
    for (size_t i = 0; i < len; ++i) {
        hash ^= static_cast<uint32_t>(static_cast<unsigned char>(str[i]));
        hash *= 16777619u;  // FNV prime
    }
    return hash;
}

}  // namespace router
