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

uint32_t fnv1a_hash(const char* str, size_t len) {
    uint32_t hash = 2166136261u;  // FNV offset basis
    for (size_t i = 0; i < len; ++i) {
        hash ^= static_cast<uint32_t>(static_cast<unsigned char>(str[i]));
        hash *= 16777619u;  // FNV prime
    }
    return hash;
}

}  // namespace router
