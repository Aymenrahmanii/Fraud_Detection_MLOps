"use strict";

const state = { token: "", selected: null };
try { state.token = sessionStorage.getItem("aiops-token") || ""; } catch (_) { /* storage blocked */ }

const $ = (sel, root = document) => root.querySelector(sel);
const el = (tag, cls, text) => {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
};
const fmt = (iso) => (iso ? new Date(iso).toLocaleString() : "");

async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}`, ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* not json */ }
    throw new Error(detail);
  }
  return res.json();
}

async function signIn(token) {
  state.token = token;
  try {
    const me = await api("/api/me");
    try { sessionStorage.setItem("aiops-token", token); } catch (_) { /* ignore */ }
    $("#who").textContent = `${me.subject} · ${me.roles.join(", ") || "read-only"}`;
    await loadScenarios();
    await refresh();
  } catch (err) {
    $("#who").textContent = `Sign-in failed: ${err.message}`;
  }
}

async function loadScenarios() {
  const box = $("#scenario-buttons");
  box.replaceChildren();
  let names = [];
  try { names = await api("/api/scenarios"); } catch (_) { return; }
  for (const name of names) {
    const button = el("button", "ghost", `Inject: ${name.replaceAll("_", " ")}`);
    button.addEventListener("click", async () => {
      button.disabled = true;
      try { await api(`/api/scenarios/${name}`, { method: "POST" }); } catch (err) { alert(err.message); }
      button.disabled = false;
      refresh();
    });
    box.append(button);
  }
}

function renderApprovals(rows) {
  const box = $("#approvals");
  $("#approval-count").textContent = rows.length;
  $("#approval-count").classList.toggle("hot", rows.length > 0);
  box.replaceChildren();
  if (!rows.length) { box.append(el("p", "empty", "No pending approvals.")); return; }
  const tpl = $("#approval-tpl");
  for (const a of rows) {
    const card = tpl.content.firstElementChild.cloneNode(true);
    const r = a.request;
    $(".action", card).textContent = r.action;
    $(".target", card).textContent = `${r.parameters.service || r.plan?.target?.name || ""} · ${r.environment}`;
    $(".expires", card).textContent = `expires ${fmt(a.expires_at)}`;
    $(".reason", card).textContent = r.reason;
    const facts = $(".facts", card);
    const add = (k, v) => { facts.append(el("dt", "", k), el("dd", "", v)); };
    add("Incident", a.incident_id);
    add("Risk", `${r.risk.level ?? "?"} (score ${r.risk.score ?? "?"})`);
    add("Decision score", r.decision_confidence);
    if (r.hitl_triggers?.length) add("Why a human", r.hitl_triggers.join("; "));
    if (r.evidence_summary?.length) add("Evidence", r.evidence_summary.join(" · "));
    if (r.plan?.plan_hash) add("Plan hash", r.plan.plan_hash.slice(0, 16) + "…");
    const patch = $(".patch", card);
    if (r.plan?.patch?.length) {
      $("pre", patch).textContent = JSON.stringify(r.plan.patch.filter((op) => op.op !== "test"), null, 2);
    } else {
      $("summary", patch).textContent = "Governance decision (no infrastructure change)";
      $("pre", patch).textContent = JSON.stringify(r.parameters, null, 2);
    }
    const comment = $(".comment", card);
    const error = $(".error", card);
    const decide = async (decision, button) => {
      error.textContent = "";
      if (comment.value.trim().length < 3) { error.textContent = "A comment is required for the audit trail."; return; }
      for (const b of card.querySelectorAll("button")) b.disabled = true;
      try {
        await api(`/api/approvals/${encodeURIComponent(a.approval_id)}/resolve`, {
          method: "POST", body: JSON.stringify({ decision, comment: comment.value.trim() }),
        });
        state.selected = a.incident_id;
        await refresh();
      } catch (err) {
        error.textContent = err.message;
        for (const b of card.querySelectorAll("button")) b.disabled = false;
      }
      void button;
    };
    $(".approve", card).addEventListener("click", (e) => decide("APPROVE", e.target));
    $(".reject", card).addEventListener("click", (e) => decide("REJECT", e.target));
    box.append(card);
  }
}

function renderIncidents(rows) {
  const body = $("#incidents");
  body.replaceChildren();
  if (!rows.length) {
    const tr = el("tr"); const td = el("td", "empty", "No workflows yet."); td.colSpan = 6; tr.append(td); body.append(tr); return;
  }
  for (const r of rows) {
    const tr = el("tr", "row" + (r.incident_id === state.selected ? " active" : ""));
    tr.append(
      el("td", "", r.incident_id),
      el("td", "", r.kind),
      el("td", "", r.service ? `${r.service.service_id} ${r.service.version}` : ""),
      el("td", "", r.priority),
    );
    const status = el("td"); status.append(el("span", `pill ${r.status}`, r.status)); tr.append(status);
    tr.append(el("td", "", fmt(r.opened_at)));
    tr.addEventListener("click", () => { state.selected = r.incident_id; refresh(); });
    body.append(tr);
  }
}

function kv(pairs) {
  const dl = el("dl", "kv");
  for (const [k, v] of pairs) {
    if (v === undefined || v === null || v === "") continue;
    dl.append(el("dt", "", k), el("dd", "", typeof v === "object" ? JSON.stringify(v) : v));
  }
  return dl;
}

async function renderDetail(id) {
  const box = $("#detail");
  if (!id) return;
  let data;
  try { data = await api(`/api/incidents/${encodeURIComponent(id)}`); } catch (err) { box.replaceChildren(el("p", "error", err.message)); return; }
  const c = data.context; const w = data.workflow;
  const head = el("div", "panel-head");
  const title = el("h2", "", id);
  const stage = el("span", `pill ${w.stage || c.status}`, w.stage || c.status);
  head.append(title, stage);
  const children = [head, kv([
    ["Kind", c.kind], ["Service", c.service ? `${c.service.service_id} ${c.service.version} (${c.service.namespace})` : ""],
    ["Environment", c.environment], ["Priority / severity", `${c.priority} / ${c.severity}`],
    ["Root cause", w.consensus?.primary_root_cause], ["Selected action", w.selected ? `${w.selected.type} (score ${w.selected.score})` : ""],
    ["Policy", w.policy ? `${w.policy.decision} · risk ${w.policy.risk?.level} · score ${w.policy.decision_score}` : ""],
    ["Outcome", w.outcome], ["Replans", w.replans],
  ])];
  if (w.consensus?.candidates?.length) {
    children.push(el("h3", "", "Candidate actions"));
    const t = el("table", "table");
    t.innerHTML = "<thead><tr><th>Action</th><th>From</th><th>Score</th><th>Why</th></tr></thead>";
    const tb = el("tbody");
    for (const cand of w.consensus.candidates) {
      const tr = el("tr");
      tr.append(el("td", "", cand.type), el("td", "", cand.source_agent), el("td", "", cand.score), el("td", "", cand.rationale));
      tb.append(tr);
    }
    t.append(tb); children.push(t);
  }
  if (c.agent_results?.length) {
    children.push(el("h3", "", "Specialist results"));
    const ul = el("ul", "timeline");
    for (const r of c.agent_results) {
      const li = el("li");
      li.append(el("span", "actor", r.agent_id), el("span", `pill ${r.status}`, r.status), document.createTextNode(" " + r.rationale_summary));
      ul.append(li);
    }
    children.push(ul);
  }
  children.push(el("h3", "", "Audit trail"));
  children.push(el("p", data.audit_chain_valid ? "chain-ok" : "chain-bad", data.audit_chain_valid ? "Hash chain verified: no record was altered." : "Hash chain BROKEN: the audit trail was modified."));
  const ul = el("ul", "timeline");
  for (const a of data.audit) {
    const li = el("li");
    li.append(el("span", "when", new Date(a.at).toLocaleTimeString()), el("span", "actor", a.actor), document.createTextNode(a.action));
    const extra = Object.entries(a.detail || {}).filter(([k]) => ["status", "type", "decision", "approver_id", "agent_id", "stage", "action_id", "execution_status", "policy_result"].includes(k));
    if (extra.length) li.append(el("span", "muted", " · " + extra.map(([k, v]) => `${k}=${v}`).join(", ")));
    ul.append(li);
  }
  children.push(ul);
  box.replaceChildren(...children);
}

async function refresh() {
  if (!state.token) return;
  try {
    const [approvals, incidents] = await Promise.all([api("/api/approvals"), api("/api/incidents")]);
    renderApprovals(approvals);
    renderIncidents(incidents);
    if (!state.selected && incidents.length) state.selected = incidents[0].incident_id;
    await renderDetail(state.selected);
  } catch (err) {
    $("#who").textContent = err.message;
  }
}

$("#login").addEventListener("submit", (e) => { e.preventDefault(); signIn($("#token").value.trim()); });
if (state.token) signIn(state.token);
setInterval(refresh, 3000);
