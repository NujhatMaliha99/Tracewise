"use strict";

const platform = {cases: [], active: null, request: 0, historyQuery: "", historyStatus: "all", historySort: "updated", archived: false};
const platformViews = {safety: "Workspace safety", reliability: "Reliability objectives", changes: "Change intelligence", reviews: "Incident reviews", catalog: "Service catalog", incidents: "Live incidents", connections: "Telemetry connections", traces: "Request traces", dependencies: "Dependency incidents", experiments: "Fault experiments", history: "Saved incidents", live: "Live ingestion", monitors: "API monitors", alerts: "Alert inbox", integrations: "SDK integrations", analytics: "Service analytics", search: "Incident search", anomalies: "Anomaly analysis", compare: "Compare incidents", runbooks: "Runbooks"};
let liveTimer = null;
let liveData = null;
let livePaused = false;
const statusBadge = value => `<span class="incident-status ${esc(value)}">${esc(value)}</span>`;
const priorityBadge = value => `<span class="priority ${esc(value)}">${esc(value)}</span>`;
const dateLabel = value => value ? new Date(value).toLocaleString("en-GB", {timeZone: "UTC", dateStyle: "medium", timeStyle: "short"}) + " UTC" : "-";

async function getJSON(path) {
  const response = await fetch(path);
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "Request failed.");
  return result;
}

function platformError(error) {
  notify(error.message || String(error), true);
  setBusy(false);
}

function heading(title, subtitle, controls = "") {
  return `<div class="platform-heading"><div><h2>${title}</h2><p>${subtitle}</p></div><div class="actions">${controls}</div></div>`;
}

function emptyState(title, description) {
  return `<div class="empty">${icon("inbox")}<h2>${title}</h2><p>${description}</p></div>`;
}

function openDialog(title, body) {
  let dialog = $("platform-dialog");
  if (!dialog) {
    dialog = document.createElement("dialog");
    dialog.id = "platform-dialog";
    dialog.setAttribute("aria-labelledby", "platform-dialog-title");
    document.body.appendChild(dialog);
  }
  dialog.innerHTML = `<div class="dialog-heading"><h2 id="platform-dialog-title">${esc(title)}</h2><button class="icon-button" id="platform-close" aria-label="Close dialog">${icon("x")}</button></div>${body}<div id="platform-form-error" class="import-error" role="alert"></div>`;
  $("platform-close").onclick = () => dialog.close();
  icons();
  if (!dialog.open) dialog.showModal();
  return dialog;
}

window.platformEnhance = function () {
  let saveButton = $("save-investigation");
  if (!saveButton) {
    saveButton = document.createElement("button");
    saveButton.id = "save-investigation";
    saveButton.className = "button secondary";
    document.querySelector(".page-heading .actions").prepend(saveButton);
  }
  const saved = platform.active && state.data?.saved_case_id === platform.active.id;
  const workspaceView = Boolean(platformViews[state.view]);
  document.querySelector(".sourcebar").hidden = workspaceView;
  saveButton.hidden = workspaceView;
  if (workspaceView) $("source-name").textContent = "Saved evidence / local investigation workspace";
  saveButton.innerHTML = icon(saved ? "clipboard-list" : "save") + (saved ? " Manage incident" : " Save investigation");
  saveButton.disabled = !state.data;
  saveButton.onclick = () => saved ? manageCase(platform.active.id) : saveCurrent();
  $("export").title = saved ? "Export saved incident JSON" : "Export unsaved investigation JSON";
  if (saved && !workspaceView) $("source-name").textContent = platform.active.title;
  icons();
};

async function saveCurrent() {
  if (!state.data) return;
  const dialog = openDialog("Save investigation", `<form id="save-form"><div class="field"><label for="case-title">Incident title</label><input id="case-title" class="text-input" maxlength="160" required value="${esc(state.data.filename)}"></div><div class="field"><label for="case-priority">Priority</label><select id="case-priority">${["low", "medium", "high", "critical"].map(p => `<option ${p === "medium" ? "selected" : ""}>${p}</option>`).join("")}</select></div><p class="security-note">Redacted normalized logs will be retained in the local SQLite workspace. Source positions are reindexed in this saved snapshot. Archiving retains data; clearing the current view does not erase saved incidents.</p><label class="consent"><input id="persist-consent" type="checkbox" required> Retain this investigation locally</label><div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} Save incident</button></div></form>`);
  $("save-form").onsubmit = async event => {
    event.preventDefault();
    const button = event.submitter;
    button.disabled = true;
    try {
      const current = await api("/api/cases", {title: $("case-title").value, priority: $("case-priority").value, content: state.data.records.map(r => JSON.stringify(r)).join("\n"), filename: state.data.filename, source: state.data.source || "Explicitly saved normalized log snapshot"});
      platform.active = current;
      load({...current.report, saved_case_id: current.id});
      dialog.close();
      notify("Investigation saved locally. Its normalized snapshot has new source positions.");
    } catch (error) {
      $("platform-form-error").textContent = error.message;
    } finally {
      button.disabled = false;
    }
  };
}

