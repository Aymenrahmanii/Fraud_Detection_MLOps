"use strict";

/* AIOps Control Room. Data comes only from the console API (/api/*).
   Every element is built with h(): API data is never parsed as HTML. */

const POLL_MS = 3000;

const STAGES = [
  { no: "01", name: "Detect" },
  { no: "02", name: "Diagnose" },
  { no: "03", name: "Decide" },
  { no: "04", name: "Approve" },
  { no: "05", name: "Execute" },
  { no: "06", name: "Verify" },
  { no: "07", name: "Close" },
];
const STAGE_OF = {
  OPEN: 0,
  DIAGNOSING: 1, ASSESSING_RELEASE: 1, ASSESSING_MODEL: 1, RETRAINING: 1, REPLANNING: 1,
  DECIDING: 2, PLANNING: 2,
  WAITING_FOR_APPROVAL: 3, AUTHORIZED: 3,
  EXECUTING: 4,
  VERIFYING: 5,
};
const TERMINAL = {
  RESOLVED: "ok", PROMOTION_APPROVED: "ok", KEEP_CHAMPION: "ink", CLOSED: "ink",
  ESCALATED: "fail", RELEASE_BLOCKED: "fail",
};
const STATUS_LABEL = {
  OPEN: "Detected", DIAGNOSING: "Diagnosing", ASSESSING_RELEASE: "Assessing release",
  ASSESSING_MODEL: "Assessing model", RETRAINING: "Retraining candidate", REPLANNING: "Re-planning",
  DECIDING: "Deciding", PLANNING: "Planning the change", WAITING_FOR_APPROVAL: "Waiting for approval",
  AUTHORIZED: "Authorized", EXECUTING: "Executing", VERIFYING: "Verifying recovery",
  RESOLVED: "Resolved", ESCALATED: "Escalated to a human", RELEASE_BLOCKED: "Blocked by release gate",
  PROMOTION_APPROVED: "Promotion approved", KEEP_CHAMPION: "Champion kept", CLOSED: "Closed",
};
const KIND_LABEL = { INCIDENT: "Incident", RELEASE: "Release", MLOPS: "Model check" };
const AGENTS = {
  "monitoring-agent": ["Agent 1", "Monitoring"],
  "diagnosis-agent": ["Agent 2", "Diagnosis"],
  "security-quality-agent": ["Agent 3", "Security & quality"],
  "deployment-recovery-agent": ["Agent 4", "Deployment"],
  "resource-optimization-agent": ["Agent 5", "Resources"],
  "mlops-lifecycle-agent": ["Agent 6", "MLOps"],
  orchestrator: ["Orchestrator", "Workflow"],
  "hitl-service": ["HITL service", "Approvals"],
};
const ACTION_VERB = {
  ROLLBACK: "Roll back", DEPLOY: "Deploy", SCALE_REPLICAS: "Scale", INCREASE_MEMORY: "Raise memory limit of",
  PROMOTE_MODEL: "Promote model", ROLLING_RESTART: "Restart",
};
const APPROVER_ROLES = {
  ROLLBACK: ["sre", "admin"], SCALE_REPLICAS: ["sre", "admin"], INCREASE_MEMORY: ["sre", "admin"],
  DEPLOY: ["release-manager", "admin"], PROMOTE_MODEL: ["ml-lead", "admin"],
};
const SCENARIO_LABEL = {
  memory_leak_after_deploy: "Memory leak after deploy",
  cpu_load_spike: "CPU load spike",
  model_drift_check: "Model drift check",
  reset: "Reset cluster",
};
const RISK_LABEL = {
  environment_criticality: "Environment criticality", blast_radius: "Blast radius",
  irreversibility: "Irreversibility", data_impact: "Data impact", uncertainty: "Uncertainty",
};
const RISK_SHORT = { environment_criticality: "Environment" };

const state = {
  token: "", me: null, incidents: [], approvals: [], selected: null,
  view: "all", kind: "", query: "", stage: null,
  detail: null, sheetKey: "", lastOk: 0, error: null,
  comments: {}, messages: {}, knownIds: null, follow: false, busy: false,
  expanded: new Set(),
};
try { state.token = sessionStorage.getItem("aiops-token") || ""; } catch (_) { /* storage blocked */ }

/* ---- tiny DOM helpers ---------------------------------------------------- */
const $ = (sel) => document.querySelector(sel);
function h(tag, attrs, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === null || value === undefined || value === false) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = String(value);
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}
function icon(name) {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("class", "icon");
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `#i-${name}`);
  svg.append(use);
  return svg;
}

/* ---- formatting ---------------------------------------------------------- */
const pad = (n) => String(n).padStart(2, "0");
function clock(iso) {
  if (!iso) return "";
  const d = new Date(iso);
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}
function ago(iso) {
  if (!iso) return "";
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  if (s < 60) return `${s} s ago`;
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return new Date(iso).toLocaleDateString();
}
function duration(from, to) {
  if (!from) return "";
  const s = Math.max(0, Math.round(((to ? new Date(to) : new Date()) - new Date(from)) / 1000));
  if (s < 60) return `${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m} min ${pad(s % 60)} s`;
  return `${Math.floor(m / 60)} h ${pad(m % 60)} min`;
}
function shortId(id) {
  const parts = String(id).split("-");
  return parts.length > 2 ? parts[parts.length - 1] : id;
}
function shortImage(value) {
  const text = String(value);
  const at = text.indexOf("@sha256:");
  if (at < 0) return text;
  const repo = text.slice(0, at).split("/").pop();
  return `${repo}@${text.slice(at + 8, at + 20)}`;
}
const pct = (x) => (typeof x === "number" ? `${Math.round(x * 100)}%` : "–");
const num = (x, digits = 2) => (typeof x === "number" ? x.toFixed(digits) : "–");
const humanize = (s) => String(s || "").toLowerCase().replaceAll("_", " ").replace(/^./, (c) => c.toUpperCase());

