// Stock page: gauge, price chart with overlays, indicator lab, levels ladder, drawdown and news.
import { ago, chip, countUp, gauge, getJSON, h, meter, num, pageData, reveal, shortDate, tilt } from "./core.js";
import { available, COLORS, indicatorChart, priceChart } from "./charts.js";

const data = pageData();
const eng = data.engine;
const params = Object.fromEntries((data.parameters || []).map((p) => [p.key, p]));

document.querySelectorAll("[data-count]").forEach((el) => countUp(el, Number(el.dataset.count)));
document.querySelectorAll(".meter[data-score]").forEach((el) => meter(el, el.dataset.score === "None" ? null : Number(el.dataset.score)));
const g = document.getElementById("gauge");
if (g) gauge(g, g.dataset.score === "None" ? null : Number(g.dataset.score), g.dataset.label);
document.querySelectorAll(".card.hover").forEach((el) => tilt(el, 3));

if (eng && eng.status === "ok" && eng.series) {
  const s = eng.series;
  const readings = Object.fromEntries(eng.readings.map((r) => [r.key, r]));

  // ---------- price chart ----------
  if (available()) {
    const pc = priceChart(document.getElementById("price-chart"), s);
    pc.range(245);
    document.querySelectorAll("#ranges button").forEach((b) => b.addEventListener("click", () => {
      document.querySelectorAll("#ranges button").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
      pc.range(Number(b.dataset.n));
    }));
    document.querySelectorAll("#overlays .toggle").forEach((b) => b.addEventListener("click", () => {
      const on = b.getAttribute("aria-pressed") !== "true";
      b.setAttribute("aria-pressed", String(on));
      if (b.dataset.key === "bb") { pc.toggle("bb_upper", on); pc.toggle("bb_lower", on); } else pc.toggle(b.dataset.key, on);
    }));
  } else {
    document.getElementById("price-chart").replaceChildren(h("p", { class: "notice err" }, "Chart library failed to load."));
  }

  // ---------- indicator lab ----------
  const rebased = (vals) => { const f = vals.find((v) => v !== null); return vals.map((v) => (v === null || !f ? null : (v / f - 1) * 100)); };
  const tabs = [
    { id: "rsi", label: "RSI", key: "rsi14", lines: [{ key: "rsi14", color: COLORS.s1, title: "RSI 14" }],
      bands: [{ value: 70, title: "70" }, { value: 50, title: "50", color: "rgba(138,148,184,0.3)" }, { value: 30, title: "30" }] },
    { id: "macd", label: "MACD", key: "macd", lines: [{ key: "hist", type: "hist" }, { key: "macd", color: COLORS.s1, title: "MACD" }, { key: "signal", color: COLORS.s3, title: "signal" }],
      bands: [{ value: 0, title: "0" }] },
    { id: "stoch", label: "Stochastic", key: "stochastic", lines: [{ key: "stoch_k", color: COLORS.s1, title: "%K" }, { key: "stoch_d", color: COLORS.s2, title: "%D" }],
      bands: [{ value: 80, title: "80" }, { value: 20, title: "20" }] },
    { id: "adx", label: "ADX", key: "adx", lines: [{ key: "adx", color: "#eaf0ff", title: "ADX", width: 3 }, { key: "plus_di", color: COLORS.s1, title: "+DI" }, { key: "minus_di", color: COLORS.s2, title: "−DI" }],
      bands: [{ value: 25, title: "25" }, { value: 20, title: "20" }] },
    { id: "bb", label: "Bollinger %B", key: "bollinger", lines: [{ key: "bb_pct", color: "#9b7bff", title: "%B ×100",
      values: (x) => x.close.map((c, i) => (c === null || x.bb_upper[i] === null || x.bb_upper[i] === x.bb_lower[i] ? null : ((c - x.bb_lower[i]) / (x.bb_upper[i] - x.bb_lower[i])) * 100)) }],
      bands: [{ value: 100, title: "upper" }, { value: 0, title: "lower" }] },
    { id: "obv", label: "OBV", key: "obv", lines: [{ key: "obv", color: COLORS.s3, title: "OBV" }] },
    { id: "rs", label: "vs KSE-100", key: "excess_return", needs: "bench_close", lines: [
      { key: "stock_rb", color: COLORS.s1, title: data.symbol + " %", values: (x) => rebased(x.close) },
      { key: "bench_rb", color: COLORS.s2, title: "KSE-100 %", values: (x) => rebased(x.bench_close) }], bands: [{ value: 0, title: "0" }] },
    { id: "atr", label: "ATR", key: "atr_pct", lines: [{ key: "atr_pct", color: COLORS.s3, title: "ATR % of price",
      values: (x) => x.atr14.map((a, i) => (a === null || !x.close[i] ? null : (a / x.close[i]) * 100)) }] },
  ].filter((t) => !t.needs || (s[t.needs] && s[t.needs].some((v) => v !== null)));

  const host = document.getElementById("ind-chart");
  const explain = document.getElementById("ind-explain");
  let current = null;
  function showTab(t) {
    document.querySelectorAll("#lab-tabs button").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.id === t.id)));
    if (current) current.chart.remove();
    host.replaceChildren();
    if (available()) current = indicatorChart(host, s, t.lines, { bands: t.bands || [] });
    const p = params[t.key] || {};
    const r = readings[t.key];
    explain.replaceChildren(
      h("div", { class: "row between" }, h("h3", {}, p.name || t.label), r ? chip(r.state.replaceAll("_", " "), r.tone) : null),
      r ? h("p", { class: "text-2", style: { margin: "10px 0 0", fontSize: "1.02rem" } }, h("b", {}, "Now: "), r.headline) : null,
      h("dl", {},
        h("dt", {}, "What it measures"), h("dd", {}, p.what || ""),
        h("dt", {}, "Formula"), h("dd", {}, h("span", { class: "formula" }, p.formula || "")),
        h("dt", {}, "How to read it"), h("dd", {}, p.how_to_read || ""),
        h("dt", {}, "Rule used here"), h("dd", {}, p.rule || ""),
        p.caveat ? h("dt", {}, "Caveat") : null, p.caveat ? h("dd", {}, p.caveat) : null));
  }
  const tabBar = document.getElementById("lab-tabs");
  tabs.forEach((t, i) => tabBar.append(h("button", { type: "button", role: "tab", "data-id": t.id, "aria-selected": String(i === 0), onclick: () => showTab(t) }, t.label)));
  if (tabs.length) showTab(tabs[0]);

  // ---------- support / resistance ladder ----------
  const ladder = document.getElementById("ladder");
  const zones = eng.zones || { support: [], resistance: [] };
  const close = eng.price.close;
  const all = [...zones.support, ...zones.resistance];
  if (ladder) {
    if (!all.length) ladder.replaceChildren(h("p", { class: "empty" }, "No established zones near the price."));
    else {
      const lo = Math.min(close, ...all.map((z) => z.low)) * 0.985, hi = Math.max(close, ...all.map((z) => z.high)) * 1.015;
      const y = (v) => ((hi - v) / (hi - lo)) * 100;
      const nodes = [];
      for (let k = 0; k <= 4; k++) { const v = hi - ((hi - lo) * k) / 4; nodes.push(h("span", { class: "axis", style: { top: `calc(${y(v)}% - 7px)` } }, num(v))); }
      for (const z of zones.resistance) nodes.push(h("div", { class: "zone res", style: { top: `${y(z.high)}%`, height: `${Math.max(3, y(z.low) - y(z.high))}%` },
        title: `Resistance ${num(z.low)}–${num(z.high)}, ${z.touches} touch(es), last ${z.last_touch}` }, `R ${num(z.low)}–${num(z.high)} · ${z.touches}×`));
      for (const z of zones.support) nodes.push(h("div", { class: "zone sup", style: { top: `${y(z.high)}%`, height: `${Math.max(3, y(z.low) - y(z.high))}%` },
        title: `Support ${num(z.low)}–${num(z.high)}, ${z.touches} touch(es), last ${z.last_touch}` }, `S ${num(z.low)}–${num(z.high)} · ${z.touches}×`));
      const marker = h("div", { class: "price", style: { top: "50%" } }, h("span", {}, `NOW ${num(close)}`));
      nodes.push(marker);
      ladder.replaceChildren(...nodes);
      requestAnimationFrame(() => requestAnimationFrame(() => { marker.style.top = `${y(close)}%`; }));
    }
  }
  document.querySelectorAll(".range-bar .dot").forEach((d) => requestAnimationFrame(() => requestAnimationFrame(() => {
    d.style.left = `${Math.max(0, Math.min(1, Number(d.dataset.pos))) * 100}%`; })));

  const dd = document.getElementById("dd-chart");
  if (dd && available()) indicatorChart(dd, s, [{ key: "dd", color: COLORS.down, title: "drawdown %",
    values: (x) => x.drawdown.map((v) => (v === null ? null : v * 100)) }], { bands: [{ value: 0, title: "peak" }] });
}