async function openCase(id) {
  try {
    const item = await getJSON(`/api/cases/${encodeURIComponent(id)}`);
    platform.active = item;
    load({...item.report, saved_case_id: item.id});
    location.hash = "investigation";
    render();
  } catch (error) { platformError(error); }
}

async function manageCase(id) {
  try {
    const item = await getJSON(`/api/cases/${encodeURIComponent(id)}`);
    const dialog = openDialog("Incident management", `<form id="manage-form"><div class="field"><label for="manage-title">Title</label><input id="manage-title" class="text-input" value="${esc(item.title)}" maxlength="160" required></div><div class="form-grid"><div class="field"><label for="manage-status">Status</label><select id="manage-status">${["open", "investigating", "monitoring", "resolved", "archived"].map(s => `<option ${s === item.status ? "selected" : ""}>${s}</option>`).join("")}</select></div><div class="field"><label for="manage-priority">Priority</label><select id="manage-priority">${["critical", "high", "medium", "low"].map(s => `<option ${s === item.priority ? "selected" : ""}>${s}</option>`).join("")}</select></div></div><div class="field"><label for="manage-owner">Owner label</label><input id="manage-owner" class="text-input" maxlength="100" value="${esc(item.owner)}" placeholder="Unassigned"></div><div class="dialog-footer"><button type="submit" class="button primary">Save changes</button></div></form><section class="management-section"><h3>Investigation notes</h3><form id="note-form"><textarea id="note-body" aria-label="Investigation note" rows="3" maxlength="4000" required placeholder="Evidence checked, findings, and next steps..."></textarea><div class="dialog-footer"><button class="button secondary" type="submit">${icon("message-square-plus")} Add note</button></div></form>${item.notes.map(n => `<div class="saved-note"><small>${dateLabel(n.created_at)}</small><p>${esc(n.body)}</p></div>`).join("")}</section><section class="management-section"><h3>Activity trail</h3>${item.audit.slice(0,20).map(a => `<div class="audit-row"><strong>${esc(a.action.replaceAll("_", " "))}</strong><small>${dateLabel(a.created_at)}</small><code>${esc(JSON.stringify(a.detail))}</code></div>`).join("")}</section><div class="report-links"><a href="/api/cases/${item.id}/export">${icon("file-json")} JSON evidence</a><a href="/api/cases/${item.id}/report">${icon("file-text")} Incident report</a></div>`);
    $("manage-form").onsubmit = async event => {
      event.preventDefault(); event.submitter.disabled = true;
      try {
        const updated = await api(`/api/cases/${item.id}`, {version: item.version, changes: {title: $("manage-title").value, status: $("manage-status").value, priority: $("manage-priority").value, owner: $("manage-owner").value}});
        if (platform.active?.id === item.id) platform.active = updated;
        dialog.close(); render(); notify("Incident updated.");
      } catch (error) { $("platform-form-error").textContent = error.message; event.submitter.disabled = false; }
    };
    $("note-form").onsubmit = async event => {
      event.preventDefault(); event.submitter.disabled = true;
      try {
        const updated = await api(`/api/cases/${item.id}/notes`, {body: $("note-body").value});
        if (platform.active?.id === item.id) platform.active = updated;
        dialog.close(); await manageCase(item.id);
      } catch (error) { $("platform-form-error").textContent = error.message; event.submitter.disabled = false; }
    };
  } catch (error) { platformError(error); }
}

