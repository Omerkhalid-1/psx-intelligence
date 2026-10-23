// "How a stock works": three teaching animations (clearly tagged as illustrations) and live demos
// built from the analysis engine's real series for the chosen symbol.
import { BALANCE_WORDS, gauge, getJSON, h, num, pageData, pageUrl, pct, reducedMotion, svg } from "./core.js";
import { available, COLORS, indicatorChart, sliceSeries } from "./charts.js";

const state = { symbol: pageData().symbol, data: null, charts: [], observers: [], gen: 0 };

// ---------- helpers ----------
function fitCanvas(c) {
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = c.clientWidth, hgt = c.clientHeight;
  c.width = Math.floor(w * dpr); c.height = Math.floor(hgt * dpr);
  const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  return { ctx, w, h: hgt };
}

/** Run `frame(dt)` only while the element is on screen (and once if motion is reduced). */
function animateWhenVisible(el, frame) {
  let raf = 0, last = 0, on = false;
  const loop = (t) => { const dt = Math.min(64, t - (last || t)); last = t; frame(dt); if (on) raf = requestAnimationFrame(loop); };
  if (reducedMotion) { frame(16); return; }
  new IntersectionObserver(([e]) => {
    if (e.isIntersecting && !on) { on = true; last = 0; raf = requestAnimationFrame(loop); }
    else if (!e.isIntersecting && on) { on = false; cancelAnimationFrame(raf); }
  }, { threshold: 0.05 }).observe(el);
}

const rnd = (a, b) => a + Math.random() * (b - a);

// ---------- 01 shares (SVG) ----------
(function shares() {
  const el = document.getElementById("demo-share");
  if (!el) return;
  const cx = 200, cy = 150, r = 110, n = 12;
  const slices = [];
  for (let i = 0; i < n; i++) {
    const a0 = (i / n) * Math.PI * 2 - Math.PI / 2, a1 = ((i + 1) / n) * Math.PI * 2 - Math.PI / 2;
    const p = (a) => `${cx + r * Math.cos(a)},${cy + r * Math.sin(a)}`;
    const mid = (a0 + a1) / 2;
    const g = svg("g", { style: "transition: transform 1.2s cubic-bezier(.2,.8,.2,1)" },
      svg("path", { d: `M${cx},${cy} L${p(a0)} A${r},${r} 0 0 1 ${p(a1)} Z`, fill: i % 3 === 0 ? "#3987e5" : i % 3 === 1 ? "#2b5fb0" : "#1f4688",
        stroke: "#04060d", "stroke-width": 2 }));
    slices.push({ g, dx: Math.cos(mid), dy: Math.sin(mid) });
    el.append(g);
  }
  el.append(svg("circle", { cx, cy, r: 34, fill: "#04060d", stroke: "#46d9ff", "stroke-width": 1.5 }),
    svg("text", { x: cx, y: cy + 5, "text-anchor": "middle", fill: "#eaf0ff", "font-size": 14, "font-family": "Space Grotesk" }, "COMPANY"));
  let t = 0, out = false;
  animateWhenVisible(el, (dt) => {
    t += dt;
    if (t > 2200) { t = 0; out = !out; slices.forEach((s, i) => {
      const k = out && (i % 4 === 1) ? 22 : out ? 6 : 0;
      s.g.style.transform = `translate(${s.dx * k}px, ${s.dy * k}px)`;
    }); }
  });
})();

