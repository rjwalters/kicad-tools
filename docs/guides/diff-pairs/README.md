# Differential Pair Routing Guides

Configure differential pairs per **net class** on `NetClassRouting` — not via
imperative `Router.method(...)` calls. The full feature set landed across
Epic #2556 (Phases 1–3).

## Which guide do I need?

| If you want to… | Read |
|---|---|
| Mark two nets as a pair | [01-declaring-pairs.md](01-declaring-pairs.md) |
| Set within-pair clearance (tighter than the manufacturer's inter-net rule) | [02-clearance-and-classes.md](02-clearance-and-classes.md) |
| Target a specific differential or single-ended impedance | [03-impedance-and-sizing.md](03-impedance-and-sizing.md) |
| Length-match a pair within a skew tolerance | [04-length-matching.md](04-length-matching.md) |
| Copy a working setup for USB / PCIe / MIPI | [05-protocol-recipes.md](05-protocol-recipes.md) |
| Understand the diff-pair DRC rules (`kct check`) | [06-drc-rules.md](06-drc-rules.md) |

## Canonical pre-configured class

`NET_CLASS_HIGH_SPEED` in `src/kicad_tools/router/rules.py:675` already has
`coupled_routing=True`, `intra_pair_clearance=0.075`, and
`length_critical=True` — opt nets into it via `high_speed_nets=[...]` on
`Autorouter` rather than building a class from scratch.

```python
from kicad_tools.router.rules import NET_CLASS_HIGH_SPEED
```

## How detection, engagement, and DRC fit together

1. **Detection** decides which nets are a pair (guide 01).
2. **Engagement** decides whether `CoupledPathfinder` routes them as a pair
   (guide 02, `coupled_routing` flag).
3. **Impedance / length matching** shape *how* the pair is routed (guides 03,
   04).
4. **DRC** validates the routed result (guide 06).
