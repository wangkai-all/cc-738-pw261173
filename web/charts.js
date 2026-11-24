/* 轻量 SVG 图表库（无第三方依赖）：折线对照图、核柱状图。
   配色读取 styles.css 中的设计令牌，支持深浅色重绘。 */
(function () {
  const SVGNS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs = {}, parent) {
    const node = document.createElementNS(SVGNS, tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (v === null || v === undefined) continue;
      if (k === "text") node.textContent = v;
      else if ((k === "fill" || k === "stroke") && String(v).includes("var(")) node.style[k] = v;
      else node.setAttribute(k, v);
    }
    if (parent) parent.appendChild(node);
    return node;
  }

  const Charts = {};
  Charts.var = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  Charts.seriesColor = (i) => Charts.var(`--series-${((i % 8) + 8) % 8 + 1}`);

  Charts.fmt = function (v, digits) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    const a = Math.abs(v);
    let d = digits;
    if (d === undefined) {
      if (a >= 1000) d = 0;
      else if (a >= 100) d = 1;
      else if (a >= 10) d = 1;
      else d = 2;
    }
    return v.toLocaleString("zh-CN", { minimumFractionDigits: d, maximumFractionDigits: d });
  };
  Charts.fmtCompact = function (v) {
    if (v === null || v === undefined || !isFinite(v)) return "–";
    const a = Math.abs(v);
    if (a >= 10000) return (v / 1000).toFixed(0) + "k";
    if (a >= 1000) return (v / 1000).toFixed(1) + "k";
    if (a >= 100) return v.toFixed(0);
    if (a >= 10) return v.toFixed(1);
    return v.toFixed(1);
  };

  let ttEl = null;
  function tt() { if (!ttEl) ttEl = document.getElementById("tooltip"); return ttEl; }
  Charts.hideTooltip = function () { const t = tt(); if (t) t.hidden = true; };
  Charts.showTooltip = function (html, cx, cy) {
    const t = tt();
    if (!t) return;
    t.innerHTML = html;
    t.hidden = false;
    const pad = 12;
    const w = t.offsetWidth, h = t.offsetHeight;
    let left = cx + 15;
    let top = cy + 15;
    if (left + w > window.innerWidth - 8) left = cx - w - 15;
    if (top + h > window.innerHeight - 8) top = cy - h - 15;
    t.style.left = Math.max(8, left) + "px";
    t.style.top = Math.max(8, top) + "px";
  };

  function niceTicks(min, max, count) {
    if (!isFinite(min) || !isFinite(max) || min === max) {
      const c = isFinite(min) ? min : 0;
      return { min: c - 1, max: c + 1, ticks: [c - 1, c, c + 1] };
    }
    const span = max - min;
    const step0 = span / Math.max(1, count);
    const mag = Math.pow(10, Math.floor(Math.log10(step0)));
    const norm = step0 / mag;
    let step;
    if (norm < 1.5) step = 1 * mag;
    else if (norm < 3) step = 2 * mag;
    else if (norm < 7) step = 5 * mag;
    else step = 10 * mag;
    const nmin = Math.floor(min / step) * step;
    const nmax = Math.ceil(max / step) * step;
    const ticks = [];
    for (let v = nmin; v <= nmax + step * 0.5; v += step) ticks.push(+v.toFixed(10));
    return { min: nmin, max: nmax, ticks };
  }

  function finite(v) { return typeof v === "number" && isFinite(v); }

  function linePath(points) {
    if (!points.length) return "";
    return "M" + points.map((p, i) => `${i ? "L" : ""}${p[0].toFixed(1)},${p[1].toFixed(1)}`).join("");
  }

  function segments(values, x, y) {
    const out = [];
    let cur = [];
    values.forEach((v, i) => {
      if (!finite(v)) { if (cur.length) { out.push(cur); cur = []; } return; }
      cur.push([x(i), y(v)]);
    });
    if (cur.length) out.push(cur);
    return out;
  }

  /* cfg 见文件头注释 */
  Charts.lineChart = function (container, cfg) {
    container.innerHTML = "";
    const width = Math.max(320, container.clientWidth || 820);
    const height = cfg.height || 340;
    const M = { t: 16, r: 18, b: 38, l: 54 };
    const pw = width - M.l - M.r;
    const ph = height - M.t - M.b;
    const n = cfg.n;

    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" }, container);

    // 域
    let lo = Infinity, hi = -Infinity;
    cfg.series.forEach((s) => {
      if (s.hidden) return;
      (s.values || []).forEach((v) => { if (finite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
      (s.partial || []).forEach((v) => { if (finite(v)) { lo = Math.min(lo, v); hi = Math.max(hi, v); } });
    });
    (cfg.markers || []).forEach((m) => { if (!m.hidden && finite(m.value)) { lo = Math.min(lo, m.value); hi = Math.max(hi, m.value); } });
    if (!isFinite(lo)) { lo = 0; hi = 1; }
    if (cfg.zeroCenter) {
      const m = Math.max(Math.abs(lo), Math.abs(hi), 1e-6);
      lo = -m; hi = m;
    } else if (cfg.includeZero !== false) {
      lo = Math.min(lo, 0);
    }
    if (lo === hi) { lo -= 1; hi += 1; }
    const pad = (hi - lo) * 0.07;
    let yLo = cfg.zeroCenter ? lo : lo - pad;
    let yHi = hi + pad;
    if (!cfg.zeroCenter && cfg.includeZero !== false) yLo = Math.min(yLo, 0);
    const tk = niceTicks(yLo, yHi, 5);
    yLo = tk.min; yHi = tk.max;

    const stepX = n > 1 ? pw / (n - 1) : 0;
    const X = (i) => M.l + (n > 1 ? i * stepX : pw / 2);
    const Y = (v) => M.t + ph - ((v - yLo) / (yHi - yLo)) * ph;
    const yBase = Y(0 >= yLo && 0 <= yHi ? 0 : yLo);

    // 带区
    (cfg.bands || []).forEach((b) => {
      const a = Math.max(0, b.from), z = Math.min(n - 1, b.to);
      if (z < a) return;
      const bx = X(a) - stepX / 2;
      const bw = Math.max(2, (z - a) * stepX + stepX);
      if (b.kind === "partial") {
        svgEl("rect", { x: bx, y: M.t, width: bw, height: ph, fill: "var(--partial-band)" }, svg);
        svgEl("rect", { x: bx, y: M.t, width: bw, height: ph, fill: "url(#hatchPartial)", opacity: 0.5 }, svg);
      } else {
        svgEl("rect", { x: bx, y: M.t, width: bw, height: ph, fill: `var(--${b.kind}-band)` }, svg);
      }
      if (b.label && bw > 46) {
        svgEl("text", { x: bx + 6, y: M.t + 13, class: "axis-label", text: b.label }, svg);
      }
    });

    // 网格 + y 轴
    tk.ticks.forEach((v) => {
      const y = Y(v);
      svgEl("line", { x1: M.l, x2: M.l + pw, y1: y, y2: y, class: v === 0 ? "axis-line" : "grid-line" }, svg);
      svgEl("text", { x: M.l - 7, y: y + 3.5, "text-anchor": "end", class: "tick-text", text: Charts.fmtCompact(v) }, svg);
    });

    // x 轴刻度
    const tickCount = Math.max(2, Math.min(9, Math.floor(width / 120)));
    for (let k = 0; k < tickCount; k++) {
      const i = Math.round((k * (n - 1)) / (tickCount - 1));
      const label = cfg.xTick ? cfg.xTick(i) : i;
      svgEl("line", { x1: X(i), x2: X(i), y1: M.t + ph, y2: M.t + ph + 4, class: "axis-line" }, svg);
      svgEl("text", { x: X(i), y: M.t + ph + 18, "text-anchor": "middle", class: "tick-text", text: label }, svg);
    }
    svgEl("line", { x1: M.l, x2: M.l + pw, y1: M.t + ph, y2: M.t + ph, class: "axis-line" }, svg);

    // 序列
    cfg.series.forEach((s, si) => {
      const color = s.color || Charts.seriesColor(si);
      if (s.hidden) return;
      // 面积
      if (s.fill) {
        segments(s.values, X, Y).forEach((pts) => {
          const d = linePath(pts) + `L${pts[pts.length - 1][0].toFixed(1)},${yBase} L${pts[0][0].toFixed(1)},${yBase} Z`;
          svgEl("path", { d, fill: color, "fill-opacity": s.fillOpacity || 0.12, stroke: "none" }, svg);
        });
      }
      // 部分抽头（残波下界）虚线
      if (s.partial) {
        segments(s.partial, X, Y).forEach((pts) => {
          svgEl("path", {
            d: linePath(pts), fill: "none", stroke: color, "stroke-width": 1.8,
            "stroke-dasharray": "4 4", "stroke-opacity": 0.6, "stroke-linecap": "round",
          }, svg);
        });
      }
      segments(s.values, X, Y).forEach((pts) => {
        svgEl("path", {
          d: linePath(pts), fill: "none", stroke: color, "stroke-width": s.width || 2.2,
          "stroke-linejoin": "round", "stroke-linecap": "round",
          "stroke-dasharray": s.dash || null, opacity: s.opacity == null ? 1 : s.opacity,
        }, svg);
      });
    });

    // 标记点（洪峰）
    (cfg.markers || []).forEach((mk) => {
      if (!finite(mk.value)) return;
      const cx = X(mk.index), cy = Y(mk.value);
      svgEl("circle", { cx, cy, r: 5, fill: mk.color || "var(--critical)", class: "hover-dot" }, svg);
      svgEl("circle", { cx, cy, r: 9, fill: "none", stroke: mk.color || "var(--critical)", "stroke-opacity": 0.35, "stroke-width": 2 }, svg);
      if (mk.label) svgEl("text", { x: cx, y: cy - 12, "text-anchor": "middle", class: "peak-label", text: mk.label }, svg);
    });

    // 悬浮层
    const hoverLine = svgEl("line", { y1: M.t, y2: M.t + ph, class: "crosshair", visibility: "hidden" }, svg);
    const dotGroup = svgEl("g", {}, svg);
    const dots = cfg.series.map((s, si) => svgEl("circle", {
      r: 4, fill: s.color || Charts.seriesColor(si), class: "hover-dot", visibility: "hidden",
    }, dotGroup));

    function hideHover() {
      hoverLine.setAttribute("visibility", "hidden");
      dots.forEach((d) => d.setAttribute("visibility", "hidden"));
      Charts.hideTooltip();
      if (cfg.onHoverIndex) cfg.onHoverIndex(null);
    }

    const overlay = svgEl("rect", { x: M.l, y: M.t, width: pw, height: ph, fill: "transparent" }, svg);
    overlay.style.cursor = "crosshair";
    overlay.addEventListener("pointermove", (ev) => {
      const rect = svg.getBoundingClientRect();
      const mx = ((ev.clientX - rect.left) / rect.width) * width;
      let i = Math.round((mx - M.l) / Math.max(stepX, 1e-6));
      i = Math.max(0, Math.min(n - 1, i));
      const cx = X(i);
      hoverLine.setAttribute("x1", cx); hoverLine.setAttribute("x2", cx);
      hoverLine.setAttribute("visibility", "visible");

      let rows = "";
      cfg.series.forEach((s, si) => {
        const d = dots[si];
        if (s.hidden) { d.setAttribute("visibility", "hidden"); return; }
        let v = s.values ? s.values[i] : null;
        let tag = "";
        if (!finite(v) && s.partial && finite(s.partial[i])) {
          v = s.partial[i]; tag = '<span class="pill partial">部分</span>';
        }
        const color = s.color || Charts.seriesColor(si);
        if (finite(v)) {
          d.setAttribute("cx", cx); d.setAttribute("cy", Y(v)); d.setAttribute("visibility", "visible");
          rows += `<div class="tt-row"><span class="name"><span class="legend-dot" style="background:${color}"></span>${s.name} ${tag}</span><span class="val">${Charts.fmt(v)}</span></div>`;
        } else {
          d.setAttribute("visibility", "hidden");
          rows += `<div class="tt-row"><span class="name"><span class="legend-dot" style="background:${color}"></span>${s.name}</span><span class="val muted">缺测</span></div>`;
        }
      });
      const fullTime = cfg.xFull ? cfg.xFull(i) : (cfg.labels ? cfg.labels[i] : `#${i}`);
      const note = cfg.noteAt ? cfg.noteAt(i) : "";
      Charts.showTooltip(
        `<div class="tt-time">${fullTime} <span class="muted">#${i}</span></div>${rows}${note ? `<div class="tt-note">${note}</div>` : ""}`,
        ev.clientX, ev.clientY);
      if (cfg.onHoverIndex) cfg.onHoverIndex(i);
    });
    overlay.addEventListener("pointerleave", hideHover);

    if (cfg.empty) {
      svgEl("text", { x: width / 2, y: height / 2, "text-anchor": "middle", class: "axis-label", text: cfg.empty }, svg);
    }
  };

  /* 核权重柱状图（带悬浮提示） */
  Charts.kernelBars = function (container, kernel, dtMinutes) {
    container.innerHTML = "";
    const weights = kernel.weights || [];
    const l0 = kernel.lag_min || 0;
    const width = Math.max(280, container.clientWidth || 460);
    const height = 168;
    const M = { t: 16, r: 8, b: 40, l: 40 };
    const pw = width - M.l - M.r, ph = height - M.t - M.b;
    const k = weights.length;
    const bw = pw / k * 0.72;
    const gap = pw / k;
    const ymax = Math.max(0.05, ...weights) * 1.18;
    const Y = (v) => M.t + ph - (v / ymax) * ph;
    const svg = svgEl("svg", { viewBox: `0 0 ${width} ${height}`, width, height, role: "img" }, container);
    svgEl("line", { x1: M.l, x2: M.l + pw, y1: M.t + ph, y2: M.t + ph, class: "axis-line" }, svg);
    [0, ymax / 2, ymax].forEach((v) => {
      svgEl("line", { x1: M.l, x2: M.l + pw, y1: Y(v), y2: Y(v), class: "grid-line" }, svg);
      svgEl("text", { x: M.l - 5, y: Y(v) + 3, "text-anchor": "end", class: "tick-text", text: v.toFixed(2) }, svg);
    });
    weights.forEach((w, j) => {
      const lag = l0 + j;
      const x = M.l + j * gap + (gap - bw) / 2;
      const h = (w / ymax) * ph;
      const rect = svgEl("rect", {
        x, y: Y(w), width: bw, height: Math.max(0.5, h), rx: 3,
        fill: "var(--series-1)", class: "bar",
      }, svg);
      rect.addEventListener("pointermove", (ev) => {
        Charts.showTooltip(
          `<div class="tt-time">滞后 ${lag} 个样本</div>
           <div class="tt-row"><span class="name">小时</span><span class="val">${(lag * dtMinutes / 60).toFixed(2)} h</span></div>
           <div class="tt-row"><span class="name">权重</span><span class="val">${Charts.fmt(w, 4)}</span></div>`,
          ev.clientX, ev.clientY);
      });
      rect.addEventListener("pointerleave", Charts.hideTooltip);
      if (k <= 16 || j % 2 === 0) {
        svgEl("text", { x: x + bw / 2, y: M.t + ph + 15, "text-anchor": "middle", class: "tick-text", text: lag }, svg);
        if (k <= 10) {
          svgEl("text", { x: x + bw / 2, y: M.t + ph + 29, "text-anchor": "middle", class: "tick-text", style: "font-size:9.5px", text: (lag * dtMinutes / 60).toFixed(1) + "h" }, svg);
        }
      }
    });
    svgEl("text", { x: M.l + pw / 2, y: height - 4, "text-anchor": "middle", class: "axis-label", text: "滞后样本数（下） / 小时（h）" }, svg);
  };

  // 注入斜纹 pattern
  function injectDefs() {
    if (document.getElementById("riverDefs")) return;
    const wrap = document.createElement("div");
    wrap.innerHTML = `<svg id="riverDefs" width="0" height="0" style="position:absolute">
      <defs>
        <pattern id="hatchPartial" patternUnits="userSpaceOnUse" width="8" height="8" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="8" style="stroke:var(--warning)" stroke-width="2" opacity="0.55"/>
        </pattern>
        <pattern id="hatchMissing" patternUnits="userSpaceOnUse" width="7" height="7" patternTransform="rotate(45)">
          <line x1="0" y1="0" x2="0" y2="7" style="stroke:var(--critical)" stroke-width="1.6" opacity="0.5"/>
        </pattern>
      </defs>
    </svg>`;
    document.body.appendChild(wrap.firstElementChild);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", injectDefs);
  else injectDefs();

  window.Charts = Charts;
})();
