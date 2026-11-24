/* 河网卷积分析台前端逻辑（无框架、无依赖）。 */
"use strict";

const S = {
  sid: null,
  state: null,
  tab: "reach",
  edgeId: null,
  target: null,
  peakSource: null,
  peakTarget: null,
  cal: { i0: 0, i1: 1 },
  exclude: [],
  reachData: null,
  contribData: null,
  residualData: null,
  peakData: null,
  reachHidden: {},
  contribHidden: {},
  residualHidden: {},
  reachSeq: 0,
  contribSeq: 0,
  residualSeq: 0,
  peakSeq: 0,
  sessionSeq: 0,
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

function esc(v) {
  return String(v == null ? "" : v)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}
function pad2(n) { return String(n).padStart(2, "0"); }

async function handle(r) {
  let j = null;
  try { j = await r.json(); } catch (_) { /* noop */ }
  if (!r.ok || (j && j.ok === false)) {
    const er = (j && j.error) || { code: "HTTP_" + r.status, message: "请求失败（" + r.status + "）" };
    const e = new Error(er.message);
    e.code = er.code; e.details = er.details || {};
    throw e;
  }
  return j;
}
async function getJSON(url) { return handle(await fetch(url)); }
async function postJSON(path, body = {}) {
  const r = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ sid: S.sid, ...body }),
  });
  return handle(r);
}
function withSid(path) { return path + (path.includes("?") ? "&" : "?") + "sid=" + encodeURIComponent(S.sid); }

function dateAt(i) {
  const g = S.state.grid;
  const d = new Date(g.start);
  d.setMinutes(d.getMinutes() + i * g.dt_minutes);
  return d;
}
function tShort(i) {
  if (!S.state) return "#" + i;
  const d = dateAt(i);
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function tFull(i) {
  const d = dateAt(i);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}
function isoText(v) { return v ? esc(String(v).replace("T", " ").slice(0, 16)) : "–"; }
function fmtMetric(v, d = 3) {
  if (v === null || v === undefined || !isFinite(v)) return "–";
  if (Math.abs(v) > 0 && Math.abs(v) < 1e-3) return v.toExponential(1);
  return Charts.fmt(v, d);
}
function nullRuns(values) {
  const runs = [];
  let a = null;
  values.forEach((v, i) => {
    const bad = v === null || v === undefined || (typeof v === "number" && !isFinite(v));
    if (bad && a === null) a = i;
    if ((!bad || i === values.length - 1) && a !== null) {
      const b = bad ? i : i - 1;
      runs.push([a, b]); a = null;
    }
  });
  return runs;
}

let toastTimer = null;
function toast(msg, kind = "") {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast " + kind;
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, 3200);
}

function cycleHtml(details) {
  const cyc = details && details.cycle;
  if (!cyc || !cyc.length) return "";
  return `<div class="cycle-chain">` + cyc.map((n, i) =>
    `<span class="cycle-node">${esc(stationName(n))}</span>` +
    (i < cyc.length - 1 ? `<span class="cycle-arrow">→</span>` : "")).join("") + `</div>`;
}
function errorPanel(e, title = "无法完成计算") {
  return `<div class="error-panel"><h2>${esc(title)} · ${esc(e.code || "ERROR")}</h2>
    <div>${esc(e.message)}</div>${cycleHtml(e.details)}</div>`;
}
function stationName(id) {
  const s = S.state && S.state.stations.find((x) => String(x.id) === String(id));
  return s ? (s.name || s.id) : id;
}

/* ----------------------------- 初始化 ----------------------------- */
async function init() {
  bindChrome();
  initTheme();
  try {
    const j = await getJSON("/api/examples");
    const sel = $("#exampleSelect");
    sel.innerHTML = j.examples.map((e) => `<option value="${esc(e.id)}">${esc(e.name)}</option>`).join("");
    const preferred = j.examples.find((e) => e.id === "conservation-tree") || j.examples[0];
    if (preferred) sel.value = preferred.id;
    await openExample(sel.value);
  } catch (e) {
    $("#globalError").innerHTML = errorPanel(e, "初始化失败：请确认服务已启动");
  }
}

function bindChrome() {
  $("#openExampleBtn").addEventListener("click", () => openExample($("#exampleSelect").value));
  $("#exampleSelect").addEventListener("change", () => openExample($("#exampleSelect").value));
  $("#themeToggle").addEventListener("click", toggleTheme);

  $$(".tab").forEach((btn) => btn.addEventListener("click", () => switchTab(btn.dataset.tab)));

  $("#importBtn").addEventListener("click", () => { $("#importModal").hidden = false; });
  $("#importClose").addEventListener("click", closeImport);
  $("#importCancel").addEventListener("click", closeImport);
  $("#importModal").addEventListener("click", (e) => { if (e.target.id === "importModal") closeImport(); });
  $("#flowsFile").addEventListener("change", (e) => readFileInto(e.target.files[0], "#flowsCsv", "#flowsFileName"));
  $("#netFile").addEventListener("change", (e) => readFileInto(e.target.files[0], "#networkJson", "#netFileName"));
  $("#importSubmit").addEventListener("click", submitImport);

  let rz;
  window.addEventListener("resize", () => { clearTimeout(rz); rz = setTimeout(rerenderVisibleCharts, 180); });
}

function closeImport() { $("#importModal").hidden = true; $("#importError").hidden = true; }

function readFileInto(file, textareaSel, nameSel) {
  if (!file) return;
  $(nameSel).textContent = file.name;
  const rd = new FileReader();
  rd.onload = () => { $(textareaSel).value = String(rd.result || ""); };
  rd.readAsText(file);
}

async function submitImport() {
  const errBox = $("#importError");
  errBox.hidden = true;
  let network = {};
  const netText = $("#networkJson").value.trim();
  if (netText) {
    try { network = JSON.parse(netText); }
    catch (e) { errBox.textContent = "network JSON 解析失败：" + e.message; errBox.hidden = false; return; }
  }
  const flows = $("#flowsCsv").value;
  if (!flows.trim()) { errBox.textContent = "请先提供 flows.csv 内容或选择文件。"; errBox.hidden = false; return; }
  const dtVal = $("#impDt").value;
  const btn = $("#importSubmit");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> 解析中';
  const seq = ++S.sessionSeq;
  try {
    const j = await postJSON("/api/import", {
      flows_csv: flows, network_json: network,
      name: $("#impName").value || "导入项目",
      dt_minutes: dtVal ? Number(dtVal) : null,
    });
    if (seq !== S.sessionSeq) return;
    S.sid = j.sid; S.state = j.state;
    initSelection(); renderShell(); closeImport(); switchTab("reach");
    toast("导入成功", "good");
  } catch (e) {
    errBox.textContent = e.message; errBox.hidden = false;
  } finally {
    btn.disabled = false; btn.textContent = "解析并打开";
  }
}

async function openExample(id) {
  const seq = ++S.sessionSeq;
  try {
    const j = await postJSON("/api/session/example", { example_id: id });
    if (seq !== S.sessionSeq) return;
    S.sid = j.sid; S.state = j.state;
    initSelection();
    renderShell();
    switchTab("reach");
    toast("已打开：" + j.state.name, "good");
  } catch (e) {
    if (seq === S.sessionSeq) $("#globalError").innerHTML = errorPanel(e, "样例打开失败");
  }
}

function initSelection() {
  const st = S.state;
  S.edgeId = st.selected_edge || (st.edges[0] && st.edges[0].id) || null;
  S.target = st.selected_target || st.stations[st.stations.length - 1].id;
  if (!st.stations.some((x) => x.id === S.target)) S.target = st.stations[st.stations.length - 1].id;
  const hasIncoming = new Set(st.edges.map((e) => e.to));
  const source = st.stations.map((x) => x.id).find((id) => !hasIncoming.has(id)) || st.stations[0].id;
  S.peakSource = source;
  S.peakTarget = S.target;
  S.cal = { i0: st.calibration[0], i1: st.calibration[1] };
  S.exclude = [];
  S.reachData = S.contribData = S.residualData = S.peakData = null;
  S.reachHidden = S.contribHidden = S.residualHidden = {};
}

