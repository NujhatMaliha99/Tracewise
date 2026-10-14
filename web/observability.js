"use strict";

const observation = {experiment: null, run: null, imported: null, trace: null, source: new URLSearchParams(location.search).get("evidence") === "live" ? "live" : "experiments", environment: "development"};
let experimentPoll = null;
const metric = value => value === null || value === undefined ? "-" : Number(value).toFixed(3);

async function observationData() {
  if (observation.source === "live" && state.view !== "experiments") {
    const catalog = await getJSON("/api/catalog");
    return {list: [], runs: [], services: catalog.services, analysis: await getJSON("/api/telemetry/evidence?environment=" + encodeURIComponent(observation.environment))};
  }
  const list = (await getJSON("/api/experiments")).experiments;
  if (!observation.experiment && list.length) observation.experiment = list[0].id;
  const detail = observation.experiment ? await getJSON("/api/experiments/" + observation.experiment) : null;
  const runs = detail?.result.runs || [];
  if (!runs.some(r => r.id === observation.run)) observation.run = runs.at(-1)?.id || null;
  const run = runs.find(r => r.id === observation.run);
  return {list, detail, runs, run, analysis: observation.imported || run?.analysis};
}

function sourceControls(data) {
  if (state.view !== "experiments") {
    const selector = `<label>Evidence source<select id="obs-source"><option value="experiments" ${observation.source === "experiments" ? "selected" : ""}>Controlled experiments</option><option value="live" ${observation.source === "live" ? "selected" : ""}>Retained live telemetry</option></select></label>`;
    if (observation.source === "live") return `<div class="observation-controls">${selector}<label>Environment<select id="obs-environment">${[...new Set([observation.environment, ...(data.services || []).map(s => s.environment)])].sort().map(e => `<option ${e === observation.environment ? "selected" : ""}>${esc(e)}</option>`).join("")}</select></label><button class="icon-button" id="obs-refresh" aria-label="Refresh evidence" title="Refresh evidence">${icon("refresh-cw")}</button></div><p class="footnote">Latest 1,000 retained records / snapshot grouping differs from durable live incidents.</p>`;
    return selector + experimentSourceControls(data);
  }
  return experimentSourceControls(data);
}
function experimentSourceControls(data) {
  return `<div class="observation-controls"><label>Experiment<select id="obs-experiment" aria-label="Experiment evidence"><option value="">No experiment selected</option>${data.list.map(e => `<option value="${e.id}" ${e.id === observation.experiment ? "selected" : ""}>${esc(e.created_at)} / ${esc(e.status)}</option>`).join("")}</select></label><label>Run<select id="obs-run" aria-label="Experiment run">${data.runs.map(r => `<option value="${r.id}" ${r.id === observation.run ? "selected" : ""}>Seed ${r.seed} / ${esc(r.scenario)}</option>`).join("")}</select></label><button class="icon-button" id="obs-refresh" aria-label="Refresh evidence" title="Refresh evidence">${icon("refresh-cw")}</button></div><p class="footnote">${observation.imported ? "Imported snapshot / transient analysis" : "Controlled local HTTP experiment evidence / not production traffic"}</p>`;
}

function bindSources(view) {
  if ($("obs-source")) $("obs-source").onchange = () => { observation.source = $("obs-source").value; observation.imported = null; window.observabilityView(view).catch(platformError); };
  if ($("obs-environment")) $("obs-environment").onchange = () => { observation.environment = $("obs-environment").value; window.observabilityView(view).catch(platformError); };
  $("obs-refresh").onclick = () => window.observabilityView(view).catch(platformError);
  if (!$("obs-experiment")) return;
  $("obs-experiment").onchange = () => { observation.experiment = $("obs-experiment").value; observation.imported = null; observation.run = null; window.observabilityView(view).catch(platformError); };
  $("obs-run").onchange = () => { observation.run = $("obs-run").value; observation.imported = null; window.observabilityView(view).catch(platformError); };
  $("obs-refresh").onclick = () => window.observabilityView(view).catch(platformError);
}

