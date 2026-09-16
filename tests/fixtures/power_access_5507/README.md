# Retained power-pad access failure (#5507)

This is a declarative, source-bound failing geometry fixture. It contains WKB
geometry and explicit layers/rules; replay does not deserialize executable data.
The gzip container has a deterministic timestamp. `manifest.json` binds it to
the original retained PCB/project/DRU and the frozen source revision.

From a checkout with development dependencies installed:

```sh
.venv/bin/python tests/fixtures/power_access_5507/replay.py \
  tests/fixtures/power_access_5507/geometry.json.gz \
  --source . --output /tmp/power-access-result.json
```

At the recorded source, the result is `None`, with 928 reached states and an
empty frontier under the unchanged 0.05 mm step and 200000-node cap. The fixture
has 18 pads, 52 track pieces, 12 vias and all 22 original targets. The complete
244-pad scene reproduces the same failure; distant obstacles were removed only
in this fixture, never from a board. Coordinates and net names are fixture data,
not production routing conditions.

This records a failure, not a successful repair or proof of physical
impossibility. A future repair must preserve copper, manufacturing rules and
budgets and demonstrate saved/refilled connectivity. The separate reviewed
early-access integration already connects U4.C2, but combined power and routing
quality acceptance remains outstanding; see issue #5507 for those receipts.
