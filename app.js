/* PDF a CAD — interfaz */
"use strict";

const PT_TO_MM = 25.4 / 72;
const UNIT_DIV = { mm: 1, cm: 10, m: 1000 };

const DEFAULT_OPTS = {
  scale_mode: "ratio", scale_ratio: 1, units: "mm", scale_factor: null,
  layer_mode: "ocg", hatch_layer: "same",
  detect_hatches: true, hatch_style: "ansi", hatch_min_lines: 6, hatch_max_spacing_mm: 12,
  hatch_angle_tol: 0.5, hatch_spacing_tol: 0.15, hatch_gap_factor: 0.5, hatch_dashes: true,
  hatch_boundary: true, fix_overlaps: true, hatch_snap: true, rejected_hatches: [],
  fills: true, merge_fills: true, skip_white_fills: true,
  clip_paths: true, curves_to_arcs: true, join_polylines: true, merge_collinear: true,
  lineweights: true, linetypes: true, texts: true, text_height_factor: 0.72,
  layer_rename: {}, layer_exclude: [],
};
// opciones que se recuerdan entre sesiones (no dependen del plano)
const STICKY = ["units", "hatch_style", "hatch_layer", "hatch_boundary", "lineweights", "linetypes",
  "text_height_factor", "join_polylines"];

const S = {
  doc: null, page: 0, opts: { ...DEFAULT_OPTS }, data: null, analysis: null,
  bg: null, view: { s: 1, tx: 0, ty: 0 }, sel: null, hFilter: "all",
  hiddenLayers: new Set(), calib: null, reqId: 0, paths: null,
};

const $ = (id) => document.getElementById(id);
const cv = $("cv");
const ctx = cv.getContext("2d");

/* ------------------------------------------------------------------ util */
function toast(msg, err = false, ms = 3200) {
  const t = $("toast");
  t.textContent = msg;
  t.className = "toast" + (err ? " err" : "");
  clearTimeout(toast._t);
  toast._t = setTimeout(() => t.classList.add("hidden"), ms);
}
function busy(on, msg) {
  $("busy").classList.toggle("hidden", !on);
  if (msg) $("busyMsg").textContent = msg;
}
function fmt(n, d = 2) {
  return Number(n).toLocaleString("es", { maximumFractionDigits: d });
}
function loadSticky() {
  try {
    const s = JSON.parse(localStorage.getItem("pdf2cad.opts") || "{}");
    for (const k of STICKY) if (k in s) S.opts[k] = s[k];
  } catch (e) { /* sin almacenamiento */ }
}
function saveSticky() {
  try {
    const o = {};
    for (const k of STICKY) o[k] = S.opts[k];
    localStorage.setItem("pdf2cad.opts", JSON.stringify(o));
  } catch (e) { /* sin almacenamiento */ }
}
/* ------------------------------------------------------------- opciones */
const OPT_IDS = ["scale_ratio", "units", "layer_mode", "hatch_layer", "detect_hatches", "hatch_style",
  "hatch_snap", "hatch_dashes", "hatch_boundary", "fix_overlaps", "hatch_min_lines", "hatch_max_spacing_mm",
  "hatch_angle_tol", "fills", "merge_fills", "skip_white_fills", "clip_paths", "curves_to_arcs",
  "join_polylines", "merge_collinear", "lineweights", "linetypes", "texts", "text_height_factor"];

function optsToUI() {
  for (const k of OPT_IDS) {
    const el = $("o_" + k);
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!S.opts[k];
    else el.value = S.opts[k];
  }
  $("o_hatch_spacing_tol_pct").value = Math.round(S.opts.hatch_spacing_tol * 100);
  updateScaleInfo();
}
function bindOpts() {
  for (const k of OPT_IDS) {
    const el = $("o_" + k);
    if (!el) continue;
    el.addEventListener("change", () => {
      let v = el.type === "checkbox" ? el.checked : el.value;
      if (el.type === "number") v = parseFloat(v);
      if (el.type === "number" && !isFinite(v)) return;
      S.opts[k] = v;
      if (k === "scale_ratio") S.opts.scale_mode = "ratio";
      if (k.startsWith("hatch_") || k === "detect_hatches") S.opts.rejected_hatches = [];
      saveSticky();
      updateScaleInfo();
      scheduleProcess();
    });
  }
  $("o_hatch_spacing_tol_pct").addEventListener("change", (e) => {
    const v = parseFloat(e.target.value);
    if (!isFinite(v)) return;
    S.opts.hatch_spacing_tol = v / 100;
    S.opts.rejected_hatches = [];
    scheduleProcess();
  });
}
function modelFactor() {
  if (S.opts.scale_mode === "factor" && S.opts.scale_factor) return S.opts.scale_factor;
  return PT_TO_MM * (S.opts.scale_ratio || 1) / UNIT_DIV[S.opts.units];
}
function updateScaleInfo() {
  const k = modelFactor();
  const ratio = k * UNIT_DIV[S.opts.units] / PT_TO_MM;
  let txt = `1 mm de papel = ${fmt(k / PT_TO_MM, 4)} ${S.opts.units} de dibujo`;
  if (S.opts.scale_mode === "factor") {
    txt = `Calibrado (≈ 1:${fmt(ratio, 2)}). ` + txt;
    $("o_scale_ratio").value = Math.round(ratio * 100) / 100;
  }
  if (S.doc) {
    const p = S.doc.pages[S.page];
    txt += ` · Hoja = ${fmt(p.w * k, 1)} × ${fmt(p.h * k, 1)} ${S.opts.units}`;
  }
  $("scaleInfo").textContent = txt;
}

