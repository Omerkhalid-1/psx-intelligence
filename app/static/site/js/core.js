// Shared helpers for the PSX Intelligence website.
// Security: server data is read from a JSON <script> block and inserted with textContent only.
// Motion: everything animated respects prefers-reduced-motion.

export const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export function pageData() {
  const el = document.getElementById("page-data");
  try { return el ? JSON.parse(el.textContent || "{}") : {}; } catch (e) { return {}; }
}

/** Tiny DOM builder: h("div", {class: "x", onclick: fn}, "text", child). Strings become text nodes. */
export function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === null || v === undefined || v === false) continue;
    if (k === "class") node.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(node.style, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2), v);
    else if (k === "dataset") Object.assign(node.dataset, v);
    else node.setAttribute(k, v === true ? "" : String(v));
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return node;
}

export function svg(tag, attrs, ...children) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs || {})) if (v !== null && v !== undefined) node.setAttribute(k, String(v));
  for (const c of children.flat()) if (c) node.append(c instanceof Node ? c : document.createTextNode(String(c)));
  return node;
}

// ---------- formatting ----------
const nf = (d) => new Intl.NumberFormat("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
export function num(v, d = 2) { return v === null || v === undefined || Number.isNaN(v) ? "n/a" : nf(d).format(v); }
export function pct(v, d = 1, signed = true) {
  if (v === null || v === undefined || Number.isNaN(v)) return "n/a";
  const s = (v * 100).toFixed(d);
  return (signed && v > 0 ? "+" : "") + s + "%";
}
export function deltaClass(v) { return v > 0 ? "delta-pos" : v < 0 ? "delta-neg" : "muted"; }
export function toneClass(tone) {
  return tone === 1 ? "tone-pos" : tone === -1 ? "tone-neg" : tone === 0 ? "tone-neu" : tone === null ? "tone-desc" : "tone-na";
}
export function toneWord(tone) { return tone === 1 ? "positive" : tone === -1 ? "negative" : tone === 0 ? "neutral" : "descriptive"; }
export function humanState(s) { return (s || "").replaceAll("_", " "); }
export const BALANCE_WORDS = {
  strongly_positive: "Strongly positive", leaning_positive: "Leaning positive", mixed: "Mixed",
  leaning_negative: "Leaning negative", strongly_negative: "Strongly negative", insufficient_data: "Not enough data",
};
export function ago(iso) {
  if (!iso) return "";
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400 * 2) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
}
export function shortDate(iso) {
  if (!iso) return "";
  return new Date(iso).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric", timeZone: "Asia/Karachi" });
}

// ---------- deployment mode ----------
// The same pages run on the Flask server (live JSON API) or as a static export (GitHub Pages), where the
// API is replaced by pre-built JSON files under <base>/data/. <body data-static data-base> tells which.
export const STATIC = document.body.dataset.static === "1";
export const BASE = document.body.dataset.base || "";

/** Link to a site page ("/stock/SYS", "/news?symbol=X") under the deployment base path. */
export function pageUrl(path) {
  if (!STATIC) return BASE + path;
  const [p, q] = path.split("?");
  return BASE + (p.endsWith("/") ? p : p + "/") + (q ? "?" + q : "");
}

let symbolIndex = null;
async function staticJSON(url) {
  const u = new URL(url, window.location.origin);
  const path = u.pathname, qs = u.searchParams;
  const file = (name) => `${BASE}/data/${name}`;
  let target = null;
  if (path === "/api/v1/engine/diagnostics") target = file("diagnostics.json");
  else if (path.startsWith("/api/v1/engine/")) target = file(`engine/${encodeURIComponent(path.split("/").pop())}.json`);
  else if (path === "/api/v1/news/search") {
    const sym = (qs.get("symbol") || "").toUpperCase();
    if (!symbolIndex) symbolIndex = (await fetchJSON(file("symbols.json"))).data;
    const row = symbolIndex.find((r) => r.symbol === sym);
    if (!row || !row.tracked) {
      const e = new Error(row ? "In this static edition, news is pre-built for the tracked watchlist only."
        : `${sym} is not a listed PSX symbol`);
      e.status = 404; e.code = row ? "static_untracked" : "unknown_symbol";
      throw e;
    }
    target = file(`news/${encodeURIComponent(sym)}.json`);
  } else if (path === "/api/v1/symbols/search") {
    // Server ranking (ticker prefix, then company-name substring), with tracked symbols first: only they
    // have pre-built news here.
    if (!symbolIndex) symbolIndex = (await fetchJSON(file("symbols.json"))).data;
    const q = (qs.get("q") || "").trim(), qu = q.toUpperCase(), ql = q.toLowerCase(), limit = Number(qs.get("limit") || 8);
    const bySym = symbolIndex.filter((r) => r.symbol.startsWith(qu)).sort((a, b) => a.symbol.localeCompare(b.symbol));
    const byName = symbolIndex.filter((r) => !r.symbol.startsWith(qu) && r.name.toLowerCase().includes(ql)).sort((a, b) => a.symbol.localeCompare(b.symbol));
    const ranked = bySym.concat(byName).sort((a, b) => Number(b.tracked) - Number(a.tracked));
    return { data: q ? ranked.slice(0, limit) : [], warnings: [] };
  }
  if (!target) { const e = new Error("Not available in the static edition"); e.status = 404; throw e; }
  return fetchJSON(target);
}

