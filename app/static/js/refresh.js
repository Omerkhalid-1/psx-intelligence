// "Refresh latest data": enqueues POST /api/v1/refresh (admin) and follows the job until it finishes.
// The admin token is held in memory; it is kept in sessionStorage (this tab only) only if the user opts in.
// All server text is inserted with textContent (never innerHTML).
(function () {
  "use strict";
  const root = document.getElementById("refresh-bar");
  if (!root) return;

  const $ = (sel) => root.querySelector(sel);
  const statusEl = $("[data-refresh-status]");
  const detailEl = $("[data-refresh-detail]");
  const form = $("form");
  const tokenInput = $("input[type=password]");
  const remember = $("input[type=checkbox]");
  const startBtn = $("[data-refresh-start]");
  const STORE_KEY = "psx.adminToken";
  const POLL_MS = 3000;
  const STEP_LABELS = {
    market_data: "daily bars", market_session: "new session check", data_quality: "data quality",
    corporate_actions: "corporate actions", news: "news", features: "analysis", forecasts: "forecasts",
    evaluation: "forecast scoring", ensemble_weights: "ensemble weights", retraining: "retraining check",
    promotion: "promotion",
  };

  try { const t = sessionStorage.getItem(STORE_KEY); if (t) { tokenInput.value = t; remember.checked = true; } }
  catch (e) { /* storage unavailable: token stays in memory only */ }

  function setStatus(text, tone) {
    statusEl.textContent = text;
    statusEl.className = "badge text-bg-" + (tone || "secondary");
  }

  function fmtTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString(undefined, {dateStyle: "medium", timeStyle: "short"});
  }

  function describe(job) {
    const s = job.result_summary || {};
    const parts = [];
    if (s.quotes) parts.push("quotes: " + s.quotes.status + (s.quotes.stored != null ? " (" + s.quotes.stored + " stored)" : ""));
    if (s.pipeline) {
      const p = s.pipeline;
      if (p.steps) {
        const done = Object.entries(p.steps).filter(([, st]) => st !== "skipped")
          .map(([k, st]) => (STEP_LABELS[k] || k) + " " + st);
        parts.push(p.new_session ? "new session " + p.session + " processed" : "no new session since " + (p.session || "n/a"));
        parts.push(done.join(", "));
      } else {
        parts.push("pipeline: " + p.status + (p.message ? " (" + p.message + ")" : ""));
      }
    }
    if (job.error_message) parts.push(job.error_message);
    return parts.join(" · ");
  }

  function showJob(job) {
    const when = job.finished_at || job.started_at || job.created_at;
    const tone = {queued: "info", running: "primary", succeeded: "success", failed: "danger", dispatch_failed: "danger"};
    const label = {queued: "Refresh queued", running: "Refreshing…", succeeded: "Last refresh succeeded",
                   failed: "Last refresh failed", dispatch_failed: "Refresh could not be dispatched"}[job.status];
    setStatus(label || job.status, tone[job.status]);
    let text = fmtTime(when);
    if (job.status === "queued") text += " · waiting for a worker (is the Celery worker running?)";
    const d = describe(job);
    detailEl.textContent = d ? text + " · " + d : text;
  }

  async function api(path, opts) {
    const r = await fetch(path, Object.assign({headers: {"Accept": "application/json"}}, opts || {}));
    let body = null;
    try { body = await r.json(); } catch (e) { /* non-JSON */ }
    return {status: r.status, body: body};
  }

  async function follow(jobId) {
    startBtn.disabled = true;
    for (;;) {
      const r = await api("/api/v1/jobs/" + encodeURIComponent(jobId));
      if (r.status !== 200 || !r.body) { setStatus("Status unavailable", "warning"); break; }
      const job = r.body.data;
      showJob(job);
      if (["succeeded", "failed", "dispatch_failed"].includes(job.status)) {
        if (job.status === "succeeded") {
          detailEl.textContent += " · reloading…";
          setTimeout(() => window.location.reload(), 1500);
        }
        break;
      }
      await new Promise((res) => setTimeout(res, POLL_MS));
    }
    startBtn.disabled = false;
  }

  form.addEventListener("submit", async (ev) => {
    ev.preventDefault();
    const token = tokenInput.value.trim();
    if (!token) { tokenInput.focus(); return; }
    try {
      if (remember.checked) sessionStorage.setItem(STORE_KEY, token); else sessionStorage.removeItem(STORE_KEY);
    } catch (e) { /* ignore */ }
    startBtn.disabled = true;
    setStatus("Requesting…", "info");
    const r = await api("/api/v1/refresh", {
      method: "POST",
      headers: {"Content-Type": "application/json", "Accept": "application/json", "Authorization": "Bearer " + token},
      body: "{}",
    });
    if (r.status === 202 || r.status === 200) { follow(r.body.data.id); return; }
    startBtn.disabled = false;
    const err = (r.body && r.body.errors && r.body.errors[0]) || {};
    if (r.status === 401 || r.status === 403) { setStatus("Admin token rejected", "danger"); }
    else if (r.status === 429 && err.details && err.details.job_id) {
      setStatus("Too soon", "warning"); detailEl.textContent = err.message || ""; follow(err.details.job_id);
      return;
    } else { setStatus("Refresh not started", "danger"); }
    detailEl.textContent = err.message || ("HTTP " + r.status);
  });

  // On load: show the most recent refresh and resume following one that is still in flight.
  api("/api/v1/jobs?job_type=refresh.latest&page_size=1").then((r) => {
    const job = r.body && r.body.data && r.body.data[0];
    if (!job) { setStatus("Not refreshed manually yet", "secondary"); return; }
    showJob(job);
    if (job.status === "queued" || job.status === "running") follow(job.id);
  });
})();