/* ---------------------------------------------------------------- carga */
async function uploadFile(file) {
  if (!file) return;
  if (!/\.pdf$/i.test(file.name) && file.type !== "application/pdf") {
    toast("El archivo debe ser un PDF", true);
    return;
  }
  const mb = file.size / 1048576;
  progShow("Cargando " + file.name, mb > 20);
  try {
    const j = await Backend.upload(file, (f) => progUpdate(f * 0.98, `Leyendo archivo ${fmt(mb, 1)} MB`, 0));
    S.doc = j;
    $("fileInfo").textContent = j.name;
    $("fileInfo").title = j.name;
    $("drop").classList.add("hidden");
    $("toolbar").classList.remove("hidden");
    $("btnDxf").disabled = false;
    $("pageSel").classList.toggle("hidden", j.pages.length < 2);
    await loadPage(0);
  } catch (e) {
    progHide();
    if (e.message !== "cancelado") toast(e.message, true, 6000);
  }
}

async function loadPage(p) {
  S.page = p;
  $("pgLabel").textContent = `${p + 1} / ${S.doc.pages.length}`;
  // opciones por plano: se reinician; se conservan las "pegajosas"
  const keep = {};
  for (const k of STICKY) keep[k] = S.opts[k];
  S.opts = { ...DEFAULT_OPTS, ...keep, rejected_hatches: [], layer_rename: {}, layer_exclude: [] };
  S.sel = null;
  S.hiddenLayers.clear();
  S.data = null;
  S.paths = null;
  draw();
  S.bg = null;
  try {
    const r = await runJob("load", { doc_id: S.doc.doc_id, page: p, opts: S.opts },
      `Convirtiendo página ${p + 1}`);
    S.analysis = r.analysis;
    Object.assign(S.opts, r.analysis.suggested || {});
    renderRecs();
    optsToUI();
    applyPreview(r.preview, true);
    // la imagen del PDF original se pide despues: no compite con la conversion
    loadBackground().then(draw);
    if (r.elapsed > 0) $("status").textContent = `Plano convertido en ${fmt(r.elapsed, 1)} s`;
  } catch (e) {
    progHide();
    if (e.message !== "cancelado") toast(e.message, true, 6000);
  }
}

function loadBackground() {
  return new Promise((res) => {
    const img = new Image();
    const page = S.doc.pages[S.page];
    const dpi = Math.min(200, Math.max(72, Math.round(3000 / (Math.max(page.w, page.h) / 72))));
    img.onload = () => { S.bg = img; res(); };
    img.onerror = () => { S.bg = null; res(); };
    Backend.render(S.doc.doc_id, S.page, dpi).then((url) => { img.src = url; }).catch(() => res());
  });
}

let procTimer = null;
function scheduleProcess() {
  clearTimeout(procTimer);
  procTimer = setTimeout(() => process(false), 250);
}

async function process(fit) {
  if (!S.doc) return;
  const id = ++S.reqId;
  try {
    const r = await runJob("process", { doc_id: S.doc.doc_id, page: S.page, opts: S.opts },
      "Actualizando vista previa", { compact: true, isCurrent: () => id === S.reqId });
    if (id !== S.reqId) return;
    applyPreview(r.preview, fit);
  } catch (e) {
    if (id === S.reqId) progHide();
    if (e.message !== "cancelado" && id === S.reqId) toast(e.message, true, 6000);
  }
}

function applyPreview(d, fit) {
  S.data = d;
  buildPaths();
  if (fit) { resize(); fitView(); }
  renderStats();
  renderHatchList();
  renderLayers();
  updateScaleInfo();
  draw();
}

/* ------------------------------------------------- tareas con progreso */
// Muestra el panel solo si la tarea dura mas de ~250 ms (sin parpadeos en cambios rapidos).
const PROG = { t0: 0, timer: null, shown: false, compact: false, last: -1, lastT: 0, eta: null };

function progShow(title, immediate, compact = false) {
  PROG.t0 = performance.now();
  PROG.compact = compact;
  $("progTitle").textContent = title;
  $("progMsg").textContent = "Iniciando…";
  $("progPct").textContent = "0%";
  $("progFill").style.width = "0%";
  $("progTime").textContent = "";
  $("prog").classList.toggle("compact", compact);
  PROG.last = -1;
  PROG.lastT = performance.now();
  PROG.eta = null;
  $("progFill").parentElement.classList.remove("stalled");
  clearTimeout(PROG.timer);
  const reveal = () => { PROG.shown = true; $("prog").classList.remove("hidden"); };
  if (immediate) reveal(); else PROG.timer = setTimeout(reveal, 250);
}

