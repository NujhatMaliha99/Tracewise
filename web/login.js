"use strict";
const form = document.getElementById("login-form");
fetch("/api/session").then(r => r.json()).then(session => {
  if (!session.enabled || session.identity) location.replace("/");
}).catch(() => { document.getElementById("login-status").textContent = "Workspace unavailable"; });
form.onsubmit = async event => {
  event.preventDefault();
  const button = form.querySelector("button");
  button.disabled = true;
  document.getElementById("login-error").textContent = "";
  try {
    const response = await fetch("/api/login", {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({username: form.username.value, password: form.password.value})});
    const result = await response.json();
    if (!response.ok) throw new Error(result.error || "Sign in failed.");
    form.password.value = "";
    location.replace("/");
  } catch (error) { document.getElementById("login-error").textContent = error.message; }
  finally { button.disabled = false; }
};
