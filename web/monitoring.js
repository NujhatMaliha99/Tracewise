"use strict";

let monitorTimer = null;
let selectedMonitor = null;
let monitorGeneration = 0;
const monitorPhase = phase => `<span class="incident-status ${phase === "healthy" ? "resolved" : phase === "failing" ? "open" : "monitoring"}">${esc(phase)}</span>`;
const numberField = (key, label, value, min, max) => `<div class="field"><label for="mon-${key}">${label}</label><input class="text-input" id="mon-${key}" type="number" min="${min}" max="${max}" value="${value}" required></div>`;

function configureMonitor(item = null) {
  const config = item?.config || {name: "", url: "http://127.0.0.1:8000/health", interval: 30, expected_status: 200, latency_limit: 1000, failure_threshold: 3, recovery_threshold: 2, cooldown: 300, enabled: false, auto_incident: false};
  const dialog = openDialog(item ? "Monitor settings" : "Create API monitor", `<form id="monitor-form"><div class="field"><label for="mon-name">Name</label><input id="mon-name" class="text-input" maxlength="100" required value="${esc(config.name)}"></div><div class="field"><label for="mon-url">Health endpoint URL</label><input id="mon-url" class="text-input" type="url" required ${item ? "readonly" : ""} value="${esc(config.url)}"></div>${item ? "" : `<button type="button" class="text-button" id="use-monitor-demo">Use controlled demo endpoint</button>`}<div class="form-grid">${numberField("interval", "Interval (seconds)", config.interval, 5, 3600)}${numberField("expected_status", "Expected HTTP status", config.expected_status, 100, 599)}${numberField("latency_limit", "Latency limit (ms)", config.latency_limit, 1, 60000)}${numberField("failure_threshold", "Consecutive failures", config.failure_threshold, 1, 20)}${numberField("recovery_threshold", "Consecutive recoveries", config.recovery_threshold, 1, 20)}${numberField("cooldown", "New-alert cooldown (seconds)", config.cooldown, 0, 86400)}</div><label class="consent"><input id="mon-enabled" type="checkbox" ${config.enabled ? "checked" : ""}> Enable scheduled checks</label><label class="consent"><input id="mon-auto_incident" type="checkbox" ${config.auto_incident ? "checked" : ""}> Automatically retain incident evidence on alert</label>${item ? "" : '<label class="consent"><input id="mon-retain" type="checkbox" required> Retain monitor settings, checks and enabled automatic incidents locally</label>'}<p class="security-note">Loopback HTTP or administrator-allowlisted public HTTPS. No response bodies or credentials retained. Up to 200 checks per monitor; recovery requires operator review before final resolution.</p><div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} ${item ? "Save settings" : "Create monitor"}</button></div></form>`);
  if (!item) $("use-monitor-demo").onclick = () => { $("mon-url").value = location.origin + "/monitor-demo"; $("mon-name").value = "Controlled demo health"; };
  $("monitor-form").onsubmit = async event => {
    event.preventDefault();
    event.submitter.disabled = true;
    try {
      const payload = {name: $("mon-name").value, url: $("mon-url").value};
      for (const key of ["interval", "expected_status", "latency_limit", "failure_threshold", "recovery_threshold", "cooldown"]) payload[key] = Number($("mon-" + key).value);
      for (const key of ["enabled", "auto_incident"]) payload[key] = $("mon-" + key).checked;
      if (item) await api("/api/monitors/" + item.id, {changes: payload});
      else await api("/api/monitors", {config: payload, retain: $("mon-retain").checked});
      dialog.close();
      await refreshMonitors();
      notify(item ? "Monitor settings saved." : "Monitor created. Scheduled checks run only when enabled.");
    } catch (error) { $("platform-form-error").textContent = error.message; }
    finally { event.submitter.disabled = false; }
  };
}