function progUpdate(frac, msg, elapsedSrv) {
  const pct = Math.max(0, Math.min(100, Math.floor(frac * 100)));
  $("progFill").style.width = pct + "%";
  $("progPct").textContent = pct + "%";
  if (msg) $("progMsg").textContent = msg;
  const now = performance.now();
  if (pct !== PROG.last) { PROG.last = pct; PROG.lastT = now; }
  // sin avance durante >1.2 s: barra a rayas animada (sigue trabajando)
  $("progFill").parentElement.classList.toggle("stalled", now - PROG.lastT > 1200 && pct < 100);
  const el = elapsedSrv || (now - PROG.t0) / 1000;
  let t = `Transcurrido ${fmt(el, 1)} s`;
  if (frac > 0.15 && frac < 0.99 && el > 2) {
    const raw = el * (1 - frac) / frac;
    // media movil para que el "quedan" no salte de un valor a otro
    PROG.eta = PROG.eta == null ? raw : PROG.eta * 0.7 + raw * 0.3;
    const rest = PROG.eta;
    t += ` · quedan ≈ ${rest < 60 ? fmt(Math.max(1, rest), 0) + " s" : fmt(rest / 60, 1) + " min"}`;
  }
  $("progTime").textContent = t;
}

function progHide() {
  clearTimeout(PROG.timer);
  PROG.shown = false;
  $("prog").classList.add("hidden");
}

/** Lanza una tarea en el motor (Web Worker) mostrando el progreso. */
async function runJob(kind, body, title, { compact = false, isCurrent = () => true } = {}) {
  if (isCurrent()) progShow(title, false, compact);
  const r = await Backend.job(kind, body, (f, m, el) => { if (isCurrent()) progUpdate(f, m, el); }, isCurrent);
  if (!isCurrent()) throw new Error("cancelado");
  progUpdate(1, "Listo", r.elapsed);
  progHide();
  return r;
}

/* -------------------------------------------------------- recomendaciones */
function renderRecs() {
  const an = S.analysis;
  const ul = $("recs");
  ul.innerHTML = "";
  $("recsEmpty").classList.toggle("hidden", !!an);
  if (!an) return;
  for (const r of an.recommendations) {
    const li = document.createElement("li");
    li.className = r.level;
    const applied = Object.keys(r.apply || {}).length ? '<span class="chip">aplicado</span>' : "";
    li.innerHTML = `<div class="t"><span></span>${applied}</div><div class="d"></div>`;
    li.querySelector(".t span").textContent = r.title;
    li.querySelector(".d").textContent = r.detail;
    ul.appendChild(li);
  }
  const m = an.metrics;
  $("metrics").innerHTML = `
    <span>Hoja: <b>${an.paper}</b></span><span>Capas PDF: <b>${m.capas}</b></span>
    <span>Trazados: <b>${fmt(m.trazados, 0)}</b></span><span>Segmentos: <b>${fmt(m.segmentos, 0)}</b></span>
    <span>Rellenos: <b>${fmt(m.rellenos, 0)}</b></span><span>Textos: <b>${fmt(m.textos, 0)}</b></span>`;
}

/* --------------------------------------------------------------- paneles */
function renderStats() {
  const s = S.data.stats;
  const rows = [
    ["Hatches reconstruidos", s.hatches], ["Líneas de hatch absorbidas", s.lineas_hatch_absorbidas],
    ["Rellenos sólidos", s.rellenos], ["Líneas", s.lineas], ["Polilíneas", s.polilineas],
    ["Círculos", s.circulos], ["Arcos", s.arcos], ["Splines", s.splines], ["Textos", s.textos],
  ];
  if (s.rellenos_blancos_omitidos) rows.push(["Rellenos blancos omitidos", s.rellenos_blancos_omitidos]);
  if (s.hatches_rechazados) rows.push(["Hatches rechazados", s.hatches_rechazados]);
  if (s.hatches_fusionados) rows.push(["Hatches fusionados (mismo patrón)", s.hatches_fusionados]);
  if (s.solapes_corregidos) rows.push(["Solapes corregidos", s.solapes_corregidos]);
  const total = s.lineas + s.polilineas + s.circulos + s.arcos + s.splines + s.hatches + s.rellenos + s.textos;
  let html = "";
  if (s.lineas_hatch_absorbidas) {
    html += `<div class="big">${fmt(s.lineas_hatch_absorbidas, 0)} líneas sueltas → ${s.hatches} hatches editables</div>`;
  }
  html += rows.map(([k, v]) => `<span>${k}</span><b>${fmt(v, 0)}</b>`).join("");
  html += `<span>Entidades CAD</span><b>${fmt(total, 0)}</b>`;
  $("stats").innerHTML = html;
}

function hatchLabel(h) {
  const f = h.families;
  const ang = f.map((x) => fmt(x.angle, 1) + "°").join(" + ");
  const name = h.ptype === "user" ? "Definido por usuario" : h.ptype === "custom" ? "Personalizado" : h.pattern;
  const sub = h.ptype === "predefined"
    ? `esc. ${fmt(h.pscale, 3)} · áng. ${fmt(h.pangle, 1)}°`
    : `${ang} · esp. ${fmt(f[0].spacing_model, 3)} ${S.opts.units}`;
  return { name, sub };
}

