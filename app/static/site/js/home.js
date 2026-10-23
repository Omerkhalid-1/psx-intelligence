// Home page: animate the server-rendered market pulse (counters, meters, sparklines, tilt).
import { countUp, meter, pageData, sparkline, tilt } from "./core.js";

const data = pageData();
const bySymbol = Object.fromEntries((data.watchlist || []).map((w) => [w.symbol, w]));

document.querySelectorAll("[data-count]").forEach((el) => countUp(el, Number(el.dataset.count), { decimals: 2 }));
document.querySelectorAll(".meter[data-score]").forEach((el) => {
  const v = el.dataset.score === "None" || el.dataset.score === "" ? null : Number(el.dataset.score);
  meter(el, v);
});
document.querySelectorAll("svg[data-spark]").forEach((el, i) => {
  const w = bySymbol[el.dataset.spark];
  if (w && w.spark) sparkline(el, w.spark.close, { delay: 0.12 * (i % 8) });
});
document.querySelectorAll("[data-tilt]").forEach((el) => tilt(el));

// Sector filter: applies to the watchlist cards and the indicator matrix together.
document.querySelectorAll("#sector-filter .toggle").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#sector-filter .toggle").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
  const want = b.dataset.sector;
  document.querySelectorAll(".stock-card[data-sector], .matrix tbody tr[data-sector]").forEach((el) => {
    el.hidden = want !== "all" && el.dataset.sector !== want;
  });
}));