async function traceView() {
  const data = await observationData();
  if (state.view !== "traces") return;
  const traces = data.analysis?.traces || [];
  if (!traces.some(t => t.id === observation.trace)) observation.trace = traces.find(t => t.errors)?.id || traces[0]?.id;
  $("content").innerHTML = heading("Request traces", "Cross-service spans / explicit parent and request IDs", `<label class="button secondary" for="span-import">${icon("upload")} Import spans<input type="file" id="span-import" accept=".json,.jsonl,.log" hidden></label>`) + sourceControls(data) + (traces.length ? `<div class="trace-layout"><section class="trace-index"><div class="section-title"><h3>Requests</h3><span class="badge">${traces.length}</span></div><label class="search"><input id="trace-search" type="search" aria-label="Filter request IDs" placeholder="Request ID"></label><div id="trace-options"></div></section><section id="span-detail" class="span-detail"></section></div>` : emptyState("No trace evidence yet", "No completed experiment runs or imported span records."));
  bindSources("traces");
  $("span-import").onchange = async event => {
    const file = event.target.files[0];
    if (!file) return;
    if (file.size > 4 * 1024 * 1024) return notify("Span input exceeds 4 MiB.", true);
    try { observation.imported = await api("/api/traces/analyze", {content: await file.text()}); observation.source = "experiments"; observation.trace = null; await traceView(); }
    catch (error) { platformError(error); }
  };
  if (traces.length) {
    const drawIndex = () => {
      const query = $("trace-search").value.toLowerCase();
      $("trace-options").innerHTML = traces.filter(t => t.id.includes(query)).map(t => `<button class="trace-choice ${t.id === observation.trace ? "selected" : ""}" data-observe-trace="${esc(t.id)}"><code>${esc(t.id)}</code><span>${t.spans.length} spans / ${t.errors} error events</span></button>`).join("");
      for (const button of document.querySelectorAll("[data-observe-trace]")) button.onclick = () => { observation.trace = button.dataset.observeTrace; drawIndex(); drawSpans(traces.find(t => t.id === observation.trace)); };
    };
    $("trace-search").oninput = drawIndex;
    drawIndex();
    drawSpans(traces.find(t => t.id === observation.trace));
  }
  icons();
}

function drawSpans(trace) {
  const dated = trace.spans.map(s => Date.parse(s.start_time)).filter(Number.isFinite);
  const base = dated.length ? Math.min(...dated) : null;
  const extent = Math.max(1, ...trace.spans.map(s => Number.isFinite(Date.parse(s.start_time)) ? Date.parse(s.start_time) - base + (s.duration_ms || 0) : 0));
  $("span-detail").innerHTML = `<div class="section-title"><h3>Request waterfall</h3><span class="badge">${trace.services.length} services</span></div><code class="trace-identifier">${esc(trace.id)}</code><p class="footnote">${trace.incomplete_parents} missing parent references / client spans overlap server spans; do not sum durations.</p><div class="span-axis"><span>0 ms</span><span>${metric(extent)} ms</span></div>${trace.spans.map(s => {
    const offset = base !== null && Number.isFinite(Date.parse(s.start_time)) ? Math.max(0, Date.parse(s.start_time) - base) / extent * 100 : 0;
    const width = Math.max(.5, (s.duration_ms || 0) / extent * 100);
    return `<details class="span-row"><summary><span><strong>${esc(s.service)}</strong><small>${esc(s.span_kind || "log")} ${s.dependency ? "to " + esc(s.dependency) : ""} / ${esc(s.evidence_id)}</small></span><span class="span-track"><i class="span-bar ${s.status >= 500 || s.level === "ERROR" ? "error" : ""}" data-offset="${Math.min(99.5, offset)}" data-width="${Math.max(0, Math.min(100 - offset, width))}"></i></span><span>${s.duration_ms === undefined ? "Undated" : metric(s.duration_ms) + " ms"}</span></summary><dl><dt>Span / parent</dt><dd><code>${esc(s.span_id || "Not supplied")} / ${esc(s.parent_span_id || "Root or not supplied")}</code></dd><dt>Observed UTC</dt><dd>${esc(s.timestamp || "Undated")}</dd><dt>HTTP / event</dt><dd>${esc(s.status ?? "-")} / ${esc(s.message)}</dd></dl></details>`;
  }).join("")}`;
  for (const bar of document.querySelectorAll(".span-bar")) {
    bar.style.setProperty("--span-left", Number(bar.dataset.offset) + "%");
    bar.style.setProperty("--span-width", Number(bar.dataset.width) + "%");
  }
}