/* ----------------------------- 主题 ----------------------------- */
function initTheme() {
  const saved = localStorage.getItem("rivernet-theme");
  if (saved) document.documentElement.dataset.theme = saved;
  else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) {
    document.documentElement.dataset.theme = "dark";
  }
}
function toggleTheme() {
  const root = document.documentElement;
  root.dataset.theme = root.dataset.theme === "dark" ? "light" : "dark";
  localStorage.setItem("rivernet-theme", root.dataset.theme);
  setTimeout(rerenderVisibleCharts, 30);
}

/* ----------------------------- 外壳渲染 ----------------------------- */
async function refreshState() {
  const j = await getJSON(withSid("/api/state"));
  S.state = j.state;
  renderShell();
}

function renderShell() {
  const st = S.state;
  if (!st) return;
  const badge = $("#dagBadge");
  if (st.ok) { badge.textContent = "DAG 有效"; badge.className = "badge good"; }
  else { badge.textContent = "存在环路"; badge.className = "badge critical"; }
  $("#schemeCount").textContent = st.schemes.length;
  if (!st.last_report) {
    const banner = $("#recomputeBanner");
    banner.hidden = true;
    banner.innerHTML = "";
  }
  renderGlobalError();
  renderNetwork();
  renderEdgeList();
}

function renderGlobalError() {
  const box = $("#globalError");
  if (S.state.ok) { box.innerHTML = ""; return; }
  const cyc = S.state.validation.errors.find((x) => x.code === "CYCLE_DETECTED");
  const others = S.state.validation.errors.filter((x) => x.code !== "CYCLE_DETECTED");
  box.innerHTML = `<div class="error-panel">
    <h2>河网不是有向无环图（DAG）</h2>
    <div>${esc(cyc ? cyc.message : "河网结构校验未通过，传播计算已被拒绝。")}</div>
    ${cycleHtml(cyc)}
    ${others.length ? `<ul>${others.map((o) => `<li>${esc(o.message)}</li>`).join("")}</ul>` : ""}
    <div class="muted" style="margin-top:8px">请返回数据文件删除反向边；左侧环路已用红色标出。此样例用于演示错误处理，不会输出任何“计算结果”。</div>
  </div>`;
}

function renderEdgeList() {
  const st = S.state;
  const wrap = $("#edgeList");
  if (!st.edges.length) { wrap.innerHTML = `<div class="empty">没有河段</div>`; return; }
  wrap.innerHTML = st.edges.map((e) => {
    const sum = e.weights.reduce((a, b) => a + b, 0);
    const sel = e.id === S.edgeId ? "selected" : "";
    return `<button class="edge-item ${sel}" data-edge="${esc(e.id)}">
      <div class="row1"><span>${esc(stationName(e.from))} → ${esc(stationName(e.to))}</span><span class="eid">${esc(e.id)}</span></div>
      <div class="row2">滞后 ${e.lag_min}–${e.lag_max} 样本（${e.lag_min_hours}–${e.lag_max_hours} h）· Σw=${sum.toFixed(3)}</div>
    </button>`;
  }).join("");
  $$(".edge-item", wrap).forEach((btn) => btn.addEventListener("click", () => selectEdge(btn.dataset.edge)));
}

function selectEdge(id) {
  S.edgeId = id;
  S.exclude = [];
  S.cal = { i0: S.state.calibration[0], i1: S.state.calibration[1] };
  renderNetwork(); renderEdgeList();
  if (S.tab !== "reach") switchTab("reach");
  else refreshReach();
}

/* ----------------------------- DAG 图 ----------------------------- */
function renderNetwork() {
  const st = S.state;
  const container = $("#networkGraph");
  if (!st) return;
  const NW = 108, NH = 48, HG = 46, VG = 14, PAD = 12;
  const ids = st.stations.map((x) => x.id);
  const out = {}, inn = {};
  ids.forEach((id) => { out[id] = []; inn[id] = []; });
  st.edges.forEach((e) => { if (out[e.from]) out[e.from].push(e); if (inn[e.to]) inn[e.to].push(e); });

  const indeg = {};
  ids.forEach((id) => { indeg[id] = inn[id].length; });
  const q = ids.filter((id) => indeg[id] === 0);
  const order = [];
  while (q.length) {
    const u = q.shift(); order.push(u);
    out[u].forEach((e) => { indeg[e.to] -= 1; if (indeg[e.to] === 0) q.push(e.to); });
  }
  const layer = {};
  order.forEach((u) => {
    layer[u] = inn[u].length ? Math.max(...inn[u].map((e) => layer[e.from] ?? 0)) + 1 : 0;
  });
  ids.filter((id) => !(id in layer)).forEach((id, i) => { layer[id] = i; });

  const maxLayer = Math.max(0, ...ids.map((id) => layer[id]));
  const cols = {};
  ids.forEach((id) => { (cols[layer[id]] = cols[layer[id]] || []).push(id); });
  const maxCount = Math.max(...Object.values(cols).map((c) => c.length), 1);
  const W = PAD * 2 + maxLayer * (NW + HG) + NW;
  const H = Math.max(150, PAD * 2 + maxCount * NH + (maxCount - 1) * VG);
  const X = (l) => PAD + l * (NW + HG);
  const pos = {};
  Object.entries(cols).forEach(([l, arr]) => {
    const k = arr.length;
    const colH = k * NH + (k - 1) * VG;
    const y0 = PAD + (H - PAD * 2 - colH) / 2;
    arr.forEach((id, j) => { pos[id] = { x: X(+l), y: y0 + j * (NH + VG) }; });
  });

  let cycleNodes = new Set(), cycleEdgeKeys = new Set();
  const cycErr = st.validation.errors.find((x) => x.code === "CYCLE_DETECTED");
  if (cycErr && cycErr.cycle) {
    cycErr.cycle.forEach((n) => cycleNodes.add(n));
    for (let i = 0; i < cycErr.cycle.length - 1; i++) cycleEdgeKeys.add(cycErr.cycle[i] + "->" + cycErr.cycle[i + 1]);
  }

  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svg.setAttribute("width", W); svg.setAttribute("height", H);
  svg.innerHTML = `<defs>
    <marker id="arrowNormal" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="userSpaceOnUse">
      <path d="M0,0 L7,3 L0,6 Z" style="fill:var(--axis)"></path></marker>
    <marker id="arrowSel" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="userSpaceOnUse">
      <path d="M0,0 L7,3 L0,6 Z" style="fill:var(--series-1)"></path></marker>
    <marker id="arrowCyc" markerWidth="9" markerHeight="9" refX="7" refY="3" orient="auto" markerUnits="userSpaceOnUse">
      <path d="M0,0 L7,3 L0,6 Z" style="fill:var(--critical)"></path></marker>
  </defs>`;

  st.edges.forEach((e) => {
    const p = pos[e.from], z = pos[e.to];
    if (!p || !z) return;
    const x1 = p.x + NW, y1 = p.y + NH / 2, x2 = z.x - 3, y2 = z.y + NH / 2;
    const dx = Math.max(26, Math.abs(x2 - x1) / 2);
    const d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    const isCyc = cycleEdgeKeys.has(e.from + "->" + e.to);
    const isSel = e.id === S.edgeId;
    const cls = isCyc ? "net-edge cycle-edge" : isSel ? "net-edge selected" : "net-edge";
    const marker = isCyc ? "url(#arrowCyc)" : isSel ? "url(#arrowSel)" : "url(#arrowNormal)";
    const path = svgEl("path", { d, class: cls, "marker-end": marker }, svg);
    path.addEventListener("click", () => selectEdge(e.id));
    const mx = (x1 + x2) / 2, my = (y1 + y2) / 2 - 7;
    const txt = svgEl("text", { x: mx, y: my, "text-anchor": "middle", class: "edge-label" }, svg);
    txt.textContent = `${e.lag_min}–${e.lag_max}`;
    const title = document.createElementNS("http://www.w3.org/2000/svg", "title");
    title.textContent = `${e.id}: 滞后 ${e.lag_min}–${e.lag_max} 样本`;
    path.appendChild(title);
  });

  ids.forEach((id) => {
    const p = pos[id];
    const s = st.stations.find((x) => x.id === id);
    const name = s.name || id;
    const show = name.length > 8 ? name.slice(0, 7) + "…" : name;
    const related = S.edgeId && st.edges.find((e) => e.id === S.edgeId && (e.from === id || e.to === id));
    const cls = "net-node" + (S.edgeId && related ? " selected" : "") + (cycleNodes.has(id) ? " cycle" : "");
    const g = svgEl("g", { class: cls, transform: `translate(${p.x},${p.y})` }, svg);
    const rect = svgEl("rect", { width: NW, height: NH, rx: 9 }, g);
    rect.addEventListener("click", () => {
      const e = st.edges.find((x) => x.from === id) || st.edges.find((x) => x.to === id);
      if (e) selectEdge(e.id);
    });
    const t1 = svgEl("text", { x: NW / 2, y: 19, "text-anchor": "middle" }, g); t1.textContent = show;
    const t2 = svgEl("text", { x: NW / 2, y: 35, "text-anchor": "middle", class: "sub" }, g); t2.textContent = id;
  });

  container.innerHTML = "";
  container.appendChild(svg);
  const errs = st.validation.errors.filter((x) => x.code !== "CYCLE_DETECTED");
  $("#networkErrors").innerHTML = errs.length
    ? errs.map((x) => `<div class="inline-error" style="position:static">${esc(x.message)}</div>`).join("")
    : "";
}
function svgEl(tag, attrs, parent) {
  const n = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}