function caseTable() {
  const query = platform.historyQuery.toLowerCase();
  const cases = platform.cases.filter(c => (platform.historyStatus === "all" || c.status === platform.historyStatus) && [c.title, c.owner, ...c.services].join(" ").toLowerCase().includes(query));
  const priorities = {critical: 0, high: 1, medium: 2, low: 3};
  cases.sort((a, b) => platform.historySort === "priority" ? priorities[a.priority] - priorities[b.priority] || b.updated_at.localeCompare(a.updated_at) : platform.historySort === "errors" ? b.summary.errors - a.summary.errors || b.updated_at.localeCompare(a.updated_at) : b.updated_at.localeCompare(a.updated_at));
  return `<div class="table-scroll"><table><thead><tr><th>Incident</th><th>Status</th><th>Priority</th><th>Errors</th><th>Owner</th><th>Updated</th><th></th></tr></thead><tbody>${cases.map(c => `<tr><td><button class="case-link" data-open="${c.id}">${esc(c.title)}</button><small class="cell-subtitle">${esc(c.services.join(", "))}</small>${c.source.includes("ynthetic") ? '<span class="fixture-label">SYNTHETIC FIXTURE</span>' : ""}</td><td>${statusBadge(c.status)}</td><td>${priorityBadge(c.priority)}</td><td>${c.summary.errors}<small class="cell-subtitle">${c.summary.records} events</small></td><td>${esc(c.owner) || "Unassigned"}</td><td class="date-cell">${dateLabel(c.updated_at)}</td><td><button class="icon-button" aria-label="Manage ${esc(c.title)}" data-manage="${c.id}">${icon("ellipsis")}</button></td></tr>`).join("")}</tbody></table></div>${cases.length ? `<div class="table-bottom">${cases.length} saved investigations</div>` : emptyState(platform.cases.length ? "No matching incidents" : "No saved incidents", platform.cases.length ? "No results for the selected filters." : "Save a current investigation or add the labelled demo workspace.")}`;
}

async function historyView() {
  const result = await getJSON("/api/cases?archived=" + (platform.archived ? "1" : "0"));
  if (state.view !== "history") return;
  platform.cases = result.cases;
  $("content").innerHTML = heading("Saved incidents", "Local SQLite workspace / explicit retention", `<button class="button secondary" id="seed-workspace">${icon("database")} Add demo workspace</button>`) + `<div class="history-overview" aria-label="Incident overview">${[["Saved", platform.cases.length], ["Open", platform.cases.filter(c => c.status === "open").length], ["High / critical", platform.cases.filter(c => ["high", "critical"].includes(c.priority)).length]].map(([label, count]) => `<div><strong>${count}</strong><span>${label}</span></div>`).join("")}</div>` + `<div class="history-tools"><label class="search">${icon("search")}<input id="history-search" type="search" aria-label="Search saved incidents" placeholder="Search title, service or owner" value="${esc(platform.historyQuery)}"></label><select id="history-status" aria-label="Incident status">${["all", "open", "investigating", "monitoring", "resolved", "archived"].map(s => `<option value="${s}" ${s === platform.historyStatus ? "selected" : ""}>${s === "all" ? "All statuses" : s}</option>`).join("")}</select><select id="history-sort" aria-label="Sort incidents">${[["updated", "Recently updated"], ["priority", "Highest priority"], ["errors", "Most errors"]].map(([value, label]) => `<option value="${value}" ${platform.historySort === value ? "selected" : ""}>${label}</option>`).join("")}</select><button id="history-reset" class="icon-button" aria-label="Reset incident filters" title="Reset filters">${icon("filter-x")}</button><label class="consent"><input id="show-archived" type="checkbox" ${platform.archived ? "checked" : ""}> Include archived</label></div><div id="case-table">${caseTable()}</div>`;
  $("history-search").oninput = e => { platform.historyQuery = e.target.value; $("case-table").innerHTML = caseTable(); icons(); };
  $("history-status").onchange = e => { platform.historyStatus = e.target.value; $("case-table").innerHTML = caseTable(); icons(); };
  $("history-sort").onchange = e => { platform.historySort = e.target.value; $("case-table").innerHTML = caseTable(); icons(); };
  $("history-reset").onclick = () => { platform.historyQuery = ""; platform.historyStatus = "all"; platform.historySort = "updated"; platform.archived = false; historyView().catch(platformError); };
  $("show-archived").onchange = e => { platform.archived = e.target.checked; historyView().catch(platformError); };
  $("seed-workspace").onclick = () => {
    openDialog("Add synthetic demo workspace", `<p class="security-note">Six labelled synthetic investigations will be retained locally. Each contains 24 observed minutes with deliberately injected error bursts. Repeating this action does not duplicate fixtures.</p><div class="dialog-footer"><button id="confirm-seed" class="button primary">Add six demo incidents</button></div>`);
    $("confirm-seed").onclick = async event => {
      event.target.disabled = true;
      try { await api("/api/workspace/seed"); $("platform-dialog").close(); await historyView(); notify("Six synthetic fixtures are available. These are not production incidents."); }
      catch (error) { $("platform-form-error").textContent = error.message; event.target.disabled = false; }
    };
  };
  icons();
}

