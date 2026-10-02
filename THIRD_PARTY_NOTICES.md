# Third-party notices

## KiCadRoutingTools (MIT)

`src/kicad_tools/router/cpp/include/dubins.hpp` is a C++ port of
`rust_router/src/dubins.rs` (`DubinsCalculator`) from
[drandyhaas/KiCadRoutingTools](https://github.com/drandyhaas/KiCadRoutingTools),
commit `64df3f582e8a862c9289205b5a608466bf21a7ba` (Issue #5786).

```
MIT License

Copyright (c) 2026 drandyhaas

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

The pose-based centerline search in `coupled_pathfinder.cpp` is a
reimplementation that follows KRT's documented design and cites it; it copies
no KRT code.

## poly2tri (BSD-3)

See `src/kicad_tools/router/cpp/third_party/poly2tri/LICENSE`.
