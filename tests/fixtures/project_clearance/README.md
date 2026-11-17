# Native effective netclass clearance oracle

`oracle.json` records KiCad 10.0.6 CLI DRC results for 36 disposable boards.
Each has two parallel 0.10 mm tracks separated by a 0.05 mm edge gap. One net
has the project's tested assignments; its reference neighbor is assigned a
zero-clearance class. The reported native clearance therefore identifies the
tested net's effective clearance without the neighbor masking it.

The 12 project configurations cover schema 3 migration, schemas 4 and 5,
opposing priorities, and missing-value inheritance. Each configuration probes
an explicit assignment combined with a pattern, a pattern alone, and Default.
This is a netclass oracle, not evidence about custom `.kicad_dru` rules,
arbitrary pattern syntax, all electrical fields, or router enforcement.

Regenerate using a Python with `pcbnew` and `kicad-cli` on PATH:

```sh
python capture.py /tmp/kct-project-clearance-oracle
```

The output directory must not exist. It retains every input, complete DRC
report, command, exit code, and input hashes, plus a new `oracle.json` for
comparison. Input files are checked unchanged after every native invocation.
The script does not initialize a GUI or use the SWIG project loader as its
referee; the CLI loads each real project sidecar.

Semantics reference:
https://github.com/KiCad/kicad-source-mirror/blob/10.0.6/common/project/net_settings.cpp