// ---------- 02 order book (canvas) ----------
(function orderBook() {
  const c = document.getElementById("demo-book");
  if (!c) return;
  let G = fitCanvas(c);
  window.addEventListener("resize", () => { G = fitCanvas(c); });
  let mid = 100, acc = 0, flash = 0, lastSide = 1;
  const hist = Array.from({ length: 80 }, () => 100);
  const mk = () => Array.from({ length: 8 }, () => rnd(0.2, 1));
  let bids = mk(), asks = mk();
  animateWhenVisible(c, (dt) => {
    const { ctx, w, h: H } = G;
    acc += dt; flash = Math.max(0, flash - dt / 400);
    if (acc > 420) {
      acc = 0;
      const pressure = bids.slice(0, 3).reduce((a, b) => a + b, 0) - asks.slice(0, 3).reduce((a, b) => a + b, 0);
      lastSide = Math.random() < 0.5 + pressure * 0.15 ? 1 : -1;
      if (lastSide > 0) { asks[0] -= rnd(0.2, 0.6); if (asks[0] <= 0) { asks.shift(); asks.push(rnd(0.2, 1)); mid += 0.05; } }
      else { bids[0] -= rnd(0.2, 0.6); if (bids[0] <= 0) { bids.shift(); bids.push(rnd(0.2, 1)); mid -= 0.05; } }
      mid += lastSide * rnd(0, 0.04);
      if (Math.random() < 0.4) bids[Math.floor(rnd(0, 8))] += rnd(0, 0.4);
      if (Math.random() < 0.4) asks[Math.floor(rnd(0, 8))] += rnd(0, 0.4);
      hist.push(mid); hist.shift(); flash = 1;
    }
    ctx.clearRect(0, 0, w, H);
    const bookH = H * 0.58, rowH = bookH / 8, cx = w / 2, maxW = w * 0.42;
    ctx.font = "11px JetBrains Mono, monospace"; ctx.textBaseline = "middle";
    for (let i = 0; i < 8; i++) {
      const y = i * rowH + 4, bw = Math.min(1.4, bids[i] || 0) / 1.4 * maxW, aw = Math.min(1.4, asks[i] || 0) / 1.4 * maxW;
      ctx.fillStyle = `rgba(31,174,132,${0.75 - i * 0.07})`; ctx.fillRect(cx - 6 - bw, y, bw, rowH - 6);
      ctx.fillStyle = `rgba(224,104,58,${0.75 - i * 0.07})`; ctx.fillRect(cx + 6, y, aw, rowH - 6);
      ctx.fillStyle = "#b3bedf";
      ctx.fillText((mid - 0.05 * (i + 1)).toFixed(2), 4, y + rowH / 2 - 3);
      ctx.textAlign = "right"; ctx.fillText((mid + 0.05 * (i + 1)).toFixed(2), w - 4, y + rowH / 2 - 3); ctx.textAlign = "left";
    }
    ctx.fillStyle = `rgba(70,217,255,${0.25 + flash * 0.75})`;
    ctx.beginPath(); ctx.arc(cx, rowH / 2, 6 + flash * 8, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = "#eaf0ff"; ctx.textAlign = "center"; ctx.font = "600 13px JetBrains Mono, monospace";
    ctx.fillText(`last ${mid.toFixed(2)}`, cx, bookH + 14); ctx.textAlign = "left";
    ctx.fillStyle = "#8691b6"; ctx.font = "10px JetBrains Mono, monospace"; ctx.fillText("BIDS", 4, bookH + 14);
    ctx.textAlign = "right"; ctx.fillText("ASKS", w - 4, bookH + 14); ctx.textAlign = "left";
    const top = bookH + 30, ph = H - top - 6, lo = Math.min(...hist), hi = Math.max(...hist), sp = hi - lo || 1;
    ctx.beginPath();
    hist.forEach((v, i) => { const x = (i / (hist.length - 1)) * w, y = top + ph - ((v - lo) / sp) * ph; i ? ctx.lineTo(x, y) : ctx.moveTo(x, y); });
    ctx.strokeStyle = "#46d9ff"; ctx.lineWidth = 2; ctx.shadowColor = "#46d9ff"; ctx.shadowBlur = 10; ctx.stroke(); ctx.shadowBlur = 0;
  });
})();

// ---------- 03 a day becomes a candle (canvas) ----------
(function candle() {
  const c = document.getElementById("demo-candle");
  if (!c) return;
  let G = fitCanvas(c);
  window.addEventListener("resize", () => { G = fitCanvas(c); });
  let path = [], t = 0;
  const newDay = () => { path = [100]; const drift = rnd(-0.08, 0.08); for (let i = 1; i < 140; i++) path.push(path[i - 1] + drift + rnd(-0.6, 0.6)); t = 0; };
  newDay();
  animateWhenVisible(c, (dt) => {
    const { ctx, w, h: H } = G;
    t += dt;
    const drawN = Math.min(path.length, Math.floor((t / 3200) * path.length) + 2);
    const shown = path.slice(0, drawN);
    const lo = Math.min(...path) - 1, hi = Math.max(...path) + 1;
    const y = (v) => 20 + (1 - (v - lo) / (hi - lo)) * (H - 40);
    const pathW = w * 0.62;
    ctx.clearRect(0, 0, w, H);
    ctx.beginPath();
    shown.forEach((v, i) => { const x = 10 + (i / (path.length - 1)) * pathW; i ? ctx.lineTo(x, y(v)) : ctx.moveTo(x, y(v)); });
    ctx.strokeStyle = "rgba(70,217,255,0.9)"; ctx.lineWidth = 1.6; ctx.stroke();
    const o = shown[0], cl = shown[shown.length - 1], hgh = Math.max(...shown), lw = Math.min(...shown);
    const cx = w * 0.82, bw = 34, up = cl >= o;
    const grow = Math.min(1, t / 3200);
    ctx.strokeStyle = up ? COLORS.up : COLORS.down; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(cx, y(hgh)); ctx.lineTo(cx, y(lw)); ctx.stroke();
    const top = y(Math.max(o, cl)), bot = y(Math.min(o, cl));
    if (up) { ctx.fillStyle = "rgba(31,174,132,0.12)"; ctx.fillRect(cx - bw / 2, top, bw, Math.max(2, bot - top)); ctx.strokeRect(cx - bw / 2, top, bw, Math.max(2, bot - top)); }
    else { ctx.fillStyle = COLORS.down; ctx.fillRect(cx - bw / 2, top, bw, Math.max(2, bot - top)); }
    ctx.font = "11px JetBrains Mono, monospace"; ctx.fillStyle = "#b3bedf"; ctx.globalAlpha = grow;
    const lab = (txt, v, dx = 26) => { ctx.fillText(txt, cx + dx, y(v) + 4); ctx.setLineDash([3, 3]); ctx.strokeStyle = "rgba(179,190,223,0.35)"; ctx.beginPath();
      ctx.moveTo(10, y(v)); ctx.lineTo(cx - 20, y(v)); ctx.stroke(); ctx.setLineDash([]); };
    lab(`H ${hgh.toFixed(1)}`, hgh); lab(`L ${lw.toFixed(1)}`, lw); lab(`O ${o.toFixed(1)}`, o); lab(`C ${cl.toFixed(1)}`, cl);
    ctx.globalAlpha = 1;
    if (t > 6200) newDay();
  });
})();

// ---------- live demos from real data ----------
function setLive(key, text) { document.querySelectorAll(`[data-live="${key}"]`).forEach((el) => { el.textContent = text; }); }
function sma(values, n) {
  const out = new Array(values.length).fill(null); let sum = 0, count = 0;
  for (let i = 0; i < values.length; i++) {
    const v = values[i]; if (v === null) { sum = 0; count = 0; continue; }
    sum += v; count++; if (count > n) { sum -= values[i - n]; count = n; }
    if (count === n) out[i] = sum / n;
  }
  return out;
}
const R = (eng, key) => (eng.readings || []).find((r) => r.key === key);

/** Build a chart the first time its box scrolls into view (once per symbol load). */
function lazyChart(id, build) {
  const el = document.getElementById(id);
  if (!el || !available()) return;
  el.replaceChildren();
  const gen = state.gen;
  const io = new IntersectionObserver(([e]) => {
    if (!e.isIntersecting) return;
    io.disconnect();
    if (gen !== state.gen) return; // a newer symbol was loaded meanwhile
    const c = build(el);
    if (c) state.charts.push(c.chart);
  }, { threshold: 0.15 });
  state.observers.push(io);
  io.observe(el);
}

function renderLive() {
  const eng = state.data;
  state.gen += 1;
  state.observers.forEach((o) => o.disconnect());
  state.observers = [];
  state.charts.forEach((c) => { try { c.remove(); } catch (e) { /* already removed */ } });
  state.charts = [];
  if (!eng || eng.status !== "ok") return;
  const s = eng.series, last = sliceSeries(s, 250);
  setLive("symbol-label", `${state.symbol} · last 60 sessions`);

  // volume bars
  const vb = document.getElementById("demo-volume");
  const n = 60, vol = s.volume.slice(-n), cl = s.close.slice(-n - 1);
  const maxV = Math.max(...vol.filter(Boolean)) || 1;
  const avg20 = (i) => { const w = s.volume.slice(-n - 20 + i, -n + i).filter(Boolean); return w.length ? w.reduce((a, b) => a + b, 0) / w.length : null; };
  vb.classList.remove("in");
  vb.replaceChildren(...vol.map((v, i) => {
    const a = avg20(i), hot = a && v / a >= 2;
    return h("span", { class: `${cl[i + 1] >= cl[i] ? "up" : "down"}${hot ? " hot" : ""}`, style: { height: `${((v || 0) / maxV) * 100}%`, "--i": i },
      title: `${s.time.slice(-n)[i]}: ${num(v, 0)} shares${a ? ` (${(v / a).toFixed(1)}× its 20-day average)` : ""}` });
  }));
  new IntersectionObserver(([e], o) => { if (e.isIntersecting) { vb.classList.add("in"); o.disconnect(); } }, { threshold: 0.2 }).observe(vb);
  const rv = R(eng, "rel_volume");
  setLive("volume-text", rv ? `Now: ${rv.headline}` : "");

  // trend + slider
  const slider = document.getElementById("ma-n");
  let trendChart = null;
  const drawTrend = () => {
    const k = Number(slider.value);
    document.getElementById("ma-n-label").textContent = String(k);
    const full = sma(s.close, k), lastK = full.slice(-250);
    if (trendChart && trendChart.series[1]) trendChart.series[1].setData(last.time.map((t, i) => (lastK[i] === null ? { time: t } : { time: t, value: lastK[i] })));
    const c = s.close[s.close.length - 1], m = full[full.length - 1];
    setLive("ma-text", m ? `${state.symbol} closed at ${num(c)}, ${pct(c / m - 1)} ${c >= m ? "above" : "below"} its ${k}-session average (${num(m)}).` : "Not enough history for this length.");
  };
  lazyChart("demo-trend", (el) => {
    trendChart = indicatorChart(el, last, [{ key: "close", color: COLORS.s1, title: "close" }, { key: "ma", color: COLORS.s2, title: "average", values: () => sma(s.close, Number(slider.value)).slice(-250) }], { animate: true });
    return trendChart;
  });
  slider.oninput = drawTrend;
  drawTrend();

  // RSI bars + gauge
  const ch = [];
  for (let i = s.close.length - 14; i < s.close.length; i++) ch.push(s.close[i] - s.close[i - 1]);
  const gains = ch.map((d) => Math.max(0, d)), losses = ch.map((d) => Math.max(0, -d));
  const ag = gains.reduce((a, b) => a + b, 0) / 14, al = losses.reduce((a, b) => a + b, 0) / 14;
  const simple = al === 0 ? 100 : 100 - 100 / (1 + ag / al);
  const mx = Math.max(...ch.map(Math.abs)) || 1;
  const rb = document.getElementById("demo-rsi");
  rb.replaceChildren(h("div", { class: "axis0" }), ...ch.map((d, i) => {
    const b = h("div", { class: "b", title: `${s.time[s.time.length - 14 + i]}: ${d >= 0 ? "+" : ""}${num(d)}`,
      style: { background: d >= 0 ? COLORS.up : COLORS.down, top: "50%", height: "0%" } });
    requestAnimationFrame(() => requestAnimationFrame(() => {
      const hp = (Math.abs(d) / mx) * 48;
      b.style.height = `${hp}%`; b.style.top = d >= 0 ? `${50 - hp}%` : "50%";
    }));
    return h("div", { class: "col" }, b);
  }));
  const rsiR = R(eng, "rsi14");
  document.getElementById("rsi-kv").replaceChildren(
    h("span", { class: "k" }, "Average gain (14 days)"), h("span", { class: "v" }, num(ag)),
    h("span", { class: "k" }, "Average loss (14 days)"), h("span", { class: "v" }, num(al)),
    h("span", { class: "k" }, "RS = gain ÷ loss"), h("span", { class: "v" }, al ? num(ag / al) : "∞"),
    h("span", { class: "k" }, "RSI from these bars"), h("span", { class: "v" }, num(simple, 1)),
    h("span", { class: "k" }, "Engine RSI (Wilder)"), h("span", { class: "v" }, rsiR ? rsiR.display : "n/a"));
  gauge(document.getElementById("rsi-gauge"), rsiR && rsiR.value !== null ? (rsiR.value - 50) / 50 : null, `RSI ${rsiR ? rsiR.display : ""}`);

  // MACD
  lazyChart("demo-macd", (el) => indicatorChart(el, last, [{ key: "hist", type: "hist" }, { key: "macd", color: COLORS.s1, title: "MACD" }, { key: "signal", color: COLORS.s3, title: "signal" }], { bands: [{ value: 0 }], animate: true }));
  const m = R(eng, "macd"); setLive("macd-text", m ? `Now: ${m.headline}` : "");

  // Bollinger
  lazyChart("demo-bb", (el) => indicatorChart(el, last, [{ key: "bb_upper", color: "#9b7bff", title: "upper", width: 1 }, { key: "bb_lower", color: "#9b7bff", title: "lower", width: 1 },
    { key: "bb_mid", color: "rgba(155,123,255,0.5)", title: "mid", width: 1 }, { key: "close", color: COLORS.s1, title: "close" }], { animate: true }));
  const atr = R(eng, "atr_pct"), bb = R(eng, "bollinger");
  setLive("vol-text", [atr && `Now: ${atr.headline}`, bb && bb.headline].filter(Boolean).join(" "));

  // levels
  const zones = eng.zones || { support: [], resistance: [] };
  const bands = [...zones.support.slice(0, 2).flatMap((z) => [{ value: z.low, color: "rgba(31,174,132,0.8)", title: "S", solid: true }, { value: z.high, color: "rgba(31,174,132,0.8)", label: false, solid: true }]),
    ...zones.resistance.slice(0, 2).flatMap((z) => [{ value: z.low, color: "rgba(224,104,58,0.8)", title: "R", solid: true }, { value: z.high, color: "rgba(224,104,58,0.8)", label: false, solid: true }])];
  lazyChart("demo-levels", (el) => indicatorChart(el, last, [{ key: "close", color: COLORS.s1, title: "close" }], { bands, animate: true }));
  const zr = R(eng, "zones"); setLive("levels-text", zr ? zr.headline : "");

  // relative strength
  if (s.bench_close && s.bench_close.some((v) => v !== null)) {
    const rb2 = (vals) => { const f = vals.find((v) => v !== null); return vals.map((v) => (v === null || !f ? null : (v / f - 1) * 100)); };
    lazyChart("demo-rel", (el) => indicatorChart(el, last, [{ key: "a", color: COLORS.s1, title: state.symbol, values: (x) => rb2(x.close) },
      { key: "b", color: COLORS.s2, title: "KSE-100", values: (x) => rb2(x.bench_close) }], { bands: [{ value: 0 }], animate: true }));
    const ex = R(eng, "excess_return"), beta = R(eng, "beta");
    setLive("rel-text", [ex && `Now: ${ex.headline}`, beta && beta.headline].filter(Boolean).join(" "));
  } else {
    document.getElementById("demo-rel").replaceChildren(h("p", { class: "empty" }, "The KSE-100 is the market itself; pick a stock to compare it with the index."));
    setLive("rel-text", "");
  }

  // drawdown
  lazyChart("demo-dd", (el) => indicatorChart(el, s, [{ key: "dd", color: COLORS.down, title: "drawdown %", values: (x) => x.drawdown.map((v) => (v === null ? null : v * 100)) }], { bands: [{ value: 0, title: "peak" }], animate: true }));
  const dd = R(eng, "drawdown"); setLive("dd-text", dd ? `Now: ${dd.headline}` : "");

  // together
  gauge(document.getElementById("together-gauge"), eng.balance.score, BALANCE_WORDS[eng.balance.label]);
  setLive("together-label", `${state.symbol}: ${BALANCE_WORDS[eng.balance.label]} (${eng.balance.score >= 0 ? "+" : ""}${eng.balance.score.toFixed(2)})`);
  document.getElementById("together-story").replaceChildren(...eng.story.slice(0, 4).map((t) => h("li", {}, t)));
  const link = document.getElementById("together-link");
  link.href = pageUrl(`/stock/${encodeURIComponent(state.symbol)}`); link.textContent = `Open the full ${state.symbol} dashboard →`;
}

async function load(symbol) {
  const status = document.getElementById("learn-status");
  status.textContent = `loading ${symbol}…`;
  try {
    const body = await getJSON(`/api/v1/engine/${encodeURIComponent(symbol)}?series=true&limit=750`);
    state.symbol = symbol; state.data = body.data;
    status.textContent = `${symbol} · session ${body.data.session_date}`;
    renderLive();
  } catch (e) {
    status.textContent = `Could not load ${symbol}: ${e.message}`;
  }
}

const picker = document.getElementById("learn-symbol");
picker.addEventListener("change", () => {
  const url = new URL(window.location.href); url.searchParams.set("symbol", picker.value); history.replaceState(null, "", url);
  load(picker.value);
});
// A ?symbol= in the URL wins (the static edition renders the page once, with the default symbol).
const wanted = (new URLSearchParams(window.location.search).get("symbol") || "").toUpperCase();
if (wanted && [...picker.options].some((o) => o.value === wanted)) { picker.value = wanted; state.symbol = wanted; }
load(state.symbol);

// reading progress bar
const bar = document.getElementById("progress");
window.addEventListener("scroll", () => {
  const max = document.documentElement.scrollHeight - innerHeight;
  bar.style.width = `${max > 0 ? (scrollY / max) * 100 : 0}%`;
}, { passive: true });
