# Match-Group Length-Matching Guides

Length-match a parallel-bus **group** (DDR data byte, MIPI lane group, HDMI
TMDS, address bus) by configuring its **net class** on `NetClassRouting`
— not by imperative `Router.method(...)` calls. The full feature set
landed across Epic #2661 (Phases 1–3).

## Which guide do I need?

| If you want to… | Read |
|---|---|
| Declare a group (`length_match_group`, suffix detection, legacy API) | [01-declaring-groups.md](01-declaring-groups.md) |
| Pick a length reference (longest / explicit / `clock`) | [02-reference-selection.md](02-reference-selection.md) |
| Compose a group whose members are diff pairs (MIPI/HDMI) | [03-group-of-pairs.md](03-group-of-pairs.md) |
| Understand why the tuner gave up (cascade-safety budget) | [04-cascade-safety.md](04-cascade-safety.md) |
| Copy a working setup for DDR / MIPI / HDMI / address bus | [05-protocol-recipes.md](05-protocol-recipes.md) |
| Understand the `match_group_length_skew` DRC rule | [06-drc-rule.md](06-drc-rule.md) |
| Run the end-to-end CLI workflow + JSON sidecar | [07-cli-and-sidecar.md](07-cli-and-sidecar.md) |

## Pair vs group

A **pair** is N=2 (two coupled traces — see [diff-pairs](../diff-pairs/README.md)).
A **group** is N>=3 (a parallel bus). Use a group when more than two nets
must arrive within a shared skew tolerance. When the group's members are
themselves pairs (MIPI lanes, HDMI TMDS), see guide 03.