/* ----------------------------- 标签切换 ----------------------------- */
function switchTab(tab) {
  S.tab = tab;
  $$(".tab").forEach((b) => b.classList.toggle("active", b.dataset.tab === tab));
  ["reach", "peaks", "contrib", "residual", "schemes"].forEach((name) => {
    const p = $("#panel-" + name);
    p.classList.toggle("active", name === tab);
    if (name === tab) p.hidden = false; else p.hidden = true;
  });
  if (tab === "reach") refreshReach();
  if (tab === "peaks") renderPeaksTab();
  if (tab === "contrib") renderContribTab();
  if (tab === "residual") renderResidualTab();
  if (tab === "schemes") renderSchemesTab();
}

function rerenderVisibleCharts() {
  if (!S.state) return;
  if (S.tab === "reach" && S.reachData) { renderKernelChart(); renderReachCharts(); }
  if (S.tab === "peaks" && S.peakData) renderPeakCharts();
  if (S.tab === "contrib" && S.contribData) renderContribCharts();
  if (S.tab === "residual" && S.residualData) renderResidualCharts();
}

function showReport(report, prefix) {
  if (!report) return;
  const box = $("#recomputeBanner");
  box.hidden = false;
  box.innerHTML = `⚡ ${esc(prefix || "已更新")}：仅重算下游边
    <code>${report.recomputed_edges.map(esc).join(", ")}</code>；
    重算节点 <code>${report.recomputed_nodes.map(esc).join(", ")}</code>；
    保留缓存的上游/旁支边 <code>${report.cached_edges.length ? report.cached_edges.map(esc).join(", ") : "（无）"}</code>。`;
}

/* ========================= ① 河段校准 ========================= */
async function refreshReach() {
  const panel = $("#panel-reach");
  if (!S.edgeId) { panel.innerHTML = `<div class="empty">请先在左侧选择一个河段。</div>`; return; }
  const seq = ++S.reachSeq;
  const edgeId = S.edgeId;
  panel.innerHTML = `<div class="block"><span class="spinner"></span> 正在计算河段视图…</div>`;
  try {
    const j = await getJSON(withSid("/api/edge") + "&edge_id=" + encodeURIComponent(edgeId));
    if (seq !== S.reachSeq || edgeId !== S.edgeId) return;
    S.reachData = j.view;
    renderReachLayout();
  } catch (e) {
    if (seq === S.reachSeq && edgeId === S.edgeId) panel.innerHTML = errorPanel(e);
  }
}

function renderReachLayout() {
  const d = S.reachData, st = S.state, e = d.edge;
  const dt = st.grid.dt_minutes;
  const stepH = dt / 60;
  const panel = $("#panel-reach");
  panel.innerHTML = `
  <div class="block">
    <h3>河段校准与滞后约束 <span class="tag">${esc(d.from_station.name)} → ${esc(d.to_station.name)} · ${esc(e.id)} · dt=${dt} 分钟</span></h3>
    <div class="control-grid">
      <label class="field">最小滞后（小时）
        <input type="number" id="lagMinH" min="0" step="${stepH}" value="${e.lag_min_hours}">
      </label>
      <label class="field">最大滞后（小时）
        <input type="number" id="lagMaxH" min="0" step="${stepH}" value="${e.lag_max_hours}">
      </label>
      <div class="field"><span class="field-label">换算为样本</span>
        <div id="lagSampleInfo" class="stat" style="padding:8px 10px"></div>
      </div>
      <label class="field">新方案名称
        <input type="text" id="schemeName" placeholder="例如：春汛校准-A">
      </label>
    </div>

    <div style="margin-top:14px" class="dual-range">
      <div class="range-labels"><span>校准期起点（严格限定拟合数据）</span><span id="calStartLab"></span></div>
      <input type="range" id="calStart" min="0" max="${st.grid.n - 1}" value="${S.cal.i0}">
      <div class="range-labels"><span>校准期终点</span><span id="calEndLab"></span></div>
      <input type="range" id="calEnd" min="0" max="${st.grid.n - 1}" value="${S.cal.i1}">
    </div>

    <div class="button-row" style="margin-top:12px">
      <button class="btn primary" id="btnCalibrate">▶ 估计校准核（仅用校准期）</button>
      <button class="btn" id="btnApplyLag">应用滞后范围（置缺省守恒核）</button>
      <span class="muted" style="font-size:12px">评估期 [${d.evaluation[0]}…${d.evaluation[1]}] 不参与拟合，仅做样本外检验。</span>
    </div>

    <div class="button-row" style="margin-top:10px; align-items:center">
      <span class="field-label">手动排除区间（索引）</span>
      <input type="number" id="exA" min="0" max="${st.grid.n - 1}" placeholder="起" style="width:84px;padding:6px 8px">
      <span>–</span>
      <input type="number" id="exB" min="0" max="${st.grid.n - 1}" placeholder="止" style="width:84px;padding:6px 8px">
      <button class="btn small" id="btnAddExclude">添加排除段</button>
      <span id="excludeChips" class="button-row" style="margin:0"></span>
    </div>
    <div id="calMessage"></div>
  </div>

  <div class="stat-row" id="reachStats"></div>

  <div class="panel-grid">
    <div class="stack">
      <div class="block">
        <h3>过程线对照 <span class="tag">上游实测 · 逐段卷积 · 下游观测</span></h3>
        <div class="legend" id="reachLegend"></div>
        <div class="chart-wrap" id="reachChart"></div>
      </div>
      <div class="block">
        <h3>校准对照残差 <span class="tag">z − share·k⊛x（z=下游实测扣除其他支流）</span></h3>
        <div class="chart-wrap" id="residualChart" style="height:180px"></div>
      </div>
    </div>
    <div class="stack">
      <div class="block">
        <h3>当前传播核</h3>
        <div id="kernelMeta" class="scheme-meta" style="margin-bottom:6px"></div>
        <div id="reachIdent" class="ident-grid" style="grid-template-columns:minmax(0,1fr);margin-bottom:8px"></div>
        <div class="kernel-bars" id="kernelChart"></div>
      </div>
      <div class="block"><h3>拟合 / 检验质量</h3><div id="fitInfo"></div></div>
      <div class="block"><h3>本河段校准方案</h3><div id="edgeSchemes" class="stack"></div></div>
    </div>
  </div>`;

  $("#calStart").addEventListener("input", onCalSlider);
  $("#calEnd").addEventListener("input", onCalSlider);
  $("#lagMinH").addEventListener("input", updateLagSampleInfo);
  $("#lagMaxH").addEventListener("input", updateLagSampleInfo);
  $("#btnAddExclude").addEventListener("click", addExclude);
  $("#btnCalibrate").addEventListener("click", doCalibrate);
  $("#btnApplyLag").addEventListener("click", applyLagRange);

  renderExcludeChips();
  onCalSlider();
  updateLagSampleInfo();
  renderStats();
  renderKernelMeta();
  renderKernelChart();
  buildReachLegend();
  renderReachCharts();
  renderEdgeSchemes();
}