/* ---- API ------------------------------------------------------------------ */
async function api(path, options = {}) {
  const res = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", Authorization: `Bearer ${state.token}`, ...(options.headers || {}) },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try { detail = (await res.json()).detail || detail; } catch (_) { /* not json */ }
    const error = new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    error.status = res.status;
    throw error;
  }
  return res.json();
}

/* ---- stages ------------------------------------------------------------- */
function stageIndex(status) {
  if (status in STAGE_OF) return STAGE_OF[status];
  if (status in TERMINAL) return 6;
  return 0;
}
function needsDecision(incident) {
  return incident.status === "WAITING_FOR_APPROVAL" || incident.hitl?.state === "WAITING_FOR_HUMAN";
}
function railStates(status, reached) {
  /* reached: optional set of stage indexes known (from the audit) to have been entered */
  const cells = Array(7).fill("");
  const terminal = TERMINAL[status];
  if (terminal) {
    for (let i = 0; i < 6; i += 1) if (!reached || reached.has(i)) cells[i] = "done";
    cells[6] = terminal === "fail" ? "fail" : terminal === "ok" ? "ok" : "done";
    return cells;
  }
  const k = stageIndex(status);
  for (let i = 0; i < k; i += 1) cells[i] = !reached || reached.has(i) ? "done" : "";
  cells[k] = status === "WAITING_FOR_APPROVAL" ? "wait" : "now";
  return cells;
}
function statusTone(status) {
  if (status === "WAITING_FOR_APPROVAL" || TERMINAL[status] === "fail") return "signal";
  if (TERMINAL[status] === "ok") return "ok";
  return null;
}

/* ---- live status ---------------------------------------------------------- */
function renderLive() {
  const live = $("#live");
  if (!state.me) { live.textContent = "Not signed in"; live.dataset.state = ""; return; }
  if (state.error) { live.textContent = `Connection lost · retrying (${state.error})`; live.dataset.state = "stale"; return; }
  const s = state.lastOk ? Math.round((Date.now() - state.lastOk) / 1000) : 0;
  live.textContent = `Live · updated ${s} s ago`;
  live.dataset.state = s > 12 ? "stale" : "live";
}

/* ---- loop strip ------------------------------------------------------------ */
function renderLoop() {
  const counts = Array(7).fill(0);
  let resolved = 0;
  let escalated = 0;
  for (const incident of state.incidents) {
    const terminal = TERMINAL[incident.status];
    if (terminal) {
      counts[6] += 1;
      if (terminal === "ok") resolved += 1;
      if (terminal === "fail") escalated += 1;
    } else counts[stageIndex(incident.status)] += 1;
  }
  const list = $("#loop-stages");
  list.replaceChildren(...STAGES.map((stage, i) => {
    const hot = i === 3 && counts[3] > 0;
    return h("li", { class: "loop-stage", "data-hot": hot },
      h("button", {
        type: "button", "aria-pressed": state.stage === i ? "true" : "false",
        "aria-label": `${stage.no} ${stage.name}: ${counts[i]} workflow${counts[i] === 1 ? "" : "s"}${hot ? ", waiting for you" : ""}`,
        onclick: () => { state.stage = state.stage === i ? null : i; renderRegister(); renderLoop(); },
      },
        h("span", { class: "stage-no", text: stage.no }),
        h("span", { class: "stage-name", text: stage.name }),
        h("span", { class: "stage-count", "data-zero": counts[i] === 0, text: counts[i] })));
  }));
  const open = state.incidents.length - counts[6];
  const recoveries = state.incidents
    .filter((i) => i.status === "RESOLVED" && i.opened_at && i.closed_at)
    .map((i) => (new Date(i.closed_at) - new Date(i.opened_at)) / 1000)
    .sort((a, b) => a - b);
  const median = recoveries.length ? recoveries[Math.floor((recoveries.length - 1) / 2)] : null;
  $("#loop-totals").replaceChildren(
    h("dl", { class: "loop-total" }, h("dt", { text: "Open" }), h("dd", { "data-zero": open === 0, text: open })),
    h("dl", { class: "loop-total", "data-kind": "resolved" }, h("dt", { text: "Resolved" }), h("dd", { "data-zero": resolved === 0, text: resolved })),
    h("dl", { class: "loop-total", "data-kind": "escalated" }, h("dt", { text: "Escalated" }), h("dd", { "data-zero": escalated === 0, text: escalated })),
    h("dl", { class: "loop-total", title: "Median time from detection to verified recovery" },
      h("dt", { text: "TTR p50" }), h("dd", { "data-zero": median === null, text: median === null ? "–" : seconds(median) })),
  );
}
function seconds(s) {
  if (s < 60) return `${Math.round(s)} s`;
  if (s < 3600) return `${Math.floor(s / 60)} m ${pad(Math.round(s % 60))}`;
  return `${Math.floor(s / 3600)} h ${pad(Math.floor((s % 3600) / 60))}`;
}

