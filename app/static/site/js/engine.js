// Engine page: live self-check results and the parameter-guide filter.
import { getJSON, h } from "./core.js";

const CIRC = 389.6;
const ICON = { pass: "✓", warn: "!", fail: "✕" };

async function run() {
  const list = document.getElementById("diag-list");
  document.getElementById("ring-num").textContent = "…";
  document.getElementById("ring-sub").textContent = "running";
  try {
    const { data } = await getJSON("/api/v1/engine/diagnostics");
    const total = data.checks.length, ok = data.summary.pass;
    const fg = document.getElementById("ring-fg");
    fg.setAttribute("stroke", data.status === "fail" ? "#e0683a" : data.status === "warn" ? "#c98500" : "#1fae84");
    requestAnimationFrame(() => fg.setAttribute("stroke-dashoffset", String(CIRC * (1 - ok / Math.max(1, total)))));
    document.getElementById("ring-num").textContent = `${ok}/${total}`;
    document.getElementById("ring-sub").textContent = data.status === "pass" ? "all passed" : data.status === "warn" ? "passed with warnings" : "failures";
    document.getElementById("diag-counts").replaceChildren(
      h("span", { class: "chip tone-pos" }, `${data.summary.pass} pass`),
      h("span", { class: "chip tone-desc" }, `${data.summary.warn} warn`),
      h("span", { class: "chip tone-neg" }, `${data.summary.fail} fail`));
    document.getElementById("diag-meta").textContent = `${data.engine_version} · ran ${new Date(data.ran_at).toLocaleString()} · ${data.duration_ms} ms`;
    const byScope = {};
    for (const c of data.checks) (byScope[c.scope] = byScope[c.scope] || []).push(c);
    let i = 0;
    list.replaceChildren(...Object.entries(byScope).map(([scope, checks]) => h("div", { style: { marginBottom: "14px" } },
      h("div", { class: "label", style: { margin: "8px 0 4px" } }, scope === "engine" ? "Engine reference" : scope),
      ...checks.map((c) => h("div", { class: "check" },
        h("span", { class: `st ${c.status}`, style: { "--i": i++ }, "aria-label": c.status }, ICON[c.status] || "?"),
        h("span", { class: "nm" }, c.name.replaceAll("_", " ")),
        h("span", { class: "text-2" }, c.detail))))));
  } catch (e) {
    list.replaceChildren(h("p", { class: "notice err" }, `Self-check could not run: ${e.message}`));
    document.getElementById("ring-sub").textContent = "unavailable";
  }
}
document.getElementById("diag-rerun").addEventListener("click", run);
run();

document.querySelectorAll("#pg-filter .toggle").forEach((b) => b.addEventListener("click", () => {
  document.querySelectorAll("#pg-filter .toggle").forEach((o) => o.setAttribute("aria-pressed", String(o === b)));
  document.querySelectorAll(".param").forEach((p) => { p.hidden = b.dataset.g !== "all" && p.dataset.g !== b.dataset.g; });
}));