function lagSamplesFromInputs() {
  const dt = S.state.grid.dt_minutes;
  const hmin = Number($("#lagMinH").value), hmax = Number($("#lagMaxH").value);
  if (!isFinite(hmin) || !isFinite(hmax) || hmin < 0 || hmax < hmin) {
    throw new Error("滞后小时范围非法：需 0 ≤ min ≤ max。");
  }
  const lmin = Math.max(0, Math.round((hmin * 60) / dt));
  const lmax = Math.max(lmin, Math.round((hmax * 60) / dt));
  return { lmin, lmax };
}
function updateLagSampleInfo() {
  const el = $("#lagSampleInfo");
  if (!el) return;
  try {
    const { lmin, lmax } = lagSamplesFromInputs();
    el.innerHTML = `<b>${lmin} – ${lmax}</b> 个采样<br><span class="sub">对应 ${(lmin * S.state.grid.dt_minutes / 60).toFixed(2)}–${(lmax * S.state.grid.dt_minutes / 60).toFixed(2)} h</span>`;
  } catch (e) { el.textContent = e.message; }
}
function onCalSlider() {
  const a = $("#calStart"), b = $("#calEnd");
  let i0 = Number(a.value), i1 = Number(b.value);
  if (i0 > i1 - 2) { // 保持至少两个点
    if (this && this.id === "calStart") { i0 = i1 - 2; a.value = i0; }
    else { i1 = i0 + 2; b.value = i1; }
  }
  S.cal.i0 = i0; S.cal.i1 = i1;
  $("#calStartLab").textContent = `#${i0} · ${tFull(i0)}`;
  $("#calEndLab").textContent = `#${i1} · ${tFull(i1)}`;
  if (S.reachData) renderReachCharts();
}
function addExclude() {
  const a = Number($("#exA").value), b = Number($("#exB").value);
  if (!isFinite(a) || !isFinite(b) || b < a) { toast("排除区间需满足 起 ≤ 止", "error"); return; }
  S.exclude.push([a, b]);
  $("#exA").value = ""; $("#exB").value = "";
  renderExcludeChips();
}
function renderExcludeChips() {
  const wrap = $("#excludeChips");
  if (!wrap) return;
  wrap.innerHTML = S.exclude.map((iv, idx) =>
    `<span class="pill neutral" style="padding:3px 8px">排除 #${iv[0]}–${iv[1]} <a href="#" data-rm="${idx}" style="margin-left:6px;color:var(--critical);text-decoration:none;font-weight:800">×</a></span>`
  ).join("");
  $$("a[data-rm]", wrap).forEach((a) => a.addEventListener("click", (ev) => {
    ev.preventDefault(); S.exclude.splice(Number(a.dataset.rm), 1); renderExcludeChips();
  }));
}

function renderStats() {
  const d = S.reachData;
  const cm = d.cal_metrics, em = d.eval_metrics;
  const tile = (k, v, sub, cls) => `<div class="stat"><div class="k">${k}</div><div class="v ${cls || ""}">${v}</div><div class="sub">${sub || ""}</div></div>`;
  $("#reachStats").innerHTML = [
    tile("校准 RMSE", fmtMetric(cm.rmse), `n=${cm.n} 个可用点`),
    tile("校准 R²", fmtMetric(cm.r2, 3), "单纯形约束拟合", cm.r2 > 0.9 ? "good" : cm.r2 != null && cm.r2 < 0.5 ? "warn" : ""),
    tile("评估 RMSE（样本外）", fmtMetric(em.rmse), `n=${em.n}，未参与拟合`, em.rmse != null ? "" : "warn"),
    tile("评估 R²", fmtMetric(em.r2, 3), "仅检验", em.r2 > 0.8 ? "good" : ""),
    tile("偏差 bias", fmtMetric(cm.bias), "预测−观测 的均值"),
  ].join("");
}

function renderKernelMeta() {
  const e = S.reachData.edge, idn = S.reachData.identifiability;
  const sum = e.weights.reduce((a, b) => a + b, 0);
  const allNonNeg = e.weights.every((w) => w >= -1e-8);
  $("#kernelMeta").innerHTML = `
    <span class="pill ${allNonNeg ? "exact" : "unknown"}">${allNonNeg ? "✓ 非负" : "存在负值"}</span>
    <span class="pill ${Math.abs(sum - 1) < 1e-4 ? "exact" : "unknown"}">Σw=${sum.toFixed(4)} 守恒</span>
    <span>滞后 <b>${e.lag_min}–${e.lag_max}</b> 样本 / ${e.lag_min_hours}–${e.lag_max_hours} h</span>
    <span>停测可识别：完整至 <b>${idn.last_full == null ? "–" : "#" + idn.last_full + " " + tShort(idn.last_full)}</b>；
      残波至 <b>${idn.last_possible == null ? "–" : "#" + idn.last_possible}</b></span>`;
  const host = $("#reachIdent");
  if (host) {
    host.innerHTML = identCard({
      station_name: `上游输入 · ${S.reachData.from_station.name}`,
      is_local: false,
      identifiability: idn,
    });
  }
}
function renderKernelChart() {
  const el = $("#kernelChart");
  if (el && S.reachData) Charts.kernelBars(el, S.reachData.edge, S.state.grid.dt_minutes);
}

function reachSeriesDefs() {
  const d = S.reachData;
  const defs = [
    { key: "up", name: `上游 ${d.from_station.name}`, values: d.upstream, color: Charts.seriesColor(0) },
    { key: "routed", name: "卷积到达（当前核）", values: d.routed, color: Charts.seriesColor(1), width: 2.8 },
    { key: "target", name: "拟合目标 z（扣其他支流）", values: d.fit_target, color: Charts.seriesColor(6), dash: "6 4", width: 1.7 },
    { key: "down", name: `下游 ${d.to_station.name} 实测`, values: d.downstream, color: Charts.var("--text-primary"), width: 2 },
  ];
  d.siblings.forEach((sb, j) => defs.push({
    key: "sib_" + sb.edge_id, name: "其他支流 · " + sb.from_name,
    values: sb.series, color: Charts.seriesColor(j + 2), width: 1.5, dash: "2 3",
  }));
  return defs;
}
function buildReachLegend() {
  const defs = reachSeriesDefs();
  // 其他支流默认隐藏
  defs.forEach((d) => { if (d.key.startsWith("sib_") && S.reachHidden[d.key] === undefined) S.reachHidden[d.key] = true; });
  const legend = $("#reachLegend");
  legend.innerHTML = defs.map((d, i) => {
    const off = S.reachHidden[d.key] ? "off" : "";
    const sw = d.dash ? `<span class="legend-swatch dashed" style="color:${d.color}"></span>`
      : `<span class="legend-swatch" style="background:${d.color}"></span>`;
    return `<button type="button" class="legend-chip ${off}" data-key="${esc(d.key)}" aria-pressed="${off ? "false" : "true"}">${sw}${esc(d.name)}</button>`;
  }).join("");
  $$(".legend-chip", legend).forEach((chip) => chip.addEventListener("click", () => {
    const k = chip.dataset.key; S.reachHidden[k] = !S.reachHidden[k]; buildReachLegend(); renderReachCharts();
  }));
}
function renderReachCharts() {
  if (!S.reachData) return;
  const reachChart = $("#reachChart");
  const residualChart = $("#residualChart");
  if (!reachChart && !residualChart) return;
  const d = S.reachData, n = S.state.grid.n;
  const defs = reachSeriesDefs().map((x) => ({ ...x, hidden: !!S.reachHidden[x.key] }));
  const bands = [
    { from: S.cal.i0, to: S.cal.i1, kind: "cal", label: "校准期" },
    { from: d.evaluation[0], to: d.evaluation[1], kind: "eval", label: "评估期" },
    ...d.identifiability.missing_runs.map((r) => ({ from: r[0], to: r[1], kind: "missing", label: "缺测" })),
    ...d.identifiability.partial_ranges.map((r) => ({ from: r[0], to: r[1], kind: "partial" })),
  ];
  const inRanges = (i, rs) => rs.some((r) => i >= r[0] && i <= r[1]);
  const markers = [];
  const pk = d.peaks;
  if (pk.upstream_peak) markers.push({ index: pk.upstream_peak.index, value: pk.upstream_peak.value, color: Charts.seriesColor(0), label: "上游峰" });
  if (pk.routed_peak) markers.push({ index: pk.routed_peak.index, value: pk.routed_peak.value, color: Charts.seriesColor(1), label: "卷积峰" });
  if (pk.downstream_peak) markers.push({ index: pk.downstream_peak.index, value: pk.downstream_peak.value, color: Charts.var("--critical"), label: "下游峰" });

  if (reachChart) Charts.lineChart(reachChart, {
    n, height: 370, series: defs, bands, markers,
    xTick: tShort, xFull: (i) => tFull(i),
    noteAt: (i) => inRanges(i, d.identifiability.missing_runs) ? "上游/测站缺测：该卷积点不可识别，未按 0 处理。"
      : inRanges(i, d.identifiability.partial_ranges) ? "停测后的部分滞后抽头：虚线为已知抽头和（非负流量下为下界）。" : "",
  });
  if (residualChart) Charts.lineChart(residualChart, {
    n, height: 170, zeroCenter: true,
    series: [{ name: "残差", values: d.residual, color: Charts.seriesColor(7), fill: true, width: 1.7 }],
    bands: [{ from: S.cal.i0, to: S.cal.i1, kind: "cal" }, { from: d.evaluation[0], to: d.evaluation[1], kind: "eval" }],
    xTick: tShort, xFull: tFull,
  });
}

