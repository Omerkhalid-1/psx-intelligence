// PSX news search: debounced suggestions over the PSX listing, then strictly filtered headlines.
import { ago, chip, getJSON, h, pageData, pageUrl, reveal, shortDate, STATIC } from "./core.js";

const form = document.getElementById("news-form");
const input = document.getElementById("news-q");
const box = document.getElementById("news-suggest");
const head = document.getElementById("news-head");
const results = document.getElementById("news-results");
let suggestions = [], active = -1, timer = 0, seq = 0;

function closeSuggest() { box.hidden = true; input.setAttribute("aria-expanded", "false"); active = -1; }
function renderSuggest() {
  if (!suggestions.length) { closeSuggest(); return; }
  box.replaceChildren(...suggestions.map((s, i) => h("button", { type: "button", role: "option", id: `sg-${i}`, "aria-selected": String(i === active),
    onmousedown: (e) => { e.preventDefault(); choose(s.symbol); } }, h("b", {}, s.symbol), h("span", { class: "muted" }, s.name), s.tracked ? chip("tracked", null) : null)));
  box.hidden = false; input.setAttribute("aria-expanded", "true");
}

input.addEventListener("input", () => {
  clearTimeout(timer);
  const q = input.value.trim();
  if (!q) { suggestions = []; closeSuggest(); return; }
  timer = setTimeout(async () => {
    const my = ++seq;
    try {
      const body = await getJSON(`/api/v1/symbols/search?q=${encodeURIComponent(q)}&limit=8`);
      if (my === seq) { suggestions = body.data; active = -1; renderSuggest(); }
    } catch (e) { closeSuggest(); }
  }, 160);
});
input.addEventListener("keydown", (e) => {
  if (box.hidden || !suggestions.length) return;
  if (e.key === "ArrowDown") { active = (active + 1) % suggestions.length; renderSuggest(); e.preventDefault(); }
  else if (e.key === "ArrowUp") { active = (active - 1 + suggestions.length) % suggestions.length; renderSuggest(); e.preventDefault(); }
  else if (e.key === "Escape") closeSuggest();
  else if (e.key === "Enter" && active >= 0) { e.preventDefault(); choose(suggestions[active].symbol); }
});
input.addEventListener("blur", () => setTimeout(closeSuggest, 120));
form.addEventListener("submit", (e) => {
  e.preventDefault();
  const q = input.value.trim();
  // A typed company name resolves to the first suggestion; a typed ticker is used as-is.
  const exact = suggestions.find((s) => s.symbol === q.toUpperCase());
  choose(exact ? exact.symbol : suggestions.length && !/^[A-Za-z0-9]{1,12}$/.test(q) ? suggestions[0].symbol : q);
});
document.querySelectorAll(".quick button").forEach((b) => b.addEventListener("click", () => choose(b.dataset.q)));

function choose(symbol) {
  const sym = (symbol || "").trim().toUpperCase().replace(/[^A-Z0-9]/g, "");
  if (!sym) return;
  input.value = sym; closeSuggest();
  const url = new URL(window.location.href); url.searchParams.set("symbol", sym); history.replaceState(null, "", url);
  search(sym);
}

function skeleton() { return [1, 2, 3, 4].map(() => h("div", { class: "skeleton", style: { height: "84px" } })); }

async function search(sym) {
  head.replaceChildren(h("p", { class: "muted" }, `Scanning headlines for ${sym}…`));
  results.replaceChildren(...skeleton());
  try {
    const { data, warnings } = await getJSON(`/api/v1/news/search?symbol=${encodeURIComponent(sym)}&limit=40`);
    const rules = data.match_rules;
    head.replaceChildren(h("div", { class: "card reveal" },
      h("div", { class: "row between" },
        h("div", {}, h("span", { class: "kicker" }, data.symbol), h("h2", { style: { marginTop: "6px" } }, data.company)),
        h("div", { class: "row" }, chip(`${data.counts.shown} headlines`, 1), chip(`${data.counts.rejected_as_unrelated} unrelated filtered out`, null),
          data.tracked ? h("a", { class: "btn btn-ghost btn-sm", href: pageUrl(`/stock/${data.symbol}`) }, "Indicator dashboard →") : null)),
      h("details", { style: { marginTop: "12px" } }, h("summary", { class: "small", style: { cursor: "pointer", color: "var(--accent)" } }, "How matching works for this symbol"),
        h("dl", { class: "explain small" },
          h("dt", {}, "Must name"), h("dd", {}, rules.aliases.join(" · ")),
          h("dt", {}, "Pakistani context required"), h("dd", {}, rules.requires_pakistan_context ? "yes (Pakistani publisher or keyword such as PSX, Karachi, SBP)" : "no (curated rules)"),
          rules.excluded_names.length ? h("dt", {}, "Never matches") : null, rules.excluded_names.length ? h("dd", {}, rules.excluded_names.join(" · ")) : null,
          h("dt", {}, "Window"), h("dd", {}, `last ${data.lookback_days} days · ${data.source}${data.cached ? " · cached" : ""}`))),
      ...(warnings || []).map((w) => h("p", { class: "notice warn", style: { marginTop: "10px" } }, w))));
    if (!data.results.length) {
      results.replaceChildren(h("div", { class: "card empty" }, h("h3", {}, "No headlines name this company right now."),
        h("p", {}, "That usually means it simply wasn't in the news over the last 90 days.")));
    } else {
      results.replaceChildren(...data.results.map((it, i) => h("article", { class: "card hover news-item reveal", style: { "--i": Math.min(i, 12) } },
        h("div", { class: "when" }, h("b", {}, shortDate(it.published_at)), ago(it.published_at)),
        h("div", {},
          h("a", { class: "title", href: it.url, target: "_blank", rel: "noopener noreferrer nofollow" }, it.title),
          h("div", { class: "meta" }, h("span", {}, it.publisher || "unknown publisher"),
            it.tone ? chip(`${it.tone} tone`, it.tone === "positive" ? 1 : it.tone === "negative" ? -1 : 0) : null,
            it.event && it.event !== "general" ? chip(it.event.replaceAll("_", " "), null) : null,
            h("span", { class: "tiny" }, `matched: ${it.match}`),
            it.also && it.also.length ? h("span", { class: "tiny" }, `also reported by ${it.also.slice(0, 3).join(", ")}`) : null)))));
    }
    reveal(head); reveal(results);
  } catch (e) {
    const msg = e.code === "static_untracked" ? `${sym}: ${e.message} Pick one of the quick picks above.`
      : e.status === 404 ? `${sym} is not a listed PSX symbol. Try typing the company name to find its ticker.`
      : e.status === 429 ? "You're searching quickly — please wait a moment and try again."
      : e.status === 503 ? "News sources are temporarily unavailable. Please try again shortly."
      : e.status === 400 ? "Symbols are 1–12 letters or digits, e.g. LUCK." : `Search failed: ${e.message}`;
    head.replaceChildren(h("p", { class: `notice ${e.status === 404 || e.status === 400 ? "warn" : "err"}` }, msg));
    results.replaceChildren();
  }
}

// The static edition renders one page for every query, so the symbol also comes from the URL.
const initial = pageData().symbol || (new URLSearchParams(window.location.search).get("symbol") || "").toUpperCase();
if (initial) { input.value = initial; search(initial); }
if (STATIC) input.placeholder = "Tracked symbol, e.g. LUCK, HBL, OGDC";
