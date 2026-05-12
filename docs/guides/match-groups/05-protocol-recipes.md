# 05 — Protocol Recipes

Copy-pasteable `NetClassRouting` blocks for parallel-bus protocols. Each
recipe declares **one** net class; declare the same `length_match_group`
on every class whose nets belong to the group (see guide 01). Reference
board: [`boards/07-matchgroup-test`](../../../boards/07-matchgroup-test/).

## DDR3/DDR4 data byte (DQ0-7 + DM + DQS pair) — ±0.1 mm

```python
from kicad_tools.router.rules import NetClassRouting

ddr_dq = NetClassRouting(
    name="DDR_DQ",
    trace_width=0.15,
    length_match_group="DDR_DATA_BYTE_0",
    length_match_reference="DQS_P",
    length_match_tolerance_mm=0.1,
    length_critical=True,
)
assert ddr_dq.effective_length_match_tolerance() == 0.1
```

10-net group: `DQ0..DQ7`, `DM`, and the `DQS_P/N` strobe pair. The
strobe is the pace-car. Declare the same group name on the DQS class
so the strobe joins as the reference.

## MIPI CSI / DSI lane group — ±0.05 mm

```python
from kicad_tools.router.rules import NetClassRouting

mipi_csi_data = NetClassRouting(
    name="MIPI_CSI_DATA",
    trace_width=0.2,
    intra_pair_clearance=0.075,
    coupled_routing=True,
    diffpair_partner="CSI_DAT0_N",
    length_match_group="MIPI_CSI",
    length_match_reference="clock",
    length_match_tolerance_mm=0.05,
    length_critical=True,
)
assert mipi_csi_data.effective_length_match_tolerance() == 0.05
```

Groups whose members are diff pairs engage the v2 symmetric-serpentine
path (guide 03). Declare an identical class per lane; detection merges
them by shared group name.

## HDMI TMDS (3 data pairs + clock pair) — ±0.075 mm

```python
from kicad_tools.router.rules import NetClassRouting

hdmi_tmds = NetClassRouting(
    name="HDMI_TMDS_DATA",
    trace_width=0.2,
    intra_pair_clearance=0.075,
    coupled_routing=True,
    diffpair_partner="TMDS_D0_N",
    length_match_group="HDMI_TMDS",
    length_match_reference="clock",
    length_match_tolerance_mm=0.075,
    length_critical=True,
)
assert hdmi_tmds.effective_length_match_group() == "HDMI_TMDS"
```

6 nets across 3 data pairs plus the TMDS clock pair in a parallel class
with `length_match_group="HDMI_TMDS"`.

## Parallel address bus — ±0.5 mm

```python
from kicad_tools.router.rules import NetClassRouting

addr_bus = NetClassRouting(
    name="ADDR_BUS",
    trace_width=0.15,
    length_match_group="ADDR_BUS",
    length_match_tolerance_mm=0.5,
    length_critical=True,
)
assert addr_bus.effective_length_match_tolerance() == 0.5
```

Commodity tier; loose tolerance, longest-in-group reference.

## DDR strobe / data / mask in one group

DDR mixes a strobe diff pair (`DQS_P/N`, the pace-car), data (`DQ0..DQ7`),
and mask (`DM`) under one group name. Declare three net classes — DQS
sets `diffpair_partner` to route the strobe coupled; all three set
`length_match_group="DDR_DATA_BYTE_0"`. DQS within-pair skew is tuned by
the diff-pair pass; DQ-to-DQS lane skew by the match-group pass.

## See also: [01](01-declaring-groups.md), [03](03-group-of-pairs.md), [07](07-cli-and-sidecar.md).
