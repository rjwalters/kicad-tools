/*
 * Dubins path-length calculator (Issue #5786, Epic #5784 Phase 2).
 *
 * C++ port of ``DubinsCalculator`` from KiCadRoutingTools
 * (https://github.com/drandyhaas/KiCadRoutingTools),
 * ``rust_router/src/dubins.rs``, pinned at commit
 * 64df3f582e8a862c9289205b5a608466bf21a7ba.
 *
 * Original work:
 *
 *   MIT License
 *
 *   Copyright (c) 2026 drandyhaas
 *
 *   Permission is hereby granted, free of charge, to any person obtaining a
 *   copy of this software and associated documentation files (the
 *   "Software"), to deal in the Software without restriction, including
 *   without limitation the rights to use, copy, modify, merge, publish,
 *   distribute, sublicense, and/or sell copies of the Software, and to permit
 *   persons to whom the Software is furnished to do so, subject to the
 *   following conditions:
 *
 *   The above copyright notice and this permission notice shall be included
 *   in all copies or substantial portions of the Software.
 *
 *   THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS
 *   OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
 *   MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN
 *   NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM,
 *   DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR
 *   OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE
 *   USE OR OTHER DEALINGS IN THE SOFTWARE.
 *
 * Modifications for this repository: translated from Rust to C++20; the
 * integer-scaled entry point is kept bit-compatible with the original
 * (``(len * r * 1000.0) as i32`` truncation), and a ``double`` variant is
 * added for the pose search, which wants an unscaled metric.
 *
 * Dubins paths are the shortest paths for a vehicle that can only move
 * forward with a minimum turning radius: at most three segments, each a
 * straight line (S) or a circular arc (L / R).
 */

#pragma once

#include <algorithm>
#include <cmath>
#include <limits>
#include <optional>

namespace router {

class DubinsCalculator {
public:
    explicit DubinsCalculator(double min_radius)
        : min_radius_(std::max(min_radius, 0.1)) {}

    double min_radius() const { return min_radius_; }

    // Shortest Dubins path length between two poses, in the same units as
    // the inputs (unscaled).  Same case analysis as ``path_length``.
    double path_length_f(double x1, double y1, double theta1,
                         double x2, double y2, double theta2) const {
        const double r = min_radius_;
        const double dx = x2 - x1;
        const double dy = y2 - y1;
        const double d = std::sqrt(dx * dx + dy * dy);

        if (d < 0.001) {
            const double dtheta = std::fabs(normalize_angle(theta2 - theta1));
            return dtheta * r;
        }

        const double d_norm = d / r;
        const double phi = std::atan2(dy, dx);
        const double alpha = normalize_angle(theta1 - phi);
        const double beta = normalize_angle(theta2 - phi);

        double min_len = std::numeric_limits<double>::max();
        auto consider = [&](std::optional<double> len) {
            if (len) min_len = std::min(min_len, *len);
        };
        consider(lsl_length(d_norm, alpha, beta));
        consider(rsr_length(d_norm, alpha, beta));
        consider(lsr_length(d_norm, alpha, beta));
        consider(rsl_length(d_norm, alpha, beta));
        consider(rlr_length(d_norm, alpha, beta));
        consider(lrl_length(d_norm, alpha, beta));

        if (min_len == std::numeric_limits<double>::max()) {
            const double dtheta = std::fabs(normalize_angle(theta2 - theta1));
            min_len = d_norm + dtheta;
        }
        return min_len * r;
    }

    // Original KRT contract: length scaled by 1000 and truncated to int.
    int path_length(double x1, double y1, double theta1,
                    double x2, double y2, double theta2) const {
        return static_cast<int>(path_length_f(x1, y1, theta1, x2, y2, theta2) * 1000.0);
    }

private:
    double min_radius_;

    static constexpr double kPi = 3.14159265358979323846;

    static double normalize_angle(double a) {
        a = std::fmod(a, 2.0 * kPi);
        if (a > kPi) a -= 2.0 * kPi;
        else if (a < -kPi) a += 2.0 * kPi;
        return a;
    }