// ---------- network ----------
export async function getJSON(url) {
  return STATIC ? staticJSON(url) : fetchJSON(url);
}

async function fetchJSON(url) {
  const resp = await fetch(url, { headers: { Accept: "application/json" }, credentials: "same-origin" });
  let body = null;
  try { body = await resp.json(); } catch (e) { /* non-JSON */ }
  if (!resp.ok) {
    const err = (body && body.errors && body.errors[0]) || {};
    const e = new Error(err.message || `HTTP ${resp.status}`);
    e.status = resp.status; e.code = err.code; e.details = err.details;
    throw e;
  }
  return body;
}

// ---------- motion ----------
export function reveal(root = document) {
  const items = root.querySelectorAll(".reveal:not(.in)");
  if (reducedMotion || !("IntersectionObserver" in window)) { items.forEach((n) => n.classList.add("in")); return; }
  const io = new IntersectionObserver((entries) => {
    for (const e of entries) if (e.isIntersecting) { e.target.classList.add("in"); io.unobserve(e.target); }
  }, { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
  items.forEach((n) => io.observe(n));
}

export function countUp(el, to, { decimals = 2, duration = 1600, prefix = "", suffix = "" } = {}) {
  if (to === null || to === undefined) { el.textContent = "n/a"; return; }
  const fmt = (v) => prefix + nf(decimals).format(v) + suffix;
  if (reducedMotion) { el.textContent = fmt(to); return; }
  const from = to * 0.92;
  const t0 = performance.now();
  const step = (t) => {
    const p = Math.min(1, (t - t0) / duration);
    const e = 1 - Math.pow(1 - p, 4);
    el.textContent = fmt(from + (to - from) * e);
    if (p < 1) requestAnimationFrame(step);
  };
  requestAnimationFrame(step);
}

export function tilt(el, max = 6) {
  if (reducedMotion || window.matchMedia("(hover: none)").matches) return;
  el.addEventListener("pointermove", (ev) => {
    const r = el.getBoundingClientRect();
    const x = (ev.clientX - r.left) / r.width - 0.5, y = (ev.clientY - r.top) / r.height - 0.5;
    el.style.transform = `perspective(900px) rotateX(${(-y * max).toFixed(2)}deg) rotateY(${(x * max).toFixed(2)}deg) translateY(-3px)`;
  });
  el.addEventListener("pointerleave", () => { el.style.transform = ""; });
}

// ---------- small visual components ----------
/** Animated sparkline into an <svg> (values = closes). Colour follows the period's direction. */
export function sparkline(svgEl, values, { delay = 0 } = {}) {
  const pts = values.filter((v) => v !== null && v !== undefined);
  if (pts.length < 2) return;
  const W = 300, H = 64, pad = 3;
  const lo = Math.min(...pts), hi = Math.max(...pts), span = hi - lo || 1;
  const x = (i) => pad + (i / (pts.length - 1)) * (W - 2 * pad);
  const y = (v) => H - pad - ((v - lo) / span) * (H - 2 * pad);
  const d = pts.map((v, i) => `${i ? "L" : "M"}${x(i).toFixed(1)},${y(v).toFixed(1)}`).join(" ");
  const up = pts[pts.length - 1] >= pts[0];
  const color = up ? "var(--pos)" : "var(--neg)";
  const id = "g" + Math.random().toString(36).slice(2, 8);
  svgEl.setAttribute("viewBox", `0 0 ${W} ${H}`);
  svgEl.setAttribute("preserveAspectRatio", "none");
  svgEl.replaceChildren(
    svg("defs", {}, svg("linearGradient", { id, x1: 0, y1: 0, x2: 0, y2: 1 },
      svg("stop", { offset: "0%", "stop-color": up ? "#1fae84" : "#e0683a", "stop-opacity": 0.55 }),
      svg("stop", { offset: "100%", "stop-color": up ? "#1fae84" : "#e0683a", "stop-opacity": 0 }))),
    svg("path", { class: "area", d: `${d} L${x(pts.length - 1)},${H} L${x(0)},${H} Z`, fill: `url(#${id})` }),
    svg("path", { class: "line draw", d, stroke: color, style: `--delay:${delay}s` }),
    svg("circle", { cx: x(pts.length - 1), cy: y(pts[pts.length - 1]), r: 3.2, fill: color }),
  );
  const line = svgEl.querySelector("path.line");
  const len = line.getTotalLength ? line.getTotalLength() : 1000;
  line.style.setProperty("--len", String(Math.ceil(len)));
}

/** Balance meter: score in [-1, 1] positions the needle. */
export function meter(container, score) {
  const pos = score === null || score === undefined ? 50 : 50 + score * 50;
  const needle = h("div", { class: "needle", role: "presentation" });
  container.replaceChildren(h("div", { class: "tick" }), needle);
  requestAnimationFrame(() => requestAnimationFrame(() => { needle.style.left = `${pos}%`; }));
}

/** Semicircle gauge for the indicator balance. */
export function gauge(container, score, label) {
  const cx = 140, cy = 140, r = 110;
  const pt = (a) => [cx + r * Math.cos(a), cy - r * Math.sin(a)];
  const arc = (a0, a1) => { const [x0, y0] = pt(a0), [x1, y1] = pt(a1); return `M${x0},${y0} A${r},${r} 0 0 1 ${x1},${y1}`; };
  const s = score === null || score === undefined ? 0 : Math.max(-1, Math.min(1, score));
  const angle = (s * 90).toFixed(1); // -90 (left) .. +90 (right)
  const needle = svg("g", { class: "needle", style: `transform: rotate(-90deg)` },
    svg("line", { x1: cx, y1: cy, x2: cx, y2: cy - r + 18, stroke: "#fff", "stroke-width": 3, "stroke-linecap": "round" }),
    svg("circle", { cx, cy, r: 8, fill: "#fff" }));
  const el = svg("svg", { class: "gauge", viewBox: "0 0 280 170", role: "img", "aria-label": `Indicator balance ${label}` },
    svg("defs", {}, svg("linearGradient", { id: "gaugeGrad", x1: "0%", y1: "0%", x2: "100%", y2: "0%" },
      svg("stop", { offset: "0%", "stop-color": "#e0683a" }), svg("stop", { offset: "50%", "stop-color": "#4a5070" }),
      svg("stop", { offset: "100%", "stop-color": "#1fae84" }))),
    svg("path", { class: "arc-bg", d: arc(Math.PI, 0) }),
    svg("path", { class: "arc", d: arc(Math.PI, 0), stroke: "url(#gaugeGrad)", opacity: 0.9 }),
    svg("text", { x: 22, y: 166, "font-size": 11, fill: "#8691b6" }, "NEGATIVE"),
    svg("text", { x: 196, y: 166, "font-size": 11, fill: "#8691b6" }, "POSITIVE"),
    needle);
  container.replaceChildren(el);
  requestAnimationFrame(() => requestAnimationFrame(() => { needle.style.transform = `rotate(${angle}deg)`; }));
}

export function chip(text, tone, extraClass = "") {
  return h("span", { class: `chip ${toneClass(tone)} ${extraClass}`, title: toneWord(tone) }, text);
}

// ---------- animated backdrop: a perspective grid with flowing "price" traces ----------
export function backdrop(canvas) {
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext("2d");
  let W = 0, H = 0, dpr = 1, raf = 0, t = 0;
  const traces = [
    { c: "rgba(70,217,255,", y: 0.42, a: 0.06, f: 1.3, s: 0.00055, w: 1.6 },
    { c: "rgba(155,123,255,", y: 0.55, a: 0.05, f: 0.9, s: 0.0004, w: 1.2 },
    { c: "rgba(31,174,132,", y: 0.68, a: 0.04, f: 1.8, s: 0.0007, w: 1 },
  ];
  const dots = Array.from({ length: 46 }, () => ({ x: Math.random(), y: Math.random(), v: 0.00004 + Math.random() * 0.00012, r: Math.random() * 1.4 + 0.3 }));
  function resize() {
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    W = canvas.clientWidth; H = canvas.clientHeight;
    canvas.width = Math.floor(W * dpr); canvas.height = Math.floor(H * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  }
  const noise = (x, k) => Math.sin(x * 2.1 + k) * 0.5 + Math.sin(x * 5.3 + k * 1.7) * 0.25 + Math.sin(x * 11.7 + k * 0.6) * 0.12;
  function frame(dt) {
    ctx.clearRect(0, 0, W, H);
    // perspective grid (floor)
    const horizon = H * 0.62;
    ctx.lineWidth = 1;
    for (let i = 0; i <= 18; i++) {
      const p = i / 18, y = horizon + Math.pow(p, 2.2) * (H - horizon);
      ctx.strokeStyle = `rgba(90,130,255,${0.03 + p * 0.07})`;
      ctx.beginPath(); ctx.moveTo(0, y); ctx.lineTo(W, y); ctx.stroke();
    }
    const offset = (t * 0.00004) % 1;
    ctx.strokeStyle = "rgba(90,130,255,0.055)";
    for (let i = -24; i <= 24; i++) {
      const k = i + offset;
      ctx.beginPath(); ctx.moveTo(W / 2 + k * 14, horizon); ctx.lineTo(W / 2 + k * (W / 9), H); ctx.stroke();
    }
    // flowing traces
    for (const tr of traces) {
      ctx.beginPath();
      for (let x = 0; x <= W; x += 6) {
        const u = x / W;
        const y = H * tr.y + noise(u * tr.f * 3 + t * tr.s, tr.f) * H * tr.a * 2.2 - u * H * 0.05;
        x === 0 ? ctx.moveTo(x, y) : ctx.lineTo(x, y);
      }
      ctx.strokeStyle = tr.c + "0.35)"; ctx.lineWidth = tr.w; ctx.shadowColor = tr.c + "0.8)"; ctx.shadowBlur = 12; ctx.stroke();
      ctx.shadowBlur = 0;
    }
    // drifting particles
    for (const d of dots) {
      d.y -= d.v * dt; if (d.y < -0.02) { d.y = 1.02; d.x = Math.random(); }
      ctx.fillStyle = "rgba(160,200,255,0.35)"; ctx.beginPath(); ctx.arc(d.x * W, d.y * H, d.r, 0, Math.PI * 2); ctx.fill();
    }
  }
  let last = performance.now();
  function loop(now) { const dt = Math.min(64, now - last); last = now; t += dt; frame(dt); raf = requestAnimationFrame(loop); }
  resize();
  window.addEventListener("resize", () => { resize(); if (reducedMotion) frame(16); });
  if (reducedMotion) { frame(16); return; }
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) cancelAnimationFrame(raf); else { last = performance.now(); raf = requestAnimationFrame(loop); }
  });
  raf = requestAnimationFrame(loop);
}

