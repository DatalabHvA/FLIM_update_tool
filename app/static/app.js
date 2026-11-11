"use strict";
const $ = (s) => document.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const LABEL = { comtrade: "UN Comtrade", destatis: "Destatis", fred: "FRED" };
const STATE_LABEL = {
  queued: "in de wachtrij", running: "bezig", done: "klaar", error: "gestopt", partial: "deels gelukt",
  pending: "wacht", skipped: "niet geselecteerd", empty: "geen codes",
};

let input = null;        // last parsed upload
let previewTab = null;
let jobId = null;
let logFrom = 0;
let lastSnap = null;
let timer = null;

async function api(path, opts = {}) {
  const r = await fetch(path, opts);
  const body = r.headers.get("content-type")?.includes("json") ? await r.json() : await r.text();
  if (!r.ok) throw new Error(body?.detail || body || r.statusText);
  return body;
}

// ---------------------------------------------------------------- 1. config
async function loadConfig() {
  const cfg = await api("/api/config");
  $("#keys").innerHTML = Object.values(cfg.credentials).map((c) =>
    `<span class="key ${c.present ? "yes" : "no"}" title="${esc(c.present ? "ingesteld" : "ontbreekt: " + c.hint)}">
       ${c.present ? "✓" : "✗"} ${esc(c.label)} ${c.present ? "sleutel aanwezig" : "sleutel ontbreekt"}</span>`).join("");
  for (const [k, v] of Object.entries(cfg.defaults)) { const el = $("#" + k); if (el) el.value = v; }
  const jobs = await api("/api/jobs");
  const active = jobs.find((j) => j.state === "running" || j.state === "queued");
  if (active) { show("#step-progress"); follow(active.id); }
}

// ---------------------------------------------------------------- 2. upload
const drop = $("#drop");
$("#file").addEventListener("change", (e) => e.target.files[0] && upload(e.target.files[0]));
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("over"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("over"); }));
drop.addEventListener("drop", (e) => e.dataTransfer.files[0] && upload(e.dataTransfer.files[0]));

async function upload(file) {
  $("#drop-text").textContent = "Bezig met lezen van " + file.name + " …";
  const fd = new FormData(); fd.append("file", file);
  try {
    input = await api("/api/inputs", { method: "POST", body: fd });
  } catch (err) {
    drop.classList.remove("loaded");
    $("#drop-text").textContent = "Fout: " + err.message;
    return;
  }
  drop.classList.add("loaded");
  const c = input.counts;
  $("#drop-text").textContent = `${file.name}: ${c.comtrade} Comtrade, ${c.destatis} Destatis, ${c.fred} FRED codes (klik om een ander bestand te kiezen)`;
  renderCheck();
}

