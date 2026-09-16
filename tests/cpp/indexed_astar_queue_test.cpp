// Standalone controls against the former std::priority_queue implementation.
#include "pathfinder.hpp"
#include <cassert>
#include <cmath>
#include <iostream>
#include <random>
#include <set>
#include <string>

using namespace router;
using Indexed = IndexedAStarQueue<GridPosHash>;
using Legacy = std::priority_queue<AStarNode, std::vector<AStarNode>, std::greater<AStarNode>>;
using Key = std::tuple<int, int, int>;

static Key key(const AStarNode& n) { return {n.x, n.y, n.layer}; }
static bool same(const AStarNode& a, const AStarNode& b) {
    return a.f_score == b.f_score && a.g_score == b.g_score && key(a) == key(b) &&
           a.parent_idx == b.parent_idx && a.via_from_parent == b.via_from_parent &&
           a.dx == b.dx && a.dy == b.dy && a.seq == b.seq;
}
static AStarNode node(int x, float f, float g, uint64_t seq) {
    return {f, g, x, 0, 0, static_cast<int>(seq), false, 1, 0, seq};
}

static void duplicates() {
    Legacy old;
    Indexed queue;
    uint64_t seq = 0;
    for (int g = 200; g > 0; --g) {
        for (int x = 0; x < 32; ++x) {
            auto n = node(x, static_cast<float>(g + x), static_cast<float>(g), seq++);
            old.push(n);
            queue.push(n);
        }
    }
    assert(queue.size() == 32);
    assert(old.size() == 6400);
    std::set<Key> closed;
    int pops = 0;
    while (!old.empty()) {
        auto n = old.top(); old.pop(); ++pops;
        if (!closed.insert(key(n)).second) continue;
        assert(!queue.empty() && same(n, queue.top()));
        queue.pop();
    }
    assert(queue.empty() && pops == 6400 && closed.size() == 32);
    // Removing a key must allow insertion again; assignment must clear index too.
    queue.push(node(7, 1, 1, seq++));
    queue = Indexed();
    assert(queue.empty());
    queue.push(node(7, 3, 3, seq));
    assert(queue.top().seq == seq && queue.size() == 1);
}

static void rounding() {
    Legacy old;
    Indexed queue;
    const float h = 100000000.0f;
    const float high_g = 2.0f, low_g = 1.0f;
    assert(h + high_g == h + low_g);
    // Lower g is NOT comparator-better if f rounded to equality.
    for (auto n : {node(0, h + high_g, high_g, 10), node(0, h + low_g, low_g, 11),
                   node(1, h, high_g, 9), node(2, h, high_g, 12),
                   node(2, h, high_g, 13)}) {
        old.push(n); queue.push(n);
    }
    std::set<Key> closed;
    while (!old.empty()) {
        auto n = old.top(); old.pop();
        if (!closed.insert(key(n)).second) continue;
        assert(same(n, queue.top())); queue.pop();
    }
    assert(queue.empty());
}

// A small A* harness preserves XYZ dominance and the production node comparator.
// Each run can pause after a useful expansion and resume with its queue retained.
template <typename Queue>
struct Search {
    Queue queue;
    std::vector<AStarNode> expanded;
    std::set<Key> closed;
    std::unordered_map<Key, float, GridPosHash> scores;
    uint64_t seq = 0;
    int pops = 0;
    bool success = false;
    void push(int x, int y, int layer, float g, float h, int parent, int dx, int dy) {
        Key k{x, y, layer};
        if (closed.count(k)) return;
        auto it = scores.find(k);
        if (it != scores.end() && g >= it->second) return;
        scores[k] = g;
        queue.push({g + h, g, x, y, layer, parent, layer != 0, dx, dy, seq++});
    }
    template <typename Expand>
    void run(int cap, int pause_after, Expand expand) {
        while (!queue.empty() && pops < cap) {
            ++pops; // Same placement as production: before pop/closed check.
            auto n = queue.top(); queue.pop();
            if (!closed.insert(key(n)).second) continue;
            expanded.push_back(n);
            expand(*this, n);
            if (success || static_cast<int>(expanded.size()) >= pause_after) return;
        }
    }
};