function renderEdgeSchemes() {
  const wrap = $("#edgeSchemes");
  const list = S.state.schemes.filter((x) => x.edge_id === S.edgeId);
  if (!list.length) { wrap.innerHTML = `<div class="empty" style="padding:18px">该河段还没有保存的校准方案。</div>`; return; }
  wrap.innerHTML = list.slice().reverse().map((s) => `
    <div class="scheme-card">
      <div class="top"><h4>${esc(s.name)}</h4><span class="pill neutral">${esc(s.id)}</span></div>
      <div class="scheme-meta">
        <span>切片 <b>${isoText(s.calibration_time[0])} ~ ${isoText(s.calibration_time[1])}</b></span>
        <span>RMSE <b>${fmtMetric(s.cal_metrics.rmse)}</b></span>
        <span>评估 RMSE <b>${fmtMetric(s.eval_metrics.rmse)}</b></span>
      </div>
      <div class="scheme-actions"><button class="btn small" data-apply="${esc(s.id)}">应用此核</button></div>
    </div>`).join("");
  $$("button[data-apply]", wrap).forEach((b) => b.addEventListener("click", () => applyScheme(b.dataset.apply)));
}

function renderFitMessage(e, okMsg) {
  const box = $("#calMessage");
  if (!box) return;
  if (e) box.innerHTML = `<div class="inline-error" style="position:static">${esc(e.message)}${e.code === "NOT_ENOUGH_SAMPLES" ? "<br>建议扩大校准期或缩小滞后范围。" : ""}</div>`;
  else if (okMsg) box.innerHTML = `<div class="inline-error" style="position:static;color:var(--good);border-color:color-mix(in srgb,var(--good) 40%,transparent);background:color-mix(in srgb,var(--good) 9%,var(--surface-1))">${esc(okMsg)}</div>`;
  else box.innerHTML = "";
}

async function doCalibrate() {
  let range;
  try { range = lagSamplesFromInputs(); } catch (e) { toast(e.message, "error"); return; }
  const btn = $("#btnCalibrate");
  btn.disabled = true; btn.innerHTML = '<span class="spinner"></span> 拟合中…';
  renderFitMessage(null, null);
  try {
    const j = await postJSON("/api/calibrate", {
      edge_id: S.edgeId, i0: S.cal.i0, i1: S.cal.i1,
      lag_min: range.lmin, lag_max: range.lmax,
      name: $("#schemeName").value.trim() || undefined,
      apply: true, exclude_intervals: S.exclude,
    });
    showReport(j.report, `已校准并应用 ${j.scheme.id}`);
    toast("校准完成：" + j.scheme.id, "good");
    await refreshState();
    S.edgeId = j.scheme.edge_id;
    await refreshReach();
  } catch (e) {
    renderFitMessage(e);
    toast(e.message, "error");
  } finally {
    btn.disabled = false; btn.textContent = "▶ 估计校准核（仅用校准期）";
  }
}

async function applyLagRange() {
  let range;
  try { range = lagSamplesFromInputs(); } catch (e) { toast(e.message, "error"); return; }
  try {
    const j = await postJSON("/api/edge/lag_range", { edge_id: S.edgeId, lag_min: range.lmin, lag_max: range.lmax });
    showReport(j.report, "滞后范围已更新（缺省守恒核）");
    toast("已应用滞后范围并重算下游", "good");
    await refreshState(); await refreshReach();
  } catch (e) { toast(e.message, "error"); }
}

async function applyScheme(id) {
  try {
    const j = await postJSON("/api/scheme/apply", { scheme_id: id });
    showReport(j.report, `已应用方案 ${id}（其数据切片保持不变）`);
    toast("已应用方案 " + id, "good");
    await refreshState(); await refreshReach();
  } catch (e) { toast(e.message, "error"); }
}

/* ========================= ② 洪峰移动 ========================= */
function renderPeaksTab() {
  const p = $("#panel-peaks");
  const st = S.state;
  const opts = st.stations.map((x) => `<option value="${esc(x.id)}">${esc(x.name || x.id)}</option>`).join("");
  p.innerHTML = `
    <div class="block">
      <h3>洪峰移动与传播时滞 <span class="tag">沿主路径逐站追踪同一洪水波</span></h3>
      <div class="control-grid">
        <label class="field">上游源站<select id="peakSource">${opts}</select></label>
        <label class="field">下游目标<select id="peakTarget">${opts}</select></label>
      </div>
      <div style="margin-top:12px" class="dual-range">
        <div class="range-labels"><span>洪水事件窗口起点</span><span id="pkStartLab"></span></div>
        <input type="range" id="pkStart" min="0" max="${st.grid.n - 1}" value="${S.cal.i0}">
        <div class="range-labels"><span>窗口终点</span><span id="pkEndLab"></span></div>
        <input type="range" id="pkEnd" min="0" max="${st.grid.n - 1}" value="${Math.min(st.grid.n - 1, S.cal.i1)}">
      </div>
      <div class="button-row" style="margin-top:10px"><button class="btn primary" id="pkRun">追踪洪峰</button></div>
    </div>
    <div id="peakResult"><div class="empty">选择源站与目标后点击“追踪洪峰”。</div></div>`;
  $("#peakSource").value = S.peakSource;
  $("#peakTarget").value = S.peakTarget;
  $("#peakSource").addEventListener("change", () => { S.peakSource = $("#peakSource").value; loadPeaks(); });
  $("#peakTarget").addEventListener("change", () => { S.peakTarget = $("#peakTarget").value; loadPeaks(); });
  let rz;
  const sync = (ev) => {
    const aEl = $("#pkStart"), bEl = $("#pkEnd");
    const nMax = S.state.grid.n - 1;
    let a = +aEl.value, b = +bEl.value;
    if (a > b - 2) {
      if (ev && ev.target === aEl) {
        a = Math.max(0, b - 2); aEl.value = a;
      } else {
        b = Math.min(nMax, a + 2); bEl.value = b;
        if (b - a < 2) { a = Math.max(0, b - 2); aEl.value = a; }
      }
    }
    $("#pkStartLab").textContent = `#${a} · ${tShort(a)}`;
    $("#pkEndLab").textContent = `#${b} · ${tShort(b)}`;
    clearTimeout(rz); rz = setTimeout(loadPeaks, 250);
  };
  $("#pkStart").addEventListener("input", sync);
  $("#pkEnd").addEventListener("input", sync);
  $("#pkRun").addEventListener("click", loadPeaks);
  $("#pkStartLab").textContent = `#${$("#pkStart").value} · ${tShort(+$("#pkStart").value)}`;
  $("#pkEndLab").textContent = `#${$("#pkEnd").value} · ${tShort(+$("#pkEnd").value)}`;
  loadPeaks();
}

async function loadPeaks() {
  S.peakSource = $("#peakSource") ? $("#peakSource").value : S.peakSource;
  S.peakTarget = $("#peakTarget") ? $("#peakTarget").value : S.peakTarget;
  const i0 = $("#pkStart") ? +$("#pkStart").value : S.cal.i0;
  const i1 = $("#pkEnd") ? +$("#pkEnd").value : S.cal.i1;
  const seq = ++S.peakSeq;
  const box = $("#peakResult");
  if (box) box.innerHTML = `<div class="block"><span class="spinner"></span> 追踪中…</div>`;
  try {
    const q = `/api/peaks?sid=${encodeURIComponent(S.sid)}&source=${encodeURIComponent(S.peakSource)}&target=${encodeURIComponent(S.peakTarget)}&i0=${i0}&i1=${i1}`;
    const j = await getJSON(q);
    if (seq !== S.peakSeq) return;
    S.peakData = j.data;
    renderPeakResult();
  } catch (e) {
    if (seq === S.peakSeq && box) box.innerHTML = errorPanel(e);
  }
}