async function refreshMonitors() {
  const result = await getJSON("/api/monitors");
  if (state.view !== "monitors" || !$("monitor-list")) return;
  $("monitor-worker-error").textContent = result.worker_error ? "Monitoring worker fault: " + result.worker_error : "";
  const items = result.monitors;
  $("monitor-stats").innerHTML = [["MONITORS", items.length], ["SCHEDULED", items.filter(m => m.config.enabled).length], ["ACTIVE ALERTS", items.filter(m => m.state.alert_id).length]].map(([label, value]) => `<div class="stat"><div class="stat-label">${label}</div><strong>${value}</strong></div>`).join("");
  $("monitor-list").innerHTML = items.length ? `<div class="table-scroll"><table><thead><tr><th>Endpoint</th><th>State</th><th>Last check</th><th>Latency</th><th>Schedule</th><th></th></tr></thead><tbody>${items.map(m => `<tr><td><button class="case-link" data-monitor-detail="${m.id}">${esc(m.config.name)}</button><small class="cell-subtitle">${esc(m.config.url)}</small></td><td>${monitorPhase(m.state.phase)}</td><td>${m.state.last ? esc(m.state.last.status ?? "Connection error") : "Not checked"}<small class="cell-subtitle">${dateLabel(m.state.last?.timestamp)}</small></td><td>${m.state.last ? m.state.last.latency + " ms" : "-"}</td><td><label class="consent"><input type="checkbox" data-monitor-toggle="${m.id}" aria-label="Schedule ${esc(m.config.name)}" ${m.config.enabled ? "checked" : ""}> ${m.config.interval}s</label></td><td class="monitor-actions"><button class="icon-button" data-monitor-check="${m.id}" aria-label="Check ${esc(m.config.name)} now" title="Check now">${icon("play")}</button><button class="icon-button" data-monitor-settings="${m.id}" aria-label="Settings ${esc(m.config.name)}" title="Monitor settings">${icon("settings-2")}</button></td></tr>`).join("")}</tbody></table></div>` : emptyState("No API monitors", "No endpoints configured.");
  for (const button of document.querySelectorAll("[data-monitor-check]")) button.onclick = async () => {
    button.disabled = true;
    try { await api(`/api/monitors/${button.dataset.monitorCheck}/check`); await refreshMonitors(); }
    catch (error) { platformError(error); button.disabled = false; }
  };
  for (const button of document.querySelectorAll("[data-monitor-settings]")) button.onclick = () => configureMonitor(items.find(m => m.id === button.dataset.monitorSettings));
  for (const button of document.querySelectorAll("[data-monitor-detail]")) button.onclick = async () => { selectedMonitor = button.dataset.monitorDetail; await showMonitor(selectedMonitor); };
  for (const input of document.querySelectorAll("[data-monitor-toggle]")) input.onchange = async () => {
    input.disabled = true;
    try { await api(`/api/monitors/${input.dataset.monitorToggle}`, {changes: {enabled: input.checked}}); await refreshMonitors(); }
    catch (error) { platformError(error); input.checked = !input.checked; input.disabled = false; }
  };
  if (selectedMonitor) await showMonitor(selectedMonitor);
  icons();
}

