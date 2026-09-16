#pragma once

#include "types.hpp"
#include <cstddef>
#include <tuple>
#include <unordered_map>
#include <utility>
#include <vector>

namespace router {

// Only the first popped entry for an XYZ cell can be expanded: the caller
// closes that cell permanently. Keep precisely the entry the existing
// AStarNode comparator would pop first, eliminating its obsolete siblings.
// In particular, do NOT always replace with the lowest g: float rounding can
// produce equal f scores, where the comparator prefers the HIGHER g score.
//
// The sparse index scales with the live frontier, not the full routing grid.
// The existing heap-pop cap still applies; removing obsolete pops leaves more
// of that same numeric work budget available for useful expansions.
template <typename PositionHash>
class IndexedAStarQueue {
public:
    bool empty() const { return heap_.empty(); }
    std::size_t size() const { return heap_.size(); }
    const AStarNode& top() const { return heap_.front(); }

    void push(const AStarNode& node) {
        auto found = positions_.find(key(node));
        if (found != positions_.end()) {
            const auto index = found->second;
            if (!(heap_[index] > node)) return;
            heap_[index] = node;
            sift_up(index);
            return;
        }
        const auto index = heap_.size();
        heap_.push_back(node);
        positions_.emplace(key(node), index);
        sift_up(index);
    }

    void pop() {
        positions_.erase(key(heap_.front()));
        if (heap_.size() == 1) {
            heap_.pop_back();
            return;
        }
        heap_.front() = heap_.back();
        heap_.pop_back();
        positions_.at(key(heap_.front())) = 0;
        sift_down(0);
    }

private:
    using Key = std::tuple<int, int, int>;
    static Key key(const AStarNode& node) { return {node.x, node.y, node.layer}; }

    void exchange(std::size_t a, std::size_t b) {
        std::swap(heap_[a], heap_[b]);
        positions_.at(key(heap_[a])) = a;
        positions_.at(key(heap_[b])) = b;
    }

    void sift_up(std::size_t index) {
        while (index > 0) {
            const auto parent = (index - 1) / 2;
            if (!(heap_[parent] > heap_[index])) break;
            exchange(parent, index);
            index = parent;
        }
    }

    void sift_down(std::size_t index) {
        while (2 * index + 1 < heap_.size()) {
            auto child = 2 * index + 1;
            if (child + 1 < heap_.size() && heap_[child] > heap_[child + 1]) ++child;
            if (!(heap_[index] > heap_[child])) break;
            exchange(index, child);
            index = child;
        }
    }

    std::vector<AStarNode> heap_;
    std::unordered_map<Key, std::size_t, PositionHash> positions_;
};

}  // namespace router