/* ---- register --------------------------------------------------------------- */
function visibleIncidents() {
  const q = state.query.trim().toLowerCase();
  return state.incidents.filter((incident) => {
    const terminal = Boolean(TERMINAL[incident.status]);
    if (state.view === "decide" && !needsDecision(incident)) return false;
    if (state.view === "active" && terminal) return false;
    if (state.view === "closed" && !terminal) return false;
    if (state.kind && incident.kind !== state.kind) return false;
    if (state.stage !== null && (terminal ? 6 : stageIndex(incident.status)) !== state.stage) return false;
    if (!q) return true;
    const hay = [incident.incident_id, incident.service?.service_id, incident.status, STATUS_LABEL[incident.status], incident.kind].join(" ").toLowerCase();
    return hay.includes(q);
  });
}
function renderRegister() {
  const rows = visibleIncidents();
  const decide = state.incidents.filter(needsDecision).length;
  $("#count-decide").textContent = decide ? String(decide) : "";
  const body = $("#rows");
  if (!state.incidents.length) {
    body.replaceChildren(h("tr", { class: "empty-row" }, h("td", { colspan: 5 },
      h("strong", { text: "No workflows yet. " }),
      state.scenarios?.length
        ? "Inject a fault above: Agent 1 detects it and the loop starts."
        : "They appear here when Agent 1 detects an anomaly, a release is submitted or a model check runs.")));
    return;
  }
  if (!rows.length) {
    body.replaceChildren(h("tr", { class: "empty-row" }, h("td", { colspan: 5 },
      "No workflow matches these filters. ",
      h("button", { type: "button", class: "link-button", onclick: clearFilters, text: "Clear filters" }))));
    return;
  }
  body.replaceChildren(...rows.map((incident) => {
    const selected = incident.incident_id === state.selected;
    const tone = statusTone(incident.status);
    return h("tr", {
      tabindex: selected ? "0" : "-1", "aria-selected": selected ? "true" : "false", "data-id": incident.incident_id,
      onclick: () => select(incident.incident_id, true),
      onkeydown: onRowKey,
    },
      h("td", {},
        h("div", { class: "row-service", text: incident.service?.service_id || incident.incident_id }),
        h("div", { class: "row-sub" },
          h("span", { text: KIND_LABEL[incident.kind] || incident.kind }),
          h("span", { class: "mono", text: shortId(incident.incident_id) }),
          incident.service?.version ? h("span", { text: incident.service.version }) : null,
          h("span", { text: incident.environment }),
          h("span", { class: "row-age", text: ago(incident.opened_at) })),
        /* The signature rail moves under the name on narrow screens instead of disappearing. */
        h("div", { class: "row-rail" }, rail(railStates(incident.status), `Loop position: ${STATUS_LABEL[incident.status] || incident.status}`))),
      h("td", { class: "col-rail" }, rail(railStates(incident.status), `Loop position: ${STATUS_LABEL[incident.status] || incident.status}`)),
      h("td", {}, h("span", { class: "status-text", "data-tone": tone, text: STATUS_LABEL[incident.status] || humanize(incident.status) })),
      h("td", { class: "col-num" }, h("span", { text: incident.priority }), " ", h("span", { class: "muted", text: incident.severity?.toLowerCase() })),
      h("td", { class: "col-num muted", title: incident.opened_at, text: ago(incident.opened_at) }));
  }));
}
function rail(cells, label) {
  return h("span", { class: "rail", role: "img", "aria-label": label }, cells.map((s) => h("i", { "data-s": s || null })));
}
function clearFilters() {
  state.view = "all"; state.kind = ""; state.query = ""; state.stage = null;
  $("#kind-filter").value = ""; $("#search").value = "";
  for (const b of document.querySelectorAll("#view-filter button")) b.setAttribute("aria-checked", b.dataset.view === "all" ? "true" : "false");
  renderLoop(); renderRegister();
}
function onRowKey(event) {
  const rows = [...document.querySelectorAll("#rows tr[data-id]")];
  const i = rows.indexOf(event.currentTarget);
  let next = null;
  if (event.key === "ArrowDown") next = rows[i + 1];
  if (event.key === "ArrowUp") next = rows[i - 1];
  if (event.key === "Enter" || event.key === " ") { event.preventDefault(); $("#sheet").focus(); return; }
  if (next) { event.preventDefault(); select(next.dataset.id, false); requestAnimationFrame(() => document.querySelector(`#rows tr[data-id="${CSS.escape(next.dataset.id)}"]`)?.focus()); }
}
function select(id, focusSheet) {
  if (state.selected !== id) { state.selected = id; state.sheetKey = ""; state.detail = null; }
  history.replaceState(null, "", `#${encodeURIComponent(id)}`);
  renderRegister();
  renderSheetLoading();
  loadDetail().then(() => { if (focusSheet && window.innerWidth <= 1180) $("#sheet").scrollIntoView({ block: "start" }); });
}

/* ---- sheet --------------------------------------------------------------------- */
function renderSheetLoading() {
  if (state.detail?.context?.incident_id === state.selected) return;
  $("#sheet").replaceChildren(h("div", { class: "sheet-head" },
    h("span", { class: "skeleton", style: "width: 60%; height: 28px" }),
    h("span", { class: "skeleton", style: "width: 40%; margin-top: 12px" }),
    h("span", { class: "skeleton", style: "width: 100%; margin-top: 24px; height: 44px" })));
}
function renderSheetEmpty() {
  const decide = state.incidents.filter(needsDecision);
  $("#sheet").replaceChildren(h("div", { class: "empty" },
    h("h2", { text: decide.length ? `${decide.length} workflow${decide.length > 1 ? "s" : ""} waiting for a decision` : "Nothing selected" }),
    h("p", { text: decide.length
      ? "Open it to see the exact change Agent 4 prepared, why the policy asked for a human, and to approve or reject."
      : "Select a workflow to see what the agents found, what the policy decided, the change that was made and the audit trail that proves it." }),
    decide.length ? h("div", {}, h("button", { type: "button", class: "button primary", onclick: () => select(decide[0].incident_id, true) }, "Open the decision", icon("arrow"))) : null));
}

async function loadDetail() {
  const id = state.selected;
  if (!id) { state.detail = null; renderSheetEmpty(); return; }
  try {
    const detail = await api(`/api/incidents/${encodeURIComponent(id)}`);
    if (state.selected !== id) return;
    state.detail = detail;
    renderSheet();
  } catch (error) {
    if (state.selected !== id) return;
    $("#sheet").replaceChildren(h("div", { class: "empty" },
      h("h2", { text: error.status === 404 ? "This workflow no longer exists" : "Could not load this workflow" }),
      h("p", { text: error.status === 404 ? "It may belong to a platform run that was reset." : `${error.message}. The console retries every few seconds.` })));
  }
}

