/*
 * Placement C++ Core - Force-directed placement engine
 *
 * Provides high-performance edge-to-edge repulsion force computation
 * for the force-directed placement optimizer. The full N^2 component
 * loop runs entirely in C++ to avoid per-element Python overhead.
 *
 * Mirrors the pure Python implementation in optim/placement.py and
 * must produce numerically identical results.
 */

#pragma once

#include <algorithm>
#include <cmath>
#include <cstddef>
#include <vector>

namespace placement {

/// 2D vector for force calculations.
struct Vec2 {
    double x = 0.0;
    double y = 0.0;

    Vec2() = default;
    Vec2(double x_, double y_) : x(x_), y(y_) {}

    Vec2 operator+(const Vec2& o) const { return {x + o.x, y + o.y}; }
    Vec2 operator-(const Vec2& o) const { return {x - o.x, y - o.y}; }
    Vec2 operator*(double s) const { return {x * s, y * s}; }

    double dot(const Vec2& o) const { return x * o.x + y * o.y; }
    double cross(const Vec2& o) const { return x * o.y - y * o.x; }
    double magnitude() const { return std::sqrt(x * x + y * y); }

    Vec2 normalized() const {
        double mag = magnitude();
        if (mag < 1e-10) return {0.0, 0.0};
        return {x / mag, y / mag};
    }
};

/// Configuration for force computation.
struct ForceConfig {
    double charge_density = 100.0;
    double min_distance = 0.5;
    int edge_samples = 5;
    double boundary_charge = 200.0;
};

/// Result of force computation for all components.
struct ForceResult {
    std::vector<double> forces_x;    // Force x-component per component
    std::vector<double> forces_y;    // Force y-component per component
    std::vector<double> torques;     // Torque per component
};

/// Compute repulsion force on a point from a charged line segment.
///
/// Uses linear charge density model with 1/r falloff.
/// Mirrors optim/placement.py:compute_edge_to_point_force().
///
/// @param px, py          Point being repelled.
/// @param e_sx, e_sy      Edge start.
/// @param e_ex, e_ey      Edge end.
/// @param charge_density  Linear charge density.
/// @param min_distance    Minimum distance clamp.
/// @return Force vector (fx, fy) on the point.
inline Vec2 compute_edge_to_point_force(
    double px, double py,
    double e_sx, double e_sy,
    double e_ex, double e_ey,
    double charge_density,
    double min_distance) {

    double edge_x = e_ex - e_sx;
    double edge_y = e_ey - e_sy;
    double edge_len = std::sqrt(edge_x * edge_x + edge_y * edge_y);
    if (edge_len < 1e-10) return {0.0, 0.0};

    // Vector from edge start to point
    double to_x = px - e_sx;
    double to_y = py - e_sy;

    // Project point onto edge line
    double t = (to_x * edge_x + to_y * edge_y) / (edge_len * edge_len);
    t = std::max(0.0, std::min(1.0, t));  // Clamp to edge

    // Closest point on edge
    double closest_x = e_sx + edge_x * t;
    double closest_y = e_sy + edge_y * t;

    // Displacement from closest point to test point
    double disp_x = px - closest_x;
    double disp_y = py - closest_y;
    double distance = std::sqrt(disp_x * disp_x + disp_y * disp_y);

    // Clamp minimum distance
    distance = std::max(distance, min_distance);

    // Force magnitude: lambda * L / r^2 (1/r^2 falloff prevents divergence)
    double force_mag = charge_density * edge_len / (distance * distance);

    // Force direction: away from edge (normalized displacement)
    double disp_mag = std::sqrt(disp_x * disp_x + disp_y * disp_y);
    if (disp_mag < 1e-10) return {0.0, 0.0};

    return {disp_x / disp_mag * force_mag, disp_y / disp_mag * force_mag};
}

/// Compute repulsion force and torque between two charged edges.
///
/// Discretizes edge1 into sample points and computes force from edge2
/// on each sample. Returns net force and torque about edge1's center.
/// Mirrors optim/placement.py:compute_edge_to_edge_force().
///
/// @param e1_sx, e1_sy    Edge 1 start (receives force).
/// @param e1_ex, e1_ey    Edge 1 end.
/// @param e2_sx, e2_sy    Edge 2 start (source of field).
/// @param e2_ex, e2_ey    Edge 2 end.
/// @param config          Force configuration.
/// @return Tuple of (force_x, force_y, torque).
inline void compute_edge_to_edge_force(
    double e1_sx, double e1_sy,
    double e1_ex, double e1_ey,
    double e2_sx, double e2_sy,
    double e2_ex, double e2_ey,
    const ForceConfig& config,
    double& out_fx, double& out_fy, double& out_torque) {

    double edge1_x = e1_ex - e1_sx;
    double edge1_y = e1_ey - e1_sy;
    double edge1_len = std::sqrt(edge1_x * edge1_x + edge1_y * edge1_y);

    out_fx = 0.0;
    out_fy = 0.0;
    out_torque = 0.0;

    if (edge1_len < 1e-10) return;

    double edge1_center_x = (e1_sx + e1_ex) * 0.5;
    double edge1_center_y = (e1_sy + e1_ey) * 0.5;

    int num_samples = config.edge_samples;

    for (int i = 0; i < num_samples; ++i) {
        double t = (i + 0.5) / num_samples;
        double sample_x = e1_sx + edge1_x * t;
        double sample_y = e1_sy + edge1_y * t;

        // Charge density scaled by sample fraction
        double sample_charge = config.charge_density * edge1_len / num_samples;

        Vec2 force = compute_edge_to_point_force(
            sample_x, sample_y,
            e2_sx, e2_sy, e2_ex, e2_ey,
            sample_charge,
            config.min_distance);

        out_fx += force.x;
        out_fy += force.y;

        // Torque: r x F where r is from edge1 center to sample point
        double rx = sample_x - edge1_center_x;
        double ry = sample_y - edge1_center_y;
        out_torque += rx * force.y - ry * force.x;
    }
}

/// Compute all pairwise component repulsion forces and torques.
///
/// Operates on flat arrays of edge data for all components. The full
/// N^2 loop runs entirely in C++ with no Python callbacks.
///
/// @param positions_x     Component center X positions (size N).
/// @param positions_y     Component center Y positions (size N).
/// @param edges_flat      Flat array of all edges: [sx, sy, ex, ey, ...].
///                         Size = sum(edge_counts) * 4.
/// @param edge_offsets    Starting index in edges_flat/4 for each component.
///                         Size N+1 (last element = total edges).
/// @param n_components    Number of components.
/// @param config          Force configuration.
/// @param fixed_mask      Boolean mask: true = component is fixed. Size N.
/// @return ForceResult with forces and torques for all components.
ForceResult compute_all_repulsion(
    const std::vector<double>& positions_x,
    const std::vector<double>& positions_y,
    const std::vector<double>& edges_flat,
    const std::vector<int>& edge_offsets,
    size_t n_components,
    const ForceConfig& config,
    const std::vector<bool>& fixed_mask);

/// Compute boundary forces from board edges on all components.
///
/// @param positions_x     Component center X positions (size N).
/// @param positions_y     Component center Y positions (size N).
/// @param edges_flat      Flat array of all component edges.
/// @param edge_offsets    Starting edge index per component (size N+1).
/// @param board_edges     Flat array of board edges: [sx, sy, ex, ey, ...].
/// @param n_board_edges   Number of board edges.
/// @param n_components    Number of components.
/// @param config          Force configuration.
/// @param fixed_mask      Boolean mask: true = component is fixed.
/// @param inside_flags    Boolean: true = component center is inside board.
/// @return ForceResult with boundary forces and torques.
ForceResult compute_boundary_forces(
    const std::vector<double>& positions_x,
    const std::vector<double>& positions_y,
    const std::vector<double>& edges_flat,
    const std::vector<int>& edge_offsets,
    const std::vector<double>& board_edges,
    size_t n_board_edges,
    size_t n_components,
    const ForceConfig& config,
    const std::vector<bool>& fixed_mask,
    const std::vector<bool>& inside_flags);

}  // namespace placement