// ---------- news ----------
async function loadNews() {
  const box = document.getElementById("stock-news");
  if (!box || !data.symbol) return;
  try {
    const body = await getJSON(`/api/v1/news/search?symbol=${encodeURIComponent(data.symbol)}&limit=8`);
    const items = body.data.results;
    if (!items.length) { box.replaceChildren(h("p", { class: "empty" }, "No recent headlines name this company.")); return; }
    box.replaceChildren(...items.map((it, i) => h("article", { class: "card news-item reveal", style: { "--i": i } },
      h("div", { class: "when" }, h("b", {}, shortDate(it.published_at)), ago(it.published_at)),
      h("div", {},
        h("a", { class: "title", href: it.url, target: "_blank", rel: "noopener noreferrer nofollow" }, it.title),
        h("div", { class: "meta" }, it.publisher || "", it.tone ? chip(`${it.tone} tone`, it.tone === "positive" ? 1 : it.tone === "negative" ? -1 : 0) : null,
          it.also && it.also.length ? h("span", {}, `also: ${it.also.slice(0, 3).join(", ")}`) : null)))));
    reveal(box);
  } catch (e) {
    box.replaceChildren(h("p", { class: "notice warn" }, e.status === 429 ? "Too many searches; try again in a minute." : `News unavailable: ${e.message}`));
  }
}
loadNews();