function stageTimes(audit) {
  const times = {};
  const mark = (i, at) => { if (i !== undefined && !(i in times)) times[i] = at; };
  for (const row of audit) {
    const d = row.detail || {};
    if (row.action === "INCIDENT_OPENED") mark(0, row.at);
    else if (row.action === "INCIDENT_UPDATED" && d.status) mark(d.status in TERMINAL ? 6 : STAGE_OF[d.status], row.at);
    else if (row.action === "DECISION_RECORDED" && ["CONSENSUS", "POLICY", "RELEASE_GATE"].includes(d.stage)) mark(2, row.at);
    else if (row.action.startsWith("APPROVAL_")) mark(3, row.at);
    else if (row.action === "ACTION_UPDATED" && d.execution_status === "EXECUTING") mark(4, row.at);
  }
  return times;
}

function renderSheet() {
  const detail = state.detail;
  if (!detail) return renderSheetEmpty();
  const context = detail.context;
  const workflow = detail.workflow || {};
  const approval = state.approvals.find((a) => a.incident_id === context.incident_id && a.state === "WAITING_FOR_HUMAN");
  const key = JSON.stringify([detail, approval, state.messages[context.incident_id]]);
  if (key === state.sheetKey) return;
  state.sheetKey = key;

  const sheet = $("#sheet");
  const active = document.activeElement;
  const typing = active && active.id === "decision-comment";
  const caret = typing ? [active.selectionStart, active.selectionEnd] : null;

  const times = stageTimes(detail.audit || []);
  const reached = new Set(Object.keys(times).map(Number));
  const cells = railStates(context.status, reached);
  const service = context.service || {};

  sheet.replaceChildren(...[
    h("div", { class: "sheet-head" },
      h("h2", { class: "sheet-title", text: service.service_id || context.incident_id }),
      h("p", { class: "sheet-id" },
        h("span", { text: KIND_LABEL[context.kind] || context.kind }), "·",
        h("code", { text: context.incident_id }),
        copyButton(context.incident_id, "Copy workflow ID")),
      h("dl", { class: "meta" },
        metaItem("Status", STATUS_LABEL[context.status] || humanize(context.status)),
        metaItem("Environment", context.environment),
        metaItem("Priority", `${context.priority} · ${String(context.severity).toLowerCase()}`),
        metaItem(context.closed_at ? "Took" : "Open for", duration(context.opened_at, context.closed_at)))),
    h("div", { class: "rail-large", role: "list", "aria-label": "Position in the closed loop" },
      STAGES.map((stage, i) => h("div", { class: "rail-step", role: "listitem", "data-s": cells[i] || null },
        h("b", { text: stage.no }), h("span", { text: stage.name }), h("time", { text: times[i] ? clock(times[i]) : "–" })))),
    outcomeBlock(context, workflow, detail.audit || []),
    approval ? decisionBlock(approval, context) : null,
    diagnosisBlock(workflow),
    candidatesBlock(workflow),
    policyBlock(workflow, Boolean(approval)),
    agentsBlock(context),
    auditBlock(detail),
  ].filter(Boolean));

  if (typing) {
    const box = document.getElementById("decision-comment");
    if (box) { box.focus(); box.setSelectionRange(...caret); }
  }
  /* When a workflow reaches its outcome, bring the outcome into view. */
  const statusKey = `${context.incident_id}:${context.status}`;
  if (state.lastStatus && state.lastStatus !== statusKey && TERMINAL[context.status]
      && state.lastStatus.startsWith(`${context.incident_id}:`)) sheet.scrollTop = 0;
  state.lastStatus = statusKey;
}

/* Agents write long rationales: show the first lines, expand on demand. */
function longText(text, cls) {
  const value = String(text || "");
  if (value.length <= 240) return h("span", { class: cls, text: value });
  const key = value.slice(0, 80);
  const open = state.expanded.has(key);
  return h("span", { class: cls },
    h("span", { class: open ? "" : "clamp", text: value }),
    h("button", {
      type: "button", class: "link-button more", "aria-expanded": String(open),
      onclick: () => { if (open) state.expanded.delete(key); else state.expanded.add(key); state.sheetKey = ""; renderSheet(); },
    }, open ? "Show less" : "Show full text"));
}

function metaItem(label, value) { return h("div", {}, h("dt", { text: label }), h("dd", { text: value || "–" })); }
function copyButton(value, label) {
  return h("button", {
    type: "button", class: "copy", title: label, "aria-label": label,
    onclick: async (e) => {
      const btn = e.currentTarget;
      try { await navigator.clipboard.writeText(value); btn.title = "Copied"; } catch (_) { btn.title = "Copy failed"; }
    },
  }, icon("copy"));
}

function outcomeBlock(context, workflow, audit) {
  const terminal = TERMINAL[context.status];
  if (!terminal) return null;
  const close = [...audit].reverse().find((r) => r.action === "DECISION_RECORDED" && r.detail?.stage === "CLOSE");
  const reason = close?.detail?.reason;
  if (context.status === "RESOLVED") {
    return h("div", { class: "outcome", "data-tone": "ok" }, icon("check"), h("div", {},
      workflow.outcome === "RECOVERED" ? "Recovered and verified" : "Resolved",
      h("small", { text: workflow.outcome === "RECOVERED"
        ? "Agent 1 measured the service back within its SLO over the observation window after the change."
        : "The workflow completed its success criteria." })));
  }
  if (context.status === "ESCALATED") {
    return h("div", { class: "outcome", "data-tone": "signal" }, icon("cross"), h("div", {},
      "Escalated to a human, nothing further was executed",
      h("small", { text: reason ? `Reason: ${humanize(reason)}` : "No safe action remained within policy." })));
  }
  if (context.status === "RELEASE_BLOCKED") {
    return h("div", { class: "outcome", "data-tone": "signal" }, icon("cross"), h("div", {},
      "Release blocked by Agent 3's gate", h("small", { text: "Nothing was deployed. The blocking reasons are in the audit trail." })));
  }
  return h("div", { class: "outcome", "data-tone": terminal === "ok" ? "ok" : "ink" }, icon(terminal === "ok" ? "check" : "arrow"),
    h("div", {}, STATUS_LABEL[context.status] || humanize(context.status)));
}