async function dependencyView() {
  const data = await observationData();
  if (state.view !== "dependencies") return;
  const analysis = data.analysis;
  $("content").innerHTML = heading("Dependency incidents", "Configured topology / evidence-linked correlation candidates") + sourceControls(data) + (analysis ? `<section class="dependency-surface"><canvas id="dependency-canvas" role="img" aria-label="Gateway calls orders; orders calls inventory and payments. Counts in dependency table below."></canvas><div class="table-scroll"><table><thead><tr><th>Caller</th><th>Dependency</th><th>Observed calls</th><th>Failed calls</th></tr></thead><tbody>${analysis.edges.map(e => `<tr><td>${esc(e.source)}</td><td>${esc(e.target)}</td><td>${e.calls}</td><td>${e.errors}</td></tr>`).join("")}</tbody></table></div></section><section class="management-section"><div class="section-title"><h3>Correlated incident candidates</h3><span class="badge">${analysis.episodes.length}</span></div>${analysis.episodes.map(e => `<details class="correlation-row"><summary><strong>${esc(e.id)} / ${esc(e.suspected_dependencies.join(", ") || "Insufficient dependency evidence")}</strong><span>${e.trace_ids.length} requests / ${e.evidence_ids.length} error events</span></summary><p>Affected: ${esc(e.affected_services.join(", "))}</p><p>First observed failure: ${esc(e.first_observed_failure?.service || "Undated")} / ${esc(e.first_observed_failure?.timestamp || "-")}</p><p>Evidence: ${esc(e.evidence_ids.join(", "))}</p>${!observation.imported ? `<button class="button secondary" data-save-episode="${e.id}">${icon("save")} Save correlated case</button>` : ""}<p>Request IDs: ${esc(e.trace_ids.join(", "))}</p></details>`).join("") || '<p>No error episodes in this run.</p>'}<p class="footnote">Anchor-matched requests within ${analysis.window_seconds} seconds of the episode start. Suspected dependencies are not verified causes.</p></section><ul class="boundary-list">${analysis.limitations.map(l => `<li>${esc(l)}</li>`).join("")}</ul>` : emptyState("No dependency evidence", "Run a fault experiment or import spans in Request traces."));
  bindSources("dependencies");
  if (observation.source === "live") for (const button of document.querySelectorAll("[data-save-episode]")) button.remove();
  $("dependency-canvas")?.setAttribute("aria-label", "Configured dependency graph. Observed call and failure counts are in the table below.");
  for (const button of document.querySelectorAll("[data-save-episode]")) button.onclick = () => {
    const dialog = openDialog("Save correlated incident evidence", `<form id="correlation-save"><p class="security-note">Retain this episode's request spans as a synthetic investigation. Correlation is not proof of a root cause.</p><label class="consent"><input type="checkbox" id="correlation-retain" required> Retain this correlated evidence as a saved case</label><div class="dialog-footer"><button class="button primary" type="submit">${icon("save")} Save case</button></div></form>`);
    $("correlation-save").onsubmit = async event => {
      event.preventDefault(); event.submitter.disabled = true;
      try {
        const result = await api(`/api/experiments/${observation.experiment}/cases`, {run_id: observation.run, episode_id: button.dataset.saveEpisode, retain: $("correlation-retain").checked});
        dialog.close(); notify("Correlated case saved: " + result.title);
      } catch (error) { $("platform-form-error").textContent = error.message; event.submitter.disabled = false; }
    };
  };
  if (analysis) drawDependencyGraph(analysis);
  icons();
}