function renderCheck() {
  show("#step-check");
  const m = [];
  input.errors.forEach((e) => m.push(`<div class="msg error">✗ ${esc(e)}</div>`));
  if (input.warnings.length) {
    const items = input.warnings.map((w) => `<div class="msg warn">⚠ ${esc(w)}</div>`).join("");
    m.push(input.warnings.length > 4
      ? `<details class="msgs"><summary>${input.warnings.length} waarschuwingen</summary>${items}</details>` : items);
  }
  if (!input.errors.length) m.push(`<div class="msg ok">✓ Bestand is bruikbaar.</div>`);
  $("#messages").innerHTML = m.join("");

  const plats = Object.keys(LABEL).filter((p) => input.counts[p]);
  previewTab = plats.includes(previewTab) ? previewTab : plats[0];
  $("#preview-tabs").innerHTML = plats.map((p) =>
    `<button class="tab ${p === previewTab ? "active" : ""}" data-p="${p}">${LABEL[p]} (${input.counts[p]})</button>`).join("");
  document.querySelectorAll("#preview-tabs .tab").forEach((b) =>
    b.addEventListener("click", () => { previewTab = b.dataset.p; renderCheck(); }));
  const rows = input.rows.filter((r) => r.platform === previewTab);
  const price = previewTab !== "comtrade";
  $("#preview").innerHTML = `<tr><th>Rij</th>${price ? "<th>ID</th>" : ""}<th>Materiaal</th><th>Code</th>
      ${previewTab === "destatis" ? "<th>Tabel</th>" : ""}${previewTab === "comtrade" ? "<th>Omschrijving</th>" : ""}</tr>` +
    rows.map((r) => `<tr><td>${r.sheet_row}</td>${price ? `<td>${esc(r.id)}</td>` : ""}<td>${esc(r.name)}</td>
      <td><code>${esc(r.code)}</code></td>${previewTab === "destatis" ? `<td>${esc(r.table || "standaard")}</td>` : ""}
      ${previewTab === "comtrade" ? `<td class="msgcell">${esc(r.description)}</td>` : ""}</tr>`).join("");

  document.querySelectorAll("input[name=platform]").forEach((cb) => {
    cb.disabled = !input.counts[cb.value];
    cb.checked = !!input.counts[cb.value];
  });
  if (input.errors.length) hide("#step-settings"); else show("#step-settings");
  $("#start").disabled = !!input.errors.length;
}