let analyticsCache = null;
let analyticsMode = "events";

async function analyticsView() {
  const result = await getJSON("/api/analytics");
  if (state.view !== "analytics") return;
  analyticsCache = result;
  const active = Object.entries(result.status_counts).filter(([key]) => key !== "resolved").reduce((sum, [, n]) => sum + n, 0);
  const metrics = [
    ["SAVED INVESTIGATIONS", result.cases, "folder-open", "Retained non-archived cases"],
    ["ACTIVE INCIDENTS", active, "circle-alert", "Awaiting final resolution"],
    ["LOG EVENTS", result.events, "logs", "Parsed log records"],
    ["ERROR EVENTS", result.errors, "zap", "ERROR / FATAL records"]
  ];
  const max = Math.max(1, ...result.services.map(s => s.errors));
  $("content").innerHTML = heading("Service analytics", "Saved evidence / workspace overview", `<a class="button secondary" href="#history">${icon("inbox")} Browse incidents</a>`) +
    `<div class="stats analytics-metrics">${metrics.map(([label, n, symbol, context], i) => `<div class="stat metric-${i}"><div class="stat-label">${label}<span class="metric-symbol">${icon(symbol)}</span></div><strong>${n.toLocaleString()}</strong><small>${context}</small></div>`).join("")}</div>
    <div class="analytics-provenance"><span>${icon("database")} Local SQLite workspace</span>${result.synthetic_cases ? '<span class="fixture-label">Includes synthetic demonstration data</span>' : ""}<span>${result.services.length} observed services</span></div>
    <div class="analytics-primary">
      <section class="activity-surface">
        <div class="section-title"><div><h2>Incident activity</h2><p>Latest ${result.activity.length} observed hours / UTC</p></div><div class="chart-modes" role="group" aria-label="Activity metric"><button id="activity-events" aria-pressed="${analyticsMode === "events"}">All events</button><button id="activity-errors" aria-pressed="${analyticsMode === "errors"}">Errors</button></div></div>
        <div class="activity-key"><span ${analyticsMode === "errors" ? "hidden" : ""}><i class="dot"></i>Non-error records</span><span><i class="dot error"></i>Error records</span><output id="activity-readout">Observed UTC hours</output></div>
        ${result.activity.length ? '<canvas id="analytics-activity" role="img" aria-label="Saved log counts by observed UTC hour"></canvas>' : emptyState("No dated observations", "Saved records with timestamps populate activity.")}
        <details class="activity-data"><summary>Hourly records</summary><div class="table-scroll"><table><thead><tr><th>UTC hour</th><th>Events</th><th>Errors</th></tr></thead><tbody>${result.activity.map(b => `<tr><td>${esc(dateLabel(b.timestamp))}</td><td>${b.events}</td><td>${b.errors}</td></tr>`).join("")}</tbody></table></div></details>
        <p class="footnote">Observed hours only; missing intervals are not zero-filled. ${result.undated} undated records excluded. Log counts, not request rates.</p>
      </section>
      <section class="lifecycle-surface"><div class="section-title"><h2>Incident lifecycle</h2><span class="badge">CASE STATUS</span></div><canvas id="analytics-lifecycle" role="img" aria-label="Counts of saved cases by lifecycle status"></canvas><div class="lifecycle-key">${["open", "investigating", "monitoring", "resolved"].map(s => `<div>${statusBadge(s)}<strong>${result.status_counts[s] || 0}</strong></div>`).join("")}</div></section>
    </div>
    <div class="analytics-secondary">
      <section class="service-surface"><div class="section-title"><div><h2>Service breakdown</h2><p>Error records / saved investigations</p></div><span class="badge">${result.services.length} SERVICES</span></div>${result.services.map((s, i) => `<div class="service-bar service-tone-${i % 4}"><span class="service-symbol">${icon("server")}</span><div><strong>${esc(s.service)}</strong><small>${s.investigations} incidents / ${s.events} log events</small></div><meter min="0" max="${max}" value="${s.errors}" aria-label="${esc(s.service)} error count"></meter><b>${s.errors}</b></div>`).join("") || emptyState("No saved services", "No retained evidence available.")}</section>
      <section class="recent-surface"><div class="section-title"><h2>Recent incidents</h2><a class="icon-button" href="#history" aria-label="All saved incidents" title="All incidents">${icon("arrow-up-right")}</a></div>${result.recent.slice(0, 5).map(c => `<div class="recent-incident"><span class="recent-symbol">${icon("file-stack")}</span><div><button class="case-link" data-open="${c.id}">${esc(c.title)}</button><small>${dateLabel(c.updated_at)}</small></div>${statusBadge(c.status)}</div>`).join("") || emptyState("No recent incidents", "No retained evidence available.")}</section>
    </div><p class="footnote">${esc(result.scope)}</p>`;
  for (const mode of ["events", "errors"]) $("activity-" + mode).onclick = () => {
    analyticsMode = mode;
    document.querySelector(".activity-key > span").hidden = mode === "errors";
    for (const value of ["events", "errors"]) $("activity-" + value).setAttribute("aria-pressed", String(value === mode));
    drawAnalytics();
  };
  drawAnalytics();
  icons();
}

