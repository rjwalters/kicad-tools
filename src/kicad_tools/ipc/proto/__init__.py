"""Generated protobuf stubs for KiCad IPC API.

This package contains lightweight message classes that mirror the KiCad IPC
protobuf schema. Rather than depending on generated protobuf code (which
requires matching the exact KiCad proto version), we use simple dataclasses
that serialize to the same JSON wire format that KiCad's IPC API accepts.

To regenerate from upstream KiCad protos::

    # Clone KiCad source
    git clone https://gitlab.com/kicad/code/kicad.git
    cd kicad/api/proto

    # Generate Python stubs
    protoc --python_out=<output_dir> \\
        kicad/common/types/*.proto \\
        kicad/api/*.proto

The current message definitions target KiCad 9.0 API compatibility.
"""