function renderPeakResult() {
  const d = S.peakData;
  const box = $("#peakResult");
  if (!d.reachable) { box.innerHTML = `<div class="empty">${esc(d.source)} 到 ${esc(d.target)} 之间没有有向路径。</div>`; return; }
  const pred = d.predicted_arrival;
  const steps = d.stations.map((row, i) => {
    const pk = row.peak;
    return `<div class="peak-step">
      <div class="st-name">${i + 1}. ${esc(row.station_name)}</div>
      ${pk ? `<div class="st-time">${esc(pk.time.replace("T", " "))}</div>
        <div class="st-val">洪峰 ${Charts.fmt(pk.value)}</div>
        <div class="st-lag">距源站峰 <b>${pk.lag_from_source_samples ?? "–"}</b> 样本
          （${pk.lag_from_source_hours == null ? "–" : pk.lag_from_source_hours + " h"}）</div>`
        : `<div class="st-time muted">窗口内无有限洪峰</div>`}
      ${i > 0 ? `<div class="st-lag">核累积期望窗口 ${row.expected_arrival_window_hours[0]}–${row.expected_arrival_window_hours[1]} h</div>` : ""}
    </div>${i < d.stations.length - 1 ? `<div class="peak-arrow">→</div>` : ""}`;
  }).join("");

  box.innerHTML = `
    <div class="block">
      <h3>洪峰到达链 ${d.n_paths > 1 ? `<span class="tag">存在 ${d.n_paths} 条路径，取最长主路径展示</span>` : ""}</h3>
      <div class="peak-chain">${steps}</div>
      <div class="stat-row">
        <div class="stat"><div class="k">路径核众数滞后（预测洪峰位移）</div><div class="v">${pred ? pred.lag_hours + " h" : "–"}</div><div class="sub">${pred ? pred.lag_samples + " 样本" : ""}</div></div>
        <div class="stat"><div class="k">预测到达时刻</div><div class="v" style="font-size:15px">${pred ? esc(pred.time.replace("T", " ")) : "超出序列"}</div><div class="sub">源峰 + 众数滞后</div></div>
        <div class="stat"><div class="k">采样间隔</div><div class="v" style="font-size:16px">${S.state.grid.dt_minutes} min</div><div class="sub">滞后时间 = 样本数 × dt</div></div>
      </div>
    </div>
    <div class="block"><h3>沿程各站过程线与洪峰标记（同时间轴、各自纵轴）</h3><div class="mini-multiples" id="peakMultiples"></div></div>`;
  renderPeakCharts();
}

function renderPeakCharts() {
  const d = S.peakData;
  if (!d) return;
  const wrap = $("#peakMultiples");
  if (!wrap) return;
  wrap.innerHTML = d.stations.map((row, i) =>
    `<div class="mini-chart"><div class="mc-title"><span>${esc(row.station_name)}</span><span>${row.peak ? Charts.fmt(row.peak.value) : ""}</span></div><div id="miniPeak${i}"></div></div>`
  ).join("");
  d.stations.forEach((row, i) => {
    const markers = row.peak ? [{ index: row.peak.index, value: row.peak.value, color: Charts.seriesColor(i), label: "峰" }] : [];
    Charts.lineChart($("#miniPeak" + i), {
      n: S.state.grid.n, height: 128,
      series: [{ name: row.station_name, values: row.series, color: Charts.seriesColor(i), width: 1.8 }],
      markers, xTick: tShort, xFull: tFull,
    });
  });
}

/* ========================= ③ 贡献分解 ========================= */
function renderContribTab() {
  const p = $("#panel-contrib");
  const opts = S.state.stations.map((x) => `<option value="${esc(x.id)}">${esc(x.name || x.id)}</option>`).join("");
  p.innerHTML = `
    <div class="block">
      <h3>各站贡献序列与下载 <span class="tag">增量本地入流沿全部路径卷积，汇合处按路径合并</span></h3>
      <div class="control-grid">
        <label class="field">目标（汇合/控制站）<select id="contribTarget">${opts}</select></label>
      </div>
      <div class="download-row" style="margin-top:12px">
        <a class="btn primary" id="dlWide" href="#">⬇ 下载宽表 CSV（严格可识别，缺测留空）</a>
        <a class="btn" id="dlLong" href="#">⬇ 下载长表 CSV（含部分下界与状态）</a>
      </div>
    </div>
    <div id="contribResult"><div class="empty"><span class="spinner"></span> 计算贡献中…</div></div>`;
  $("#contribTarget").value = S.target;
  $("#contribTarget").addEventListener("change", (e) => { S.target = e.target.value; loadContrib(); });
  $("#dlWide").addEventListener("click", (e) => {
    e.preventDefault(); downloadContributions("wide", e.currentTarget);
  });
  $("#dlLong").addEventListener("click", (e) => {
    e.preventDefault(); downloadContributions("long", e.currentTarget);
  });
  loadContrib();
}

async function downloadContributions(format, btn) {
  const suffix = format === "long" ? "&format=long" : "";
  const url = `/api/contributions.csv?sid=${encodeURIComponent(S.sid)}&target=${encodeURIComponent(S.target)}${suffix}`;
  const original = btn ? btn.innerHTML : "";
  if (btn) { btn.setAttribute("aria-busy", "true"); btn.style.pointerEvents = "none"; btn.style.opacity = ".65"; }
  try {
    const r = await fetch(url);
    const ct = r.headers.get("content-type") || "";
    if (!r.ok || ct.includes("application/json")) {
      let msg = `CSV 下载失败（${r.status}）`;
      try { const j = await r.json(); if (j.error && j.error.message) msg = j.error.message; } catch (_) { /* 保留默认消息 */ }
      throw new Error(msg);
    }
    const blob = await r.blob();
    const objectUrl = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = objectUrl;
    a.download = `contributions_${S.target}${format === "long" ? "_long" : ""}.csv`;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(objectUrl), 1500);
    toast("CSV 已开始下载", "good");
  } catch (e) {
    toast(e.message, "error");
  } finally {
    if (btn) { btn.removeAttribute("aria-busy"); btn.style.pointerEvents = ""; btn.style.opacity = ""; btn.innerHTML = original; }
  }
}

async function loadContrib() {
  const seq = ++S.contribSeq;
  const target = S.target;
  try {
    const j = await getJSON(withSid("/api/contributions") + "&target=" + encodeURIComponent(target));
    if (seq !== S.contribSeq || target !== S.target) return;
    S.contribData = j.data;
    renderContribResult();
  } catch (e) {
    if (seq === S.contribSeq && target === S.target) $("#contribResult").innerHTML = errorPanel(e);
  }
}

function identStatus(idn, n) {
  if (idn.last_observed == null || !idn.supported || !idn.supported.length) return ["unknown", "不可识别"];
  const tailOutage = idn.last_observed < n - 1;
  if (tailOutage) return ["partial", "末有效 " + tShort(idn.last_observed)];
  if ((idn.missing_runs || []).length) return ["partial", "含缺口/缺测"];
  if (idn.last_full != null && idn.last_full >= n - 2) return ["exact", "全程可识别"];
  return ["partial", "部分可识别"];
}
function identCard(s) {
  const idn = s.identifiability;
  const n = S.state.grid.n;
  const seg = (ranges, cls) => ranges.map(([a, b]) =>
    `<div class="${cls}" style="left:${(a / n) * 100}%;width:${Math.max(0.4, ((b - a + 1) / n) * 100)}%"></div>`).join("");
  const status = identStatus(idn, n);
  return `<div class="ident-item">
    <div class="top"><span>${esc(s.station_name)}${s.is_local ? ' <span class="pill neutral">本地增量</span>' : ""}</span>
      <span class="pill ${status[0]}">${status[1]}</span></div>
    <div class="ident-track">${seg(idn.supported, "fill-exact")}${seg(idn.partial_ranges, "fill-partial")}</div>
    <div class="ident-scale"><span>完整至 ${idn.last_full == null ? "–" : "#" + idn.last_full}</span>
      <span>残波至 ${idn.last_possible == null ? "–" : "#" + idn.last_possible}</span></div>
  </div>`;
}