function chartSurface(id, height) {
  const canvas = $(id);
  if (!canvas) return null;
  const width = canvas.clientWidth || 300, scale = window.devicePixelRatio || 1;
  canvas.width = width * scale; canvas.height = height * scale;
  const ctx = canvas.getContext("2d"); ctx.scale(scale, scale);
  return {canvas, ctx, width, height};
}

function drawAnalytics() {
  if (state.view !== "analytics" || !analyticsCache) return;
  const result = analyticsCache, chart = chartSurface("analytics-activity", 220);
  if (chart) {
    const {canvas, ctx, width, height} = chart;
    const buckets = result.activity, max = Math.max(1, ...buckets.map(b => b[analyticsMode]));
    const left = 34, bottom = 37, plotHeight = height - bottom - 12, step = (width - left - 10) / buckets.length;
    ctx.font = "10px Segoe UI"; ctx.fillStyle = themeColor("--muted"); ctx.strokeStyle = themeColor("--line");
    for (let i = 0; i <= 3; i++) {
      const y = 12 + i * plotHeight / 3;
      ctx.beginPath(); ctx.moveTo(left, y); ctx.lineTo(width, y); ctx.stroke();
      ctx.fillText(String(Math.round(max * (1 - i / 3))), 0, y + 3);
    }
    buckets.forEach((b, i) => {
      const x = left + step * (i + .5), bar = Math.max(3, Math.min(42, step * .55)), base = height - bottom;
      const errorHeight = b.errors / max * plotHeight;
      if (analyticsMode === "events") {
        const normalHeight = (b.events - b.errors) / max * plotHeight;
        ctx.fillStyle = themeColor("--accent"); ctx.fillRect(x - bar / 2, base - normalHeight, bar, normalHeight);
        ctx.fillStyle = themeColor("--red"); ctx.fillRect(x - bar / 2, base - normalHeight - errorHeight, bar, errorHeight);
      } else {
        ctx.fillStyle = themeColor("--red"); ctx.fillRect(x - bar / 2, base - errorHeight, bar, errorHeight);
      }
      if (i % Math.ceil(buckets.length / (width < 450 ? 4 : 7)) === 0) {
        const date = new Date(b.timestamp);
        ctx.fillStyle = themeColor("--muted"); ctx.textAlign = "center";
        ctx.fillText(date.toLocaleDateString("en-GB", {day: "2-digit", month: "short", timeZone: "UTC"}), x, height - 17);
        ctx.fillText(date.toLocaleTimeString("en-GB", {hour: "2-digit", minute: "2-digit", timeZone: "UTC"}), x, height - 3);
        ctx.textAlign = "left";
      }
    });
    canvas.onmousemove = event => {
      const x = event.clientX - canvas.getBoundingClientRect().left;
      const index = Math.max(0, Math.min(buckets.length - 1, Math.floor((x - left) / step)));
      const bucket = buckets[index];
      $("activity-readout").textContent = dateLabel(bucket.timestamp) + " / " + bucket.events + " events / " + bucket.errors + " errors";
    };
    canvas.onmouseleave = () => { $("activity-readout").textContent = "Observed UTC hours"; };
  }
  const ring = chartSurface("analytics-lifecycle", 170);
  if (ring) {
    const {ctx, width} = ring, x = width / 2, y = 84, radius = 61, total = result.cases;
    let angle = -Math.PI / 2;
    ctx.lineWidth = 15; ctx.strokeStyle = themeColor("--line"); ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.stroke();
    for (const [status, color] of [["open", "--red"], ["investigating", "--amber"], ["monitoring", "--cyan"], ["resolved", "--success"]]) {
      const length = total ? (result.status_counts[status] || 0) / total * Math.PI * 2 : 0;
      if (length) { ctx.strokeStyle = themeColor(color); ctx.beginPath(); ctx.arc(x, y, radius, angle + .015, angle + length - .015); ctx.stroke(); }
      angle += length;
    }
    ctx.textAlign = "center"; ctx.fillStyle = themeColor("--ink"); ctx.font = "600 30px Bahnschrift"; ctx.fillText(String(total), x, y + 3);
    ctx.fillStyle = themeColor("--muted"); ctx.font = "11px Segoe UI"; ctx.fillText("SAVED CASES", x, y + 24);
  }
}

