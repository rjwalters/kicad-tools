/*
 * Router C++ Core - Geometry Functions (Issue #2439)
 *
 * Ported from src/kicad_tools/core/geometry.py to eliminate Python
 * callbacks during post-route geometric validation.
 */

#pragma once

#include <cstdint>
#include <cstddef>

namespace router {

// Calculate the minimum distance from a point to a line segment.
// Projects the point onto the infinite line through (x1,y1)-(x2,y2),
// clamps the projection parameter t to [0, 1], and returns the
// Euclidean distance to the closest point on the segment.
float point_to_segment_distance(
    float px, float py,
    float x1, float y1,
    float x2, float y2);

// Test whether two line segments properly intersect.
// Uses the standard cross-product orientation test. Shared endpoints
// and collinear overlap are NOT counted as intersections.
bool segments_intersect(
    float ax1, float ay1, float ax2, float ay2,
    float bx1, float by1, float bx2, float by2);

// Calculate the minimum distance between two line segments.
// First checks for proper intersection (returns 0 immediately),
// then falls back to checking all four endpoint-to-segment distances.
float segment_to_segment_distance(
    float x1, float y1, float x2, float y2,
    float x3, float y3, float x4, float y4);

// Compute FNV-1a hash of a string.
// Deterministic across runs (unlike Python's hash() which is randomized).
uint32_t fnv1a_hash(const char* str, size_t len);

}  // namespace router