function drawDependencyGraph(analysis) {
  const canvas = $("dependency-canvas");
  if (!canvas) return;
  const groups = new Map();
  for (const node of analysis.nodes) { const depth = node.depth || 0; if (!groups.has(depth)) groups.set(depth, []); groups.get(depth).push(node); }
  const columns = Math.max(1, groups.size), width = Math.max(canvas.clientWidth, columns * 160), height = Math.max(260, ...[...groups.values()].map(nodes => nodes.length * 110)), ratio = window.devicePixelRatio || 1;
  canvas.style.width = width + "px"; canvas.style.height = height + "px";
  canvas.width = width * ratio; canvas.height = height * ratio;
  const ctx = canvas.getContext("2d"); ctx.scale(ratio, ratio);
  const positions = {};
  [...groups.entries()].sort((a,b) => a[0] - b[0]).forEach(([depth,nodes], column) => nodes.forEach((node,row) => { positions[node.id] = [(column + .5) / columns, (row + .5) / nodes.length]; }));
  const radius = width < 500 ? 22 : 32;
  for (const edge of analysis.edges) {
    const a = positions[edge.source], b = positions[edge.target];
    const x1 = a[0] * width, y1 = a[1] * height, x2 = b[0] * width, y2 = b[1] * height;
    const angle = Math.atan2(y2 - y1, x2 - x1), endX = x2 - Math.cos(angle) * radius, endY = y2 - Math.sin(angle) * radius;
    ctx.strokeStyle = themeColor(edge.errors ? "--red" : "--cyan"); ctx.fillStyle = ctx.strokeStyle; ctx.lineWidth = 2;
    ctx.beginPath(); ctx.moveTo(x1, y1); ctx.lineTo(endX, endY); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(endX, endY); ctx.lineTo(endX - 9 * Math.cos(angle - .4), endY - 9 * Math.sin(angle - .4)); ctx.lineTo(endX - 9 * Math.cos(angle + .4), endY - 9 * Math.sin(angle + .4)); ctx.fill();
  }
  for (const node of analysis.nodes) {
    const [x, y] = positions[node.id].map((v, i) => v * (i ? height : width));
    ctx.fillStyle = themeColor("--chart-bg"); ctx.strokeStyle = themeColor(node.errors ? "--red" : "--accent"); ctx.lineWidth = 2;
    ctx.beginPath(); ctx.arc(x, y, radius, 0, Math.PI * 2); ctx.fill(); ctx.stroke();
    ctx.fillStyle = themeColor("--ink"); ctx.font = "12px Segoe UI"; ctx.textAlign = "center";
    ctx.fillText(node.id.length > 19 ? node.id.slice(0,16) + "..." : node.id, x, y + radius + 17); ctx.font = "bold 16px Segoe UI"; ctx.fillText(String(node.spans), x, y + 5);
  }
}

