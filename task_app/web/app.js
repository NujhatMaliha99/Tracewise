"use strict";
const $ = id => document.getElementById(id);
const board = {tasks: [], status: "all", priority: "all", query: "", session: null, edit: null};
const esc = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const icon = name => `<i data-lucide="${name}"></i>`;
const icons = () => window.lucide?.createIcons();
function notice(message, failure=false) { $("notice").textContent=message; $("notice").hidden=false; $("notice").classList.toggle("failure",failure); }
async function request(path, body, method) {
  const response = await fetch(path,{method:method || (body ? "POST":"GET"),headers:body ? {"Content-Type":"application/json"}:{},body:body ? JSON.stringify(body):undefined});
  const data = await response.json();
  if (response.status === 401) { $("workspace").hidden=true; $("login").hidden=false; }
  if (!response.ok) throw new Error(data.error || "Request failed.");
  return data;
}
async function load() {
  const result = await request("/api/tasks?archived="+(board.status === "archived" ? "1":"0"));
  board.tasks=result.tasks; render();
}
function render() {
  const readonly = board.session.identity?.role === "viewer";
  $("new-task").disabled=readonly;
  const today = new Date(), date = `${today.getFullYear()}-${String(today.getMonth()+1).padStart(2,"0")}-${String(today.getDate()).padStart(2,"0")}`;
  for (const status of ["todo","doing","done"]) $("count-"+status).textContent=board.tasks.filter(t=>t.status===status).length;
  $("count-overdue").textContent=board.tasks.filter(t=>t.status!=="done" && t.due_date && t.due_date<date).length;
  for (const button of document.querySelectorAll("[data-status]")) button.setAttribute("aria-pressed",String(button.dataset.status===board.status));
  const tasks=board.tasks.filter(t=>(["all","archived"].includes(board.status)||t.status===board.status)&&(board.priority==="all"||t.priority===board.priority)&&(`${t.title} ${t.description}`.toLowerCase().includes(board.query)));
  $("tasks").innerHTML=tasks.map(t=>`<tr class="${t.status==="done"?"completed":""}"><td><input type="checkbox" data-complete="${t.id}" aria-label="Complete ${esc(t.title)}" ${t.status==="done"?"checked":""} ${readonly||t.archived?"disabled":""}></td><td><span class="task-title">${esc(t.title)}</span><div class="task-description">${esc(t.description)}</div></td><td><select class="task-status" data-task-status="${t.id}" aria-label="Status for ${esc(t.title)}" ${readonly||t.archived?"disabled":""}>${[["todo","To do"],["doing","In progress"],["done","Done"]].map(([value,label])=>`<option value="${value}" ${value===t.status?"selected":""}>${label}</option>`).join("")}</select></td><td><span class="priority ${t.priority}">${esc(t.priority)}</span></td><td class="due ${t.due_date&&t.due_date<date&&t.status!=="done"?"overdue":""}">${esc(t.due_date||"- ")}</td><td><div class="row-actions"><button class="icon" data-edit="${t.id}" aria-label="Edit ${esc(t.title)}" title="Edit task" ${readonly?"disabled":""}>${icon("pencil")}</button><button class="icon" data-history="${t.id}" aria-label="History for ${esc(t.title)}" title="Task history">${icon("history")}</button><button class="icon" data-archive="${t.id}" aria-label="${t.archived?"Restore":"Archive"} ${esc(t.title)}" title="${t.archived?"Restore task":"Archive task"}" ${readonly?"disabled":""}>${icon(t.archived?"rotate-ccw":"archive")}</button></div></td></tr>`).join("");
  $("empty").hidden=tasks.length>0; $("visible-count").textContent=`${tasks.length} ${tasks.length===1?"task":"tasks"}${board.status==="archived"?" / archived":""}`;
  for (const control of document.querySelectorAll("[data-complete]")) control.onchange=()=>update(control.dataset.complete,{status:control.checked?"done":"todo"});
  for (const control of document.querySelectorAll("[data-task-status]")) control.onchange=()=>update(control.dataset.taskStatus,{status:control.value});
  for (const button of document.querySelectorAll("[data-edit]")) button.onclick=()=>edit(board.tasks.find(t=>t.id===button.dataset.edit));
  for (const button of document.querySelectorAll("[data-archive]")) button.onclick=()=>update(button.dataset.archive,{archived:!board.tasks.find(t=>t.id===button.dataset.archive).archived});
  for (const button of document.querySelectorAll("[data-history]")) button.onclick=()=>history(button.dataset.history);
  icons();
}
async function update(id,changes) {
  const task=board.tasks.find(t=>t.id===id);
  try { await request("/api/tasks/"+id,{changes,version:task.version},"PATCH"); await load(); }
  catch(error) { notice(error.message,true); try { await load(); } catch(_) {} }
}
function edit(task=null) {
  board.edit=task;
  $("task-heading").textContent=task?"Edit task":"New task";
  $("task-title").value=task?.title||""; $("task-description").value=task?.description||"";
  $("task-status").value=task?.status||"todo"; $("task-priority").value=task?.priority||"medium"; $("task-due").value=task?.due_date||"";
  $("form-error").textContent=""; $("task-dialog").showModal(); $("task-title").focus();
}
async function history(id) {
  try {
    const data=await request(`/api/tasks/${id}/history`);
    $("history-list").innerHTML=data.history.map(h=>`<li><strong>${esc(h.action)}</strong><small>${esc(new Date(h.timestamp).toLocaleString())}</small><span>${esc(h.fields.join(", "))}</span></li>`).join("");
    $("history-dialog").showModal();
  } catch(error) { notice(error.message,true); }
}
async function session() {
  board.session=await request("/api/session");
  const identity=board.session.identity;
  $("identity").textContent=identity ? (board.session.enabled ? identity.username : "Local workspace") : "";
  $("signout").hidden=!board.session.enabled||!identity;
  $("login").hidden=Boolean(identity); $("workspace").hidden=!identity;
  $("telemetry-state").textContent=board.session.telemetry_configured?"Telemetry configured":"Telemetry not configured";
  if(identity) await load(); icons();
}
$("new-task").onclick=()=>edit(); $("close-task").onclick=()=>$("task-dialog").close(); $("close-history").onclick=()=>$("history-dialog").close();
$("refresh").onclick=()=>load().catch(e=>notice(e.message,true));
$("search").oninput=()=>{board.query=$("search").value.toLowerCase();render();};
$("priority-filter").onchange=()=>{board.priority=$("priority-filter").value;render();};
for(const button of document.querySelectorAll("[data-status]")) button.onclick=async()=>{const previous=board.status;board.status=button.dataset.status;try{if((previous==="archived")!==(board.status==="archived"))await load();else render();}catch(e){notice(e.message,true);}};
$("task-form").onsubmit=async event=>{
  event.preventDefault(); event.submitter.disabled=true;
  const changes={title:$("task-title").value,description:$("task-description").value,status:$("task-status").value,priority:$("task-priority").value,due_date:$("task-due").value||null};
  try { if(board.edit)await request("/api/tasks/"+board.edit.id,{changes,version:board.edit.version},"PATCH");else {await request("/api/tasks",changes);board.status="all";board.priority="all";board.query="";$("search").value="";$("priority-filter").value="all";} $("task-dialog").close(); await load(); }
  catch(e){$("form-error").textContent=e.message;}finally{event.submitter.disabled=false;}
};
$("login-form").onsubmit=async event=>{event.preventDefault();event.submitter.disabled=true;try{await request("/api/login",{username:$("username").value,password:$("password").value});$("password").value="";$("notice").hidden=true;await session();}catch(e){notice(e.message,true);}finally{event.submitter.disabled=false;}};
$("signout").onclick=async()=>{try{await request("/api/logout",{});board.tasks=[];await session();}catch(e){notice(e.message,true);}};
session().catch(error=>notice(error.message,true));
