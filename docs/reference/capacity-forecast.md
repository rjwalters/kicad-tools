# Pre-route escape-capacity forecast (`kct route --capacity-forecast`)

A **placement-only** capacity/solvability predictor. It runs before any router
or component loading and answers one counting question per dense pad field:

> can every pin that has to leave this footprint physically get out, given the
> channels the pad geometry leaves and the layers the stack offers?

Issue [#4799]. Unlike the [crossing-tail census report](crosstail-census-report.md)
— the earlier slices of the same issue — this predictor needs **no prior run**:
ring depth, inter-pad channels and the pin count that must leave a footprint are
properties of the placement, so the forecast is exact on the first route of a
brand-new board.

```bash
uv run kct route board.kicad_pcb --capacity-forecast
uv run kct route board.kicad_pcb --capacity-forecast-json forecast.json
```

Both flags are **off by default**, and neither can change what ships or what
`kct route` returns.

## Why a *local* model

Feeding the in-repo fleet through the existing RUDY estimator
(`CongestionEstimator`, `router/congestion_estimator.py`) and dividing per-tile
demand by a per-tile track supply gives **1.2–3.5 % global utilisation and a
peak tile of 0.24×** on *every* board — including the two with open router
epics ([#4409], [#3438]). A board-scale predictor would hand out a clean bill
of health to the boards that do not route. Escape geometry is local, so this
model is local. (Measured 2026-08-15; the RUDY probe is not shipped, only the
conclusion it produced.)

## The model

For each footprint with at least `MIN_PADS_FOR_FIELD` (5) pads:

1. **Ring depth, by onion peel** (`ring_depths`). A pad is on the boundary
   (depth 0) when it is missing a near neighbour in one of the four cardinal
   directions; peel the boundary away and repeat for depth 1, 2, …
   Shape-agnostic on purpose: a two-row connector (USB-C, a pin header) is
   *all* boundary and must not be modelled as an array, while a 7×7 grid peels
   into four rings. A bbox-inset rule gets the connector wrong and invents
   interior pins that do not exist.
2. **Channel width** (`channels_in_gap`). `gap` is the smallest clear spacing
   between neighbouring pads; a channel of clear width *g* carries
   `floor((g - clearance) / (track + clearance))` tracks.
3. **Via drop** (`via_site_clearance`). An interior pin reaches another layer
   only if a barrel can be placed: *in* the pad (a fab-tier capability, taken
   from `--manufacturer`) or at an **interstitial** site — for a grid array the
   diagonal gap between four pads, which is `sqrt(2)` further from every pad
   centre than the orthogonal one. On a 1.27 mm / 0.45 mm array the orthogonal
   half-gap is 0.41 mm but the interstice is 0.58 mm: modelling the drop on the
   orthogonal gap is the difference between "this BGA cannot reach an inner
   layer" and the dogbone every BGA in the world uses.
4. **Ring cuts.** Every pin at depth ≥ *d* must cross the ring at depth
   *d − 1*. Demand is those pins; supply is that ring's gap count times the
   channels per gap — the pad's own layer, plus (only where a via drop exists)
   the other signal layers, whose channels are narrowed by via barrels rather
   than by pads.

The worst cut's `demand / supply` is the field's utilisation; the worst field
is the board's.

| Verdict | Condition | Reading |
|---------|-----------|---------|
| `not-applicable` | no multi-ring pad field with interior pins to route | Every pad field is a single ring — perimeter pins escape outward directly. **Not** an `ample` result. |
| `ample` | worst ratio < `TIGHT_RATIO` (0.8) | The rings are not the binding constraint. Not a routability guarantee — see the biases below. |
| `tight` | ≥ 0.8 | Clears the cut with little margin; the counting model ignores neck-down, keepouts and teardrops, so expect the real escape to be harder. |
| `over-capacity` | ≥ `OVER_CAPACITY_RATIO` (1.0) | More pins must cross the ring than there are channels crossing it. The shortfall is geometric: no ordering, budget or engine setting fixes it. |
| `infeasible` | demand > 0 with **zero** supply | No track fits between the pads and no via can be dropped — those pins cannot leave on any layer. |

`TIGHT_RATIO` is a documented judgement call, not a calibrated constant; 1.0 is
arithmetic.

### Poured pins

A pad on a net that will be a copper pour reaches its plane straight down, so
it is deferred from ring demand — **but only where a via drop actually
exists**. If no barrel can be placed, that pin has to leave through the surface
rings exactly like a signal, and crediting it anyway is how a counting model
talks itself out of a real blockage. Pour nets are the union of the zones
already in the file and the POWER/GROUND nets `kct route`'s auto-pour would
create zones for (`auto_pour.classify_pour_candidates`, including its
all-power-board guard), so an unrouted board is forecast the way it will
actually be routed. The deferred count is printed and reported.

## What it says on this fleet

Every board's *unrouted* input, default rules (2026-08-15, same machine):

| Board | Footprints | Multi-ring fields | Worst field | Verdict | Ratio | Forecast cost |
|-------|-----------:|------------------:|-------------|---------|------:|--------------:|
| 00-simple-led | 3 | 0 | — | `not-applicable` | — | 1 ms |
| 01-voltage-divider | 4 | 0 | — | `not-applicable` | — | 1 ms |
| 02-charlieplex-led | 14 | 0 | — | `not-applicable` | — | 4 ms |
| 03-usb-joystick | 19 | 0 | — | `not-applicable` | — | 9 ms |
| 04-stm32-devboard | 17 | 0 | — | `not-applicable` | — | 8 ms |
| 05-bldc-motor-controller | 55 | 0 | — | `not-applicable` | — | 23 ms |
| 06-diffpair-test | 7 | 1 | U2, 49 pads / 4 rings | `ample` | 0.08× | 13 ms |
| 07-matchgroup-test | 8 | 1 | U4, 49 pads / 4 rings | `ample` | 0.05× | 16 ms |
| chorus-test-revA v24 (local-only, 90 footprints) | 90 | 0 | — | `not-applicable` | — | 883 ms |

**No board in the fleet is escape-capacity blocked**, and the model says so
rather than inventing a finding: boards 06 and 07 carry the only two multi-ring
arrays, both have a legal dogbone interstice, and both read `ample`. Those two
boards fail for reasons this counting model does not claim to see (#4409's
diff-pair coupling, #3438's bundle congestion) — the
[crossing-tail census](crosstail-census-report.md) is the instrument for the
first of them.

The cost line matters as much as the verdict: **1–23 ms** on the fleet, 0.9 s on
a 90-footprint external design whose route takes tens of minutes. A leading
indicator that costs what it is trying to avoid is not one.

### The counterfactuals it exists for

The same board-06 array, routed with a via too big for its interstice:

```console
$ uv run kct route boards/06-diffpair-test/output/diffpair_test.kicad_pcb \
    --capacity-forecast --via-diameter 1.0 --dry-run
[capacity-forecast] pre-route escape-capacity forecast for boards/06-diffpair-test/output/diffpair_test.kicad_pcb (placement-only, #4799)
[capacity-forecast]   rules: 4 signal layer(s), track 0.200 mm, clearance 0.150 mm, via 1.000 mm, via-in-pad no
[capacity-forecast]   1 multi-ring pad field(s) of 7 footprint(s), verdict=over-capacity
[capacity-forecast]   U2 (Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm): 49 pads / 4 rings, pitch 1.270 mm, gap 0.820 mm -> 1.04x at ring cut 1 (25 pin(s) must cross 24 channel(s)) [over-capacity]
[capacity-forecast]   reading: U2 has more interior pins than channels to carry them; the router will spend its budget and leave some of them unrouted whatever the ordering, because the shortfall is geometric
[capacity-forecast]   FIX LAYER: make a via drop possible (a fab tier with via-in-pad, or a via small enough for the interstice between pads) so the inner layers become reachable; widen the pad field's pitch or shrink track/clearance; move pins so fewer interior nets have to leave the part -- placement / stackup, not the router
[capacity-forecast]   ADVISORY ONLY -- this route is unchanged by the above (#4799)
```

A 0.5 mm-pitch array (0.25 mm pads, 0.1/0.1 rules, 0.45 mm vias) reads
`infeasible`: no track fits between the pads and no via fits between them
either, so the interior cannot leave on any layer — the HDI/microvia case, in
milliseconds, before the router starts. Both counterfactuals are pinned in
`tests/router/test_capacity_forecast_4799.py`.

## Document schema (v1)

`--capacity-forecast-json` writes one JSON object with sorted keys, following
the [machine-output](machine-output.md) conventions. Verbatim values from
board-06 at `--via-diameter 1.0`, shown here in logical rather than sorted
order and with the two inner ring cuts (`depth: 2` at 9/16, `depth: 3` at 1/8)
elided:

```json
{
  "schema_version": 1,
  "generated_at": "2026-08-15T00:00:00+00:00",
  "report": "capacity-forecast",
  "source": "board.kicad_pcb",
  "rules": {
    "signal_layers": 4,
    "track_width_mm": 0.2,
    "clearance_mm": 0.15,
    "via_diameter_mm": 1.0,
    "via_in_pad": false
  },
  "summary": {
    "applicable": true,
    "footprints_seen": 7,
    "fields_modelled": 1,
    "worst_ref": "U2",
    "worst_ratio": 1.042,
    "verdict": "over-capacity",
    "over_capacity_ratio": 1.0,
    "tight_ratio": 0.8
  },
  "fields": [
    {
      "ref": "U2",
      "footprint": "Package_BGA:BGA-49_5.0x5.0mm_Layout7x7_P0.5mm",
      "pads": 49,
      "escaping_pads": 49,
      "pour_pads_deferred": 0,
      "pitch_mm": 1.27,
      "gap_mm": 0.82,
      "rings": 4,
      "surface_channels_per_gap": 1,
      "drop_channels_per_gap": 0,
      "via_drop_available": false,
      "worst_depth": 1,
      "worst_ratio": 1.042,
      "verdict": "over-capacity",
      "cuts": [
        {"depth": 1, "gaps": 24, "channels_per_gap": 1, "supply": 24, "demand": 25, "ratio": 1.042}
      ]
    }
  ],
  "warnings": []
}
```

`ratio` is `null` — not a huge number — when supply is zero, so `infeasible` is
distinguishable from "very congested" by a reader that only looks at the JSON.
Fields are ordered worst-first; cuts are ordered outermost-first.

## Biases (both directions, deliberately not tuned away)

* Pad extent is approximated by `min(width, height)`, so the gap between oblong
  pads is understated in one axis — **pessimistic**.
* Supply assumes every gap in a ring is usable, ignoring neck-down rules,
  keepouts and teardrops — **optimistic**.
* Escaping pins are counted as pads, not nets: two pads of one net inside a
  field could in principle share one exit — **pessimistic**, rarely by much.
* Deferred pour pins consume interstitial barrels that are not charged against
  the drop-layer channels — **optimistic**.
* Nothing outside the footprint is modelled. A field that escapes cleanly can
  still fail in the corridor beyond it (board-05's sense band), so `ample` is
  not a routability guarantee.

## Advisory, and only advisory

`--capacity-forecast` cannot fail a route. The preflight returns nothing for
the caller to turn into an exit code; a board that cannot be read or parsed
prints one `[capacity-forecast] no forecast: …` line and routing proceeds, and
an unwritable `--capacity-forecast-json` path prints a diagnostic without
losing the printed block. This is the `_offboard_preflight` precedent that
report-only surfaces must never block a route, restated for every advisory
surface on #4799.

Promoting it to a go/no-go the way `--census-advisory-gate` did for the census
([#4865]) is deliberate follow-up work, not an oversight: the thresholds above
are one fleet's judgement, and a gate needs calibration first.

## API

```python
from kicad_tools.router.capacity_forecast import (
    FieldPad,  # x/y/width/height/ref/net_name -- the model's whole input
    build_forecast,  # pads -> CapacityForecast (pure; no file needed)
    forecast_from_board,  # .kicad_pcb -> CapacityForecast
    emit_forecast,  # the above + print (+ optional JSON); never raises
    write_report,  # CapacityForecast -> JSON document
    channels_in_gap,  # tracks through a clear channel
    ring_depths,  # onion-peel ring index per pad
    via_site_clearance,  # largest clear radius at an interstitial site
)

forecast = forecast_from_board("board.kicad_pcb", via_in_pad=True)
print(forecast.verdict, forecast.worst_ratio)
for field in forecast.ranked_fields:
    print(field.ref, field.rings, field.worst_cut.demand, field.worst_cut.supply)
```

`build_forecast` accepts any object with the `FieldPad` attributes, so a board
script or a test can drive it from pads it already has. Board files are read
through `kicad_tools.schema.PCB`, which understands both the legacy
`(fp_text reference …)` and the modern `(property "Reference" …)` spellings —
the regex loader in `router/io.py` reads only the former and silently sees zero
footprints on a board saved by a current KiCad.

[#3438]: https://github.com/rjwalters/kicad-tools/issues/3438
[#4409]: https://github.com/rjwalters/kicad-tools/issues/4409
[#4799]: https://github.com/rjwalters/kicad-tools/issues/4799
[#4865]: https://github.com/rjwalters/kicad-tools/pull/4865