window.addEventListener("resize", () => { if (state.view === "analytics") drawAnalytics(); });

function searchView() {
  $("content").innerHTML = heading("Cross-incident search", "TF-IDF + cosine similarity / local lexical retrieval") + `<form id="retrieval-form" class="retrieval-form"><label class="search">${icon("search")}<input id="retrieval-query" aria-label="Incident search query" type="search" maxlength="2000" required placeholder="Payment timed out, connection pool exhausted..."></label><button class="button primary" type="submit">Search incidents</button></form><div id="retrieval-results">${emptyState("Search your investigation history", "Results link to saved evidence. Similarity is not root-cause confidence.")}</div>`;
  $("retrieval-form").onsubmit = async event => {
    event.preventDefault(); const button = event.submitter; button.disabled = true;
    try {
      const result = await api("/api/search", {query: $("retrieval-query").value});
      if (!result.available) { $("retrieval-results").innerHTML = emptyState("ML dependency unavailable", esc(result.reason)); return; }
      $("retrieval-results").innerHTML = result.matches.map(m => `<article class="search-result"><div class="section-title"><button class="case-link" data-open="${m.id}">${esc(m.title)}</button><span class="badge">COSINE ${m.score.toFixed(3)}</span></div><div class="detail-meta">${statusBadge(m.status)}<span>${esc(m.services.join(", "))}</span></div>${m.evidence.map(e => `<code>${esc(e)}</code>`).join("")}</article>`).join("") || emptyState("No lexical matches", "Try terms that appear in saved incident messages.");
      icons();
    } catch (error) { platformError(error); }
    finally { button.disabled = false; }
  };
  icons();
}

async function anomalyView() {
  const result = await getJSON("/api/cases");
  if (state.view !== "anomalies") return;
  const options = result.cases.map(c => `<option value="${c.id}">${esc(c.title)}</option>`).join("");
  $("content").innerHTML = heading("Anomaly analysis", "Isolation Forest / descriptive observed-minute outliers") + `<form id="anomaly-form" class="retrieval-form"><select id="anomaly-source" aria-label="Anomaly investigation">${state.data ? '<option value="current">Current unsaved / loaded investigation</option>' : ""}${options}</select><button class="button primary" type="submit" ${!state.data && !result.cases.length ? "disabled" : ""}>${icon("scan-line")} Analyze windows</button></form><div id="anomaly-results">${emptyState("Select an investigation", "At least 12 observed minute buckets are required. Demo workspace fixtures contain 24.")}</div>`;
  $("anomaly-form").onsubmit = async event => {
    event.preventDefault(); event.submitter.disabled = true;
    try {
      const source = $("anomaly-source").value;
      const payload = source === "current" ? {content: state.data.records.map(r => JSON.stringify(r)).join("\n")} : {case_id: source};
      const result = await api("/api/anomalies", payload);
      if (!result.available) { $("anomaly-results").innerHTML = emptyState("Insufficient analysis data", esc(result.reason)); return; }
      $("anomaly-results").innerHTML = `<div class="analysis-callout"><strong>${result.flagged} flagged observed windows</strong><p>${esc(result.limitation || result.reason)}</p><small>${esc(result.method)}</small></div><div class="table-scroll"><table><thead><tr><th>Observed minute (UTC)</th><th>Events</th><th>Errors</th><th>Error fraction</th><th>Decision score</th><th>Flag</th></tr></thead><tbody>${result.windows.map(w => `<tr><td>${time(w.timestamp)}</td><td>${w.events}</td><td>${w.errors}</td><td>${(100 * w.errors / Math.max(1,w.events)).toFixed(1)}%</td><td>${w.decision_score.toFixed(5)}</td><td>${w.flagged ? '<span class="priority high">Outlier</span>' : '<span class="cell-subtitle">Within model boundary</span>'}</td></tr>`).join("")}</tbody></table></div><p class="footnote">Negative decision scores cross the fitted model boundary. They are not calibrated probabilities. Windows are ranked by decision score.</p>`;
    } catch (error) { platformError(error); }
    finally { event.submitter.disabled = false; }
  };
  icons();
}