// ---------- navigation ----------
function initNav() {
  const nav = document.querySelector(".nav");
  const toggle = document.querySelector(".nav-toggle");
  if (toggle && nav) toggle.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    toggle.setAttribute("aria-expanded", String(open));
  });
  const stocksBtn = document.querySelector(".nav-stocks-btn"), mega = document.getElementById("stocks-menu");
  if (stocksBtn && mega) {
    const set = (open) => { mega.hidden = !open; stocksBtn.setAttribute("aria-expanded", String(open)); };
    stocksBtn.addEventListener("click", (e) => { e.stopPropagation(); set(mega.hidden); });
    document.addEventListener("click", (e) => { if (!mega.hidden && !mega.contains(e.target)) set(false); });
    document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !mega.hidden) { set(false); stocksBtn.focus(); } });
  }
  const form = document.querySelector(".nav-search");
  if (form) form.addEventListener("submit", (ev) => {
    ev.preventDefault();
    const v = (form.querySelector("input").value || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
    if (v) window.location.href = pageUrl(`/news?symbol=${encodeURIComponent(v)}`);
  });
}

// Page-level setup runs once per document, however this module is reached.
if (!window.__psxSiteInit) {
  window.__psxSiteInit = true;
  document.documentElement.classList.remove("no-js");
  initNav();
  backdrop(document.getElementById("bg-field"));
  reveal();
}
