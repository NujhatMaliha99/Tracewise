"use strict";

const operations = {environment: "development", selected: null, timer: null};
const operationQuery = () => "?environment=" + encodeURIComponent(operations.environment);
function environmentControl(services) {
  const environments = [...new Set([operations.environment, ...services.map(s => s.environment)])].sort();
  return `<label>Environment<select id="operation-environment">${environments.map(e => `<option ${e === operations.environment ? "selected" : ""}>${esc(e)}</option>`).join("")}</select></label>`;
}
function bindEnvironment(view) {
  $("operation-environment").onchange = () => { operations.environment = $("operation-environment").value; operations.selected = null; window.telemetryView(view).catch(platformError); };
}
function operationForm(id, dialog, action) {
  $(id).onsubmit = async event => {
    event.preventDefault(); event.submitter.disabled = true;
    try { await action(); dialog.close(); await window.telemetryView(state.view); }
    catch (error) { $("platform-form-error").textContent = error.message; event.submitter.disabled = false; }
  };
}
async function catalogView() {
  const {services} = await getJSON("/api/catalog");
  if (state.view !== "catalog") return;
  const rows = services.filter(s => s.environment === operations.environment);
  $("content").innerHTML = heading("Service catalog", "Owned services / environment-scoped dependencies", `<button id="service-create" class="button primary" data-admin-only>${icon("plus")} Register service</button>`) + `<div class="observation-controls">${environmentControl(services)}</div><div class="table-scroll"><table><thead><tr><th>Service</th><th>Owner</th><th>Dependencies</th><th>Monitors</th><th>Status</th><th></th></tr></thead><tbody>${rows.map(s => `<tr><td><strong>${esc(s.name)}</strong><small class="catalog-description">${esc(s.description)}</small></td><td>${esc(s.owner || "Unassigned")}</td><td>${esc(s.dependencies.map(id => services.find(t => t.id === id)?.name || id).join(", ") || "- / root")}</td><td>${s.monitors.length}</td><td>${statusBadge(s.active ? "active" : "archived")}</td><td><button class="icon-button" title="Edit service" aria-label="Edit ${esc(s.name)}" data-edit-service="${s.id}" data-admin-only>${icon("pencil")}</button></td></tr>`).join("")}</tbody></table></div>` + (!rows.length ? emptyState("No registered services", "No service identities in this environment.") : "");
  bindEnvironment("catalog");
  $("service-create").onclick = () => serviceDialog(null, services);
  for (const button of document.querySelectorAll("[data-edit-service]")) button.onclick = () => serviceDialog(services.find(s => s.id === button.dataset.editService), services);
  icons();
}
async function serviceDialog(service, services) {
  const env = service?.environment || operations.environment;
  const monitors = (await getJSON("/api/monitors")).monitors;
  const dialog = openDialog(service ? "Edit service" : "Register service", `<form id="service-form"><div class="form-grid"><label>Service name<input id="service-name" class="text-input" maxlength="120" required value="${esc(service?.name || "")}" ${service ? "disabled" : ""}></label><label>Environment<input id="service-env" class="text-input" pattern="[a-z0-9][a-z0-9_-]{0,31}" maxlength="32" required value="${esc(env)}" ${service ? "disabled" : ""}></label><label>Owner<input id="service-owner" class="text-input" maxlength="100" value="${esc(service?.owner || "")}"></label><label>Description<input id="service-description" class="text-input" maxlength="600" value="${esc(service?.description || "")}"></label></div><fieldset><legend>Dependencies / ${esc(env)}</legend>${services.filter(s => s.environment === env && s.id !== service?.id).map(s => `<label class="consent"><input type="checkbox" name="service-dependency" value="${s.id}" ${service?.dependencies.includes(s.id) ? "checked" : ""}>${esc(s.name)}</label>`).join("")}</fieldset>${service ? `<label class="consent"><input type="checkbox" id="service-active" ${service.active ? "checked" : ""}> Active</label>` : ""}<div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} Save service</button></div></form>${service ? `<form id="binding-form"><label>Health monitor<select id="service-monitor" required><option value="">Select monitor</option>${monitors.map(m => `<option value="${esc(m.id)}">${esc(m.config?.name || m.name || m.id)}</option>`).join("")}</select></label><div class="dialog-footer"><button class="button secondary" type="submit">${icon("link")} Bind monitor</button></div></form>` : ""}`);
  operationForm("service-form", dialog, () => {
    const changes = {owner: $("service-owner").value, description: $("service-description").value, dependencies: [...document.querySelectorAll('[name="service-dependency"]:checked')].map(c => c.value)};
    if (service) return api("/api/catalog/" + service.id, {version: service.version, changes: {...changes, active: $("service-active").checked}});
    return api("/api/catalog", {...changes, name: $("service-name").value, environment: $("service-env").value});
  });
  if (service) operationForm("binding-form", dialog, () => api("/api/catalog/bind-monitor", {monitor_id: $("service-monitor").value, service_id: service.id}));
}
async function connectionsView() {
  const data = await getJSON("/api/telemetry/status");
  if (state.view !== "connections") return;
  $("content").innerHTML = heading("Telemetry connections", "OTLP/HTTP traces", `<button id="retention-edit" class="button secondary" data-admin-only>${icon("settings-2")} Retention</button>`) + `<div class="stats"><div class="stat"><div class="stat-label">COLLECTION</div><strong>${data.enabled ? "Enabled" : "Disabled"}</strong></div><div class="stat"><div class="stat-label">RETAINED SPANS</div><strong>${data.spans} / ${data.span_limit}</strong></div><div class="stat"><div class="stat-label">INCIDENTS</div><strong>${data.incidents} / ${data.incident_limit}</strong></div><div class="stat"><div class="stat-label">EXPORTER KEY</div><strong>${data.key_configured ? "Configured" : "Not configured"}</strong></div></div><section class="management-section"><h3>Receiver</h3><dl class="operation-facts"><dt>Endpoint</dt><dd><code>${esc(location.origin)}/v1/traces</code></dd><dt>Protocol</dt><dd>HTTP JSON / protobuf / gzip</dd><dt>Scope</dt><dd>Registered active services / exact environment</dd><dt>Correlation window</dt><dd>${data.window_seconds} seconds / candidate grouping</dd><dt>Monitor observer</dt><dd>${esc(data.observer_error || "No reported error")}</dd></dl></section>`;
  $("retention-edit").onclick = () => {
    const dialog = openDialog("Durable telemetry retention", `<form id="retention-form"><label class="consent"><input id="retention-enabled" type="checkbox" ${data.enabled ? "checked" : ""}> Enable collection</label><label class="consent"><input id="retention-consent" type="checkbox"> Consent to storing redacted telemetry in workspace SQLite</label><p class="security-note">Disabling collection does not delete existing evidence. Capacity rejects new evidence; this is not an unlimited production collector.</p><div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} Apply</button></div></form>`);
    operationForm("retention-form", dialog, () => api("/api/telemetry/settings", {enabled: $("retention-enabled").checked, retain: $("retention-consent").checked}));
  };
  icons();
}
async function incidentsView() {
  clearTimeout(operations.timer);
  const {services} = await getJSON("/api/catalog");
  const {incidents} = await getJSON("/api/live-incidents" + operationQuery());
  if (state.view !== "incidents") return;
  if (!incidents.some(i => i.id === operations.selected)) operations.selected = incidents.find(i => i.status !== "merged")?.id;
  const item = operations.selected ? await getJSON("/api/live-incidents/" + operations.selected) : null;
  if (state.view !== "incidents") return;
  $("content").innerHTML = heading("Live incidents", "Incremental evidence / operator decisions", `<button id="incident-refresh" class="icon-button" title="Refresh incidents" aria-label="Refresh incidents">${icon("refresh-cw")}</button>`) + `<div class="observation-controls">${environmentControl(services)}</div><div class="trace-layout"><section class="trace-index">${incidents.map(i => `<button class="trace-choice ${i.id === operations.selected ? "selected" : ""}" data-live-incident="${i.id}"><strong>${esc(i.anchor.join(", ") || "Unattributed")}</strong><span>${esc(i.status)} / ${i.trace_count} requests / ${i.evidence_count} records</span><code>${i.id}</code></button>`).join("") || '<p>No live incidents</p>'}</section><section class="span-detail">${item ? `<div class="section-title"><h3>${esc(item.anchor.join(", ") || "Unattributed incident")}</h3>${statusBadge(item.status)}</div><p>${esc(item.owner || "Unassigned")} / ${esc(dateLabel(item.last_seen))}</p><div class="actions"><button id="incident-edit" class="button secondary" data-operator-only ${item.status === "merged" ? "disabled" : ""}>${icon("pencil")} Update</button><button id="incident-merge" class="button secondary" data-operator-only ${item.status === "merged" ? "disabled" : ""}>${icon("merge")} Merge</button><button id="incident-split" class="button secondary" data-operator-only ${item.trace_ids.length < 2 || item.status === "merged" ? "disabled" : ""}>${icon("split")} Split</button><a class="icon-button" href="/api/live-incidents/${item.id}/export" title="Export incident" aria-label="Export incident">${icon("download")}</a></div><p class="footnote">Candidate dependency, not a verified root cause. Recovery checks do not automatically resolve incidents.</p><h3>Timeline</h3><ol class="incident-timeline">${item.timeline.map(e => `<li><strong>${esc(e.kind.replaceAll("_", " "))}</strong><small>${esc(dateLabel(e.timestamp))} / ${esc(e.actor)}</small><pre>${esc(JSON.stringify(e.detail, null, 2))}</pre></li>`).join("")}</ol><details><summary>Evidence / ${item.records.length} records</summary><pre>${esc(JSON.stringify(item.records, null, 2))}</pre></details>` : emptyState("No incident evidence", "No failed telemetry requests in this environment.")}</section></div>`;
  bindEnvironment("incidents");
  $("incident-refresh").onclick = () => incidentsView().catch(platformError);
  for (const button of document.querySelectorAll("[data-live-incident]")) button.onclick = () => { operations.selected = button.dataset.liveIncident; incidentsView().catch(platformError); };
  if (item) {
    const review = document.createElement("button");
    review.className = "button secondary";
    review.innerHTML = icon("clipboard-check") + " Review";
    review.onclick = () => window.openIncidentReview(item.id, item.environment);
    document.querySelector("#content .span-detail .actions").append(review);
    $("incident-edit").onclick = () => {
      const dialog = openDialog("Update incident", `<form id="incident-edit-form"><label>Status<select id="incident-status">${["open", "investigating", "monitoring", "resolved"].map(s => `<option ${s === item.status ? "selected" : ""}>${s}</option>`).join("")}</select></label><label>Owner<input class="text-input" id="incident-owner" maxlength="100" value="${esc(item.owner)}"></label><label>Note<textarea id="incident-note" maxlength="2000"></textarea></label><div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} Save update</button></div></form>`);
      operationForm("incident-edit-form", dialog, () => api("/api/live-incidents/" + item.id, {version: item.version, changes: {status: $("incident-status").value, owner: $("incident-owner").value, note: $("incident-note").value}}));
    };
    $("incident-merge").onclick = () => {
      const choices = incidents.filter(i => i.id !== item.id && i.status !== "merged");
      const dialog = openDialog("Merge into this incident", `<form id="incident-merge-form"><label>Source incident<select id="incident-source" required><option value="">Choose incident</option>${choices.map(i => `<option value="${i.id}">${esc(i.id + " / " + i.anchor.join(", "))}</option>`).join("")}</select></label><div class="dialog-footer"><button class="button primary" type="submit">${icon("merge")} Merge complete requests</button></div></form>`);
      operationForm("incident-merge-form", dialog, () => api(`/api/live-incidents/${item.id}/merge`, {source_id: $("incident-source").value, target_version: item.version, source_version: choices.find(i => i.id === $("incident-source").value)?.version}));
    };
    $("incident-split").onclick = () => {
      const dialog = openDialog("Split requests into a new incident", `<form id="incident-split-form"><fieldset><legend>Requests</legend>${item.trace_ids.map(t => `<label class="consent"><input type="checkbox" name="split-trace" value="${esc(t)}"><code>${esc(t)}</code></label>`).join("")}</fieldset><div class="dialog-footer"><button class="button primary" type="submit">${icon("split")} Split selected requests</button></div></form>`);
      operationForm("incident-split-form", dialog, async () => { const result = await api(`/api/live-incidents/${item.id}/split`, {version: item.version, trace_ids: [...document.querySelectorAll('[name="split-trace"]:checked')].map(c => c.value)}); operations.selected = result.id; });
    };
  }
  icons();
  const poll = () => {
    if (state.view !== "incidents") return;
    if ($("platform-dialog")?.open || $("content").contains(document.activeElement)) operations.timer = setTimeout(poll, 5000);
    else incidentsView().catch(platformError);
  };
  operations.timer = setTimeout(poll, 5000);
}
window.telemetryView = view => ({catalog: catalogView, connections: connectionsView, incidents: incidentsView})[view]();
window.openLiveIncident = (id, environment) => { operations.selected = id; operations.environment = environment; location.hash = "incidents"; };
if (["catalog", "connections", "incidents"].includes(state.view)) window.platformRender(state.view);
window.addEventListener("pagehide", () => clearTimeout(operations.timer));
