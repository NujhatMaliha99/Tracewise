"use strict";

// Expired sessions return users to a real login screen, including during polling.
const workspaceFetch = window.fetch.bind(window);
window.fetch = async (...args) => {
  const response = await workspaceFetch(...args);
  if (response.status === 401 && location.pathname !== "/login.html") location.replace("/login.html");
  return response;
};

document.addEventListener("DOMContentLoaded", async () => {
  try {
    const response = await fetch("/api/session");
    const session = await response.json();
    window.workspaceSession = session;
    const privacy = document.querySelector(".privacy");
    privacy.textContent = session.enabled ? `${session.identity?.username || "Signed out"} / ${session.identity?.role || ""}` : "Local workspace";
    if (session.identity?.role === "admin") {
      const button = document.createElement("button");
      button.className = "icon-button";
      button.title = "Workspace security and delivery";
      button.setAttribute("aria-label", button.title);
      button.innerHTML = '<i data-lucide="shield-check"></i>';
      privacy.after(button);
      button.onclick = showOperations;
    }
    if (session.enabled) {
      const button = document.createElement("button");
      button.className = "icon-button";
      button.title = "Sign out";
      button.setAttribute("aria-label", "Sign out");
      button.innerHTML = '<i data-lucide="log-out"></i>';
      privacy.after(button);
      button.onclick = async () => {
        const result = await fetch("/api/logout", {method: "POST", headers: {"Content-Type": "application/json"}, body: "{}"});
        if (result.ok) location.replace("/login.html");
      };
    }
    if (window.lucide) window.lucide.createIcons();
    const applyRole = () => {
      if (session.identity?.role === "admin") return;
      for (const control of document.querySelectorAll("#monitor-add,[data-monitor-settings],[data-monitor-check],[data-monitor-toggle],#demo-healthy,#demo-failure")) {
        control.disabled = true;
        control.title = "Administrator role required";
      }
      if (session.identity?.role === "viewer") {
        for (const control of document.querySelectorAll("#save-investigation,#demo,#seed-workspace,#live-clear,#manage-form input,#manage-form select,#manage-form button,#note-form textarea,#note-form button")) {
          control.disabled = true;
          control.title = "Operator role required";
        }
      }
    };
    applyRole();
    new MutationObserver(applyRole).observe(document.body, {childList: true, subtree: true});
  } catch (error) { console.error("Workspace session unavailable", error.message); }
});

async function showOperations() {
  try {
    const data = await getJSON("/api/operations");
    const delivery = data.delivery;
    openDialog("Workspace operations", `<section class="management-section"><h3>Approved remote targets</h3><p>${data.remote_hosts.map(esc).join(", ") || "Remote HTTPS monitoring disabled"}</p><p class="footnote">${esc(data.public_origin || "Loopback server / no public origin configured")}</p></section><section class="management-section"><h3>Notification delivery</h3><p>${delivery.configured ? esc(delivery.channel || "webhook") + " configured" : "No notification channel configured"} / ${delivery.counts.pending || 0} pending / ${delivery.counts.sent || 0} accepted / ${delivery.counts.failed || 0} failed</p><div class="table-scroll"><table><thead><tr><th>Event</th><th>State</th><th>Attempts</th><th>Last error</th><th></th></tr></thead><tbody>${delivery.recent.map(d => `<tr><td>${esc(d.event_key)}</td><td>${esc(d.state)}</td><td>${d.attempts}</td><td>${esc(d.last_error || "-")}</td><td>${d.state === "failed" && delivery.configured ? `<button class="icon-button" data-delivery-retry="${esc(d.event_key)}" aria-label="Retry ${esc(d.event_key)}" title="Retry delivery">${icon("rotate-cw")}</button>` : ""}</td></tr>`).join("")}</tbody></table></div></section><section class="management-section"><h3>Recent workspace changes</h3><div class="table-scroll"><table><thead><tr><th>UTC</th><th>Account</th><th>Role</th><th>Action</th></tr></thead><tbody>${data.audit.map(a => `<tr><td>${esc(a.timestamp)}</td><td>${esc(a.username)}</td><td>${esc(a.role)}</td><td>${esc(a.path)}</td></tr>`).join("")}</tbody></table></div></section>`);
    for (const button of document.querySelectorAll("[data-delivery-retry]")) button.onclick = async () => {
      button.disabled = true;
      try {
        await api("/api/operations", {action: "retry", event_key: button.dataset.deliveryRetry});
        button.closest("tr").querySelector("td:nth-child(2)").textContent = "pending";
        button.remove();
      } catch (error) { button.disabled = false; platformError(error); }
    };
    icons();
  } catch (error) { platformError(error); }
}