function areaTxt(a) {
  const u = S.opts.units;
  if (u === "mm" && a > 1e5) return `${fmt(a / 1e6, 3)} m²`;
  if (u === "cm" && a > 1e3) return `${fmt(a / 1e4, 3)} m²`;
  return `${fmt(a, 3)} ${u}²`;
}

function renderHatchList() {
  const ul = $("hList");
  ul.innerHTML = "";
  const hs = S.data.hatches;
  $("hCount").textContent = hs.filter((h) => !h.rejected).length;
  const list = hs.filter((h) => S.hFilter === "all" || (S.hFilter === "suspect" ? h.suspect : h.rejected));
  for (const h of list) {
    const li = document.createElement("li");
    li.className = (h.rejected ? "rej " : "") + (S.sel === h.id ? "sel" : "");
    const lb = hatchLabel(h);
    li.innerHTML = `<span class="sw" style="background:${h.color}"></span>
      <div><div class="nm"></div><div class="sm"></div></div>
      <button>${h.rejected ? "Restaurar" : "Rechazar"}</button>`;
    li.querySelector(".nm").textContent = `#${h.id} ${lb.name}`;
    if (h.suspect) li.querySelector(".nm").insertAdjacentHTML("beforeend", '<span class="tag s">dudoso</span>');
    if (h.snapped) li.querySelector(".nm").insertAdjacentHTML("beforeend", '<span class="tag k">ajustado</span>');
    li.querySelector(".sm").textContent = `${lb.sub} · ${h.n_lines} lín.`;
    li.addEventListener("click", (e) => {
      if (e.target.tagName === "BUTTON") { toggleReject(h.id); return; }
      select(h.id, true);
    });
    ul.appendChild(li);
  }
  if (!list.length) ul.innerHTML = '<li class="muted" style="cursor:default">Nada que mostrar</li>';
  renderHatchDetail();
}

function renderHatchDetail() {
  const box = $("hDetail");
  const h = S.data && S.data.hatches.find((x) => x.id === S.sel);
  if (!h) { box.classList.add("hidden"); return; }
  box.classList.remove("hidden");
  const lb = hatchLabel(h);
  const u = S.opts.units;
  const fam = h.families.map((f) => `${fmt(f.angle, 2)}° / ${fmt(f.spacing_model, 3)} ${u}${f.dashes.length ? " (discontinuo)" : ""}`).join("<br>");
  box.innerHTML = `<b>Hatch #${h.id}</b> — ${lb.name}${h.rejected ? " <span class='muted'>(rechazado)</span>" : ""}
    <div class="kv"><span>Capa</span><span></span>
    <span>Familias</span><span>${fam}</span>
    <span>Patrón CAD</span><span>${h.ptype === "predefined" ? `${h.pattern} · escala ${fmt(h.pscale, 3)} · ángulo ${fmt(h.pangle, 2)}°` : lb.name}</span>
    <span>Área</span><span>${areaTxt(h.area)}</span>
    <span>Líneas origen</span><span>${h.n_lines}</span>
    <span>Contorno</span><span>${h.snapped ? "ajustado a líneas existentes" : "deducido de las líneas"}</span></div>
    <button class="btn small">${h.rejected ? "Restaurar como hatch" : "Rechazar (dejar como líneas)"}</button>`;
  box.querySelector(".kv span:nth-child(2)").textContent = h.layer;
  box.querySelector("button").onclick = () => toggleReject(h.id);
}

function renderLayers() {
  const ul = $("lList");
  ul.innerHTML = "";
  const ls = S.data.layers;
  $("lCount").textContent = ls.length;
  for (const l of ls) {
    const li = document.createElement("li");
    const vis = !S.hiddenLayers.has(l.name);
    li.className = l.excluded ? "off" : "";
    li.innerHTML = `<span class="eye" title="Mostrar/ocultar en el visor">${vis ? "👁" : "◌"}</span>
      <input type="checkbox" title="Exportar esta capa" ${l.excluded ? "" : "checked"}>
      <span class="sw" style="background:${l.color}"></span>
      <input type="text" spellcheck="false">
      <span class="cnt">${l.count}</span>`;
    const inp = li.querySelector("input[type=text]");
    inp.value = l.rename || l.name;
    inp.title = "Origen: " + l.name;
    li.querySelector(".eye").onclick = () => {
      if (S.hiddenLayers.has(l.name)) S.hiddenLayers.delete(l.name);
      else S.hiddenLayers.add(l.name);
      renderLayers();
      draw();
    };
    li.querySelector("input[type=checkbox]").onchange = (e) => {
      const ex = new Set(S.opts.layer_exclude);
      if (e.target.checked) ex.delete(l.name); else ex.add(l.name);
      S.opts.layer_exclude = [...ex];
      scheduleProcess();
    };
    inp.onchange = () => {
      const v = inp.value.trim();
      if (!v || v === l.name) delete S.opts.layer_rename[l.name];
      else S.opts.layer_rename[l.name] = v;
      scheduleProcess();
    };
    ul.appendChild(li);
  }
}

function select(id, center) {
  S.sel = id;
  renderHatchList();
  if (center && id != null) {
    const h = S.data.hatches.find((x) => x.id === id);
    if (h) centerOn(h);
  }
  draw();
}