function renderContribResult() {
  const d = S.contribData;
  const box = $("#contribResult");
  const closeErr = d.max_abs_closure_error;
  const rows = d.sources.map((s, i) => `
    <tr>
      <td>${esc(s.station_name)} <span class="eid">${esc(s.station_id)}</span></td>
      <td>${s.is_local ? "目标站本地增量" : `上游 · ${s.n_paths} 条路径`}</td>
      <td class="num">${fmtMetric(s.mean, 3)}</td>
      <td class="num">${s.volume == null ? "–" : Charts.fmt(s.volume, 0)}</td>
      <td class="num">${s.paths && s.paths[0] ? s.paths[0].lag_min + "–" + s.paths[0].lag_max : "–"}</td>
      <td class="num">${s.identifiability.last_full == null ? "–" : "#" + s.identifiability.last_full} / ${s.identifiability.last_possible == null ? "–" : "#" + s.identifiability.last_possible}</td>
    </tr>`).join("");

  box.innerHTML = `
    <div class="stat-row">
      <div class="stat"><div class="k">可识别域闭合最大误差</div><div class="v good">${closeErr == null ? "–" : closeErr.toExponential(2)}</div><div class="sub">Σ各站贡献 − 目标实测</div></div>
      <div class="stat"><div class="k">闭合 R²</div><div class="v ${d.closure_metrics.r2 > 0.99 ? "good" : ""}">${fmtMetric(d.closure_metrics.r2, 4)}</div><div class="sub">仅统计完全可识别点 n=${d.closure_metrics.n}</div></div>
      <div class="stat"><div class="k">贡献来源数</div><div class="v">${d.sources.length}</div><div class="sub">含目标站本地增量</div></div>
    </div>

    <div class="block">
      <h3>可识别范围（停测不补零）</h3>
      <div class="ident-grid">${d.sources.map(identCard).join("")}</div>
      <p class="muted" style="font-size:12px;margin:10px 0 0">绿色＝全部滞后抽头有观测（精确）；黄色斜纹＝停测后仅部分长滞后抽头可知的残波区间；其后为不可识别，CSV 中留空而非 0。</p>
    </div>

    <div class="block">
      <h3>各站贡献过程线 <span class="tag">虚线为停测后的部分残波（下界）</span></h3>
      <div class="legend" id="contribLegend"></div>
      <div class="chart-wrap" id="contribChart"></div>
    </div>

    <div class="block">
      <h3>贡献统计表</h3>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>测站</th><th>类型</th><th class="num">平均贡献</th><th class="num">积分水量*</th><th class="num">路径滞后(样本)</th><th class="num">完整/残波 终点</th></tr></thead>
        <tbody>${rows}</tbody>
      </table></div>
      <p class="muted" style="font-size:11.5px;margin-top:8px">* 若流量单位为 m³/s，积分水量按 ΣQ·dt 秒折算为 m³；虚构数据单位仅作演示。负的本地增量通常表示核偏差或观测误差。</p>
    </div>`;
  buildContribLegend();
  renderContribCharts();
}

function contribDefs() {
  const d = S.contribData;
  const defs = d.sources.map((s, i) => ({
    key: "c_" + s.station_id, name: s.station_name + (s.is_local ? "（本地）" : ""),
    values: s.series, partial: s.partial_series, color: Charts.seriesColor(i), width: 2,
  }));
  defs.push({ key: "c_total", name: "可识别总量", values: d.total, color: Charts.seriesColor(6), width: 2.8 });
  defs.push({ key: "c_obs", name: "目标实测 " + d.target_name, values: d.observed, color: Charts.var("--text-primary"), width: 2, dash: "5 4" });
  return defs;
}
function buildContribLegend() {
  const defs = contribDefs();
  const legend = $("#contribLegend");
  legend.innerHTML = defs.map((d) => {
    const off = S.contribHidden[d.key] ? "off" : "";
    const sw = d.dash ? `<span class="legend-swatch dashed" style="color:${d.color}"></span>` : `<span class="legend-swatch" style="background:${d.color}"></span>`;
    return `<button type="button" class="legend-chip ${off}" data-key="${esc(d.key)}" aria-pressed="${off ? "false" : "true"}">${sw}${esc(d.name)}</button>`;
  }).join("");
  $$(".legend-chip", legend).forEach((c) => c.addEventListener("click", () => {
    const k = c.dataset.key; S.contribHidden[k] = !S.contribHidden[k]; buildContribLegend(); renderContribCharts();
  }));
}
function renderContribCharts() {
  if (!S.contribData || S.tab !== "contrib") return;
  const chart = $("#contribChart");
  if (!chart) return;
  const d = S.contribData;
  const defs = contribDefs().map((x) => ({ ...x, hidden: !!S.contribHidden[x.key] }));
  const miss = nullRuns(d.observed).map((r) => ({ from: r[0], to: r[1], kind: "missing", label: "目标缺测" }));
  Charts.lineChart(chart, {
    n: S.state.grid.n, height: 400, series: defs, bands: miss,
    xTick: tShort, xFull: tFull,
    noteAt: (i) => d.total[i] === null ? "总量在该时刻不可完全识别（至少一个来源缺测）；各来源分线仍按其支持域显示。" : "",
  });
}