// ---------------------------------------------------------------- 3. start
$("#start").addEventListener("click", async () => {
  const platforms = [...document.querySelectorAll("input[name=platform]:checked")].map((c) => c.value);
  const body = { input_id: input.input_id, platforms };
  ["comtrade_period", "comtrade_reporter", "prices_start", "prices_end", "destatis_table"]
    .forEach((k) => (body[k] = $("#" + k).value));
  $("#start").disabled = true; $("#start-msg").textContent = "";
  try {
    const r = await api("/api/jobs", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    hide("#step-results"); show("#step-progress");
    follow(r.job_id);
    $("#step-progress").scrollIntoView({ behavior: "smooth" });
  } catch (err) {
    $("#start-msg").textContent = err.message;
    $("#start-msg").style.color = "var(--err)";
    $("#start").disabled = false;
  }
});

// ---------------------------------------------------------------- 4. progress
function follow(id) {
  jobId = id; logFrom = 0; $("#log").innerHTML = ""; lastSnap = null;
  clearTimeout(timer); poll();
}

async function poll() {
  let snap;
  try { snap = await api(`/api/jobs/${jobId}?log_from=${logFrom}`); }
  catch (err) { $("#run-state").textContent = err.message; timer = setTimeout(poll, 3000); return; }
  lastSnap = snap;
  renderProgress(snap);
  const active = snap.state === "running" || snap.state === "queued";
  if (active) timer = setTimeout(poll, 1000);
  else { $("#start").disabled = !input || !!input.errors.length; renderResults(snap); }
}

function renderProgress(s) {
  const st = $("#run-state");
  st.className = "pill st-" + s.state; st.textContent = STATE_LABEL[s.state] || s.state;
  $("#elapsed").textContent = s.elapsed ? fmtTime(s.elapsed) : "";
  $("#cancel").classList.toggle("hidden", !(s.state === "running" || s.state === "queued") || s.cancel_requested);

  $("#platforms").innerHTML = Object.entries(s.platforms).filter(([, p]) => p.total).map(([k, p]) => {
    const c = p.counts, active = p.total - c.skipped;
    const pct = (n) => (active ? (100 * n) / active : 0);
    return `<div class="plat">
      <div class="plat-head">${esc(p.label)} <span class="pill st-${p.state}">${esc(STATE_LABEL[p.state] || p.state)}</span></div>
      <div class="bar"><span class="b-ok" style="width:${pct(c.ok)}%"></span><span class="b-fetched" style="width:${pct(c.fetched)}%"></span>
        <span class="b-nodata" style="width:${pct(c.nodata)}%"></span><span class="b-failed" style="width:${pct(c.failed)}%"></span></div>
      <div class="counts">${p.done} / ${active} verwerkt · ✓ ${c.ok + c.fetched} · geen data ${c.nodata} · ✗ ${c.failed}</div>
      ${p.message ? `<div class="pmsg ${p.state === "error" ? "error" : ""}">${esc(p.message)}</div>` : ""}
    </div>`;
  }).join("");

  const onlyProblems = document.querySelector("input[name=filter]:checked").value === "problems";
  const items = s.items.filter((it) => !onlyProblems || ["failed", "nodata"].includes(it.status));
  $("#items").innerHTML = `<tr><th>Platform</th><th>Code</th><th>Materiaal</th><th>Status</th><th>Melding</th></tr>` +
    (items.length ? items.map((it) => `<tr><td>${esc(it.platform_label)}</td><td><code>${esc(it.code)}</code></td>
      <td>${esc(it.name)}</td><td><span class="pill st-${it.status}">${esc(it.status_label)}</span></td>
      <td class="msgcell" title="${esc(it.detail)}">${esc(it.message)}</td></tr>`).join("")
      : `<tr><td colspan="5" class="muted">Geen problemen.</td></tr>`);

  if (s.log.length) {
    $("#log").insertAdjacentHTML("beforeend", s.log.map((l) =>
      `<span class="l-${l.level}">${esc(l.t)}  ${esc(l.msg)}</span>\n`).join(""));
    logFrom = s.log_total;
    const pre = $("#log"); pre.scrollTop = pre.scrollHeight;
  }
  $("#log-count").textContent = s.log_total;
}
document.querySelectorAll("input[name=filter]").forEach((r) => r.addEventListener("change", () => lastSnap && renderProgress(lastSnap)));
$("#cancel").addEventListener("click", async () => {
  if (!confirm("Update afbreken? Wat al is opgehaald gaat verloren.")) return;
  await api(`/api/jobs/${jobId}/cancel`, { method: "POST" });
});

// ---------------------------------------------------------------- 5. results
function renderResults(s) {
  show("#step-results");
  const all = s.items.filter((i) => i.status !== "skipped");
  const failed = all.filter((i) => i.status === "failed").length;
  const nodata = all.filter((i) => i.status === "nodata").length;
  let banner;
  if (s.state === "error") banner = `<div class="msg error">✗ ${esc(s.error || "De update is gestopt.")}</div>`;
  else if (failed || nodata) banner = `<div class="msg warn">⚠ Klaar, maar ${failed} code(s) mislukt en ${nodata} zonder data.
      Zie de tabel hierboven (filter “Alleen problemen”) of rapport.xlsx. De bestanden bevatten alleen wat wel gelukt is.</div>`;
  else banner = `<div class="msg ok">✓ Alles gelukt (${all.length} codes) in ${fmtTime(s.elapsed)}.</div>`;
  $("#result-banner").innerHTML = banner;
  const outs = Object.entries(s.outputs);
  $("#downloads").innerHTML = outs.length ? outs.map(([k, o]) =>
    `<a class="dl" href="/api/jobs/${s.id}/files/${k}"><b>⬇ ${esc(o.filename)}</b><span class="muted">${esc(o.summary)}</span></a>`).join("")
    : `<span class="muted">Er zijn geen bestanden gemaakt.</span>`;
}
$("#again").addEventListener("click", () => {
  hide("#step-results"); hide("#step-progress");
  $("#step-input").scrollIntoView({ behavior: "smooth" });
});

// ---------------------------------------------------------------- utils
function show(sel) { $(sel).classList.remove("hidden"); }
function hide(sel) { $(sel).classList.add("hidden"); }
function fmtTime(sec) { sec = Math.round(sec); return sec < 60 ? `${sec} s` : `${Math.floor(sec / 60)} min ${sec % 60} s`; }

loadConfig().catch((e) => ($("#keys").textContent = "Kan configuratie niet laden: " + e.message));