/* ---- the decision ------------------------------------------------------------------ */
function patchChanges(patch) {
  const tests = new Map();
  const changes = [];
  const preconditions = [];
  for (const op of patch || []) {
    if (op.op === "test") tests.set(op.path, op.value);
    else changes.push(op);
  }
  const changed = new Set(changes.map((c) => c.path));
  for (const [path, value] of tests) {
    if (changed.has(path)) continue;
    /* [short label for the line, full statement for the tooltip] */
    if (path === "/metadata/uid") preconditions.push(["UID", `Deployment UID is ${value}`]);
    else if (path === "/metadata/resourceVersion") preconditions.push([`resourceVersion ${value}`, `resourceVersion is still ${value} (as dry-run)`]);
    else if (path.endsWith("/name")) preconditions.push(["container name", `Container is ${value}`]);
    else preconditions.push([path, `${path} = ${JSON.stringify(value)}`]);
  }
  return {
    preconditions,
    rows: changes.map((op) => {
      const label = op.path.endsWith("/image") ? "Image" : op.path === "/spec/replicas" ? "Replicas" : op.path.endsWith("/resources") ? "Resources" : op.path;
      const fmt = (v) => (label === "Image" ? shortImage(v) : label === "Resources" ? resources(v) : typeof v === "object" ? JSON.stringify(v) : String(v));
      return { label, from: tests.has(op.path) ? fmt(tests.get(op.path)) : "unset", to: fmt(op.value), raw: op };
    }),
  };
}
function resources(value) {
  if (!value || typeof value !== "object") return String(value);
  const limits = value.limits || {};
  return [limits.memory && `memory ${limits.memory}`, limits.cpu && `cpu ${limits.cpu}`].filter(Boolean).join(", ") || JSON.stringify(value);
}
function actionHeadline(request) {
  const verb = ACTION_VERB[request.action] || humanize(request.action);
  const target = request.plan?.target?.name || request.parameters?.service || request.parameters?.model_name || "";
  if (request.action === "PROMOTE_MODEL") return `${verb} ${request.parameters?.model_name || ""} v${request.parameters?.version ?? "?"}`;
  return `${verb} ${target}`.trim();
}
function canApprove(action) {
  const roles = state.me?.roles || [];
  const allowed = APPROVER_ROLES[action] || ["admin"];
  return { ok: roles.some((r) => allowed.includes(r)), allowed };
}

function decisionBlock(approval, context) {
  const request = approval.request || {};
  const plan = request.plan;
  const { rows, preconditions } = patchChanges(plan?.patch);
  const permission = canApprove(request.action);
  const message = state.messages[context.incident_id];
  const risk = request.risk || {};

  return h("section", { class: "decision", "aria-labelledby": "decision-title" },
    h("div", { class: "decision-band" },
      h("h2", { id: "decision-title", text: "Decision required" }),
      h("span", { class: "permission", "data-allowed": String(permission.ok), text: permissionText(permission) }),
      h("span", { class: "countdown", "data-expires": approval.expires_at, "aria-label": "Time left to decide", text: countdown(approval.expires_at) })),
    h("div", { class: "decision-body" },
      h("p", { class: "decision-action" }, actionHeadline(request),
        h("small", { text: [request.environment, plan?.target ? `${plan.target.namespace}/${plan.target.name} on ${plan.target.cluster_id}` : null].filter(Boolean).join(" · ") })),
      plan ? h("div", {},
        h("h3", { text: "Exact change Agent 4 will apply" }),
        rows.length ? h("div", { class: "change" }, rows.map((r) => h("div", { class: "change-row" },
          h("span", { class: "field-name", text: r.label }),
          h("span", { class: "from mono" }, h("span", { class: "visually-hidden", text: "from " }), r.from),
          h("span", { class: "to mono", title: typeof r.raw.value === "string" ? r.raw.value : JSON.stringify(r.raw.value) },
            icon("arrow"), h("span", { class: "visually-hidden", text: "to " }), r.to)))) : h("p", { class: "muted", text: "No field changes." }),
        preconditions.length ? h("p", { class: "preconditions", title: preconditions.map((p) => p[1]).join("\n") }, icon("check"),
          h("span", { text: `Re-checked before apply: ${preconditions.map((p) => p[0]).join(", ")}` })) : null)
        : h("div", {}, h("h3", { text: "Governance decision" }),
          h("p", { class: "lead", text: "No infrastructure change is attached: approving records the decision; nothing is executed." })),
      h("div", { class: "decision-grid" },
        h("div", {},
          h("h3", { text: "Why a human is needed" }),
          (request.hitl_triggers || []).length
            ? h("ul", { class: "reasons" }, request.hitl_triggers.map((t) => h("li", { text: t })))
            : h("p", { class: "muted", text: "Policy requires approval for this action class." })),
        h("div", {},
          h("h3", { text: `Risk ${String(risk.level || "").toLowerCase()} · score ${num(risk.score)}` }),
          h("div", { class: "bars" }, Object.entries(risk.factors || {}).map(([k, v]) => h("div", { class: "bar" },
            h("span", { title: RISK_LABEL[k] || humanize(k), text: RISK_SHORT[k] || RISK_LABEL[k] || humanize(k) }),
            h("span", { class: "bar-track" }, h("span", { class: "bar-fill", style: `transform:scaleX(${Math.max(0, Math.min(1, Number(v) || 0))})` })),
            h("span", { class: "bar-value", text: pct(v) })))))),
      decideForm(approval, permission, message),
      evidenceDetails(request.evidence_summary || [], plan)));
}

