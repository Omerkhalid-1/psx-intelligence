// Chart helpers on TradingView Lightweight Charts™ 4.2.3 (vendored; window.LightweightCharts).
// Colours are the validated set from site.css. The library's TradingView attribution logo stays on.

import { h, num } from "./core.js";

const LWC = window.LightweightCharts;
export const COLORS = { up: "#1fae84", down: "#e0683a", s1: "#3987e5", s2: "#d55181", s3: "#c98500", text: "#b3bedf",
  grid: "rgba(120,150,255,0.07)", accent: "#46d9ff", neutral: "#8a94b8" };

export function available() { return !!LWC; }

function baseOptions(el, extra = {}) {
  return Object.assign({
    width: el.clientWidth, height: el.clientHeight,
    layout: { background: { type: "solid", color: "transparent" }, textColor: COLORS.text, fontFamily: "JetBrains Mono, ui-monospace, monospace", fontSize: 11, attributionLogo: true },
    grid: { vertLines: { color: COLORS.grid }, horzLines: { color: COLORS.grid } },
    rightPriceScale: { borderColor: "rgba(120,150,255,0.18)" },
    timeScale: { borderColor: "rgba(120,150,255,0.18)", rightOffset: 4, minBarSpacing: 1.5 },
    crosshair: { mode: LWC.CrosshairMode.Normal, vertLine: { color: "rgba(70,217,255,0.45)", labelBackgroundColor: "#0e1530" },
      horzLine: { color: "rgba(70,217,255,0.45)", labelBackgroundColor: "#0e1530" } },
    handleScroll: { vertTouchDrag: false },
  }, extra);
}

function autoSize(chart, el) {
  const ro = new ResizeObserver(() => chart.applyOptions({ width: el.clientWidth, height: el.clientHeight }));
  ro.observe(el);
  return ro;
}

/** [{time, value}] from parallel arrays. Missing values become whitespace points ({time} only), so every
 *  chart built from the same series has the same bar indices and their ranges can be synced. */
export function points(time, values) {
  const out = new Array(time.length);
  for (let i = 0; i < time.length; i++) {
    const v = values[i];
    out[i] = v === null || v === undefined ? { time: time[i] } : { time: time[i], value: v };
  }
  return out;
}

/** Candles (hollow up / filled down), volume, toggleable overlays and a live legend. */
export function priceChart(el, s, { overlays = ["sma20", "sma50", "sma200"] } = {}) {
  const chart = LWC.createChart(el, baseOptions(el));
  autoSize(chart, el);
  const candles = chart.addCandlestickSeries({
    upColor: "rgba(31,174,132,0.08)", downColor: COLORS.down, borderUpColor: COLORS.up, borderDownColor: COLORS.down,
    wickUpColor: COLORS.up, wickDownColor: COLORS.down, priceLineColor: COLORS.accent,
  });
  const bars = [];
  for (let i = 0; i < s.time.length; i++) {
    if (s.open[i] === null || s.close[i] === null) continue;
    bars.push({ time: s.time[i], open: s.open[i], high: s.high[i], low: s.low[i], close: s.close[i] });
  }
  candles.setData(bars);
  const vol = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, lastValueVisible: false, priceLineVisible: false });
  chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  vol.setData(s.time.map((t, i) => ({ time: t, value: s.volume[i] || 0,
    color: (s.close[i] ?? 0) >= (s.open[i] ?? 0) ? "rgba(31,174,132,0.35)" : "rgba(224,104,58,0.35)" })));
  candles.priceScale().applyOptions({ scaleMargins: { top: 0.08, bottom: 0.22 } });

  const defs = {
    sma20: { color: COLORS.s1, width: 2, title: "SMA 20" },
    sma50: { color: COLORS.s2, width: 2, title: "SMA 50" },
    sma200: { color: COLORS.s3, width: 2, title: "SMA 200", style: LWC.LineStyle.Dashed },
    bb_upper: { color: "rgba(155,123,255,0.8)", width: 1, title: "BB upper", style: LWC.LineStyle.Dotted },
    bb_lower: { color: "rgba(155,123,255,0.8)", width: 1, title: "BB lower", style: LWC.LineStyle.Dotted },
  };
  const lines = {};
  for (const [key, d] of Object.entries(defs)) {
    if (!s[key]) continue;
    const series = chart.addLineSeries({ color: d.color, lineWidth: d.width, lineStyle: d.style ?? LWC.LineStyle.Solid,
      priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false, visible: overlays.includes(key) });
    series.setData(points(s.time, s[key]));
    lines[key] = series;
  }

  const legend = h("div", { class: "chart-legend", "aria-live": "off" });
  el.append(legend);
  const byTime = new Map(s.time.map((t, i) => [t, i]));
  const show = (i) => {
    if (i === undefined) return;
    const chg = i > 0 && s.close[i - 1] ? s.close[i] / s.close[i - 1] - 1 : null;
    legend.replaceChildren(h("b", {}, s.time[i]), "  O ", num(s.open[i]), "  H ", num(s.high[i]), "  L ", num(s.low[i]),
      "  C ", h("b", {}, num(s.close[i])), chg === null ? "" : `  ${chg >= 0 ? "+" : ""}${(chg * 100).toFixed(2)}%`,
      "  Vol ", num(s.volume[i], 0));
  };
  show(s.time.length - 1);
  chart.subscribeCrosshairMove((p) => show(p && p.time ? byTime.get(p.time) : s.time.length - 1));

  return {
    chart,
    toggle(key, visible) { if (lines[key]) lines[key].applyOptions({ visible }); },
    range(sessions) {
      const n = s.time.length;
      if (!sessions || sessions >= n) chart.timeScale().fitContent();
      else chart.timeScale().setVisibleLogicalRange({ from: n - sessions, to: n + 3 });
    },
  };
}