async function experimentView() {
  const data = await observationData();
  if (state.view !== "experiments") return;
  if (experimentPoll) clearTimeout(experimentPoll);
  const active = data.detail && ["queued", "running"].includes(data.detail.status);
  $("content").innerHTML = heading("Fault experiments", "Actual HTTP services / repeated controlled faults", `<button id="experiment-start" class="button primary" ${active ? "disabled" : ""}>${icon("play")} Run experiment</button>${active ? `<button id="experiment-cancel" class="button secondary">${icon("square")} Cancel</button>` : ""}${data.detail ? `<a class="button secondary" href="/api/experiments/${data.detail.id}/export">${icon("download")} Report JSON</a><a class="button secondary" href="/api/experiments/${data.detail.id}/report">${icon("file-text")} Markdown</a>` : ""}`) + sourceControls(data) + `<div class="stats"><div class="stat"><div class="stat-label">STATUS</div><strong>${esc(data.detail?.status || "Not run")}</strong></div><div class="stat"><div class="stat-label">COMPLETED RUNS</div><strong>${data.detail?.result.completed || 0}</strong></div><div class="stat"><div class="stat-label">SEEDS</div><strong>${data.detail?.config.seeds.length || 0}</strong></div><div class="stat"><div class="stat-label">SCENARIOS</div><strong>${data.detail?.config.scenarios.length || 0}</strong></div></div><p class="footnote">${esc(data.detail?.result.protocol || "Baseline, payments timeout, stopped payments service, and exclusive SQLite contention.")}</p>${data.detail?.result.error ? `<p class="import-error" role="alert">Worker failure: ${esc(data.detail.result.error)}</p>` : ""}<div class="table-scroll"><table><thead><tr><th>Seed / scenario</th><th>Detection (s)</th><th>Recovery (s)</th><th>Missed</th><th>Duplicate alerts</th><th>False alerts</th><th>P95 headers (ms)</th></tr></thead><tbody>${data.runs.map(r => `<tr><td><button class="case-link" data-run-trace="${r.id}">${r.seed} / ${esc(r.scenario)}</button></td><td>${metric(r.metrics.detection_seconds)}</td><td>${metric(r.metrics.recovery_seconds)}</td><td>${r.metrics.missed_incidents ?? "N/A"}</td><td>${r.metrics.duplicate_alerts ?? "N/A"}</td><td>${r.metrics.false_alerts ?? "N/A"}</td><td>${metric(r.metrics.p95_latency_ms)}</td></tr>`).join("")}</tbody></table></div><p class="footnote">${esc(data.detail?.result.limits || "No results until actual probes finish. Experiments do not send email or alter user monitors.")}</p>`;
  bindSources("experiments");
  $("experiment-start").onclick = configureExperiment;
  if ($("experiment-cancel")) $("experiment-cancel").onclick = async () => { try { await api("/api/experiments/cancel", {}); await experimentView(); } catch (error) { platformError(error); } };
  for (const button of document.querySelectorAll("[data-run-trace]")) button.onclick = () => { observation.run = button.dataset.runTrace; observation.imported = null; location.hash = "traces"; };
  if (active) experimentPoll = setTimeout(() => { if (state.view === "experiments" && !$('platform-dialog')?.open) experimentView().catch(platformError); }, 2500);
  icons();
}

function configureExperiment() {
  const dialog = openDialog("Run isolated fault experiments", `<form id="experiment-form"><div class="form-grid">${[7,19,42].map((seed,i) => `<div class="field"><label for="experiment-seed-${i}">Seed ${i + 1}</label><input class="text-input" type="number" min="0" max="999999" id="experiment-seed-${i}" value="${seed}" required></div>`).join("")}</div><fieldset><legend>Scenarios</legend>${["baseline","payments-timeout","service-down","database-contention"].map(s => `<label class="consent"><input type="checkbox" name="scenario" value="${s}" checked> ${s}</label>`).join("")}</fieldset><p class="security-note">Four temporary loopback services and a temporary SQLite database. Actual timeout, shutdown and lock faults; no user service disruption or email sending.</p><label class="consent"><input type="checkbox" id="experiment-retain" required> Retain measured results and span evidence in this workspace</label><div class="dialog-footer"><button class="button primary" type="submit">${icon("play")} Start</button></div></form>`);
  $("experiment-form").onsubmit = async event => {
    event.preventDefault(); event.submitter.disabled = true;
    try {
      const config = {seeds: [0,1,2].map(i => Number($("experiment-seed-" + i).value)), scenarios: [...document.querySelectorAll('[name="scenario"]:checked')].map(i => i.value)};
      const result = await api("/api/experiments", {config, retain: $("experiment-retain").checked});
      observation.experiment = result.id; observation.imported = null; observation.run = null; dialog.close(); await experimentView();
    } catch (error) { $("platform-form-error").textContent = error.message; event.submitter.disabled = false; }
  };
}

window.observabilityView = view => ({traces: traceView, dependencies: dependencyView, experiments: experimentView})[view]();
if (["traces", "dependencies", "experiments"].includes(state.view)) window.platformRender(state.view);
window.addEventListener("resize", () => { if (state.view === "dependencies") observationData().then(data => data.analysis && drawDependencyGraph(data.analysis)).catch(platformError); });
window.addEventListener("pagehide", () => clearTimeout(experimentPoll));