/* The orchestrator sends monitoring signals as "key=value"; show them as measurements. */
const SIGNAL = {
  memory_pct: ["Memory used", (v) => `${num(v, 1)}%`],
  memory_slope_mb_min: ["Memory growth", (v) => `${num(v, 1)} MB/min`],
  error_rate: ["Error rate", (v) => `${num(v * 100, 1)}%`],
  latency_p95_ms: ["Latency p95", (v) => `${Math.round(v)} ms`],
  pod_restarts: ["Pod restarts", (v) => String(Math.round(v))],
  cpu_avg_pct: ["CPU average", (v) => `${num(v, 1)}%`],
};
function evidenceDetails(evidence, plan) {
  const signals = [];
  const prose = [];
  for (const item of evidence) {
    const m = /^([a-z0-9_]+)=(-?[\d.]+)$/.exec(String(item));
    if (m && SIGNAL[m[1]]) signals.push([SIGNAL[m[1]][0], SIGNAL[m[1]][1](Number(m[2]))]);
    else if (String(item).trim()) prose.push(item);
  }
  if (!signals.length && !prose.length && !plan) return null;
  return h("div", { class: "decision-evidence" },
    signals.length ? h("div", {},
      h("h3", { text: "Signals Agent 1 measured" }),
      h("dl", { class: "kv signals" }, signals.map(([k, v]) => [h("dt", { text: k }), h("dd", { text: v })]))) : null,
    prose.length || plan ? h("details", {
      class: "more-details", open: state.expanded.has("evidence-details"),
      /* Polling re-renders the sheet; remember whether the operator opened this. */
      ontoggle: (e) => { if (e.target.open) state.expanded.add("evidence-details"); else state.expanded.delete("evidence-details"); },
    },
      h("summary", { text: plan ? "Diagnosis notes and permit hashes" : "Diagnosis notes" }),
      prose.length ? h("ul", { class: "reasons" }, prose.map((e) => h("li", {}, longText(e)))) : null,
      plan ? h("dl", { class: "hashes", "aria-label": "Values the signed permit binds" },
        hashRow("Plan", plan.plan_hash), hashRow("Patch", plan.patch_hash), hashRow("Operation", plan.operation_id)) : null) : null);
}
function permissionText(permission) {
  return permission.ok
    ? `You approve as ${state.me.roles.join(", ")}`
    : `Needs ${permission.allowed.filter((r) => r !== "admin").join(" or ")}; you are ${state.me?.roles?.join(", ") || "read-only"}`;
}
/* One row: comment, approve, reject. It stays pinned to the bottom of the sheet. */
function decideForm(approval, permission, message) {
  return h("form", { class: "decide-form", onsubmit: (e) => e.preventDefault() },
    h("label", { class: "decide-comment" }, h("span", { text: "Comment for the audit trail" }),
      h("textarea", {
        id: "decision-comment", rows: 1, required: true, minlength: 3, maxlength: 2000,
        placeholder: "Required",
        oninput: (e) => { state.comments[approval.approval_id] = e.target.value; },
      }, state.comments[approval.approval_id] || "")),
    h("button", { type: "button", class: "button primary", disabled: !permission.ok || state.busy, onclick: () => resolve(approval, "APPROVE") },
      icon("check"), "Approve and execute"),
    h("button", { type: "button", class: "button", disabled: !permission.ok || state.busy, onclick: () => resolve(approval, "REJECT") },
      icon("cross"), "Reject"),
    message ? h("p", { class: "form-message", role: "status", "data-tone": message.tone, text: message.text }) : null);
}
function hashRow(label, value) {
  if (!value) return null;
  return h("div", {}, h("dt", { text: label }), h("dd", { class: "mono", title: value, text: value.length > 24 ? `${value.slice(0, 16)}…${value.slice(-6)}` : value }), copyButton(value, `Copy ${label.toLowerCase()} hash`));
}
function countdown(iso) {
  const s = Math.round((new Date(iso).getTime() - Date.now()) / 1000);
  if (s <= 0) return "Expired";
  return `${pad(Math.floor(s / 60))}:${pad(s % 60)} left`;
}

async function resolve(approval, decision) {
  const comment = (state.comments[approval.approval_id] || "").trim();
  const id = approval.incident_id;
  if (comment.length < 3) {
    state.messages[id] = { tone: "signal", text: "Write a short comment first: it is recorded with your decision." };
    state.sheetKey = ""; renderSheet();
    document.getElementById("decision-comment")?.focus();
    return;
  }
  state.busy = true; state.sheetKey = ""; renderSheet();
  try {
    const result = await api(`/api/approvals/${encodeURIComponent(approval.approval_id)}/resolve`, {
      method: "POST", body: JSON.stringify({ decision, comment }),
    });
    state.messages[id] = decision === "APPROVE"
      ? { tone: "ok", text: `Approved by ${result.approver_id}. Agent 4 is applying the exact change; Agent 1 verifies it next.` }
      : { tone: "ok", text: `Rejected by ${result.approver_id}. Nothing was executed; the Orchestrator re-plans.` };
    delete state.comments[approval.approval_id];
  } catch (error) {
    const text = error.status === 403 ? `Not allowed: ${error.message}`
      : error.status === 409 ? "This approval was already resolved or has expired."
        : error.status === 503 ? error.message
          : `Could not record the decision: ${error.message}. Nothing was executed.`;
    state.messages[id] = { tone: "signal", text };
  } finally {
    state.busy = false; state.sheetKey = "";
    await refresh();
  }
}

