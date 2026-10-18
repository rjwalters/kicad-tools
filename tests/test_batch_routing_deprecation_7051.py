"""``--batch-routing`` is a deprecated no-op (issue #7051).

The GPU ``BatchPathfinder`` kernel was never wired into the router, so the
flag only printed "GPU-accelerated".  It is now accepted-but-ignored and
warns; routing output must not change.
"""

from __future__ import annotations

import warnings

import pytest

_PCB = """(kicad_pcb (version 20240108) (generator "test") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "") (net 1 "A") (net 2 "B")
  (gr_rect (start 90 90) (end 120 110) (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts"))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 100 100)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "A"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "B")))
  (footprint "Resistor_SMD:R_0402_1005Metric" (layer "F.Cu") (at 110 100)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at -0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 1 "A"))
    (pad "2" smd roundrect (at 0.51 0) (size 0.54 0.64) (layers "F.Cu" "F.Paste" "F.Mask") (net 2 "B")))
)
"""


def _pads(ref, x, n1, n2):
    return [
        {
            "number": "1",
            "x": x - 0.51,
            "y": 100.0,
            "width": 0.54,
            "height": 0.64,
            "net": n1,
            "net_name": "A" if n1 == 1 else "B",
        },
        {
            "number": "2",
            "x": x + 0.51,
            "y": 100.0,
            "width": 0.54,
            "height": 0.64,
            "net": n2,
            "net_name": "A" if n2 == 1 else "B",
        },
    ]


def _router():
    from kicad_tools.router.core import Autorouter

    r = Autorouter(width=30.0, height=20.0, origin_x=90.0, origin_y=90.0)
    r.add_component("R1", _pads("R1", 100.0, 1, 2))
    r.add_component("R2", _pads("R2", 110.0, 1, 2))
    return r


def test_api_batch_routing_true_warns_and_prints_no_gpu(capsys):
    r = _router()
    with pytest.warns(DeprecationWarning, match="batch_routing"):
        r.route_all_negotiated(batch_routing=True, max_iterations=1)
    assert "GPU-accelerated" not in capsys.readouterr().out


@pytest.mark.parametrize("kwargs", [{}, {"batch_routing": False}])
def test_api_default_and_false_are_silent(kwargs):
    r = _router()
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        r.route_all_negotiated(max_iterations=1, **kwargs)


def test_cli_flag_warns_and_output_is_byte_identical(tmp_path, capsys):
    from kicad_tools.cli.route_cmd import main

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_PCB)
    plain, flagged = tmp_path / "plain.kicad_pcb", tmp_path / "flag.kicad_pcb"
    common = ["--quiet", "--no-auto-layers", "--iterations", "2"]

    rc_plain = main([str(pcb), "-o", str(plain), *common])
    plain_err = capsys.readouterr().err
    with pytest.warns(DeprecationWarning, match="--batch-routing"):
        rc_flag = main([str(pcb), "-o", str(flagged), "--batch-routing", *common])
    cap = capsys.readouterr()

    assert rc_plain == rc_flag
    assert "--batch-routing has no effect" in cap.err
    assert "--batch-routing" not in plain_err
    assert "GPU-accelerated" not in cap.out + cap.err
    assert plain.exists() and flagged.exists()
    assert plain.read_bytes() == flagged.read_bytes()


def test_high_performance_alone_does_not_trigger_batch_deprecation(tmp_path, capsys):
    from kicad_tools.cli.route_cmd import main

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_PCB)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        main(
            [
                str(pcb),
                "-o",
                str(tmp_path / "o.kicad_pcb"),
                "--high-performance",
                "--quiet",
                "--no-auto-layers",
                "--dry-run",
            ]
        )
    assert "--batch-routing" not in capsys.readouterr().err


def test_gpu_batch_kernel_removed_but_optim_kernels_remain():
    import kicad_tools.acceleration.kernels as k

    assert not hasattr(k, "BatchPathfinder")
    assert not hasattr(k, "compute_batch_costs_gpu")
    assert k.PlacementGPUAccelerator is not None
    assert k.evaluate_population_gpu is not None
