/* Interactive PCB Report - Canvas Renderer and UI Controller */
(function () {
  "use strict";

  // ---- Data injected by Python template engine ----
  // window.PCB_DATA  = { board_outline, bounds, footprints, segments, vias, layers }
  // window.DRC_DATA  = { violations: [...], error_count, warning_count }
  // window.REPORT_META = { project_name, date, ... }

  var pcb = window.PCB_DATA || {};
  var drc = window.DRC_DATA || { violations: [], error_count: 0, warning_count: 0 };

  // ---- Layer colors ----
  var LAYER_COLORS = {
    "F.Cu": "#cc0000",
    "B.Cu": "#0000cc",
    "In1.Cu": "#cc8800",
    "In2.Cu": "#008800",
    "In3.Cu": "#8800cc",
    "In4.Cu": "#00cccc",
    "Edge.Cuts": "#cccc00",
    "F.SilkS": "#cccccc",
    "B.SilkS": "#666666",
    "F.Mask": "rgba(160,0,160,0.3)",
    "B.Mask": "rgba(0,160,160,0.3)",
  };

  function layerColor(name) {
    return LAYER_COLORS[name] || "#888888";
  }

  // ---- Canvas state ----
  var canvas, ctx;
  var viewState = { offsetX: 0, offsetY: 0, scale: 1 };
  var isDragging = false;
  var dragStart = { x: 0, y: 0 };
  var mouseBoard = { x: 0, y: 0 };
  var selectedViolation = null;
  var visibleLayers = {};
  var highlightLocations = [];

  // ---- Initialization ----
  function init() {
    canvas = document.getElementById("pcb-canvas");
    ctx = canvas.getContext("2d");

    // Initialize visible layers
    (pcb.layers || []).forEach(function (l) {
      visibleLayers[l] = true;
    });
    visibleLayers["Edge.Cuts"] = true;

    fitBoard();
    setupEvents();
    buildLayerControls();
    buildViolationList();
    updateStats();
    render();
  }

  function fitBoard() {
    var b = pcb.bounds;
    if (!b) return;
    var rect = canvas.getBoundingClientRect();
    canvas.width = rect.width * window.devicePixelRatio;
    canvas.height = rect.height * window.devicePixelRatio;
    canvas.style.width = rect.width + "px";
    canvas.style.height = rect.height + "px";

    var bw = b.max_x - b.min_x;
    var bh = b.max_y - b.min_y;
    if (bw === 0 || bh === 0) return;

    var margin = 20 * window.devicePixelRatio;
    var scaleX = (canvas.width - 2 * margin) / bw;
    var scaleY = (canvas.height - 2 * margin) / bh;
    viewState.scale = Math.min(scaleX, scaleY);
    viewState.offsetX =
      (canvas.width - bw * viewState.scale) / 2 - b.min_x * viewState.scale;
    viewState.offsetY =
      (canvas.height - bh * viewState.scale) / 2 - b.min_y * viewState.scale;
  }

  // ---- Coordinate transforms ----
  function boardToScreen(bx, by) {
    return {
      x: bx * viewState.scale + viewState.offsetX,
      y: by * viewState.scale + viewState.offsetY,
    };
  }

  function screenToBoard(sx, sy) {
    return {
      x: (sx - viewState.offsetX) / viewState.scale,
      y: (sy - viewState.offsetY) / viewState.scale,
    };
  }

  // ---- Event handling ----
  function setupEvents() {
    canvas.addEventListener("mousedown", onMouseDown);
    canvas.addEventListener("mousemove", onMouseMove);
    canvas.addEventListener("mouseup", onMouseUp);
    canvas.addEventListener("mouseleave", onMouseUp);
    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("dblclick", onDblClick);
    window.addEventListener("resize", onResize);
  }

  function onMouseDown(e) {
    isDragging = true;
    dragStart.x = e.clientX;
    dragStart.y = e.clientY;
    canvas.style.cursor = "grabbing";
  }

  function onMouseMove(e) {
    var rect = canvas.getBoundingClientRect();
    var sx = (e.clientX - rect.left) * window.devicePixelRatio;
    var sy = (e.clientY - rect.top) * window.devicePixelRatio;
    mouseBoard = screenToBoard(sx, sy);

    var coordEl = document.getElementById("coord-display");
    if (coordEl) {
      coordEl.textContent =
        "X: " + mouseBoard.x.toFixed(2) + " mm  Y: " + mouseBoard.y.toFixed(2) + " mm";
    }

    if (isDragging) {
      var dx = (e.clientX - dragStart.x) * window.devicePixelRatio;
      var dy = (e.clientY - dragStart.y) * window.devicePixelRatio;
      viewState.offsetX += dx;
      viewState.offsetY += dy;
      dragStart.x = e.clientX;
      dragStart.y = e.clientY;
      render();
    }
  }

  function onMouseUp() {
    isDragging = false;
    canvas.style.cursor = "crosshair";
  }

  function onWheel(e) {
    e.preventDefault();
    var rect = canvas.getBoundingClientRect();
    var sx = (e.clientX - rect.left) * window.devicePixelRatio;
    var sy = (e.clientY - rect.top) * window.devicePixelRatio;

    var factor = e.deltaY > 0 ? 0.9 : 1.1;
    var newScale = viewState.scale * factor;

    // Zoom toward cursor position
    viewState.offsetX = sx - (sx - viewState.offsetX) * (newScale / viewState.scale);
    viewState.offsetY = sy - (sy - viewState.offsetY) * (newScale / viewState.scale);
    viewState.scale = newScale;
    render();
  }

  function onDblClick() {
    fitBoard();
    render();
  }

  function onResize() {
    fitBoard();
    render();
  }

  // ---- Rendering ----
  function render() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = "#0a0a1a";
    ctx.fillRect(0, 0, canvas.width, canvas.height);

    // Draw board outline
    drawOutline();

    // Draw segments (traces)
    drawSegments();

    // Draw vias
    drawVias();

    // Draw footprints
    drawFootprints();

    // Draw DRC violation highlights
    drawHighlights();
  }

  function drawOutline() {
    var outline = pcb.board_outline;
    if (!outline || outline.length < 2) return;

    ctx.strokeStyle = layerColor("Edge.Cuts");
    ctx.lineWidth = Math.max(1, 0.15 * viewState.scale);
    ctx.beginPath();
    var p0 = boardToScreen(outline[0][0], outline[0][1]);
    ctx.moveTo(p0.x, p0.y);
    for (var i = 1; i < outline.length; i++) {
      var p = boardToScreen(outline[i][0], outline[i][1]);
      ctx.lineTo(p.x, p.y);
    }
    ctx.closePath();
    ctx.stroke();

    // Fill board area with subtle color
    ctx.fillStyle = "rgba(20, 40, 20, 0.4)";
    ctx.fill();
  }

  function drawSegments() {
    var segs = pcb.segments || [];
    for (var i = 0; i < segs.length; i++) {
      var seg = segs[i];
      if (!visibleLayers[seg.layer]) continue;

      var s = boardToScreen(seg.start[0], seg.start[1]);
      var e = boardToScreen(seg.end[0], seg.end[1]);
      var w = Math.max(1, seg.width * viewState.scale);

      ctx.strokeStyle = layerColor(seg.layer);
      ctx.lineWidth = w;
      ctx.lineCap = "round";
      ctx.beginPath();
      ctx.moveTo(s.x, s.y);
      ctx.lineTo(e.x, e.y);
      ctx.stroke();
    }
  }

  function drawVias() {
    var vias = pcb.vias || [];
    for (var i = 0; i < vias.length; i++) {
      var via = vias[i];
      var p = boardToScreen(via.position[0], via.position[1]);
      var r = Math.max(2, (via.size / 2) * viewState.scale);
      var dr = Math.max(1, (via.drill / 2) * viewState.scale);

      ctx.fillStyle = "#aaaaaa";
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.fill();

      // Drill hole
      ctx.fillStyle = "#0a0a1a";
      ctx.beginPath();
      ctx.arc(p.x, p.y, dr, 0, Math.PI * 2);
      ctx.fill();
    }
  }

  function drawFootprints() {
    var fps = pcb.footprints || [];
    for (var i = 0; i < fps.length; i++) {
      var fp = fps[i];
      // Draw pads
      for (var j = 0; j < fp.pads.length; j++) {
        var pad = fp.pads[j];
        // Absolute position: footprint position + pad local offset
        // (pad positions from the schema are already in absolute board coords)
        var px = pad.position[0];
        var py = pad.position[1];
        var pp = boardToScreen(px, py);
        var sw = Math.max(1, pad.size[0] * viewState.scale);
        var sh = Math.max(1, pad.size[1] * viewState.scale);

        // Check if any pad layer is visible
        var padVisible = false;
        for (var k = 0; k < pad.layers.length; k++) {
          if (visibleLayers[pad.layers[k]] || pad.layers[k] === "*.Cu") {
            padVisible = true;
            break;
          }
        }
        if (!padVisible && pad.layers.length > 0) continue;

        var padColor =
          fp.layer === "B.Cu" ? "rgba(0,0,200,0.6)" : "rgba(200,0,0,0.6)";
        ctx.fillStyle = padColor;

        if (pad.shape === "circle") {
          ctx.beginPath();
          ctx.arc(pp.x, pp.y, sw / 2, 0, Math.PI * 2);
          ctx.fill();
        } else {
          ctx.fillRect(pp.x - sw / 2, pp.y - sh / 2, sw, sh);
        }
      }

      // Draw reference designator text
      if (viewState.scale > 3) {
        var tp = boardToScreen(fp.position[0], fp.position[1]);
        var fontSize = Math.max(8, Math.min(14, 1.2 * viewState.scale));
        ctx.font = fontSize + "px monospace";
        ctx.fillStyle = "#ffffff";
        ctx.textAlign = "center";
        ctx.textBaseline = "middle";
        ctx.fillText(fp.reference, tp.x, tp.y);
      }
    }
  }

  function drawHighlights() {
    if (highlightLocations.length === 0) return;

    for (var i = 0; i < highlightLocations.length; i++) {
      var loc = highlightLocations[i];
      var p = boardToScreen(loc.x_mm, loc.y_mm);

      // Pulsing circle
      var r = Math.max(8, 2 * viewState.scale);

      // Outer ring
      ctx.strokeStyle = "#e94560";
      ctx.lineWidth = 3;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r, 0, Math.PI * 2);
      ctx.stroke();

      // Inner ring
      ctx.strokeStyle = "#ffffff";
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r * 0.6, 0, Math.PI * 2);
      ctx.stroke();

      // Crosshair
      var ch = r * 1.5;
      ctx.strokeStyle = "rgba(233, 69, 96, 0.6)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(p.x - ch, p.y);
      ctx.lineTo(p.x + ch, p.y);
      ctx.moveTo(p.x, p.y - ch);
      ctx.lineTo(p.x, p.y + ch);
      ctx.stroke();
    }
  }

  // ---- Hit detection ----
  function pointToSegmentDist(px, py, x1, y1, x2, y2) {
    var dx = x2 - x1;
    var dy = y2 - y1;
    var lenSq = dx * dx + dy * dy;
    if (lenSq === 0) return Math.hypot(px - x1, py - y1);
    var t = Math.max(0, Math.min(1, ((px - x1) * dx + (py - y1) * dy) / lenSq));
    var projX = x1 + t * dx;
    var projY = y1 + t * dy;
    return Math.hypot(px - projX, py - projY);
  }

  // ---- Layer controls ----
  function buildLayerControls() {
    var container = document.getElementById("layer-controls");
    if (!container) return;

    var allLayers = Object.keys(visibleLayers);
    allLayers.forEach(function (layer) {
      var label = document.createElement("label");
      var cb = document.createElement("input");
      cb.type = "checkbox";
      cb.checked = visibleLayers[layer];
      cb.addEventListener("change", function () {
        visibleLayers[layer] = cb.checked;
        render();
      });

      var colorDot = document.createElement("span");
      colorDot.style.display = "inline-block";
      colorDot.style.width = "8px";
      colorDot.style.height = "8px";
      colorDot.style.borderRadius = "50%";
      colorDot.style.backgroundColor = layerColor(layer);
      colorDot.style.marginRight = "4px";
      colorDot.style.verticalAlign = "middle";

      label.appendChild(cb);
      label.appendChild(colorDot);
      label.appendChild(document.createTextNode(layer));
      container.appendChild(label);
    });
  }

  // ---- Violation list ----
  function buildViolationList() {
    var list = document.getElementById("violation-list");
    var filter = document.getElementById("violation-filter");
    if (!list) return;

    var violations = drc.violations || [];

    if (violations.length === 0) {
      list.innerHTML =
        '<div class="no-violations"><div class="check-mark">&#10003;</div>No DRC violations found</div>';
      return;
    }

    // Build filter options
    if (filter) {
      var types = {};
      violations.forEach(function (v) {
        types[v.type_str || v.type] = true;
      });
      Object.keys(types)
        .sort()
        .forEach(function (t) {
          var opt = document.createElement("option");
          opt.value = t;
          opt.textContent = t;
          filter.appendChild(opt);
        });
      filter.addEventListener("change", function () {
        renderViolationList(filter.value);
      });
    }

    renderViolationList("all");
  }

  function renderViolationList(filterType) {
    var list = document.getElementById("violation-list");
    if (!list) return;
    list.innerHTML = "";

    var violations = drc.violations || [];
    violations.forEach(function (v, idx) {
      if (filterType !== "all" && (v.type_str || v.type) !== filterType) return;

      var item = document.createElement("div");
      item.className = "violation-item";
      item.dataset.idx = idx;

      var sev = (v.severity || "error").toLowerCase();
      var typeEl = document.createElement("div");
      typeEl.className = "v-type " + sev;
      typeEl.textContent = (v.type_str || v.type) + " [" + sev + "]";

      var msgEl = document.createElement("div");
      msgEl.className = "v-message";
      msgEl.textContent = v.message || "";

      item.appendChild(typeEl);
      item.appendChild(msgEl);

      // Show location if available
      if (v.locations && v.locations.length > 0) {
        var locEl = document.createElement("div");
        locEl.className = "v-location";
        var loc = v.locations[0];
        locEl.textContent =
          "(" + loc.x_mm.toFixed(2) + ", " + loc.y_mm.toFixed(2) + ") mm" +
          (loc.layer ? " on " + loc.layer : "");
        item.appendChild(locEl);
      }

      item.addEventListener("click", function () {
        selectViolation(idx);
      });

      list.appendChild(item);
    });
  }

  function selectViolation(idx) {
    var violations = drc.violations || [];
    if (idx < 0 || idx >= violations.length) return;

    selectedViolation = idx;
    var v = violations[idx];

    // Update selection UI
    var items = document.querySelectorAll(".violation-item");
    items.forEach(function (el) {
      el.classList.toggle("selected", parseInt(el.dataset.idx) === idx);
    });

    // Scroll selected item into view
    var selectedEl = document.querySelector('.violation-item[data-idx="' + idx + '"]');
    if (selectedEl) {
      selectedEl.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }

    // Highlight locations on canvas
    highlightLocations = v.locations || [];

    // Pan to first location
    if (highlightLocations.length > 0) {
      var loc = highlightLocations[0];
      panToLocation(loc.x_mm, loc.y_mm);
    }

    render();
  }

  function panToLocation(bx, by) {
    var sp = boardToScreen(bx, by);
    var cx = canvas.width / 2;
    var cy = canvas.height / 2;
    viewState.offsetX += cx - sp.x;
    viewState.offsetY += cy - sp.y;
  }

  // ---- Stats ----
  function updateStats() {
    var statsEl = document.getElementById("report-stats");
    if (!statsEl) return;

    var parts = [];
    if (drc.error_count > 0) parts.push(drc.error_count + " errors");
    if (drc.warning_count > 0) parts.push(drc.warning_count + " warnings");
    if (parts.length === 0) parts.push("No violations");
    var fpCount = (pcb.footprints || []).length;
    var segCount = (pcb.segments || []).length;
    parts.push(fpCount + " footprints");
    parts.push(segCount + " traces");
    statsEl.textContent = parts.join(" | ");
  }

  // ---- Zoom controls ----
  window.zoomIn = function () {
    var cx = canvas.width / 2;
    var cy = canvas.height / 2;
    var factor = 1.3;
    viewState.offsetX = cx - (cx - viewState.offsetX) * factor;
    viewState.offsetY = cy - (cy - viewState.offsetY) * factor;
    viewState.scale *= factor;
    render();
  };

  window.zoomOut = function () {
    var cx = canvas.width / 2;
    var cy = canvas.height / 2;
    var factor = 1 / 1.3;
    viewState.offsetX = cx - (cx - viewState.offsetX) * factor;
    viewState.offsetY = cy - (cy - viewState.offsetY) * factor;
    viewState.scale *= factor;
    render();
  };

  window.zoomFit = function () {
    fitBoard();
    render();
  };

  // ---- Start ----
  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }
})();