async function compareView() {
  const result = await getJSON("/api/cases");
  if (state.view !== "compare") return;
  const options = result.cases.map(c => `<option value="${c.id}">${esc(c.title)}</option>`).join("");
  $("content").innerHTML = heading("Compare investigations", "Exact normalized signatures / descriptive count differences") + `<form id="compare-form" class="comparison-form"><div class="field"><label for="compare-left">Baseline investigation</label><select id="compare-left" required>${options}</select></div><i data-lucide="arrow-right"></i><div class="field"><label for="compare-right">Comparison investigation</label><select id="compare-right" required>${options}</select></div><button class="button primary" ${result.cases.length < 2 ? "disabled" : ""}>Compare</button></form><div id="comparison-results">${emptyState("Choose two saved investigations", "New and absent signatures describe these inputs, not a proven improvement or regression.")}</div>`;
  if (result.cases.length > 1) $("compare-right").selectedIndex = 1;
  $("compare-form").onsubmit = async event => {
    event.preventDefault(); event.submitter.disabled = true;
    try {
      const result = await api("/api/compare", {left: $("compare-left").value, right: $("compare-right").value});
      $("comparison-results").innerHTML = `<div class="comparison-summary"><div><small>BASELINE</small><h3>${esc(result.left.title)}</h3><span>${result.left.summary.errors} errors / ${result.left.summary.records} events</span></div><div><small>COMPARISON</small><h3>${esc(result.right.title)}</h3><span>${result.right.summary.errors} errors / ${result.right.summary.records} events</span></div></div><p class="footnote">${esc(result.limitation)}</p><div class="table-scroll"><table><thead><tr><th>Signature / service</th><th>Baseline</th><th>Comparison</th><th>Presence</th></tr></thead><tbody>${result.changes.map(c => `<tr><td>${esc(c.sample)}<small class="cell-subtitle">${esc(c.service)}</small></td><td>${c.left}</td><td>${c.right}</td><td>${statusBadge(c.kind)}</td></tr>`).join("")}</tbody></table></div>`;
    } catch (error) { platformError(error); }
    finally { event.submitter.disabled = false; }
  };
  icons();
}

async function runbookView() {
  const result = await getJSON("/api/runbooks");
  if (state.view !== "runbooks") return;
  $("content").innerHTML = heading("Investigation runbooks", "Evidence-led checklists / manual execution only") + `<div class="runbook-list">${result.runbooks.map(r => `<section class="runbook"><div><span class="badge">${esc(r.id.toUpperCase())}</span><h3>${esc(r.title)}</h3><p>${esc(r.description)}</p></div><ul class="checks">${r.steps.map((step, i) => `<li><input type="checkbox" id="runbook-${r.id}-${i}"><label for="runbook-${r.id}-${i}">${esc(step)}</label></li>`).join("")}</ul></section>`).join("")}</div><p class="footnote">Checklist checkmarks are temporary UI state and reset on navigation. No commands or remediation actions execute automatically.</p>`;
  icons();
}

window.platformRender = function (view) {
  if (!platformViews[view]) return false;
  const token = ++platform.request;
  $("crumb").textContent = platformViews[view];
  document.querySelectorAll("[data-view]").forEach(a => { a.classList.toggle("active", a.dataset.view === view); if (a.dataset.view === view) a.setAttribute("aria-current", "page"); else a.removeAttribute("aria-current"); });
  $("content").innerHTML = '<div class="loading">Loading workspace...</div>';
  window.platformEnhance();
  const action = {safety: () => window.safetyView?.(), reliability: () => window.assuranceView?.("reliability"), changes: () => window.assuranceView?.("changes"), reviews: () => window.assuranceView?.("reviews"), catalog: () => window.telemetryView?.("catalog"), incidents: () => window.telemetryView?.("incidents"), connections: () => window.telemetryView?.("connections"), traces: () => window.observabilityView?.("traces"), dependencies: () => window.observabilityView?.("dependencies"), experiments: () => window.observabilityView?.("experiments"), history: historyView, live: liveView, monitors: () => window.monitoringView?.("monitors"), alerts: () => window.monitoringView?.("alerts"), integrations: () => window.monitoringView?.("integrations"), analytics: analyticsView, search: searchView, anomalies: anomalyView, compare: compareView, runbooks: runbookView}[view];
  Promise.resolve().then(action).catch(error => { if (token === platform.request && state.view === view) { $("content").innerHTML = emptyState("Workspace unavailable", esc(error.message)); platformError(error); } });
  return true;
};