function toggleReject(id) {
  const r = new Set(S.opts.rejected_hatches);
  if (r.has(id)) r.delete(id); else r.add(id);
  S.opts.rejected_hatches = [...r];
  S.sel = id;
  process(false);
}

/* --------------------------------------------------------------- dibujo */
function srcLayer(name) {
  // nombre de capa original (las capas renombradas llegan con el nombre nuevo)
  const ren = S.opts.layer_rename || {};
  for (const k in ren) if (ren[k] === name) return k;
  return name;
}
function layerVisible(name) {
  return !S.hiddenLayers.has(srcLayer(name));
}

function ringsPath(rings) {
  const p = new Path2D();
  for (const r of rings) {
    p.moveTo(r[0], r[1]);
    for (let i = 2; i < r.length; i += 2) p.lineTo(r[i], r[i + 1]);
    p.closePath();
  }
  return p;
}
function ringsBBox(rings) {
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const r of rings) for (let i = 0; i < r.length; i += 2) {
    if (r[i] < x0) x0 = r[i]; if (r[i] > x1) x1 = r[i];
    if (r[i + 1] < y0) y0 = r[i + 1]; if (r[i + 1] > y1) y1 = r[i + 1];
  }
  return [x0, y0, x1, y1];
}

function buildPaths() {
  const d = S.data;
  const strokes = d.strokes.map((g) => {
    const p = new Path2D();
    for (const a of g.paths) {
      p.moveTo(a[0], a[1]);
      for (let i = 2; i < a.length; i += 2) p.lineTo(a[i], a[i + 1]);
    }
    return { g, p };
  });
  const hatches = d.hatches.map((h) => ({ h, p: ringsPath(h.rings), bb: ringsBBox(h.rings) }));
  const fills = d.fills.map((f) => ({ f, p: ringsPath(f.rings) }));
  // vertices para el "snap" de calibracion
  const verts = [];
  for (const g of d.strokes) for (const a of g.paths) {
    verts.push(a[0], a[1], a[a.length - 2], a[a.length - 1]);
    if (a.length <= 12) for (let i = 2; i < a.length - 2; i += 2) verts.push(a[i], a[i + 1]);
  }
  S.paths = { strokes, hatches, fills, verts: new Float32Array(verts) };
}

function resize() {
  const r = cv.parentElement.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
  if (w === cv.width && h === cv.height) {
    if (S.needFit && w > 50 && h > 50) fitView();
    return draw();
  }
  // mantener el centro de la vista al cambiar el tamano
  S.view.tx += (w - cv.width) / 2;
  S.view.ty += (h - cv.height) / 2;
  cv.width = w;
  cv.height = h;
  // si el encuadre se pidio cuando el lienzo aun no tenia tamaño, hacerlo ahora
  if (S.needFit && w > 50 && h > 50) return fitView();
  draw();
}

function fitView() {
  if (!S.doc) return;
  const p = S.doc.pages[S.page];
  const W = cv.width, H = cv.height;
  if (W < 50 || H < 50) { S.needFit = true; return; }  // aun sin tamaño (pestaña oculta, etc.)
  S.needFit = false;
  const s = Math.min(W / p.w, H / p.h) * 0.94;
  S.view = { s, tx: (W - p.w * s) / 2, ty: (H + p.h * s) / 2 };
  draw();
}
function centerOn(h) {
  const [x0, y0, x1, y1] = ringsBBox(h.rings);
  const W = cv.width, H = cv.height;
  let s = Math.min(W / (x1 - x0 + 1), H / (y1 - y0 + 1)) * 0.6;
  s = Math.min(s, S.view.s * 4, 60);
  s = Math.max(s, S.view.s);
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  S.view = { s, tx: W / 2 - cx * s, ty: H / 2 + cy * s };
}
function toPaper(px, py) {
  const v = S.view;
  return [(px - v.tx) / v.s, (v.ty - py) / v.s];
}
function toScreen(x, y) {
  const v = S.view;
  return [x * v.s + v.tx, -y * v.s + v.ty];
}

function colorFor(c, dark) {
  if (!dark) return c;
  const l = c.toLowerCase();
  if (l === "#000000") return "#f2f2f2";
  if (l === "#ffffff") return "#1e2329";
  return c;
}

let rafPending = false;
function draw() {
  if (rafPending) return;
  rafPending = true;
  requestAnimationFrame(() => { rafPending = false; drawNow(); });
}