/* ---- sheet sections ------------------------------------------------------------------ */
function diagnosisBlock(workflow) {
  const c = workflow.consensus;
  if (!c || (!c.primary_root_cause && !(c.candidates || []).length)) return null;
  return h("section", { class: "sheet-block" },
    h("h2", {}, "Diagnosis", h("span", { class: "aside", text: "Agent 2 + consensus" })),
    h("dl", { class: "kv" },
      c.primary_root_cause ? [h("dt", { text: "Root cause" }), h("dd", { class: "strong", text: humanize(c.primary_root_cause) })] : null,
      h("dt", { text: "RCA confidence" }), h("dd", { text: pct(c.rca_confidence) }),
      h("dt", { text: "Evidence strength" }), h("dd", { text: pct(c.evidence_strength) }),
      h("dt", { text: "Specialist agreement" }), h("dd", { text: pct(c.agreement) }),
      (c.conflicts || []).length ? [h("dt", { text: "Conflicts" }), h("dd", { text: c.conflicts.join("; ") })] : null));
}
function candidatesBlock(workflow) {
  const candidates = workflow.consensus?.candidates || [];
  if (!candidates.length) return null;
  const selected = workflow.selected?.action_id;
  return h("section", { class: "sheet-block" },
    h("h2", {}, "Candidate actions", h("span", { class: "aside", text: "ranked by score" })),
    h("div", { class: "data-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", { text: "Action" }), h("th", { text: "Proposed by" }), h("th", { text: "Why" }), h("th", { class: "num", text: "Score" }))),
      h("tbody", {}, candidates.map((c) => h("tr", { "data-selected": c.action_id === selected },
        h("td", {}, humanize(c.type), c.action_id === selected ? h("div", { class: "tag", text: "selected" }) : null),
        h("td", { text: (AGENTS[c.source_agent] || [c.source_agent])[0] }),
        h("td", { class: "muted" }, longText(c.rationale)),
        h("td", { class: "num", text: num(c.score) })))))));
}
function policyBlock(workflow, pending) {
  const p = workflow.policy;
  if (!p) return null;
  const tone = p.decision === "BLOCK" ? "signal" : p.decision === "AUTO" ? "ok" : "signal";
  return h("section", { class: "sheet-block" },
    h("h2", {}, "Policy decision", h("span", { class: "aside", text: p.policy_version || "" })),
    h("p", { class: "lead" },
      h("span", { class: "tag", "data-tone": tone, text: p.decision === "APPROVAL_REQUIRED" ? "Approval required" : humanize(p.decision) }),
      ` · Decision score ${num(p.decision_score)} · risk ${String(p.risk?.level || "–").toLowerCase()} (${num(p.risk?.score)})`),
    /* While the decision is open its reasons are already shown under "Why a human is needed". */
    !pending && (p.reasons || []).length ? h("ul", { class: "reasons", style: "margin-top: 8px" }, p.reasons.map((r) => h("li", { text: r }))) : null);
}
function agentsBlock(context) {
  const results = context.agent_results || [];
  if (!results.length) return null;
  return h("section", { class: "sheet-block" },
    h("h2", {}, "Agent results", h("span", { class: "aside", text: `${results.length} result${results.length > 1 ? "s" : ""} · ${(context.evidence_refs || []).length} evidence records` })),
    h("ul", { class: "agents" }, results.map((r) => {
      const [name, role] = AGENTS[r.agent_id] || [r.agent_id, ""];
      return h("li", { class: "agent" },
        h("span", { class: "agent-name" }, name, h("small", { text: role })),
        longText(r.rationale_summary, "agent-summary"),
        h("span", { class: "tag", "data-tone": r.status === "SUCCEEDED" ? "ok" : "signal", text: r.status === "SUCCEEDED" ? "ok" : humanize(r.status) }));
    })));
}
function auditLine(row) {
  const d = row.detail || {};
  const agent = (id) => (AGENTS[id] || [id])[0];
  switch (row.action) {
    case "INCIDENT_OPENED": return ["Workflow opened", null];
    case "INCIDENT_UPDATED": return [d.status ? `Status → ${STATUS_LABEL[d.status] || humanize(d.status)}` : "Workflow updated", TERMINAL[d.status] === "ok" ? "ok" : TERMINAL[d.status] === "fail" ? "signal" : null];
    case "AGENT_RESULT_APPENDED": return [`${agent(d.agent_id)} reported ${humanize(String(d.type || "").split(":").pop())}`, null];
    case "EVIDENCE_APPENDED": return ["Evidence stored", null, d.evidence_id];
    case "ACTION_RECORDED": return [`Candidate ${humanize(d.type)} (score ${num(d.score)})`, null];
    case "ACTION_UPDATED": return [`Action ${humanize(d.execution_status || d.policy_result || "updated")}`, d.execution_status === "VERIFIED" ? "ok" : null];
    case "DECISION_RECORDED": return [`${humanize(d.stage)}: ${humanize(d.type)}`, d.type === "ESCALATED" ? "signal" : d.type === "RESOLVED" ? "ok" : null];
    case "APPROVAL_WAITING_FOR_HUMAN": return ["Approval requested", "signal"];
    case "APPROVAL_APPROVED": return [`Approved by ${d.approver_id}`, "ok"];
    case "APPROVAL_REJECTED": return [`Rejected by ${d.approver_id}`, "signal"];
    case "APPROVAL_EXPIRED": return ["Approval expired", "signal"];
    case "MODEL_EVENT_RECORDED": return [`Model ${humanize(d.type)}`, null, d.model];
    default: return [humanize(row.action), null];
  }
}
function groupAudit(audit) {
  /* Consecutive "evidence stored" records by the same actor read as one line. */
  const out = [];
  for (const row of audit) {
    const last = out[out.length - 1];
    if (row.action === "EVIDENCE_APPENDED" && last && last.action === "EVIDENCE_APPENDED" && last.actor === row.actor) {
      last.count += 1;
    } else out.push({ ...row, count: 1 });
  }
  return out;
}
function auditBlock(detail) {
  const audit = detail.audit || [];
  const rows = groupAudit(audit);
  return h("section", { class: "sheet-block" },
    h("h2", {}, "Audit trail",
      h("span", { class: "chain", "data-ok": String(detail.audit_chain_valid) },
        icon("chain"), detail.audit_chain_valid ? `Hash chain verified · ${audit.length} records` : "Hash chain broken: records were altered")),
    h("ol", { class: "timeline" }, rows.map((row) => {
      const [base, tone, single] = auditLine(row);
      const text = row.count > 1 ? `${base} ×${row.count}` : base;
      const extra = row.count > 1 ? null : single;
      const [name] = AGENTS[row.actor] || [row.actor];
      return h("li", { "data-tone": tone },
        h("time", { datetime: row.at, text: clock(row.at) }),
        h("span", { class: "who", title: row.actor, text: name }),
        h("span", { class: "what" }, text, extra ? h("small", { text: ` ${extra}` }) : null));
    })));
}