document.addEventListener("click", event => {
  const open = event.target.closest("[data-open]");
  if (open) openCase(open.dataset.open);
  const manage = event.target.closest("[data-manage]");
  if (manage) manageCase(manage.dataset.manage);
});

// Saved exports use real same-origin attachment endpoints rather than Blob-only delivery.
$("export").addEventListener("click", event => {
  if (platform.active && state.data?.saved_case_id === platform.active.id) {
    const anchor = document.createElement("a");
    anchor.href = `/api/cases/${platform.active.id}/export`;
    anchor.download = `investigation-${platform.active.id}.json`;
    document.body.appendChild(anchor); anchor.click(); anchor.remove();
  }
});

window.platformEnhance();
if (platformViews[state.view]) window.platformRender(state.view);

async function refreshLive(force = false) {
  if (state.view !== "live" || (livePaused && !force)) return;
  try {
    const result = await getJSON("/api/live");
    if (state.view !== "live") return;
    liveData = result;
    $("live-results").innerHTML = `<div class="stats"><div class="stat"><div class="stat-label">BUFFERED EVENTS</div><strong>${result.count}</strong><small>Rolling cap: 1,000</small></div><div class="stat"><div class="stat-label">SERVICES</div><strong>${result.services}</strong><small>Observed in buffer</small></div><div class="stat"><div class="stat-label">RECENT ERROR EVENTS</div><strong class="red">${result.recent_errors}</strong><small>Latest ${result.recent_count} log events</small></div><div class="stat"><div class="stat-label">THRESHOLD ALERT</div><strong class="${result.alert ? "amber" : "green"}">${result.alert ? "Active" : "Clear"}</strong><small>${(result.recent_error_fraction * 100).toFixed(1)}% recent error-event fraction</small></div></div><p class="footnote">${esc(result.alert_rule)}. Rule-based alert, not an ML prediction or request failure rate.</p><div class="table-scroll"><table><thead><tr><th>Event</th><th>Time (UTC)</th><th>Level</th><th>Service</th><th>Message</th></tr></thead><tbody>${result.records.slice(-100).reverse().map(r => `<tr><td class="trace-title">${r.id}</td><td class="date-cell">${time(r.timestamp)}</td><td>${severity(r.level)}</td><td>${esc(r.service)}</td><td class="event-message">${esc(r.message)}</td></tr>`).join("")}</tbody></table></div>${result.count ? "" : emptyState("Waiting for local events", "Run the controlled demo API or send logs to the local ingestion endpoint.")}<p class="footnote">Latest 100 events shown. ${esc(result.retention)}.</p>`;
    $("live-snapshot").disabled = !result.count;
    icons();
  } catch (error) { if (state.view === "live") platformError(error); }
}

async function liveView() {
  if (liveTimer) clearInterval(liveTimer);
  livePaused = false;
  $("content").innerHTML = heading("Live log ingestion", "POST /api/ingest / rolling in-memory event feed", `<button id="live-pause" class="button secondary">${icon("pause")} Pause</button><button id="live-clear" class="button secondary">${icon("eraser")} Clear buffer</button><button id="live-snapshot" class="button primary" disabled>${icon("scan-line")} Investigate snapshot</button>`) + '<div id="live-results"></div>';
  $("live-pause").onclick = () => { livePaused = !livePaused; $("live-pause").innerHTML = icon(livePaused ? "play" : "pause") + (livePaused ? " Resume" : " Pause"); icons(); if (!livePaused) refreshLive(); };
  $("live-clear").onclick = async () => { try { await api("/api/live/clear"); liveData = null; await refreshLive(true); } catch (error) { platformError(error); } };
  $("live-snapshot").onclick = async () => {
    if (!liveData?.count) return;
    try { const report = await api("/api/analyze", {content: liveData.records.map(r => JSON.stringify(r)).join("\n"), filename: "Live buffer snapshot"}); report.source = "Snapshot of local ingested log events; not automatically persisted"; load(report); location.hash = "investigation"; }
    catch (error) { platformError(error); }
  };
  await refreshLive();
  liveTimer = setInterval(() => { if (state.view !== "live") { clearInterval(liveTimer); liveTimer = null; } else refreshLive(); }, 2500);
  icons();
}

window.addEventListener("pagehide", () => { if (liveTimer) clearInterval(liveTimer); });