function drawNow() {
  const dark = $("vDark").checked;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = "#0f1115";
  ctx.fillRect(0, 0, cv.width, cv.height);
  if (!S.doc) return;
  const v = S.view;
  const page = S.doc.pages[S.page];
  const [sx, sy] = toScreen(0, page.h);
  ctx.fillStyle = dark ? "#1e2329" : "#ffffff";
  ctx.shadowColor = "rgba(0,0,0,.5)";
  ctx.shadowBlur = 18;
  ctx.fillRect(sx, sy, page.w * v.s, page.h * v.s);
  ctx.shadowBlur = 0;

  if ($("vPdf").checked && S.bg) {
    ctx.globalAlpha = $("vPdfOpacity").value / 100;
    ctx.drawImage(S.bg, sx, sy, page.w * v.s, page.h * v.s);
    ctx.globalAlpha = 1;
  }
  if (!S.paths) return;
  const px = 1 / v.s; // 1 pixel en unidades de papel
  ctx.setTransform(v.s, 0, 0, -v.s, v.tx, v.ty);
  ctx.lineCap = "round";
  ctx.lineJoin = "round";

  // rellenos
  if ($("vFill").checked) {
    for (const { f, p } of S.paths.fills) {
      if (!layerVisible(f.layer)) continue;
      ctx.fillStyle = colorFor(f.color, dark);
      ctx.fill(p, "evenodd");
    }
  }

  // hatches
  if ($("vHatch").checked) {
    for (const { h, p, bb } of S.paths.hatches) {
      if (h.rejected || !layerVisible(h.layer)) continue;
      drawHatch(h, p, bb, px, dark);
    }
  }

  // lineas
  if ($("vLines").checked) {
    for (const { g, p } of S.paths.strokes) {
      if (!layerVisible(g.layer)) continue;
      ctx.strokeStyle = colorFor(g.color, dark);
      ctx.lineWidth = Math.max(px * 0.8, g.width || 0);
      ctx.setLineDash(g.dashed ? [6 * px, 3 * px] : []);
      ctx.stroke(p);
    }
    ctx.setLineDash([]);
  }

  // textos
  if ($("vText").checked) {
    for (const t of S.data.texts) {
      if (!layerVisible(t.layer) || t.size * v.s < 2) continue;
      ctx.save();
      ctx.translate(t.x, t.y);
      ctx.rotate((t.angle * Math.PI) / 180);
      ctx.scale(1, -1);
      ctx.fillStyle = colorFor(t.color, dark);
      ctx.font = `${t.size}px Arial, sans-serif`;
      ctx.fillText(t.text, 0, 0);
      ctx.restore();
    }
  }

  // marcas: dudosos, rechazados, seleccion
  for (const { h, p } of S.paths.hatches) {
    if (!layerVisible(h.layer)) continue;
    if (h.id === S.sel) continue;
    if (h.rejected) {
      ctx.strokeStyle = "rgba(239,90,90,.8)";
      ctx.lineWidth = 1.2 * px;
      ctx.setLineDash([5 * px, 4 * px]);
      ctx.stroke(p);
    } else if (h.suspect) {
      ctx.fillStyle = "rgba(240,163,58,.13)";
      ctx.fill(p, "evenodd");
      ctx.strokeStyle = "rgba(240,163,58,.95)";
      ctx.lineWidth = 1.5 * px;
      ctx.setLineDash([6 * px, 3 * px]);
      ctx.stroke(p);
    }
  }
  ctx.setLineDash([]);
  const selH = S.paths.hatches.find((x) => x.h.id === S.sel);
  if (selH) {
    ctx.fillStyle = "rgba(59,156,255,.18)";
    ctx.fill(selH.p, "evenodd");
    ctx.strokeStyle = "#3b9cff";
    ctx.lineWidth = 2 * px;
    ctx.stroke(selH.p);
  }

  // calibracion
  if (S.calib) {
    ctx.strokeStyle = "#ff3bd4";
    ctx.fillStyle = "#ff3bd4";
    ctx.lineWidth = 1.5 * px;
    const pts = S.calib.pts.slice();
    if (S.calib.hover) pts.push(S.calib.hover);
    if (pts.length === 2) {
      ctx.beginPath(); ctx.moveTo(pts[0][0], pts[0][1]); ctx.lineTo(pts[1][0], pts[1][1]); ctx.stroke();
    }
    for (const q of pts) {
      ctx.beginPath(); ctx.arc(q[0], q[1], 4 * px, 0, Math.PI * 2); ctx.fill();
    }
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
}

function drawHatch(h, path, bb, px, dark) {
  const col = colorFor(h.color, dark);
  const minSp = Math.min(...h.families.map((f) => f.spacing));
  ctx.save();
  ctx.clip(path, "evenodd");
  if (minSp / px < 2.2) {
    // demasiado denso para dibujar las lineas: tono
    ctx.globalAlpha = 0.35;
    ctx.fillStyle = col;
    ctx.fill(path, "evenodd");
    ctx.restore();
    return;
  }
  const [x0, y0, x1, y1] = bb;
  const cx = (x0 + x1) / 2, cy = (y0 + y1) / 2;
  const R = Math.hypot(x1 - x0, y1 - y0) / 2 + 1;
  ctx.strokeStyle = col;
  ctx.lineWidth = Math.max(px * 0.8, 0.1);
  for (const f of h.families) {
    const a = (f.angle * Math.PI) / 180;
    const ux = Math.cos(a), uy = Math.sin(a), nx = -uy, ny = ux;
    const db = f.base[0] * nx + f.base[1] * ny;
    const dc = cx * nx + cy * ny;
    const k0 = Math.ceil((dc - R - db) / f.spacing), k1 = Math.floor((dc + R - db) / f.spacing);
    ctx.setLineDash(f.dashes.length ? f.dashes.map(Math.abs) : []);
    if (f.dashes.length) ctx.lineDashOffset = 0;
    ctx.beginPath();
    for (let k = k0; k <= k1; k++) {
      const d = db + k * f.spacing;
      // punto de la linea: base + (d-db)*n ; centrado cerca del bbox
      const qx = f.base[0] + (d - db) * nx, qy = f.base[1] + (d - db) * ny;
      const tc = (cx - qx) * ux + (cy - qy) * uy;
      // mantener la fase del discontinuo: avanzar en multiplos del periodo
      let t0 = tc - R;
      if (f.dashes.length) {
        const per = f.dashes.reduce((s, v) => s + Math.abs(v), 0);
        t0 = Math.floor(t0 / per) * per;
      }
      ctx.moveTo(qx + ux * t0, qy + uy * t0);
      ctx.lineTo(qx + ux * (tc + R), qy + uy * (tc + R));
    }
    ctx.stroke();
  }
  ctx.setLineDash([]);
  ctx.restore();
}

/* ---------------------------------------------------------- interaccion */
let drag = null;
cv.addEventListener("mousedown", (e) => {
  const dpr = window.devicePixelRatio || 1;
  drag = { x: e.clientX, y: e.clientY, tx: S.view.tx, ty: S.view.ty, moved: false, dpr };
});
window.addEventListener("mousemove", (e) => {
  const rect = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const mx = (e.clientX - rect.left) * dpr, my = (e.clientY - rect.top) * dpr;
  if (drag) {
    const dx = (e.clientX - drag.x) * dpr, dy = (e.clientY - drag.y) * dpr;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    if (drag.moved) {
      cv.classList.add("panning");
      S.view.tx = drag.tx + dx;
      S.view.ty = drag.ty + dy;
      draw();
    }
  }
  if (S.doc && e.target === cv) {
    const [x, y] = toPaper(mx, my);
    const k = S.data ? S.data.k : modelFactor();
    $("status").textContent = `X ${fmt(x * k, 3)}  Y ${fmt(y * k, 3)} ${S.opts.units}   ·   zoom ${fmt(S.view.s * 100, 0)}%`;
    if (S.calib) {
      S.calib.hover = snap(mx, my);
      draw();
    }
  }
});
window.addEventListener("mouseup", (e) => {
  if (!drag) return;
  const wasClick = !drag.moved && e.target === cv;
  drag = null;
  cv.classList.remove("panning");
  if (!wasClick || !S.data) return;
  const rect = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const mx = (e.clientX - rect.left) * dpr, my = (e.clientY - rect.top) * dpr;
  if (S.calib) { calibClick(mx, my); return; }
  // seleccionar hatch bajo el cursor (el de menor area si se solapan)
  const v = S.view;
  ctx.setTransform(v.s, 0, 0, -v.s, v.tx, v.ty);
  let best = null;
  for (const { h, p } of S.paths.hatches) {
    if (!layerVisible(h.layer)) continue;
    if (ctx.isPointInPath(p, mx, my, "evenodd")) {
      if (!best || h.area < best.area) best = h;
    }
  }
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  select(best ? best.id : null, false);
});
cv.addEventListener("wheel", (e) => {
  e.preventDefault();
  const rect = cv.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const mx = (e.clientX - rect.left) * dpr, my = (e.clientY - rect.top) * dpr;
  const f = Math.exp(-e.deltaY * 0.0015);
  const v = S.view;
  const ns = Math.min(400, Math.max(0.02, v.s * f));
  const r = ns / v.s;
  v.tx = mx - (mx - v.tx) * r;
  v.ty = my - (my - v.ty) * r;
  v.s = ns;
  draw();
}, { passive: false });

window.addEventListener("keydown", (e) => {
  if (e.target.tagName === "INPUT" || e.target.tagName === "SELECT") return;
  if (e.key === "f" || e.key === "F") fitView();
  if ((e.key === "r" || e.key === "R") && S.sel != null) toggleReject(S.sel);
  if (e.key === "Escape") { if (S.calib) endCalib(); else select(null); }
});

/* ---------------------------------------------------------- calibracion */
function snap(mx, my) {
  const [x, y] = toPaper(mx, my);
  const tol = 10 * (window.devicePixelRatio || 1) / S.view.s;
  let best = null, bd = tol * tol;
  const vs = S.paths ? S.paths.verts : [];
  for (let i = 0; i < vs.length; i += 2) {
    const dx = vs[i] - x, dy = vs[i + 1] - y;
    const d = dx * dx + dy * dy;
    if (d < bd) { bd = d; best = [vs[i], vs[i + 1]]; }
  }
  return best || [x, y];
}
function startCalib() {
  if (!S.data) return;
  S.calib = { pts: [], hover: null };
  cv.classList.add("calib");
  $("calibBar").classList.remove("hidden");
  $("calibMsg").textContent = "Clic en el primer extremo de una cota conocida (se ajusta a vértices)";
}
function endCalib() {
  S.calib = null;
  cv.classList.remove("calib");
  $("calibBar").classList.add("hidden");
  draw();
}
function calibClick(mx, my) {
  S.calib.pts.push(snap(mx, my));
  if (S.calib.pts.length === 1) {
    $("calibMsg").textContent = "Clic en el segundo extremo";
    draw();
    return;
  }
  const [a, b] = S.calib.pts;
  const dpt = Math.hypot(b[0] - a[0], b[1] - a[1]);
  if (dpt < 1e-6) { toast("Los puntos coinciden", true); S.calib.pts = []; return; }
  const cur = dpt * modelFactor();
  openModal("Calibrar escala",
    `Distancia actual: ${fmt(cur, 3)} ${S.opts.units}. Escribe la medida real de esa cota:`,
    S.opts.units, (val) => {
      if (!(val > 0)) return;
      S.opts.scale_mode = "factor";
      S.opts.scale_factor = val / dpt;
      endCalib();
      updateScaleInfo();
      toast(`Escala calibrada ≈ 1:${fmt(S.opts.scale_factor * UNIT_DIV[S.opts.units] / PT_TO_MM, 2)}`);
      process(false);
    }, () => endCalib());
}
function openModal(title, text, units, onOk, onCancel) {
  $("mTitle").textContent = title;
  $("mText").textContent = text;
  $("mUnits").textContent = units;
  $("mInput").value = "";
  $("modal").classList.remove("hidden");
  setTimeout(() => $("mInput").focus(), 30);
  const close = () => $("modal").classList.add("hidden");
  $("mOk").onclick = () => { const v = parseFloat($("mInput").value.replace(",", ".")); close(); onOk(v); };
  $("mCancel").onclick = () => { close(); onCancel && onCancel(); };
  $("mInput").onkeydown = (e) => {
    if (e.key === "Enter") $("mOk").click();
    if (e.key === "Escape") $("mCancel").click();
  };
}

/* ---------------------------------------------------------------- export */
async function doExport(fmtName) {
  if (!S.doc) return;
  try {
    const r = await runJob("export",
      { doc_id: S.doc.doc_id, page: S.page, opts: S.opts, format: fmtName },
      `Exportando ${fmtName.toUpperCase()}`);
    const a = document.createElement("a");
    a.href = r.url;
    a.download = r.name;
    document.body.appendChild(a);
    a.click();
    setTimeout(() => a.remove(), 2000);
    toast(`Exportado ${r.name} en ${fmt(r.elapsed, 1)} s`);
  } catch (e) {
    progHide();
    if (e.message !== "cancelado") toast(e.message, true, 7000);
  }
}

/* --------------------------------------------------------------- arranque */
async function initBackend() {
  progShow("Preparando el motor de conversión", true);
  try {
    await Backend.init((f, m) => progUpdate(f, m));
  } catch (e) {
    toast("No se pudo iniciar el motor: " + e.message, true, 10000);
  } finally {
    progHide();
  }
}

function init() {
  initBackend();
  loadSticky();
  optsToUI();
  bindOpts();
  window.addEventListener("resize", resize);
  if (window.ResizeObserver) new ResizeObserver(() => resize()).observe($("viewer"));
  resize();

  const drop = $("drop");
  const viewer = $("viewer");
  ["dragenter", "dragover"].forEach((ev) => viewer.addEventListener(ev, (e) => {
    e.preventDefault();
    drop.classList.remove("hidden");
    drop.classList.add("over");
  }));
  ["dragleave", "drop"].forEach((ev) => viewer.addEventListener(ev, (e) => {
    e.preventDefault();
    if (ev === "dragleave" && viewer.contains(e.relatedTarget)) return;
    drop.classList.remove("over");
    if (S.doc) drop.classList.add("hidden");
  }));
  viewer.addEventListener("drop", (e) => {
    const f = e.dataTransfer.files && e.dataTransfer.files[0];
    if (f) uploadFile(f);
  });
  document.addEventListener("dragover", (e) => e.preventDefault());
  document.addEventListener("drop", (e) => e.preventDefault());
  $("fileInput").addEventListener("change", (e) => uploadFile(e.target.files[0]));
  $("fileInput2").addEventListener("change", (e) => uploadFile(e.target.files[0]));

  $("btnDxf").onclick = () => doExport("dxf");
  $("tFit").onclick = fitView;
  ["vPdf", "vHatch", "vFill", "vLines", "vText", "vDark"].forEach((id) => $(id).addEventListener("change", draw));
  $("vPdfOpacity").addEventListener("input", () => { $("vPdf").checked = true; draw(); });
  $("btnCalib").onclick = startCalib;
  $("calibCancel").onclick = endCalib;
  $("pgPrev").onclick = () => S.doc && S.page > 0 && loadPage(S.page - 1);
  $("pgNext").onclick = () => S.doc && S.page < S.doc.pages.length - 1 && loadPage(S.page + 1);
  $("hFilter").addEventListener("click", (e) => {
    const b = e.target.closest("button");
    if (!b) return;
    S.hFilter = b.dataset.f;
    for (const x of $("hFilter").children) x.classList.toggle("on", x === b);
    if (S.data) renderHatchList();
  });
  $("btnRejectSuspect").onclick = () => {
    if (!S.data) return;
    const r = new Set(S.opts.rejected_hatches);
    S.data.hatches.filter((h) => h.suspect).forEach((h) => r.add(h.id));
    S.opts.rejected_hatches = [...r];
    process(false);
  };
  $("btnRestoreAll").onclick = () => { S.opts.rejected_hatches = []; process(false); };
}
init();
