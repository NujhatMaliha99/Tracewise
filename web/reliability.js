"use strict";

const assurance = {environment: "development", objective: null, change: null, incident: null, revision: 0, timer: null};
const assuranceQuery = () => "?environment=" + encodeURIComponent(assurance.environment);
const percentLabel = value => value == null ? "Not enough data" : Number(value).toFixed(3) + "%";
const decimalLabel = value => value == null ? "-" : Number(value) > 0 && Number(value) < .01 ? Number(value).toPrecision(3) : Number(value).toFixed(2);
const assuranceState = value => `<span class="assurance-state ${esc(value)}">${esc(value.replaceAll("_", " "))}</span>`;
function assuranceEnvironment(services) {
  const values = [...new Set([assurance.environment, ...services.map(s => s.environment)])].sort();
  return `<label>Environment<select id="assurance-env">${values.map(v => `<option ${v === assurance.environment ? "selected" : ""}>${esc(v)}</option>`).join("")}</select></label>`;
}
function assuranceBindEnvironment(view) {
  $("assurance-env").onchange = () => { assurance.environment = $("assurance-env").value; assurance.objective = assurance.change = assurance.incident = null; window.assuranceView(view).catch(platformError); };
}
function assuranceSubmit(id, dialog, action) {
  $(id).onsubmit = async event => {
    const view = state.view;
    event.preventDefault(); event.submitter.disabled = true;
    try { await action(); dialog.close(); if (state.view === view) await window.assuranceView(view); }
    catch (error) { if (dialog.open) $("platform-form-error").textContent = error.message; else platformError(error); event.submitter.disabled = false; }
  };
}
function scheduleAssurance(view) {
  clearTimeout(assurance.timer);
  const tick = () => {
    if (state.view !== view) return;
    if ($("platform-dialog")?.open || $("content").contains(document.activeElement)) assurance.timer = setTimeout(tick, 15000);
    else window.assuranceView(view).catch(platformError);
  };
  assurance.timer = setTimeout(tick, 15000);
}
function sampleFacts(sample) {
  return `<dl class="sample-facts"><div><dt>Classified requests</dt><dd>${sample.total}</dd></div><div><dt>Failures</dt><dd>${sample.bad}</dd></div><div><dt>Unknown outcome</dt><dd>${sample.unknown}</dd></div><div><dt>P95 (observed)</dt><dd>${decimalLabel(sample.p95_ms)} ms</dd></div></dl>`;
}
async function reliabilityView() {
  const revision = ++assurance.revision;
  const {services} = await getJSON("/api/catalog");
  const data = await getJSON("/api/reliability" + assuranceQuery());
  if (state.view !== "reliability" || revision !== assurance.revision) return;
  const items = data.objectives;
  if (!items.some(i => i.id === assurance.objective)) assurance.objective = items[0]?.id;
  const current = items.find(i => i.id === assurance.objective);
  $("content").innerHTML = heading("Reliability objectives", "Sampled availability / latency / error budgets", `<button id="objective-add" class="button primary" data-admin-only>${icon("plus")} Add objective</button><button id="assurance-refresh" class="icon-button" aria-label="Refresh reliability" title="Refresh reliability">${icon("refresh-cw")}</button>`) + `<div class="observation-controls">${assuranceEnvironment(services)}</div><div class="stats"><div class="stat"><div class="stat-label">OBJECTIVES</div><strong>${items.length}</strong></div><div class="stat"><div class="stat-label">TARGET BREACHES</div><strong>${items.filter(i => i.state === "breached").length}</strong></div><div class="stat"><div class="stat-label">BURN WATCHES</div><strong>${items.filter(i => i.burn_watch).length}</strong></div><div class="stat"><div class="stat-label">COLLECTION</div><strong>${data.retention_enabled ? "Enabled" : "Disabled"}</strong></div></div><p class="security-note">${esc(data.basis)}</p><div class="table-scroll"><table><thead><tr><th>Objective / service</th><th>State</th><th>Target</th><th>Observed good</th><th>Budget remaining</th><th>Requests</th><th></th></tr></thead><tbody>${items.map(i => `<tr class="${i.id === assurance.objective ? "assurance-selected" : ""}"><td><button class="case-link" data-objective="${i.id}">${esc(i.name)}</button><small class="catalog-description">${esc(i.service)} / ${esc(i.kind)} / ${i.window_hours}h</small></td><td>${assuranceState(i.state)}</td><td>${percentLabel(i.target)}</td><td>${percentLabel(i.sample.good_percent)}</td><td>${percentLabel(i.budget_remaining_percent)}</td><td>${i.sample.total}</td><td><button class="icon-button" data-edit-objective="${i.id}" data-admin-only title="Edit objective" aria-label="Edit ${esc(i.name)}">${icon("settings-2")}</button></td></tr>`).join("")}</tbody></table></div>${current ? `<section class="assurance-detail"><div class="section-title"><h3>${esc(current.name)}</h3>${current.burn_watch ? assuranceState("burn_watch") : assuranceState(current.state)}</div><p class="footnote">${esc(current.service)} / ${esc(current.owner || "Unassigned")} / ${current.window_hours}h rolling sample${current.kind === "latency" ? " / successful response within " + current.latency_ms + " ms" : " / HTTP 5xx or span ERROR is bad"}</p>${sampleFacts(current.sample)}<div class="budget-line"><div><span>Observed error allowance</span><strong>${decimalLabel(current.allowed_bad_requests)} requests</strong></div><div><span>Budget consumed</span><strong>${percentLabel(current.budget_consumed_percent)}</strong></div><div><span>1h / 6h burn</span><strong>${decimalLabel(current.burn["1h"].rate)}x / ${decimalLabel(current.burn["6h"].rate)}x</strong></div></div><figure class="request-histogram"><div class="histogram-columns">${current.buckets.map(b => `<div class="histogram-column" role="img" aria-label="${esc(new Date(b.end_unix * 1000).toISOString())}: ${b.total} requests, ${b.bad} failed" title="${esc(new Date(b.end_unix * 1000).toISOString())} / ${b.total} requests / ${b.bad} bad"><div class="histogram-stack" data-total="${b.total}" data-bad="${b.bad}"><i class="histogram-bad"></i><i class="histogram-good"></i></div></div>`).join("")}</div><figcaption><span>${esc(dateLabel(new Date((data.evaluated_at-current.window_hours*3600)*1000).toISOString()))}</span><span>Good / bad server requests</span><span>${esc(dateLabel(new Date(data.evaluated_at*1000).toISOString()))}</span></figcaption></figure><p class="footnote">Excluded undated: ${current.sample.undated} / future-dated: ${current.sample.future}. Burn watch requires at least ${current.min_requests} classified requests in both windows and 2x burn in each. No notifications are sent.</p><details id="objective-history"><summary>Objective configuration history</summary><div id="objective-history-body"></div></details><details><summary>Bad-request evidence IDs / ${current.sample.bad_evidence_ids.length}${current.sample.bad_evidence_truncated ? " of " + current.sample.bad : ""}</summary><pre>${esc(current.sample.bad_evidence_ids.join("\n") || "None")}</pre></details></section>` : emptyState("No reliability objectives", "No configured SLOs in this environment.")}`;
  assuranceBindEnvironment("reliability");
  $("assurance-refresh").onclick = () => reliabilityView().catch(platformError);
  $("objective-add").onclick = () => objectiveDialog(null, services);
  for (const button of document.querySelectorAll("[data-objective]")) button.onclick = () => { assurance.objective = button.dataset.objective; reliabilityView().catch(platformError); };
  for (const button of document.querySelectorAll("[data-edit-objective]")) button.onclick = () => objectiveDialog(items.find(i => i.id === button.dataset.editObjective), services);
  if (current) {
    const maximum = Math.max(1, ...current.buckets.map(b => b.total));
    for (const bar of document.querySelectorAll(".histogram-stack")) {
      bar.style.setProperty("--height", Number(bar.dataset.total) / maximum * 100 + "%");
      bar.style.setProperty("--bad", Number(bar.dataset.total) ? Number(bar.dataset.bad) / Number(bar.dataset.total) * 100 + "%" : "0%");
    }
    $("objective-history").ontoggle = async event => {
      if (!event.target.open) return;
      try { const history = await getJSON(`/api/objectives/${current.id}/history`); $("objective-history-body").innerHTML = history.history.map(h => `<p>${esc(dateLabel(h.timestamp))} / ${esc(h.actor)} / v${h.config.version} / ${percentLabel(h.config.target)}</p>`).join(""); }
      catch(error) { platformError(error); }
    };
  }
  icons(); scheduleAssurance("reliability");
}
function objectiveDialog(item, services) {
  const available = services.filter(s => s.environment === assurance.environment);
  const dialog = openDialog(item ? "Edit reliability objective" : "Create reliability objective", `<form id="objective-form"><div class="form-grid"><label>Name<input id="objective-name" class="text-input" maxlength="100" value="${esc(item?.name || "")}" required></label><label>Service<select id="objective-service" required ${item ? "disabled" : ""}><option value="">Select service</option>${available.map(s => `<option value="${s.id}" ${s.id === item?.service_id ? "selected" : ""}>${esc(s.name)}</option>`).join("")}</select></label><label>Indicator<select id="objective-kind"><option value="availability">Availability</option><option value="latency" ${item?.kind === "latency" ? "selected" : ""}>Availability + latency</option></select></label><label>Target (%)<input id="objective-target" class="text-input" type="number" min="90" max="99.999" step="0.001" value="${item?.target ?? 99.9}" required></label><label>Window (hours)<input id="objective-window" class="text-input" type="number" min="1" max="720" value="${item?.window_hours ?? 24}" required></label><label>Latency limit (ms)<input id="objective-latency" class="text-input" type="number" min="1" max="60000" value="${item?.latency_ms ?? 1000}" required></label><label>Minimum requests<input id="objective-min" class="text-input" type="number" min="1" max="100000" value="${item?.min_requests ?? 20}" required></label></div><label class="consent"><input type="checkbox" id="objective-active" ${item?.active !== false ? "checked" : ""}> Active</label><div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} Save objective</button></div></form>`);
  const indicator = () => { $("objective-latency").disabled = $("objective-kind").value !== "latency"; };
  $("objective-kind").onchange = indicator; indicator();
  assuranceSubmit("objective-form", dialog, async () => {
    const config = {name: $("objective-name").value, kind: $("objective-kind").value, target: Number($("objective-target").value), window_hours: Number($("objective-window").value), latency_ms: Number($("objective-latency").value), min_requests: Number($("objective-min").value), active: $("objective-active").checked};
    const result = await api(item ? "/api/objectives/" + item.id : "/api/objectives", item ? {config, version: item.version} : {...config, service_id: $("objective-service").value});
    assurance.objective = result.id;
  });
}
async function changesView() {
  const revision = ++assurance.revision;
  const {services} = await getJSON("/api/catalog");
  const {changes} = await getJSON("/api/changes" + assuranceQuery());
  if (!changes.some(c => c.id === assurance.change)) assurance.change = changes[0]?.id;
  const current = assurance.change ? await getJSON("/api/changes/" + assurance.change) : null;
  if (state.view !== "changes" || revision !== assurance.revision) return;
  $("content").innerHTML = heading("Change intelligence", "Deployment / configuration / rollback context", `<button id="change-add" class="button primary" data-operator-only>${icon("git-commit-horizontal")} Record change</button><button id="change-refresh" class="icon-button" title="Refresh changes" aria-label="Refresh changes">${icon("refresh-cw")}</button>`) + `<div class="observation-controls">${assuranceEnvironment(services)}</div><div class="trace-layout"><section class="trace-index">${changes.map(c => `<button class="trace-choice ${c.id === assurance.change ? "selected" : ""}" data-change="${c.id}"><strong>${esc(c.title)}</strong><span>${esc(c.service)} / ${esc(c.kind)}</span><small>${esc(dateLabel(c.occurred_at))}</small></button>`).join("") || '<p>No recorded changes</p>'}</section><section class="span-detail">${current ? `<div class="section-title"><h3>${esc(current.title)}</h3>${assuranceState(current.state)}</div><p>${esc(current.service)} / ${esc(current.revision || "No revision")} / ${esc(current.kind)}</p><p class="footnote">Occurred ${esc(dateLabel(current.occurred_at))} / recorded by ${esc(current.actor)}</p><p>${esc(current.note)}</p><div class="change-comparison"><section><h3>30 minutes before</h3>${sampleFacts(current.before)}<strong>${percentLabel(current.before.good_percent)} good</strong></section><section><h3>30 minutes after</h3>${sampleFacts(current.after)}<strong>${percentLabel(current.after.good_percent)} good</strong><small>${current.after_window_complete ? "Window complete" : "Window still open"}</small></section></div><div class="change-delta"><span>Error-rate delta</span><strong>${current.error_delta_percentage_points == null ? "Insufficient evidence" : decimalLabel(current.error_delta_percentage_points) + " percentage points"}</strong></div><p class="security-note">${esc(current.interpretation)}</p><p class="footnote">Comparison requires 20 classified requests on each side, no unknown outcomes, and a completed after window. Samples are not controlled experiments.</p><details><summary>Failed request evidence</summary><pre>${esc([...current.before.bad_evidence_ids, ...current.after.bad_evidence_ids].join("\n") || "None")}</pre></details>` : emptyState("No change context", "No retained deployment or configuration markers.")}</section></div>`;
  assuranceBindEnvironment("changes");
  $("change-refresh").onclick = () => changesView().catch(platformError);
  $("change-add").onclick = () => changeDialog(services);
  for (const button of document.querySelectorAll("[data-change]")) button.onclick = () => { assurance.change = button.dataset.change; changesView().catch(platformError); };
  icons(); scheduleAssurance("changes");
}
function changeDialog(services) {
  const dialog = openDialog("Record a service change", `<form id="change-form"><div class="form-grid"><label>Service<select id="change-service" required><option value="">Select service</option>${services.filter(s => s.environment === assurance.environment).map(s => `<option value="${s.id}">${esc(s.name)}</option>`).join("")}</select></label><label>Kind<select id="change-kind"><option value="deployment">Deployment</option><option value="configuration">Configuration</option><option value="rollback">Rollback</option></select></label><label>Title<input id="change-title" class="text-input" maxlength="150" required></label><label>Revision / reference<input id="change-revision" class="text-input" maxlength="100"></label><label>Occurred at (UTC)<input id="change-time" class="text-input" type="datetime-local" value="${new Date().toISOString().slice(0,16)}" required></label></div><label>Note<textarea id="change-note" maxlength="1000"></textarea></label><label class="consent"><input id="change-retain" type="checkbox" required> Retain this change marker and note</label><div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} Record change</button></div></form>`);
  assuranceSubmit("change-form", dialog, async () => {
    const change = {service_id: $("change-service").value, kind: $("change-kind").value, title: $("change-title").value, revision: $("change-revision").value, occurred_at: $("change-time").value + "Z", note: $("change-note").value};
    const result = await api("/api/changes", {change, retain: $("change-retain").checked}); assurance.change = result.id;
  });
}
async function reviewsView() {
  const revision = ++assurance.revision;
  const {services} = await getJSON("/api/catalog");
  const {incidents} = await getJSON("/api/live-incidents" + assuranceQuery());
  if (!incidents.some(i => i.id === assurance.incident)) assurance.incident = incidents.find(i => i.status !== "merged")?.id || incidents[0]?.id;
  const report = assurance.incident ? await getJSON("/api/reviews/" + assurance.incident) : null;
  if (state.view !== "reviews" || revision !== assurance.revision) return;
  const item = report?.incident;
  $("content").innerHTML = heading("Incident reviews", "Evidence summary / operator notes / accountable follow-ups", `<button id="review-refresh" class="icon-button" aria-label="Refresh review" title="Refresh review">${icon("refresh-cw")}</button>${item ? `<a class="button secondary" href="/api/reviews/${item.id}/report">${icon("file-down")} Markdown</a><a class="icon-button" href="/api/reviews/${item.id}/export" title="Download review JSON" aria-label="Download review JSON">${icon("download")}</a>` : ""}`) + `<div class="observation-controls">${assuranceEnvironment(services)}<label>Incident<select id="review-incident"><option value="">Select incident</option>${incidents.map(i => `<option value="${i.id}" ${i.id === assurance.incident ? "selected" : ""}>${esc(i.id)} / ${esc(i.anchor.join(", ") || "Unattributed")} / ${esc(i.status)}</option>`).join("")}</select></label></div>${report ? `<div class="review-banner"><span class="badge">DRAFT</span><strong>${esc(item.id)} / ${esc(item.environment)}</strong>${statusBadge(item.status)}</div><div class="stats"><div class="stat"><div class="stat-label">REQUESTS</div><strong>${report.summary.requests}</strong></div><div class="stat"><div class="stat-label">ERROR RECORDS</div><strong>${report.summary.error_records}</strong></div><div class="stat"><div class="stat-label">SERVICES</div><strong>${report.summary.services}</strong></div><div class="stat"><div class="stat-label">OPEN ACTIONS</div><strong>${report.summary.outstanding_actions}</strong></div></div><section class="assurance-detail"><h3>Observed evidence interval</h3><p>${esc(dateLabel(item.first_seen))} to ${esc(dateLabel(item.last_seen))}</p><p>${esc(item.owner || "Unassigned")} / candidate dependencies: ${esc(item.anchor.join(", ") || "Insufficient evidence")}</p><p class="footnote">This interval is not confirmed downtime, user impact or MTTR.</p></section><section class="assurance-detail"><div class="section-title"><h3>Follow-up actions</h3><button id="followup-add" class="button secondary" data-operator-only ${item.status === "merged" ? "disabled" : ""}>${icon("plus")} Add action</button></div><div class="table-scroll"><table><thead><tr><th>Action</th><th>Owner</th><th>Deadline</th><th>Status</th><th></th></tr></thead><tbody>${report.actions.map(a => `<tr><td><strong>${esc(a.title)}</strong>${a.incident_id !== item.id ? `<small class="catalog-description">From ${esc(a.incident_id)}</small>` : ""}</td><td>${esc(a.owner || "Unassigned")}</td><td>${esc(a.due_at ? dateLabel(a.due_at) : "Not set")}${a.overdue ? assuranceState("overdue") : ""}</td><td>${assuranceState(a.status)}</td><td><button class="icon-button" data-followup="${a.id}" data-operator-only title="Edit action" aria-label="Edit action ${esc(a.title)}">${icon("pencil")}</button></td></tr>`).join("")}</tbody></table></div>${!report.actions.length ? '<p class="footnote">No follow-up actions recorded.</p>' : ""}</section><section class="assurance-detail"><h3>Operator notes</h3>${report.operator_notes.map(n => `<article class="review-note"><small>${esc(dateLabel(n.timestamp))} / ${esc(n.actor)} / ${esc(n.incident_id)}</small><p>${esc(n.note)}</p></article>`).join("") || '<p class="footnote">No notes recorded.</p>'}</section><section class="assurance-detail"><h3>Nearby changes / temporal context</h3>${report.nearby_changes.map(c => `<p>${esc(dateLabel(c.occurred_at))} / ${esc(c.service)} / ${esc(c.kind)} / ${esc(c.title)}</p>`).join("") || '<p class="footnote">No change markers in the context window.</p>'}</section><details class="review-evidence"><summary>Evidence index / ${item.records.length} records</summary><div class="table-scroll"><table><thead><tr><th>Evidence</th><th>Service</th><th>HTTP / level</th></tr></thead><tbody>${item.records.slice(0,200).map(r => `<tr><td><code>${esc(r.evidence_id)}</code></td><td>${esc(r.service)}</td><td>${esc(r.status ?? "-")} / ${esc(r.level)}</td></tr>`).join("")}</tbody></table></div>${item.records.length > 200 ? '<p class="footnote">First 200 records shown. Exports include all retained incident evidence.</p>' : ""}</details><ul class="boundary-list">${report.limitations.map(s => `<li>${esc(s)}</li>`).join("")}</ul>` : emptyState("No incident review", "No retained live incidents in this environment.")}`;
  assuranceBindEnvironment("reviews");
  $("review-refresh").onclick = () => reviewsView().catch(platformError);
  $("review-incident").onchange = () => { assurance.incident = $("review-incident").value; reviewsView().catch(platformError); };
  if (report) {
    const manage = document.createElement("button");
    manage.className = "button secondary";
    manage.innerHTML = icon("arrow-up-right") + " Open incident";
    manage.onclick = () => window.openLiveIncident(item.id, item.environment);
    document.querySelector("#content .platform-heading .actions").prepend(manage);
    $("followup-add").onclick = () => followupDialog(null, item);
    for (const button of document.querySelectorAll("[data-followup]")) button.onclick = () => followupDialog(report.actions.find(a => a.id === button.dataset.followup), item);
  }
  icons(); scheduleAssurance("reviews");
}
function followupDialog(action, incident) {
  const dialog = openDialog(action ? "Edit follow-up action" : "Add follow-up action", `<form id="followup-form"><label>Action<input class="text-input" id="followup-title" maxlength="300" value="${esc(action?.title || "")}" required></label><div class="form-grid"><label>Owner<input class="text-input" id="followup-owner" maxlength="100" value="${esc(action?.owner || "")}"></label><label>Deadline (UTC)<input class="text-input" id="followup-due" type="datetime-local" value="${action?.due_at ? new Date(action.due_at).toISOString().slice(0,16) : ""}"></label>${action ? `<label>Status<select id="followup-status">${["open","in_progress","done","cancelled"].map(s => `<option value="${s}" ${s === action.status ? "selected" : ""}>${esc(s.replaceAll("_", " "))}</option>`).join("")}</select></label>` : ""}</div>${!action ? '<label class="consent"><input type="checkbox" id="followup-retain" required> Retain this follow-up action</label>' : ""}<div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} Save action</button></div></form>`);
  assuranceSubmit("followup-form", dialog, () => {
    const data = {title: $("followup-title").value, owner: $("followup-owner").value, due_at: $("followup-due").value ? $("followup-due").value + "Z" : null};
    return action ? api("/api/followups/" + action.id, {changes: {...data, status: $("followup-status").value}, version: action.version}) : api(`/api/reviews/${incident.id}/actions`, {action: data, retain: $("followup-retain").checked});
  });
}
window.assuranceView = view => ({reliability: reliabilityView, changes: changesView, reviews: reviewsView})[view]();
window.openIncidentReview = (id, environment) => { assurance.incident = id; assurance.environment = environment; location.hash = "reviews"; };
if (["reliability", "changes", "reviews"].includes(state.view)) window.platformRender(state.view);
window.addEventListener("pagehide", () => clearTimeout(assurance.timer));