/* ========================= ④ 残差来源 ========================= */
function renderResidualTab() {
  const p = $("#panel-residual");
  const opts = S.state.stations.map((x) => `<option value="${esc(x.id)}">${esc(x.name || x.id)}</option>`).join("");
  p.innerHTML = `
    <div class="block">
      <h3>残差来源分解 <span class="tag">下游实测 − 各直接支流传播和</span></h3>
      <label class="field" style="max-width:360px">目标汇合/控制站<select id="resTarget">${opts}</select></label>
    </div>
    <div id="residualResult"><div class="empty"><span class="spinner"></span> 分析残差中…</div></div>`;
  $("#resTarget").value = S.target;
  $("#resTarget").addEventListener("change", (e) => { S.target = e.target.value; loadResidual(); });
  loadResidual();
}
async function loadResidual() {
  const seq = ++S.residualSeq;
  const target = S.target;
  try {
    const j = await getJSON(withSid("/api/residual") + "&target=" + encodeURIComponent(target));
    if (seq !== S.residualSeq || target !== S.target) return;
    S.residualData = j.data;
    renderResidualResult();
  } catch (e) {
    if (seq === S.residualSeq && target === S.target) $("#residualResult").innerHTML = errorPanel(e);
  }
}
function renderResidualResult() {
  const d = S.residualData;
  const cm = d.cal_metrics, em = d.eval_metrics;
  const means = d.branches.map((b) => b.mean || 0);
  const totalMean = means.reduce((a, b) => a + Math.max(0, b), 0);
  const rows = d.branches.map((b, i) => {
    const share = totalMean > 0 ? (Math.max(0, b.mean || 0) / totalMean * 100) : null;
    const rmse = b.cal_eval_rmse;
    return `<tr>
      <td><span class="legend-dot" style="background:${Charts.seriesColor(i)};display:inline-block;margin-right:6px"></span>${esc(b.station_name)}</td>
      <td class="num">${fmtMetric(b.mean, 3)}</td>
      <td class="num">${share == null ? "–" : share.toFixed(1) + "%"}</td>
      <td class="num">${rmse == null ? "–" : fmtMetric(rmse, 3)}</td>
      <td class="num">${b.identifiability.last_full == null ? "–" : "#" + b.identifiability.last_full} / ${b.identifiability.last_possible == null ? "–" : "#" + b.identifiability.last_possible}</td>
      <td>${rmse != null && em && em.rmse != null && rmse > em.rmse * 1.15 ? '<span class="pill partial">核误差偏大</span>' : '<span class="pill exact">正常</span>'}</td>
    </tr>`;
  }).join("");
  const missChips = d.missing_runs_target.length
    ? d.missing_runs_target.map((r) => `<span class="pill unknown">目标缺测 #${r[0]}–${r[1]}</span>`).join(" ")
    : `<span class="pill exact">目标站无缺测</span>`;

  $("#residualResult").innerHTML = `
    <div class="stat-row">
      <div class="stat"><div class="k">校准期 RMSE / R²</div><div class="v" style="font-size:17px">${fmtMetric(cm.rmse)} / ${fmtMetric(cm.r2, 3)}</div><div class="sub">n=${cm.n}</div></div>
      <div class="stat"><div class="k">评估期 RMSE / R²</div><div class="v" style="font-size:17px">${fmtMetric(em.rmse)} / ${fmtMetric(em.r2, 3)}</div><div class="sub">样本外，未拟合</div></div>
      <div class="stat"><div class="k">评估期偏差</div><div class="v ${em.bias > 0 ? "warn" : "good"}" style="font-size:17px">${fmtMetric(em.bias)}</div><div class="sub">正＝总体低估/有额外入流</div></div>
    </div>
    <div class="block">
      <h3>实测 vs 支流传播合计</h3>
      <div class="legend" id="resLegend"></div>
      <div class="chart-wrap" id="resChart"></div>
    </div>
    <div class="block">
      <h3>残差过程（正：未解释来水；负：核高估）</h3>
      <div class="chart-wrap" id="residOnlyChart" style="height:200px"></div>
    </div>
    <div class="block">
      <h3>残差来源排序与缺测</h3>
      <div class="table-wrap"><table class="data">
        <thead><tr><th>直接支流</th><th class="num">平均传播量</th><th class="num">占已知来水*</th><th class="num">该边评估 RMSE</th><th class="num">完整/残波终点</th><th>诊断</th></tr></thead>
        <tbody>${rows}
          <tr><td><b>未解释残差（评估期平均）＝本地/未测入流＋误差</b></td><td class="num"><b>${fmtMetric(d.eval_metrics.bias, 3)}</b><div class="sub">平均残差</div></td><td></td><td class="num">${fmtMetric(em.rmse, 3)}<div class="sub">整体 RMSE</div></td><td></td><td>${missChips}</td></tr>
        </tbody>
      </table></div>
      <p class="muted" style="font-size:12px;margin-top:8px">${esc(d.interpretation)} *按各支流平均流量（取正）估算占比，仅用于来源排序。若某边校准/评估 RMSE 明显偏大，残差更可能来自该段核失配；缺测传播造成的空白是“不可识别”，不是新的支流来源。</p>
    </div>`;
  buildResidualLegend();
  renderResidualCharts();
}
function residualDefs() {
  const d = S.residualData;
  const defs = [{ key: "r_pred", name: "支流传播合计", values: d.prediction, color: Charts.seriesColor(0), width: 2.6 }];
  d.branches.forEach((b, i) => defs.push({ key: "r_b_" + b.edge_id, name: b.station_name, values: b.series, color: Charts.seriesColor(i + 1), width: 1.4, hiddenDefault: true }));
  defs.push({ key: "r_obs", name: "目标实测", values: d.observed, color: Charts.var("--text-primary"), width: 2, dash: "5 4" });
  return defs;
}
function buildResidualLegend() {
  const defs = residualDefs();
  defs.forEach((d) => { if (d.hiddenDefault && S.residualHidden[d.key] === undefined) S.residualHidden[d.key] = true; });
  const legend = $("#resLegend");
  if (!legend) return;
  legend.innerHTML = defs.map((d) => {
    const off = S.residualHidden[d.key] ? "off" : "";
    const sw = d.dash ? `<span class="legend-swatch dashed" style="color:${d.color}"></span>` : `<span class="legend-swatch" style="background:${d.color}"></span>`;
    return `<button type="button" class="legend-chip ${off}" data-key="${esc(d.key)}" aria-pressed="${off ? "false" : "true"}">${sw}${esc(d.name)}</button>`;
  }).join("");
  $$(".legend-chip", legend).forEach((c) => c.addEventListener("click", () => {
    const k = c.dataset.key; S.residualHidden[k] = !S.residualHidden[k]; buildResidualLegend(); renderResidualCharts();
  }));
}
function renderResidualCharts() {
  if (!S.residualData || S.tab !== "residual") return;
  const chart = $("#resChart");
  const onlyChart = $("#residOnlyChart");
  if (!chart && !onlyChart) return;
  const d = S.residualData;
  const defs = residualDefs().map((x) => ({ ...x, hidden: !!S.residualHidden[x.key] }));
  if (chart) Charts.lineChart(chart, { n: S.state.grid.n, height: 360, series: defs, xTick: tShort, xFull: tFull });
  if (onlyChart) Charts.lineChart(onlyChart, {
    n: S.state.grid.n, height: 200, zeroCenter: true,
    series: [{ name: "残差", values: d.residual, color: Charts.seriesColor(8 - 1), fill: true, width: 1.8 }],
    xTick: tShort, xFull: tFull,
  });
}

/* ========================= ⑤ 方案 ========================= */
function renderSchemesTab() {
  const p = $("#panel-schemes");
  const list = S.state.schemes;
  if (!list.length) {
    p.innerHTML = `<div class="block"><div class="empty">还没有校准方案。请到“① 河段校准与过程线”选择滞后范围并估计一个核。<br><br>每个方案都会保存自己的校准时间切片与数据快照，之后修改窗口或重新校准都不会改变旧方案。</div></div>`;
    return;
  }
  p.innerHTML = `<div class="block"><h3>已保存的校准方案（${list.length}）</h3>
    <p class="muted" style="margin-top:-4px">方案永久绑定创建时的数据切片；“快照重放”使用方案自带的 x/z 切片重新拟合，验证绑定关系。</p>
    <div class="stack" id="schemeList">${list.slice().reverse().map(schemeCardHtml).join("")}</div></div>`;
  $$("button[data-apply-s]").forEach((b) => b.addEventListener("click", () => applyScheme(b.dataset.applyS)));
  $$("button[data-refit]").forEach((b) => b.addEventListener("click", () => refitScheme(b.dataset.refit, b)));
}
function schemeCardHtml(s) {
  const w = s.kernel.weights.map((x) => Number(x).toFixed(3)).join(", ");
  return `<div class="scheme-card">
    <div class="top"><h4>${esc(s.name)}</h4><span class="pill neutral">${esc(s.id)} · ${esc(s.edge_id)}</span></div>
    <div class="scheme-meta">
      <span>绑定切片 <b>${isoText(s.calibration_time[0])} ~ ${isoText(s.calibration_time[1])}</b></span>
      <span>滞后 <b>${s.lag_min}–${s.lag_max}</b> 样本（dt=${s.dt_minutes}′）</span>
      <span>使用样本 <b>${s.fit.n_samples}</b></span>
      <span>校准 RMSE <b>${fmtMetric(s.cal_metrics.rmse)}</b> R² <b>${fmtMetric(s.cal_metrics.r2, 3)}</b></span>
      <span>评估 RMSE <b>${fmtMetric(s.eval_metrics.rmse)}</b> R² <b>${fmtMetric(s.eval_metrics.r2, 3)}</b></span>
    </div>
    <div class="kernel-mini">[${w}]（lag_min=${s.kernel.lag_min}，Σ=${s.kernel.weights.reduce((a, b) => a + b, 0).toFixed(4)}）</div>
    <div class="scheme-actions">
      <button class="btn small primary" data-apply-s="${esc(s.id)}">应用此核到河网</button>
      <button class="btn small" data-refit="${esc(s.id)}">用自带切片重放拟合</button>
      <span class="muted" id="refit-${esc(s.id)}" style="align-self:center;font-size:12px"></span>
    </div>
  </div>`;
}
async function refitScheme(id, btn) {
  const out = $("#refit-" + id);
  if (btn) btn.disabled = true;
  try {
    const j = await postJSON("/api/scheme/refit", { scheme_id: id });
    const r = j.result;
    const err = Math.max(...r.weights.map((w, i) => Math.abs(w - r.original_weights[i])));
    out.innerHTML = err < 1e-6
      ? `<span class="pill exact">切片绑定 ✓ 重放最大偏差 ${err.toExponential(1)}</span>`
      : `<span class="pill partial">重放偏差 ${err.toExponential(2)}</span>`;
  } catch (e) { out.innerHTML = `<span class="pill unknown">${esc(e.message)}</span>`; }
  finally { if (btn) btn.disabled = false; }
}

document.addEventListener("DOMContentLoaded", init);