async function showMonitor(id) {
  const item = await getJSON("/api/monitors/" + id);
  if (state.view !== "monitors" || !$("monitor-detail")) return;
  $("monitor-detail").innerHTML = `<section class="management-section"><div class="section-title"><h2>${esc(item.config.name)} / check history</h2>${monitorPhase(item.state.phase)}</div><div class="detail-meta"><span>${item.sample_count} retained checks</span><span>Check success: ${item.check_success_percent === null ? "No data" : item.check_success_percent + "%"}</span><span>${item.state.failures} consecutive failures</span></div><p class="footnote">Sample success is not time-weighted uptime. Latency measures response headers, not body download. No check means unknown availability.</p><canvas id="monitor-chart" class="monitor-chart" aria-label="Response-header latency over retained checks" role="img"></canvas><div class="table-scroll"><table><thead><tr><th>Time (UTC)</th><th>HTTP</th><th>Latency</th><th>Result</th></tr></thead><tbody>${item.checks.slice(0, 20).map(c => `<tr><td>${dateLabel(c.timestamp)}</td><td>${c.status ?? "-"}</td><td>${c.latency} ms</td><td>${esc(c.reason)}</td></tr>`).join("")}</tbody></table></div></section>`;
  const canvas = $("monitor-chart");
  const ctx = canvas.getContext("2d"), width = canvas.clientWidth || 600, height = 140;
  const scale = window.devicePixelRatio || 1;
  canvas.width = width * scale; canvas.height = height * scale; ctx.scale(scale, scale);
  ctx.fillStyle = themeColor("--chart-bg"); ctx.fillRect(0, 0, width, height);
  const checks = [...item.checks].reverse();
  const max = Math.max(1, ...checks.map(c => c.latency));
  ctx.strokeStyle = themeColor("--line");
  for (let i = 1; i < 4; i++) { ctx.beginPath(); ctx.moveTo(0, i * height / 4); ctx.lineTo(width, i * height / 4); ctx.stroke(); }
  ctx.strokeStyle = themeColor("--accent"); ctx.lineWidth = 2; ctx.beginPath();
  checks.forEach((c, i) => { const x = 10 + i * (width - 20) / Math.max(1, checks.length - 1), y = height - 10 - c.latency / max * (height - 20); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.stroke();
  checks.forEach((c, i) => { ctx.fillStyle = themeColor(c.success ? "--accent" : "--red"); ctx.beginPath(); ctx.arc(10 + i * (width - 20) / Math.max(1, checks.length - 1), height - 10 - c.latency / max * (height - 20), 3, 0, Math.PI * 2); ctx.fill(); });
  icons();
}

async function monitorView() {
  const generation = ++monitorGeneration;
  if (monitorTimer) clearInterval(monitorTimer);
  selectedMonitor = null;
  $("content").innerHTML = heading("API monitors", "HTTP health / response-header latency", `<button id="monitor-refresh" class="icon-button" aria-label="Refresh monitors" title="Refresh monitors">${icon("refresh-cw")}</button><button id="monitor-add" class="button primary">${icon("plus")} Create monitor</button>`) + `<div id="monitor-worker-error" class="import-error" role="alert"></div><div id="monitor-stats" class="stats monitor-stats"></div><div id="monitor-list"></div><section class="demo-controls management-section"><div><h3>Controlled health endpoint</h3><p>Synthetic failure injection / no external service</p></div><div class="actions"><button id="demo-healthy" class="button secondary">${icon("check")} Healthy</button><button id="demo-failure" class="button secondary">${icon("triangle-alert")} Inject failure</button></div><span id="monitor-demo-mode" class="badge"></span></section><div id="monitor-detail"></div>`;
  $("monitor-add").onclick = () => configureMonitor();
  $("monitor-refresh").onclick = () => refreshMonitors().catch(platformError);
  for (const mode of ["healthy", "failure"]) $("demo-" + mode).onclick = async () => {
    try { const result = await api("/api/monitor-demo", {mode}); $("monitor-demo-mode").textContent = result.mode; }
    catch (error) { platformError(error); }
  };
  await refreshMonitors();
  const demo = await getJSON("/api/monitor-demo");
  if (generation !== monitorGeneration || state.view !== "monitors") return;
  if (state.view === "monitors") $("monitor-demo-mode").textContent = demo.mode;
  monitorTimer = setInterval(() => {
    if (state.view !== "monitors") { clearInterval(monitorTimer); monitorTimer = null; }
    else if (!$('platform-dialog')?.open && !document.querySelector('[data-monitor-toggle]:focus, [data-monitor-check]:focus')) refreshMonitors().catch(platformError);
  }, 5000);
}

async function alertView() {
  const result = await getJSON("/api/alerts");
  if (state.view !== "alerts") return;
  $("content").innerHTML = heading("Alert inbox", "Consecutive-check thresholds / one active alert per monitor", `<button class="icon-button" id="alert-refresh" aria-label="Refresh alerts" title="Refresh alerts">${icon("refresh-cw")}</button>`) + (result.alerts.length ? `<div class="table-scroll"><table><thead><tr><th>Monitor / signal</th><th>Status</th><th>Opened</th><th>Recovered</th><th>Incident</th><th></th></tr></thead><tbody>${result.alerts.map(a => `<tr><td><strong>${esc(a.monitor_name)}</strong><small class="cell-subtitle">${esc(a.message)}</small></td><td>${statusBadge(a.status)}</td><td>${dateLabel(a.opened_at)}</td><td>${dateLabel(a.recovered_at)}</td><td>${a.case_id ? `<button class="case-link" data-open="${a.case_id}">Open evidence</button>` : "Not enabled"}</td><td>${a.status === "open" ? `<button class="icon-button" data-ack="${a.id}" aria-label="Acknowledge ${esc(a.monitor_name)}" title="Acknowledge">${icon("check-check")}</button>` : ""}</td></tr>`).join("")}</tbody></table></div>` : emptyState("No monitor alerts", "No failure threshold has been reached.")) + '<p class="footnote">Acknowledgement does not resolve an outage. Recovery moves eligible automatic incidents to monitoring, never to resolved.</p>';
  $("alert-refresh").onclick = () => alertView().catch(platformError);
  for (const button of document.querySelectorAll("[data-ack]")) button.onclick = async () => {
    try { await api(`/api/alerts/${button.dataset.ack}/acknowledge`); await alertView(); }
    catch (error) { platformError(error); }
  };
  icons();
}

function integrationsView() {
  const python = `from sdk.tracewise import TracewiseClient, TracewiseHandler\nimport logging\n\nclient = TracewiseClient("checkout-api")\nlogging.getLogger("checkout").addHandler(TracewiseHandler(client))\n\nclient.event("Request completed", request_id="req-1", status=200)\nwith client.capture(request_id="req-2"):\n    raise RuntimeError("Payment dependency timed out")`;
  const node = `import { TracewiseClient } from "./sdk/tracewise.mjs";\n\nconst client = new TracewiseClient("payments-api");\nawait client.event("Request completed", "INFO", {status: 200});\nawait client.capture(async () => {\n  throw new Error("Payment dependency timed out");\n}, {request_id: "req-2"});`;
  $("content").innerHTML = heading("SDK integrations", "Python and Node.js / dependency-free local telemetry") + `<div class="sdk-tabs" role="tablist" aria-label="SDK language"><button class="button secondary" role="tab" id="sdk-python" aria-selected="true">Python</button><button class="button secondary" role="tab" id="sdk-node" aria-selected="false">Node.js</button></div><section class="sdk-source"><div class="section-title"><h2 id="sdk-title">Python</h2><button id="sdk-copy" class="icon-button" aria-label="Copy SDK example" title="Copy example">${icon("copy")}</button></div><pre><code id="sdk-code">${esc(python)}</code></pre></section><div class="management-section"><h3>Delivery contract</h3><p>Loopback origin only. Bounded events and delivery timeout. No durable retry queue. Original application exceptions are rethrown. Review sensitive messages before emission; server-side redaction is best effort.</p><p>Python delivery is synchronous. Node delivery is asynchronous. Failed delivery returns false and increments the client's dropped counter. No event is automatically saved as an incident.</p></div>`;
  for (const language of ["python", "node"]) $("sdk-" + language).onclick = () => {
    $("sdk-code").textContent = language === "python" ? python : node;
    $("sdk-title").textContent = language === "python" ? "Python" : "Node.js";
    for (const option of ["python", "node"]) $("sdk-" + option).setAttribute("aria-selected", String(language === option));
  };
  $("sdk-copy").onclick = async () => { try { await navigator.clipboard.writeText($("sdk-code").textContent); notify("SDK example copied."); } catch { notify("Clipboard unavailable.", true); } };
  icons();
}

window.monitoringView = view => ({monitors: monitorView, alerts: alertView, integrations: integrationsView})[view]();
if (["monitors", "alerts", "integrations"].includes(state.view)) window.platformRender(state.view);
window.addEventListener("pagehide", () => { if (monitorTimer) clearInterval(monitorTimer); });