static void cap() {
    // 0 -> 1 costs 100; 0 -> 2 -> 1 costs 2. The obsolete 100 entry
    // precedes goal 3 at 200, wasting the old queue's fourth and final pop.
    auto expand = [](auto& s, const AStarNode& n) {
        int parent = static_cast<int>(s.expanded.size()) - 1;
        if (n.x == 0) {
            s.push(1, 0, 0, 100, 0, parent, 1, 0);
            s.push(2, 0, 0, 1, 0, parent, 1, 0);
        } else if (n.x == 2) s.push(1, 0, 0, 2, 0, parent, -1, 0);
        else if (n.x == 1) s.push(3, 0, 0, 200, 0, parent, 1, 0);
        else if (n.x == 3) s.success = true;
    };
    Search<Legacy> old;
    Search<Indexed> current;
    old.push(0, 0, 0, 0, 0, -1, 0, 0);
    current.push(0, 0, 0, 0, 0, -1, 0, 0);
    old.run(2, 100, expand); current.run(2, 100, expand);
    assert(!old.success && !current.success && old.pops == 2 && current.pops == 2);
    old.run(4, 100, expand); current.run(4, 100, expand);
    assert(!old.success && current.success && old.pops == 4 && current.pops == 4);
    old.run(100, 100, expand);
    assert(old.success && old.pops == 5 && old.expanded.size() == current.expanded.size());
    for (std::size_t i = 0; i < old.expanded.size(); ++i)
        assert(same(old.expanded[i], current.expanded[i]));
}

static void paths() {
    // Spatial A* with turn and destination-cell costs, including ties, vias,
    // and repeated relaxations. Compare every useful expansion and its parent
    // index, not just cost or reachability. This also preserves the whole path.
    for (unsigned seed = 0; seed < 40; ++seed) {
        std::mt19937 rng(seed);
        int costs[2][13][13];
        for (auto& layer : costs) for (auto& row : layer) for (int& c : row) c = rng() % 8;
        auto expand = [&](auto& s, const AStarNode& n) {
            if (n.x == 12 && n.y == 12 && n.layer == 0) { s.success = true; return; }
            const int parent = static_cast<int>(s.expanded.size()) - 1;
            const int dirs[][2] = {{1,0},{-1,0},{0,1},{0,-1},{1,1},{-1,1},{1,-1},{-1,-1},{0,0}};
            for (const auto& d : dirs) {
                int x = n.x + d[0], y = n.y + d[1];
                const bool via = d[0] == 0 && d[1] == 0;
                int layer = via ? 1 - n.layer : n.layer;
                if (x < 0 || y < 0 || x > 12 || y > 12) continue;
                float turn = !via && (n.dx || n.dy) && (n.dx != d[0] || n.dy != d[1]) ? 5 : 0;
                float step = via ? 10 : (d[0] && d[1] ? 1.414f : 1);
                float h = std::max(12-x,12-y) + .414f * std::min(12-x,12-y) + (layer ? 10 : 0);
                s.push(x, y, layer, n.g_score + step + turn + costs[layer][y][x], h, parent,
                       via ? n.dx : d[0], via ? n.dy : d[1]);
            }
        };
        Search<Legacy> old;
        Search<Indexed> current;
        old.push(0, 0, 0, 0, 0, -1, 0, 0); current.push(0, 0, 0, 0, 0, -1, 0, 0);
        old.run(100000, 17, expand); current.run(100000, 17, expand);
        assert(old.expanded.size() == 17 && current.expanded.size() == 17);
        old.run(100000, 100000, expand); current.run(100000, 100000, expand);
        assert(old.success && current.success && current.pops <= old.pops);
        assert(old.expanded.size() == current.expanded.size());
        for (std::size_t i = 0; i < old.expanded.size(); ++i)
            assert(same(old.expanded[i], current.expanded[i]));
    }
}

int main(int argc, char** argv) {
    assert(argc == 2);
    std::string test = argv[1];
    if (test == "duplicates") duplicates();
    else if (test == "rounding") rounding();
    else if (test == "cap") cap();
    else if (test == "paths") paths();
    else return 2;
    std::cout << test << " passed\n";
}