    static double mod2pi(double a) {
        a = std::fmod(a, 2.0 * kPi);
        if (a < 0.0) a += 2.0 * kPi;
        return a;
    }

    static std::optional<double> lsl_length(double d, double alpha, double beta) {
        const double ca = std::cos(alpha), sa = std::sin(alpha);
        const double cb = std::cos(beta), sb = std::sin(beta);
        const double tmp = 2.0 + d * d - 2.0 * (ca * cb + sa * sb - d * (sa - sb));
        if (tmp < 0.0) return std::nullopt;
        const double p = std::sqrt(tmp);
        const double theta = std::atan2(cb - ca, d + sa - sb);
        const double t = mod2pi(-alpha + theta);
        const double q = mod2pi(beta - theta);
        return t + p + q;
    }

    static std::optional<double> rsr_length(double d, double alpha, double beta) {
        const double ca = std::cos(alpha), sa = std::sin(alpha);
        const double cb = std::cos(beta), sb = std::sin(beta);
        const double tmp = 2.0 + d * d - 2.0 * (ca * cb + sa * sb - d * (sb - sa));
        if (tmp < 0.0) return std::nullopt;
        const double p = std::sqrt(tmp);
        const double theta = std::atan2(ca - cb, d - sa + sb);
        const double t = mod2pi(alpha - theta);
        const double q = mod2pi(-beta + theta);
        return t + p + q;
    }

    static std::optional<double> lsr_length(double d, double alpha, double beta) {
        const double ca = std::cos(alpha), sa = std::sin(alpha);
        const double cb = std::cos(beta), sb = std::sin(beta);
        const double tmp = -2.0 + d * d + 2.0 * (ca * cb + sa * sb + d * (sa + sb));
        if (tmp < 0.0) return std::nullopt;
        const double p = std::sqrt(tmp);
        const double theta = std::atan2(-ca - cb, d + sa + sb) - std::atan2(-2.0, p);
        const double t = mod2pi(-alpha + theta);
        const double q = mod2pi(-beta + theta);
        return t + p + q;
    }

    static std::optional<double> rsl_length(double d, double alpha, double beta) {
        const double ca = std::cos(alpha), sa = std::sin(alpha);
        const double cb = std::cos(beta), sb = std::sin(beta);
        const double tmp = -2.0 + d * d + 2.0 * (ca * cb + sa * sb - d * (sa + sb));
        if (tmp < 0.0) return std::nullopt;
        const double p = std::sqrt(tmp);
        const double theta = std::atan2(ca + cb, d - sa - sb) - std::atan2(2.0, p);
        const double t = mod2pi(alpha - theta);
        const double q = mod2pi(beta - theta);
        return t + p + q;
    }

    static std::optional<double> rlr_length(double d, double alpha, double beta) {
        const double ca = std::cos(alpha), sa = std::sin(alpha);
        const double cb = std::cos(beta), sb = std::sin(beta);
        const double tmp = (6.0 - d * d + 2.0 * (ca * cb + sa * sb + d * (sa - sb))) / 8.0;
        if (std::fabs(tmp) > 1.0) return std::nullopt;
        const double p = mod2pi(2.0 * kPi - std::acos(tmp));
        const double theta = std::atan2(ca - cb, d - sa + sb);
        const double t = mod2pi(alpha - theta + p / 2.0);
        const double q = mod2pi(alpha - beta - t + p);
        return t + p + q;
    }

    static std::optional<double> lrl_length(double d, double alpha, double beta) {
        const double ca = std::cos(alpha), sa = std::sin(alpha);
        const double cb = std::cos(beta), sb = std::sin(beta);
        const double tmp = (6.0 - d * d + 2.0 * (ca * cb + sa * sb - d * (sa - sb))) / 8.0;
        if (std::fabs(tmp) > 1.0) return std::nullopt;
        const double p = mod2pi(2.0 * kPi - std::acos(tmp));
        const double theta = std::atan2(cb - ca, d + sa - sb);
        const double t = mod2pi(-alpha + theta + p / 2.0);
        const double q = mod2pi(beta - alpha - t + p);
        return t + p + q;
    }
};

}  // namespace router
