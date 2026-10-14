"use strict";

window.safetyView = async () => {
  const data = await getJSON("/api/safety");
  if (state.view !== "safety") return;
  const limits = data.rate_limits;
  $("content").innerHTML = heading("Workspace safety", "Bounded request handling / recoverable workspace snapshots", `<button id="safety-refresh" class="icon-button" title="Refresh safety status" aria-label="Refresh safety status">${icon("refresh-cw")}</button>`) + `<div class="stats"><div class="stat"><div class="stat-label">CONNECTION LIMIT</div><strong>${data.connection_limit}</strong></div><div class="stat"><div class="stat-label">CAPACITY REJECTIONS</div><strong>${data.connection_denials}</strong></div><div class="stat"><div class="stat-label">RATE REJECTIONS</div><strong>${Object.values(limits.rejected).reduce((a,b) => a+b,0)}</strong></div><div class="stat"><div class="stat-label">TRACKED BUCKETS</div><strong>${limits.tracked_buckets} / ${limits.bucket_limit}</strong></div></div><section class="assurance-detail"><div class="section-title"><h3>Workspace backup</h3><button id="backup-download" class="button secondary" data-admin-only>${icon("archive")} Export backup</button></div><dl class="operation-facts"><dt>Snapshot</dt><dd>Consistent SQLite backup / manifest / SHA-256</dd><dt>Database limit</dt><dd>${data.backup.database_limit_mib} MiB</dd><dt>Encryption</dt><dd>Not encrypted</dd><dt>Restore mode</dt><dd>${esc(data.backup.restore)}</dd></dl><p class="security-note">Archive includes all retained workspace evidence and operational state. Passwords, external account files and environment configuration are not included. Store it privately; a checksum does not prove authenticity.</p></section><section class="assurance-detail"><h3>Request limits</h3><div class="table-scroll"><table><thead><tr><th>Category</th><th>Burst capacity</th><th>Refill period (s)</th><th>Rejected</th></tr></thead><tbody>${Object.entries(limits.policies).map(([name,policy]) => `<tr><td>${esc(name)}</td><td>${policy.capacity}</td><td>${policy.period_seconds}</td><td>${limits.rejected[name] || 0}</td></tr>`).join("")}</tbody></table></div><p class="footnote">${esc(limits.scope)} Counters and buckets reset on restart. HTTP 429 includes Retry-After; connection saturation returns 503.</p></section><section class="assurance-detail"><h3>Recovery safeguards</h3><ul class="boundary-list"><li>Offline restore creates a new database; existing files are never overwritten.</li><li>Monitor scheduling and telemetry collection are disabled in restored workspaces.</li><li>Pending notifications require manual review before retry. Experiments do not resume.</li><li>Application throttling is not a replacement for a hardened HTTPS reverse proxy or distributed rate limiting.</li></ul></section>`;
  $("safety-refresh").onclick = () => window.safetyView().catch(platformError);
  $("backup-download").onclick = () => {
    const dialog = openDialog("Export private workspace backup", `<form id="backup-form"><p class="security-note">This unencrypted archive contains all retained logs, spans, incidents, monitor settings, notes and notification payloads. Keep it private and use a trusted source when restoring.</p><label class="consent"><input id="backup-consent" type="checkbox" required> Include all retained workspace data in this download</label><div class="dialog-footer"><button class="button primary" type="submit">${icon("download")} Download backup</button></div></form>`);
    $("backup-form").onsubmit = async event => {
      event.preventDefault(); event.submitter.disabled = true;
      try {
        const response = await fetch("/api/backups/export", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({include_workspace: $("backup-consent").checked})});
        if (!response.ok) { const error = await response.json(); throw new Error(error.error || "Backup failed."); }
        const blob = await response.blob(), url = URL.createObjectURL(blob);
        const anchor = document.createElement("a");
        anchor.href = url; anchor.download = "tracewise-workspace-backup.zip"; anchor.click();
        setTimeout(() => URL.revokeObjectURL(url), 30000);
        dialog.close(); notify("Workspace backup prepared. Keep the archive private.");
      } catch (error) { $("platform-form-error").textContent = error.message; event.submitter.disabled = false; }
    };
  };
  icons();
};
if (state.view === "safety") window.platformRender("safety");