/* ---- scenarios --------------------------------------------------------------------------- */
async function loadScenarios() {
  try { state.scenarios = await api("/api/scenarios"); } catch (_) { state.scenarios = []; }
  const box = $("#inject-buttons");
  $("#inject").hidden = !state.scenarios.length;
  box.replaceChildren(...state.scenarios.map((name) => h("button", {
    type: "button", class: "button small",
    onclick: async (e) => {
      const btn = e.currentTarget;
      btn.disabled = true; btn.setAttribute("aria-busy", "true");
      state.follow = true;
      try { await api(`/api/scenarios/${encodeURIComponent(name)}`, { method: "POST" }); }
      catch (error) { state.follow = false; alertLive(`Could not inject ${SCENARIO_LABEL[name] || name}: ${error.message}`); }
      btn.disabled = false; btn.removeAttribute("aria-busy");
      refresh();
    },
  }, name === "reset" ? null : icon("bolt"), SCENARIO_LABEL[name] || humanize(name))));
}
function alertLive(text) { state.error = text; renderLive(); }

/* ---- refresh loop ---------------------------------------------------------------------------- */
async function refresh() {
  if (!state.token) return;
  try {
    const [incidents, approvals] = await Promise.all([api("/api/incidents"), api("/api/approvals")]);
    const previous = state.knownIds;
    state.incidents = incidents;
    state.approvals = approvals;
    state.knownIds = new Set(incidents.map((i) => i.incident_id));
    state.lastOk = Date.now();
    state.error = null;
    if (previous && state.follow) {
      const fresh = incidents.find((i) => !previous.has(i.incident_id));
      if (fresh) { state.follow = false; state.selected = fresh.incident_id; state.sheetKey = ""; history.replaceState(null, "", `#${encodeURIComponent(fresh.incident_id)}`); }
    }
    if (state.selected && !state.knownIds.has(state.selected)) { state.selected = null; state.detail = null; }
    renderLoop();
    renderRegister();
    renderLive();
    if (state.selected) await loadDetail(); else renderSheetEmpty();
  } catch (error) {
    if (error.status === 401) { signOut("Your session token was not accepted."); return; }
    state.error = error.status ? `HTTP ${error.status}` : "network";
    renderLive();
  }
}

/* ---- session ------------------------------------------------------------------------------ */
async function signIn(token) {
  state.token = token;
  try {
    state.me = await api("/api/me");
  } catch (error) {
    state.token = ""; state.me = null;
    $("#signin-error").textContent = error.status === 401 ? "This token is not a known operator." : `Sign-in failed: ${error.message}`;
    return;
  }
  try { sessionStorage.setItem("aiops-token", token); } catch (_) { /* ignore */ }
  $("#signin").hidden = true;
  $("#workspace").hidden = false;
  $("#loop").hidden = false;
  $("#operator").hidden = false;
  $("#operator-name").textContent = state.me.subject;
  $("#operator-roles").textContent = state.me.roles.length ? state.me.roles.join(" · ") : "read-only";
  const fromHash = decodeURIComponent(location.hash.slice(1));
  if (fromHash) state.selected = fromHash;
  await loadScenarios();
  await refresh();
}
function signOut(reason) {
  state.token = ""; state.me = null; state.incidents = []; state.selected = null; state.detail = null;
  try { sessionStorage.removeItem("aiops-token"); } catch (_) { /* ignore */ }
  $("#workspace").hidden = true; $("#loop").hidden = true; $("#operator").hidden = true;
  $("#signin").hidden = false;
  $("#signin-error").textContent = reason || "";
  $("#token").value = "";
  $("#token").focus();
  renderLive();
}

/* ---- wiring ---------------------------------------------------------------------------------- */
$("#signin-form").addEventListener("submit", (e) => { e.preventDefault(); $("#signin-error").textContent = ""; signIn($("#token").value.trim()); });
$("#signout").addEventListener("click", () => signOut());
for (const button of document.querySelectorAll("#view-filter button")) {
  button.addEventListener("click", () => {
    state.view = button.dataset.view;
    for (const b of document.querySelectorAll("#view-filter button")) b.setAttribute("aria-checked", b === button ? "true" : "false");
    renderRegister();
  });
}
$("#kind-filter").addEventListener("change", (e) => { state.kind = e.target.value; renderRegister(); });
$("#search").addEventListener("input", (e) => { state.query = e.target.value; renderRegister(); });
document.addEventListener("keydown", (e) => {
  if (e.key === "/" && document.activeElement?.tagName !== "INPUT" && document.activeElement?.tagName !== "TEXTAREA" && state.me) {
    e.preventDefault(); $("#search").focus();
  }
});
window.addEventListener("hashchange", () => {
  const id = decodeURIComponent(location.hash.slice(1));
  if (id && id !== state.selected && state.me) select(id, false);
});

setInterval(() => { if (state.token && !state.busy) refresh(); }, POLL_MS);
setInterval(() => {
  renderLive();
  for (const node of document.querySelectorAll(".countdown[data-expires]")) node.textContent = countdown(node.dataset.expires);
}, 1000);

$("#loop").hidden = true;
if (state.token) signIn(state.token); else $("#token").focus();