/** Line/histogram indicator pane. lines: [{key, color, title, type: "line"|"hist", style}] ; bands: [values]. */
export function indicatorChart(el, s, lines, { bands = [], height, animate = false, last } = {}) {
  if (height) el.style.height = `${height}px`;
  if (last) s = sliceSeries(s, last);
  const chart = LWC.createChart(el, baseOptions(el, { rightPriceScale: { borderColor: "rgba(120,150,255,0.18)", scaleMargins: { top: 0.1, bottom: 0.1 } } }));
  autoSize(chart, el);
  const made = [];
  let first = null;
  for (const ln of lines) {
    const values = typeof ln.values === "function" ? ln.values(s) : s[ln.key];
    if (!values) continue;
    let series;
    if (ln.type === "hist") {
      series = chart.addHistogramSeries({ priceLineVisible: false, lastValueVisible: true });
      series.setData(points(s.time, values).map((p) => (p.value === undefined ? p
        : { ...p, color: p.value >= 0 ? "rgba(31,174,132,0.75)" : "rgba(224,104,58,0.75)" })));
    } else {
      series = chart.addLineSeries({ color: ln.color, lineWidth: ln.width || 2, lineStyle: ln.style ?? LWC.LineStyle.Solid,
        priceLineVisible: false, lastValueVisible: true, title: ln.title || "" });
      series.setData(points(s.time, values));
    }
    first = first || series;
    made.push(series);
  }
  for (const b of bands) {
    if (first) first.createPriceLine({ price: b.value, color: b.color || "rgba(138,148,184,0.55)", lineWidth: b.width || 1,
      lineStyle: b.solid ? LWC.LineStyle.Solid : LWC.LineStyle.Dashed, axisLabelVisible: b.label !== false, title: b.title || "" });
  }
  chart.timeScale().fitContent();
  if (animate && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) drawIn(chart, made);
  return { chart, series: made };
}

/** Progressive "live drawing" of already-loaded series (re-feeds growing slices over ~1.2 s). */
function drawIn(chart, seriesList) {
  const full = seriesList.map((sr) => sr.data());
  const n = Math.max(...full.map((d) => d.length));
  if (!n) return;
  const range = { from: 0, to: n - 1 }; // full extent; getVisibleLogicalRange() is not settled yet here
  const t0 = performance.now(), dur = 1200;
  const step = (t) => {
    const p = Math.min(1, (t - t0) / dur), k = Math.max(2, Math.floor(n * (1 - Math.pow(1 - p, 3))));
    seriesList.forEach((sr, i) => sr.setData(full[i].slice(0, k)));
    chart.timeScale().setVisibleLogicalRange(range);
    if (p < 1) requestAnimationFrame(step);
    else chart.timeScale().fitContent();
  };
  requestAnimationFrame(step);
}

/** The last n sessions of every column. */
export function sliceSeries(s, n) {
  const out = {};
  for (const [k, v] of Object.entries(s)) out[k] = Array.isArray(v) ? v.slice(-n) : v;
  return out;
}

/** Keep the visible time range of several charts in step. */
export function syncRanges(charts) {
  let busy = false;
  for (const c of charts) {
    c.timeScale().subscribeVisibleLogicalRangeChange((r) => {
      if (busy || !r) return;
      busy = true;
      for (const o of charts) if (o !== c) o.timeScale().setVisibleLogicalRange(r);
      busy = false;
    });
  }
}
