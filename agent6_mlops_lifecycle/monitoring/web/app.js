"use strict";

const $ = id => document.getElementById(id);
const ns = "http://www.w3.org/2000/svg";
const pages = {
  overview: ["Overview", "YOUR MODELS, AT A GLANCE", "A clear picture of what’s running and what needs your attention."],
  predictions: ["Predictions", "FOLLOW YOUR MODEL’S ACTIVITY", "See each prediction and the real outcome that follows it."],
  quality: ["Model quality", "UNDERSTAND THE PERFORMANCE", "Separate live evidence, probability quality, and historical results."],
  lifecycle: ["Lifecycle", "FOLLOW EVERY IMPROVEMENT", "From automatic monitoring to evaluated model candidates."],
  insights: ["Insights", "TURN EVIDENCE INTO UNDERSTANDING", "Understand the findings and decide what deserves a closer look."],
  evidence: ["Activity & evidence", "EVERY DECISION, TRACEABLE", "Inspect important changes, connection health, and supporting evidence."]
};
let token = "", busy = false, session = 0, currentPage = "overview", qualityTab = "live";
let dataCache = null, selectedVersion = "", currentReport = null, predictionRows = [];
let selectionKey = "", activeReview = null, baselineVersion = null, toastTimer;
let benchmarkKey = "", insightKey = "", jobKey = "", predictionKey = "";
function acceptFragment() {
  const fragment = new URLSearchParams(location.hash.slice(1));
  if (!fragment.has("token")) return false;
  token = fragment.get("token") || ""; session++;
  history.replaceState(null, "", location.pathname + location.search);
  return true;
}
acceptFragment();
window.addEventListener("hashchange", () => {if (acceptFragment()) refresh();});
const number = (v, digits = 3) => v == null || !Number.isFinite(Number(v)) ? "—" : Number(v).toFixed(digits);
const percent = v => v == null ? "—" : `${(v * 100).toFixed(1)}%`;
const count = v => Number(v || 0).toLocaleString();
const shortId = v => String(v || "").replace(/^artifact-/, "").slice(0, 8);
const time = v => v ? new Date(v).toLocaleTimeString([], {hour:"2-digit", minute:"2-digit", second:"2-digit"}) : "Not yet observed";
const dateTime = v => v ? new Date(v).toLocaleString([], {month:"short", day:"numeric", hour:"2-digit", minute:"2-digit"}) : "Not yet observed";
function duration(seconds) {
  if (seconds >= 86400) return `${Math.round(seconds / 86400)} days`;
  if (seconds >= 3600) return `${Math.round(seconds / 3600)} hours`;
  return `${Math.round(seconds / 60)} minutes`;
}
function make(tag, className, text) {
  const e = document.createElement(tag);
  if (className) e.className = className;
  if (text != null) e.textContent = String(text);
  return e;
}
function icon(name) {
  const svg = document.createElementNS(ns, "svg"), use = document.createElementNS(ns, "use");
  svg.classList.add("icon"); svg.setAttribute("aria-hidden", "true"); use.setAttribute("href", `#i-${name}`); svg.append(use); return svg;
}
function badge(text, tone = "neutral") { return make("span", `badge ${tone}`, text); }
function setBadge(id, text, tone = "neutral") { const e = $(id); e.textContent = text; e.className = `badge ${tone}`; }
function cell(row, value) { const td = make("td"); if (value instanceof Node) td.append(value); else td.textContent = value ?? "—"; row.append(td); return td; }
function toast(text) { clearTimeout(toastTimer); $("toast").textContent = text; $("toast").hidden = false; toastTimer = setTimeout(() => {$("toast").hidden = true;}, 2800); }
function error(text) { $("error").textContent = text || ""; $("error").hidden = !text; }
function namedModel(version) {
  if (version === "fraud-api") return "Fraud prediction API";
  if (version === "fraud-application") return "Fraud application";
  const job = dataCache?.assurance?.benchmarks?.find(j => j.model_version === version);
  return job?.artifact_name || `Model ${shortId(version)}`;
}
function displayText(text) { return String(text || "").replace(/artifact-[a-f0-9]{64}/g, id => `${namedModel(id)} (${shortId(id)})`); }
function go(page, focus = true) {
  if (!pages[page]) return;
  currentPage = page;
  document.querySelectorAll("[data-section]").forEach(e => {e.hidden = e.dataset.section !== page;});
  document.querySelectorAll("[data-page]").forEach(e => {
    const active = e.dataset.page === page; e.classList.toggle("is-active", active);
    if (active) e.setAttribute("aria-current", "page"); else e.removeAttribute("aria-current");
  });
  $("page-title").textContent = pages[page][0]; $("page-eyebrow").textContent = pages[page][1];
  $("page-description").textContent = pages[page][2]; $("breadcrumb-current").textContent = pages[page][0];
  if (focus) {window.scrollTo({top:0, behavior:"instant"}); $("main-content").focus({preventScroll:true});}
  if (page === "quality" && qualityTab === "probability") drawPR(currentReport?.fraud_evaluation?.probabilities);
}
function showTab(tab) {
  if (!["live", "probability", "benchmark"].includes(tab)) return;
  qualityTab = tab;
  document.querySelectorAll("[data-tab-panel]").forEach(e => {e.hidden = e.dataset.tabPanel !== tab;});
  document.querySelectorAll("[data-tab]").forEach(e => {const active = e.dataset.tab === tab; e.classList.toggle("is-active", active); e.setAttribute("aria-selected", String(active)); e.tabIndex = active ? 0 : -1;});
  if (tab === "probability") drawPR(currentReport?.fraud_evaluation?.probabilities);
}
class RequestError extends Error { constructor(text, status) {super(text); this.status = status;} }
async function api(path, method = "GET", body) {
  const controller = new AbortController(), timer = setTimeout(() => controller.abort(), 15000);
  try {
    const response = await fetch(path, {method, headers:{Authorization:`Bearer ${token}`, "Content-Type":"application/json"}, body:body === undefined ? undefined : JSON.stringify(body), cache:"no-store", signal:controller.signal});
    const result = await response.json();
    if (!response.ok) throw new RequestError(typeof result.detail === "string" ? result.detail : "Check the submitted fields and try again.", response.status);
    return result;
  } finally {clearTimeout(timer);}
}
function connection(label, tone) { $("connection-label").textContent = label; $("connection").className = `status-pill ${tone}`; $("session-state").textContent = label; }
function loggedOut(message) {
  token = ""; session++; dataCache = null; currentReport = null; predictionRows = [];
  $("workspace").hidden = true; $("login").hidden = false; $("report").textContent = "No assessment yet.";
  $("login-error").textContent = message || ""; $("login-error").hidden = !message; $("export").disabled = true;
  document.querySelectorAll("dialog[open]").forEach(d => d.close());
}
function decision(report) {
  const generic = report?.evaluation?.decision, risk = report?.fraud_evaluation?.status;
  if (generic === "FAIL" || risk === "FAIL") return "FAIL";
  if (generic === "PASS" && (!risk || risk === "PASS")) return "PASS";
  return "UNKNOWN";
}
function stat(id, text, tone = "") { const e = $(id); e.textContent = text; e.className = `${tone}${text.length > 13 ? " long-value" : ""}`.trim(); }
function metrics(container, entries) {
  const target = $(container); target.replaceChildren();
  for (const [label, value, hint, tone] of entries) {
    const block = make("div", "metric"); block.append(make("span", "", label), make("strong", tone || "", value));
    if (hint) block.append(make("small", "", hint)); target.append(block);
  }
}
function overview(data, r) {
  const c = data.configuration || {}, w = data.worker, s = r?.ingestion?.summary, quality = decision(r);
  const probes = Object.entries(w?.services || {}), down = probes.some(([,p]) => p.state !== "UP");
  const fresh = Boolean(w && !data.stale && !["ERROR","STOPPED"].includes(w.state));
  const ops = w?.operations?.[selectedVersion] || r?.operational_health;
  const total = s?.available_prediction_count || 0;
  stat("count", count(total));
  $("coverage").textContent = s ? `${count(s.matched_count)} confirmed · ${percent(s.outcome_coverage)} coverage` : `No observations in the ${duration(c.quality_window_seconds || 604800)} evaluation window`;
  stat("decision", quality === "PASS" ? "Checks passed" : quality === "FAIL" ? "Needs attention" : total ? "Awaiting outcomes" : "Waiting for data", quality === "PASS" ? "good" : quality === "FAIL" ? "bad" : "");
  $("recommendation").textContent = quality === "PASS" ? "Current evidence meets the configured gates" : quality === "FAIL" ? "Review the measured quality checks" : "Verified outcomes enable correctness scores";
  const baseline = data.baselines?.[selectedVersion], drift = r?.drift;
  const shifted = drift?.status === "DETECTED" || r?.feature_drift?.status === "DETECTED";
  stat("drift", shifted ? "Shift detected" : drift?.status === "STABLE" ? "No shift detected" : baseline ? "Collecting data" : "Building reference", shifted ? "bad" : drift?.status === "STABLE" ? "good" : "");
  $("drift-note").textContent = shifted ? "The current data differs from its reference" : baseline ? `${count(drift?.current_samples)} later observations · ${count(drift?.reference_samples)} reference` : `${count(c.min_distribution_samples || 100)} predictions and ${c.baseline_warmup_seconds ?? 60}s warm-up needed`;
  stat("op-latency", ops?.latency_p95_ms == null ? "—" : `${number(ops.latency_p95_ms, 1)} ms`);
  $("hero-title").textContent = !fresh ? "Monitoring needs a closer look" : down ? "A connected service needs attention" : quality === "FAIL" ? "A model check needs your attention" : ops?.status === "DEGRADED" ? "Serving checks found a change" : !total ? "Ready for your first prediction" : quality === "PASS" ? "Your model checks are looking good" : "Your models are being watched";
  $("hero-description").textContent = !fresh ? "Review the connection and latest activity. Older results are retained so you can inspect the evidence." : down ? "Agent 6 is keeping the evidence. Review the affected service and its recent activity." : quality === "FAIL" ? "The measured evidence crossed a configured quality gate. Explore the evaluation before deciding what comes next." : ops?.status === "DEGRADED" ? "Inference, errors or input checks crossed a configured limit. Technical health is separate from prediction correctness." : !total ? "Your application is connected to monitoring. Make a prediction there, and Agent 6 will pick it up automatically." : quality === "PASS" ? "The available confirmed outcomes meet your configured checks. Monitoring continues as new evidence arrives." : "Predictions are arriving automatically. Quality scores will become available as verified business outcomes arrive.";
  $("service-list").replaceChildren();
  for (const [name, p] of probes) {
    const row = make("div", "service-row"), symbol = make("span", "service-icon"), text = make("div"); symbol.append(icon("server"));
    text.append(make("strong", "", name === "fraud-api" ? "Fraud prediction API" : name === "fraud-application" ? "Fraud application" : name), make("p", "", p.checked_at ? `Checked ${time(p.checked_at)}` : "Waiting for a health check"));
    row.append(symbol, text, badge(p.state === "UP" ? "Online" : "Unavailable", p.state === "UP" ? "success" : "danger")); $("service-list").append(row);
  }
  if (!probes.length) $("service-list").append(make("p", "caption", "Health probes have not reported yet."));
  setBadge("services-summary", probes.length ? down ? "Needs attention" : "Services online" : "Checking", probes.length ? down ? "warning" : "success" : "neutral");
  $("worker").textContent = !fresh ? "Monitor needs attention" : w.state === "WAITING_FOR_PREDICTIONS" ? "Monitor ready · waiting for predictions" : "Monitor running";
  let step = ["Start in your application", "Make your first prediction", "Use the connected fraud application. Agent 6 will record its predictions automatically.", "predictions"];
  if (!fresh || down) step = ["Check the connection", "Review the latest activity", "Inspect the service checks and recent activity to understand what interrupted monitoring.", "evidence"];
  else if (quality === "FAIL") step = ["Investigate the measured change", "Review the quality evidence", "See which checks failed, how much evidence supports them, and whether a candidate is being prepared.", "quality"];
  else if (ops?.status === "DEGRADED") step = ["Check serving performance", "Understand the serving change", "Review inference failures, response times and input quality before drawing conclusions about accuracy.", "quality"];
  else if (total && quality !== "PASS") step = ["Let real outcomes arrive", "Connect confirmed business results", "Predictions alone cannot prove correctness. Your business system should supply the verified outcomes; Agent 6 matches them automatically.", "evidence"];
  else if (quality === "PASS") step = ["Continue monitoring", "Keep an eye on meaningful changes", "Use your application normally. Agent 6 watches new predictions, evaluates available evidence, and prepares eligible improvements.", "lifecycle"];
  $("next-step-kicker").textContent = step[0]; $("next-step-title").textContent = step[1]; $("next-step-description").textContent = step[2];
  $("next-step-action").dataset.go = step[3]; $("next-step-action").replaceChildren(document.createTextNode(step[3] === "evidence" ? "View connection & activity" : step[3] === "quality" ? "Explore model quality" : step[3] === "lifecycle" ? "View lifecycle" : "See prediction activity"), icon("arrow"));
  $("op-errors").textContent = percent(ops?.failure_rate); $("op-inputs").textContent = percent(ops?.input_issue_rate);
  setBadge("op-status", ops?.status === "HEALTHY" ? "Within limits" : ops?.status === "DEGRADED" ? "Needs attention" : "Collecting data", ops?.status === "HEALTHY" ? "success" : ops?.status === "DEGRADED" ? "warning" : "neutral");
  $("op-detail").textContent = ops ? `${ops.attempts} attempts; ${ops.failures} failed. Telemetry coverage: ${percent(ops.telemetry_coverage)}. ${ops.reasons?.join(", ") || "No configured serving limit crossed."} Serving health does not establish prediction correctness.` : "Use your application normally. Serving checks run without needing confirmed outcomes.";
  $("feature-shift").textContent = r?.feature_drift?.status === "DETECTED" ? "Shift detected" : r?.feature_drift?.status === "STABLE" ? "No shift detected" : "Collecting data";
}
function renderQuality(data, r) {
  const e = r?.evaluation, c = data.configuration || {}, d = decision(r);
  setBadge("quality-status", d === "PASS" ? "Checks passed" : d === "FAIL" ? "Needs attention" : "Awaiting evidence", d === "PASS" ? "success" : d === "FAIL" ? "danger" : "neutral");
  $("assessed").textContent = e ? `Measured on ${count(e.sample_count)} labeled examples · assessed ${dateTime(r.assessed_at)}` : "Confirmed business outcomes will enable these scores.";
  metrics("metrics", [["Precision",percent(e?.metrics?.["binary.precision.v1"]),"How often a fraud flag was correct"],["Recall",percent(e?.metrics?.["binary.recall.v1"]),"How much confirmed fraud was caught"],["F1 score",number(e?.metrics?.["binary.f1.v1"]),"Balance of precision and recall"],["Balanced accuracy",percent(e?.metrics?.["binary.balanced_accuracy.v1"]),"Performance across both classes"]]);
  $("policy").textContent = `Evaluation window: ${duration(c.quality_window_seconds || 604800)}. Minimum ${c.min_samples} labels and ${percent(c.min_outcome_coverage)} coverage. General quality gate: ${c.min_quality_score}. Poll interval: ${c.interval_seconds}s.`;
  $("version").textContent = selectedVersion || "No model version observed yet.";
  $("report").textContent = r ? JSON.stringify(r, null, 2) : "No assessment yet. Serving checks can run before predictions arrive.";
  $("export").disabled = !r; $("baseline").hidden = c.baseline_mode !== "manual"; $("baseline").disabled = !r;
  const f = r?.fraud_evaluation, m = f?.metrics, q = f?.probabilities;
  $("fraud-state").textContent = f ? `${f.status === "FAIL" ? "A fraud-specific gate needs attention." : f.status === "PASS" ? "Supported fraud checks meet the configured limits." : "More confirmed evidence is needed."} Recall target ${percent(f.policy?.min_recall)} · false-positive maximum ${percent(f.policy?.max_false_positive_rate)}.` : "Fraud-specific checks need captured predictions and confirmed outcomes.";
  metrics("fraud-metrics", [["Missed fraud", m ? count(m.false_negatives) : "—", "Confirmed fraud the model allowed"],["False alarms",m ? count(m.false_positives) : "—","Legitimate transactions it flagged"],["Fraud recall",percent(m?.recall),"Among confirmed fraud cases"],["False-positive rate",percent(m?.false_positive_rate),"Among confirmed legitimate cases"]]);
  $("fraud-support").textContent = f ? `${count(m.samples)} verified labels (${count(m.positive_labels)} fraud, ${count(m.negative_labels)} legitimate) · ${percent(f.outcome_coverage)} coverage. ${f.duplicate_transactions_excluded} repeated business transactions excluded; ${f.missing_feature_evidence} records lack subgroup evidence.` : "Results appear once verified business outcomes arrive. Missing evidence is not a failure.";
  $("fraud-cost").textContent = m?.estimated_error_cost != null ? `Estimated error cost: ${number(m.estimated_error_cost,2)} ${f.cost_unit}, or ${number(m.cost_per_1000,2)} per 1,000 labeled transactions. This uses configured constant costs, not measured losses.` : "Error cost is not configured. Both missed-fraud and false-alarm costs are needed before it can be estimated.";
  $("fraud-slices").replaceChildren();
  for (const row of f?.slices || []) {
    const tr = make("tr"), v = row.metrics, interval = v.recall_interval95;
    const label = ({paymentMethod:"Payment method",Category:"Category",accountAgeDays:"Account age",paymentMethodAgeDays:"Payment age"})[row.feature] || row.feature;
    cell(tr, `${label} · ${row.bucket}`); cell(tr, `${v.samples} (${v.positive_labels} fraud)`); cell(tr,v.false_negatives); cell(tr,v.false_positives);
    cell(tr,`${percent(v.recall)}${interval ? ` [${percent(interval[0])}–${percent(interval[1])}]` : ""}`); cell(tr,percent(v.false_positive_rate)); cell(tr,number(v.cost_per_1000,2));
    cell(tr,badge(row.status === "PASS" ? "Passed" : row.status === "FAIL" ? "Review" : "More evidence needed",row.status === "PASS" ? "success" : row.status === "FAIL" ? "warning" : "neutral")); $("fraud-slices").append(tr);
  }
  $("slices-empty").hidden = Boolean(f?.slices?.length);
  setBadge("probability-status", q?.status === "ASSESSED" ? "Evidence assessed" : q?.samples ? "More evidence needed" : "Waiting for scores", q?.status === "ASSESSED" ? "success" : "neutral");
  $("fraud-probability-support").textContent = q ? `${count(q.samples)} scored, confirmed observations · ${percent(q.labeled_probability_coverage)} probability coverage. Small or selectively labeled samples may be biased.` : "Captured scores and confirmed outcomes are needed for this assessment.";
  metrics("probability-metrics", [["Average precision",number(q?.average_precision),"Higher is better"],["ROC AUC",number(q?.roc_auc),"Higher is better"],["Brier score",number(q?.brier_score),"Lower is better"],["Calibration error",number(q?.expected_calibration_error),"Lower is better"]]);
  $("fraud-calibration").replaceChildren();
  for (const bin of q?.calibration_bins || []) {const tr=make("tr"); [ `${percent(bin.lower)}–${percent(bin.upper)}`,count(bin.samples),percent(bin.mean_probability),percent(bin.observed_fraud_rate)].forEach(v=>cell(tr,v)); $("fraud-calibration").append(tr);}
  $("calibration-empty").hidden = Boolean(q?.calibration_bins?.length);
  compareThreshold(); if (currentPage === "quality" && qualityTab === "probability") drawPR(q);
}
function compareThreshold() {
  const q = currentReport?.fraud_evaluation?.probabilities, index = Number($("fraud-threshold").value), point = q?.threshold_comparison?.[index];
  $("threshold-number").textContent = (index/20).toFixed(2); $("threshold-preview").replaceChildren();
  for (const [label,value] of [["Precision",percent(point?.precision)],["Recall",percent(point?.recall)],["False-positive rate",percent(point?.false_positive_rate)],["Cost per 1,000",number(point?.cost_per_1000,2)]]) {const box=make("div"); box.append(make("span","",label),make("strong","",value)); $("threshold-preview").append(box);}
  $("fraud-threshold-result").textContent = !point ? "Capture scored predictions and their confirmed outcomes to explore this trade-off." : q.status !== "ASSESSED" ? "Descriptive preview only. Evidence support is still insufficient." : q.advisory_cost_threshold == null ? "Compared on this evaluation sample. No supported cost recommendation is available." : `Advisory cost threshold: ${q.advisory_cost_threshold.toFixed(2)}. Validate independently before making a change.`;
}
function drawPR(q) {
  const canvas=$("fraud-pr"),ctx=canvas.getContext("2d"); if (!ctx) return;
  const width=Math.max(280,Math.round(canvas.getBoundingClientRect().width)),height=Math.round(width*300/720),ratio=window.devicePixelRatio || 1;
  canvas.width=Math.round(width*ratio);canvas.height=Math.round(height*ratio);ctx.setTransform(ratio,0,0,ratio,0,0);
  const left=43,right=width-19,top=23,bottom=height-35;
  ctx.clearRect(0,0,width,height);ctx.font="12px Segoe UI, sans-serif";ctx.lineWidth=1;
  for(let i=0;i<=4;i++){const y=bottom-(bottom-top)*i/4;ctx.strokeStyle="#eceef6";ctx.beginPath();ctx.moveTo(left,y);ctx.lineTo(right,y);ctx.stroke();ctx.fillStyle="#78849a";ctx.fillText(`${i*25}%`,5,y+4);const x=left+(right-left)*i/4;ctx.fillText(`${i*25}%`,x-12,height-17);}
  ctx.fillStyle="#78849a";ctx.fillText("Recall",width/2-12,height-1);ctx.fillText("Precision",left,13);
  const points=(q?.precision_recall_curve || []).filter(p=>p.precision!=null && p.recall!=null);
  if (!points.length) {ctx.fillStyle="#8792ab";ctx.textAlign="center";ctx.fillText("Waiting for confirmed scores",width/2,height/2);ctx.textAlign="start";}
  else {ctx.beginPath();points.forEach((p,i)=>{const x=left+p.recall*(right-left),y=bottom-p.precision*(bottom-top);if(i===0)ctx.moveTo(x,y);else ctx.lineTo(x,y);});ctx.strokeStyle="#6864df";ctx.lineWidth=2.5;ctx.stroke();const last=points[points.length-1],first=points[0];ctx.lineTo(left+last.recall*(right-left),bottom);ctx.lineTo(left+first.recall*(right-left),bottom);ctx.closePath();const fill=ctx.createLinearGradient(0,top,0,bottom);fill.addColorStop(0,"#7067e621");fill.addColorStop(1,"#7067e604");ctx.fillStyle=fill;ctx.fill();}
  $("pr-description").textContent = !points.length ? "No scored, confirmed observations to plot yet." : `${count(q.samples)} labeled scores. ${q.status !== "ASSESSED" ? "Evidence support is insufficient; treat this curve as descriptive." : "Curves describe this sample, not guaranteed future performance."}`;
}
function benchmarks(data) {
  const a=data.assurance || {},jobs=a.benchmarks || [], seen=new Set();
  const key=JSON.stringify([a.error,a.benchmark_interval_seconds,jobs]);if(key===benchmarkKey)return;benchmarkKey=key;
  $("benchmark-results").replaceChildren();
  $("benchmark-status").textContent = a.error || (jobs.length ? `Rechecked automatically every ${duration(a.benchmark_interval_seconds || 3600)}.` : "No historical benchmark has completed yet.");
  for (const job of jobs) {
    if(seen.has(job.model_version))continue;seen.add(job.model_version);
    const card=make("article","panel benchmark-card"), heading=make("div","panel-heading"),title=make("div","benchmark-title"),symbol=make("span"),name=make("div");symbol.append(icon("shield"));name.append(make("h2","",job.artifact_name),make("p","",`Version ${shortId(job.model_version)}`));title.append(symbol,name);heading.append(title,badge(job.status === "COMPLETED" ? "Evaluated" : job.status.replaceAll("_"," "),job.status === "COMPLETED" ? "success" : job.status === "ERROR" ? "danger" : "neutral"));card.append(heading);
    const r=job.report;
    if(r){const all=r.evaluations?.ALL, score=make("div","score-overview"), description=make("div");description.append(make("span","","Historical F1 score"),make("strong","",number(all?.metrics?.["binary.f1.v1"])));score.append(description,badge("Historical data only","neutral"));card.append(score);
      const wrap=make("div","table-scroll"),table=make("table"),head=make("thead"),hr=make("tr");for(const label of ["Group","Examples","Fraud labels","Policy","Precision","Recall","F1 score","Balanced accuracy"])hr.append(make("th","",label));head.append(hr);table.append(head);const body=make("tbody");
      for(const [group,e] of Object.entries(r.evaluations || {})){const row=make("tr");cell(row,group);cell(row,count(e.sample_count));cell(row,r.label_counts?.[group]?.[1] ?? "—");cell(row,badge(e.decision === "PASS" ? "Passed" : e.decision === "FAIL" ? "Review" : "More evidence",e.decision === "PASS" ? "success" : e.decision === "FAIL" ? "warning" : "neutral"));for(const metric of ["precision","recall","f1","balanced_accuracy"])cell(row,metric === "f1" ? number(e.metrics?.[`binary.${metric}.v1`]) : percent(e.metrics?.[`binary.${metric}.v1`]));body.append(row);}table.append(body);wrap.append(table);card.append(wrap);
      card.append(make("p","benchmark-warning","Small groups can give unstable results. Training independence is unverified; these scores do not establish live accuracy."));const details=make("details","disclosure");details.append(make("summary","","Dataset & evidence"),make("p","",`Evaluated ${dateTime(r.assessed_at)}. Label source: ${r.label_source}. Independence: ${r.training_independence}.`),make("code","break-code",`${job.model_version}\nEvidence: sha256:${job.report_ref}`));card.append(details);
    }else card.append(make("p","caption",job.error || "This benchmark is waiting for evidence or a scheduled check."));$("benchmark-results").append(card);
  }
}
function insights(data) {
  const advisor=data.assurance?.advisor || {status:"DISABLED"}, explanation=advisor.report?.explanation;
  const key=JSON.stringify(advisor);if(key===insightKey)return;insightKey=key;
  const labels={COMPLETED:"Analysis ready",DISABLED:"Advisor disabled",NOT_CONFIGURED:"Not connected",WAITING_FOR_EVIDENCE:"Waiting for evidence",ERROR:"Advisor unavailable",FAILED:"Advisor unavailable",RUNNING:"Reviewing evidence"};
  setBadge("advisor-status",labels[advisor.status] || advisor.status.replaceAll("_"," "),advisor.status === "COMPLETED" ? "success" : ["ERROR","FAILED"].includes(advisor.status) ? "warning" : "neutral");
  $("advisor-detail").textContent=advisor.error || advisor.detail || (explanation ? "Based on the measured evidence available to this assessment." : advisor.status === "DISABLED" ? "Optional explanations are off. Measured monitoring continues automatically." : "The advisor needs useful evidence before it can identify investigation steps.");
  $("advisor-summary").textContent=displayText(explanation?.summary || "No explanation available yet.");$("advisor-findings").replaceChildren();
  (explanation?.findings || []).forEach((finding,index)=>{const card=make("div","insight-finding"),body=make("div");body.append(make("p","",displayText(finding.text)));const refs=make("details","disclosure");refs.append(make("summary","","Supporting evidence"),make("code","break-code",finding.evidence_ids.join("\n")));body.append(refs);card.append(make("span","insight-number",String(index+1).padStart(2,"0")),body);$("advisor-findings").append(card);});
}
const jobLabels={QUEUED:"Queued",TRAINING:"Training candidate",REJECTED:"Candidate rejected",FAILED:"Preparation failed",AWAITING_REVIEW:"Review needed",DEPLOYMENT_READY:"Handoff queued",DEPLOYED:"Deployment confirmed",ROLLBACK_REQUESTED:"Rollback requested",ROLLED_BACK:"Restoration confirmed",ROLLBACK_FAILED:"Restoration failed",EXPIRED:"Request expired",DEPLOYMENT_FAILED:"Deployment failed"};
function lifecycle(data) {
  const l=data.lifecycle || {}, jobs=l.jobs || [], latest=jobs[0];
  setBadge("lifecycle-mode",l.enabled ? "Automatic" : "Disabled",l.enabled ? "lavender" : "neutral");
  $("lifecycle-status").textContent=l.error || (l.enabled ? `Candidate preparation needs at least ${l.min_verified_labels} verified labels, class support, and an eligible quality incident.` : "Automatic candidate preparation is disabled.");
  const states=[["pulse","Observe", data.worker && !data.stale && !["ERROR","STOPPED"].includes(data.worker.state) ? "Monitoring active" : "Waiting for monitor",Boolean(data.worker && !data.stale && !["ERROR","STOPPED"].includes(data.worker.state))],["shield","Evaluate",currentReport?.evaluation ? "Evidence assessed" : "Awaiting outcomes",Boolean(currentReport?.evaluation)],["flow","Prepare",latest ? jobLabels[latest.status] || latest.status : "No eligible incident",Boolean(latest)],["check","Handoff",latest?.status === "DEPLOYED" ? "Deployment confirmed" : latest?.status === "DEPLOYMENT_READY" ? "Queued for deployment" : "Not requested",["DEPLOYED","DEPLOYMENT_READY"].includes(latest?.status)]];
  $("lifecycle-stages").replaceChildren();for(const [symbol,title,status,active] of states){const box=make("div",`lifecycle-stage${active ? " active" : ""}`),ico=make("span","stage-icon");ico.append(icon(symbol));box.append(ico,make("strong","",title),make("p","",status));$("lifecycle-stages").append(box);}
  $("job-count").textContent=jobs.length ? `${jobs.length} recorded ${jobs.length === 1 ? "job" : "jobs"}` : "No jobs yet";
  const pending=jobs.filter(j=>j.status === "AWAITING_REVIEW").length;$("review-nav-count").hidden=!pending;$("review-nav-count").textContent=count(pending);
  const key=JSON.stringify(jobs);if(key===jobKey)return;jobKey=key;
  const target=$("lifecycle-jobs");target.replaceChildren();
  if(!jobs.length){const card=make("article","panel"),empty=make("div","empty-state"),symbol=make("span","empty-icon");symbol.append(icon("flow"));empty.append(symbol,make("h3","","No candidate needs preparation"),make("p","","Agent 6 keeps watching. An eligible quality incident with enough verified evidence can start candidate preparation automatically."));card.append(empty);target.append(card);}
  for(const job of jobs){const card=make("article","panel job-card"),head=make("div","panel-heading"),title=make("div");title.append(make("h2","",jobLabels[job.status] || job.status),make("p","",`${namedModel(job.model_version)} · ${dateTime(job.created_at)}`));head.append(title,badge(jobLabels[job.status] || job.status,["FAILED","ROLLBACK_FAILED"].includes(job.status) ? "danger" : job.status === "AWAITING_REVIEW" ? "warning" : ["DEPLOYED","ROLLED_BACK"].includes(job.status) ? "success" : "neutral"));card.append(head,make("p","",displayText(job.rationale || job.error || "Candidate preparation is queued or in progress.")));
    if(job.status === "AWAITING_REVIEW"){const actions=make("div","job-actions");if(job.guard_origin !== "ISOLATED_VERIFICATION_FIXTURES"){const approve=make("button","button primary","Review approval");approve.onclick=()=>openReview(job,"APPROVE");actions.append(approve);}const reject=make("button","button secondary","Reject candidate");reject.onclick=()=>openReview(job,"REJECT");actions.append(reject);card.append(actions);if(job.guard_origin === "ISOLATED_VERIFICATION_FIXTURES")card.append(make("p","caption","Verification fixture: this candidate cannot authorize deployment."));}
    if(job.candidate_sha256){const details=make("details","disclosure");details.append(make("summary","","Exact candidate & evaluation"),make("code","break-code",`Candidate: sha256:${job.candidate_sha256}\nEvaluation: ${job.evaluation_ref || "pending"}\nModel: ${job.model_version}`));card.append(details);}target.append(card);}
}
const alertNames={QUALITY_FAILURE:"Model quality gate failed",FRAUD_RISK:"Fraud risk gate needs attention",PREDICTION_SHIFT:"Prediction distribution changed",FEATURE_SHIFT:"Input distribution changed",SERVICE_DOWN:"Service became unavailable",SERVICE_RECOVERED:"Service connection recovered",SERVING_DEGRADED:"Serving performance changed",OPERATIONAL_FAILURE:"Serving performance changed"};
function alertRow(alert) {const row=make("div","activity-item"),symbol=make("span","activity-icon"),text=make("div");symbol.append(icon("info"));text.append(make("strong","",alertNames[alert.kind] || alert.kind.replaceAll("_"," ").toLowerCase()),make("p","",`${namedModel(alert.model_version)} · recorded evidence available`));row.append(symbol,text,make("time","",time(alert.created)));return row;}
function activity(data) {
  const alerts=data.alerts || [];$("alerts").replaceChildren();$("attention-list").replaceChildren();
  for(const a of alerts)$("alerts").append(alertRow(a));
  if(!alerts.length){const empty=make("div","attention-empty"),symbol=make("span");symbol.append(icon("check"));const text=make("div");text.append(make("strong","","No monitoring alerts recorded"),make("p","","New threshold events will appear here as the model is observed."));empty.append(symbol,text);$("alerts").append(empty);}
  const r=currentReport, actionable=decision(r) === "FAIL" || r?.operational_health?.status === "DEGRADED" || data.stale || data.worker?.state === "ERROR" || Object.values(data.worker?.services || {}).some(p=>p.state !== "UP");
  if(actionable){const row=make("div","attention-item"),symbol=make("span","activity-icon"),body=make("div"),button=make("button","text-button","Inspect evidence");symbol.append(icon("info"));body.append(make("strong","",data.stale ? "The monitoring heartbeat is stale" : "Current checks need a closer look"),make("p","",data.stale ? "The visible assessment may be older than the current application state." : "Review the quality and serving checks before deciding what to change."));button.onclick=()=>go("quality");row.append(symbol,body,button);$("attention-list").append(row);}
  else {const empty=make("div","attention-empty"),symbol=make("span"),body=make("div");symbol.append(icon("check"));body.append(make("strong","","No active threshold issue detected"),make("p","",decision(r) === "UNKNOWN" ? "Correctness is still waiting for enough verified evidence." : "Keep using your application. Agent 6 will watch for changes."));empty.append(symbol,body);$("attention-list").append(empty);}
  const connector=data.configuration?.outcome_connector,feed=data.worker?.outcome_feed;
  setBadge("feed-state",feed?.state === "CONNECTED" ? "Connected" : feed?.state === "ERROR" ? "Needs attention" : connector?.enabled ? "Ready" : "Not configured",feed?.state === "CONNECTED" ? "success" : feed?.state === "ERROR" ? "warning" : "neutral");
  $("connector").textContent=feed?.error || (connector?.database_configured ? feed?.state === "CONNECTED" ? `Automatic outcome collection is connected. Checkpoint: ${feed.last_sequence || 0}. Verified confirmations are matched automatically.` : "An outcome database is configured. Waiting for a successful collection cycle." : connector?.enabled ? "The trusted outcome endpoint is ready. Your business system must supply verified results before live correctness can be assessed." : "No outcome source is configured. Serving and distribution checks can continue; prediction correctness needs a verified source.");
}
function renderPredictions() {
  const search=$("prediction-search").value.trim().toLowerCase(),filter=$("prediction-filter").value;
  const key=JSON.stringify([predictionRows,search,filter,selectedVersion,dataCache?.configuration?.manual_review_enabled]);if(key===predictionKey)return;predictionKey=key;
  const rows=predictionRows.filter(r=>(!search || r.observation_id.toLowerCase().includes(search)) && (filter === "all" || filter === "pending" && r.actual == null || filter === "fraud" && r.predicted === 1 || filter === "legitimate" && r.predicted === 0));
  $("predictions").replaceChildren();$("prediction-empty").hidden=Boolean(predictionRows.length);$("prediction-table").hidden=!rows.length;$("prediction-filter-empty").hidden=!predictionRows.length || Boolean(rows.length);
  $("prediction-results-count").textContent=`${count(rows.length)} ${rows.length === 1 ? "observation" : "observations"}${search || filter !== "all" ? ` of ${count(predictionRows.length)}` : ""}`;
  for(const item of rows){const tr=make("tr");const observed=cell(tr,time(item.observed_at));observed.title=dateTime(item.observed_at);const id=make("span","observation-id",`${item.observation_id.slice(0,8)}…`);id.title=item.observation_id;const copy=make("button","icon-button");copy.type="button";copy.setAttribute("aria-label",`Copy observation ${item.observation_id}`);copy.append(icon("copy"));copy.onclick=()=>copyText(item.observation_id);id.append(copy);cell(tr,id);cell(tr,badge(item.predicted === 1 ? "Fraud" : "Legitimate",item.predicted === 1 ? "warning" : "neutral"));const prob=cell(tr,percent(item.positive_probability));prob.title=item.positive_probability == null ? "Not captured: legacy record or unsupported probability adapter" : `Estimated fraud probability; not confirmed correctness. Actual threshold: ${number(item.decision_threshold)}`;cell(tr,badge(item.actual == null ? "Awaiting outcome" : item.actual === 1 ? "Confirmed fraud" : "Confirmed legitimate",item.actual == null ? "neutral" : item.actual !== item.predicted ? "warning" : "success"));cell(tr,({SYSTEM:"Business feed",HUMAN:"Verified review",SIMULATED:"Simulated"})[item.outcome_kind] || "Not confirmed");const action=cell(tr,"");if(dataCache?.configuration?.manual_review_enabled && item.actual == null){const button=make("button","text-button","Review");button.onclick=()=>{go("evidence");$("manual-review").closest("details").open=true;$("observation").value=item.observation_id;$("actual").value="";$("verified").checked=false;$("note").focus();};action.append(button);}$("predictions").append(tr);}
}
async function copyText(value) {try {await navigator.clipboard.writeText(value);toast("Observation ID copied");}catch {toast("Clipboard unavailable. The full ID is available in the assessment evidence.");}}
function render(data) {
  dataCache=data;const c=data.configuration || {},w=data.worker;
  let applicationURL = null;
  try {const candidate = new URL(c.application_url); if (["http:", "https:"].includes(candidate.protocol)) applicationURL = candidate.href;} catch {}
  $("open-application").hidden = !applicationURL;
  if (applicationURL) $("open-application").href = applicationURL; else $("open-application").removeAttribute("href");
  $("hero-predictions-action").className = applicationURL ? "text-button" : "button primary";

  const names=new Map((data.assurance?.benchmarks || []).map(j=>[j.model_version,j.artifact_name]));
  const active=w?.active_versions || [], versions=[...new Set([...active,...Object.keys(w?.operations || {}),...names.keys()])];
  if(!versions.includes(selectedVersion))selectedVersion=versions[0] || "";
  currentReport=(data.reports || []).find(r=>r.model_version === selectedVersion) || null;
  const key=JSON.stringify(versions.map(v=>[v,names.get(v)]));
  if(key !== selectionKey){selectionKey=key;$("model-select").replaceChildren();if(!versions.length)$("model-select").append(new Option("Waiting for models",""));for(const v of versions)$("model-select").append(new Option(names.get(v) || `Model ${shortId(v)}`,v));}
  $("model-select").value=selectedVersion;$("heartbeat").textContent=w ? `Updated ${time(w.polled_at)}` : "Waiting for first update";
  connection(data.stale ? "Update overdue" : w?.state === "ERROR" ? "Monitor needs attention" : w?.state === "STOPPED" ? "Monitor stopped" : "Live connection",data.stale || ["ERROR","STOPPED"].includes(w?.state) ? "warning" : "success");
  error(w?.state === "WAITING_FOR_SOURCE" ? null : w?.error || (data.stale ? "The monitoring heartbeat is stale. These results may be historical." : null));
  const fixture=c.scope?.tenant_id === "verification" || c.demo_mode;$("profile").hidden=!fixture;$("profile").textContent=c.scope?.tenant_id === "verification" ? "Verification session · real model inference with historical replayed outcomes. These are not production confirmations." : "Demonstration session · scripted outcomes are marked simulated.";
  $("simulation-control").hidden=!c.demo_mode;$("manual-review").hidden=!c.manual_review_enabled;
  overview(data,currentReport);renderQuality(data,currentReport);benchmarks(data);insights(data);lifecycle(data);activity(data);
  const total=currentReport?.ingestion?.summary?.available_prediction_count || 0;$("prediction-nav-count").hidden=!total;$("prediction-nav-count").textContent=count(total);$("prediction-window").textContent=`${duration(c.quality_window_seconds || 604800)} evaluation window`;$("prediction-summary").textContent=selectedVersion ? `Latest observations from ${namedModel(selectedVersion)}.` : "Waiting for application activity.";
}
async function refresh() {
  if(!token || busy)return;busy=true;const started=session;$("refresh").disabled=true;
  try {const data=await api("/api/status");if(started !== session || !token)return;$("login").hidden=true;$("workspace").hidden=false;$("login-error").hidden=true;render(data);const requestedVersion=selectedVersion;try {const rows=await api("/api/predictions?limit=100"+(requestedVersion ? `&model_version=${encodeURIComponent(requestedVersion)}` : ""));if(started === session && requestedVersion === selectedVersion){predictionRows=rows;renderPredictions();}}catch(err){if(err.status===401)throw err;if(data.worker?.state !== "WAITING_FOR_SOURCE")error("Prediction activity is temporarily unavailable. The last successful observations are retained.");}}
  catch(err){if(started !== session)return;if(err.status===401){loggedOut("The monitoring token was not accepted. Check the token and reconnect.");}else if(dataCache){connection("Connection interrupted","warning");error("Cannot reach the monitoring API. Your last successful results are retained; reconnecting automatically.");}else{$("login-error").textContent="Cannot reach the monitoring service. Check that it is running and try again.";$("login-error").hidden=false;}}
  finally{busy=false;$("refresh").disabled=false;}
}
function openReview(job,action) {activeReview={job_id:job.job_id,candidate_sha256:job.candidate_sha256,action};$("review-title").textContent=action === "APPROVE" ? "Approve this exact candidate?" : "Reject this candidate?";$("review-description").textContent=action === "APPROVE" ? "Approval authorizes a deployment handoff for this candidate. It does not perform the rollout." : "The current serving model is retained. Record the evidence supporting rejection.";$("review-identity").textContent=`sha256:${job.candidate_sha256}`;$("candidate-note").value="";$("candidate-review-error").hidden=true;$("candidate-review-submit").textContent=action === "APPROVE" ? "Approve handoff" : "Reject candidate";$("review-dialog").showModal();}
document.querySelectorAll("[data-page]").forEach(b=>b.addEventListener("click",()=>go(b.dataset.page)));
document.querySelectorAll(".brand").forEach(a=>a.addEventListener("click",event=>{if(dataCache){event.preventDefault();go("overview");}}));
document.querySelectorAll("[data-go]").forEach(b=>b.addEventListener("click",()=>go(b.dataset.go)));
document.querySelectorAll("[data-close]").forEach(b=>b.addEventListener("click",()=>$(b.dataset.close).close()));
document.querySelectorAll("[data-open-help]").forEach(b=>b.addEventListener("click",()=>$("help-dialog").showModal()));
document.querySelectorAll("[data-tab]").forEach(b=>{b.addEventListener("click",()=>showTab(b.dataset.tab));b.addEventListener("keydown",event=>{const keys=["ArrowLeft","ArrowRight","Home","End"];if(!keys.includes(event.key))return;event.preventDefault();const tabs=[...document.querySelectorAll("[data-tab]")],i=tabs.indexOf(b),next=event.key === "Home" ? 0 : event.key === "End" ? tabs.length-1 : (i+(event.key === "ArrowRight" ? 1 : -1)+tabs.length)%tabs.length;showTab(tabs[next].dataset.tab);tabs[next].focus();});});
$("login-form").onsubmit=e=>{e.preventDefault();session++;token=$("token").value.trim();$("token").value="";refresh();};
$("logout").onclick=()=>loggedOut();$("refresh").onclick=refresh;
$("model-select").onchange=()=>{selectedVersion=$("model-select").value;predictionRows=[];if(dataCache)render(dataCache);renderPredictions();refresh();};
$("prediction-search").oninput=renderPredictions;$("prediction-filter").onchange=renderPredictions;$("fraud-threshold").oninput=compareThreshold;
$("export").onclick=()=>{if(!currentReport)return;const blob=new Blob([JSON.stringify(currentReport,null,2)],{type:"application/json"}),url=URL.createObjectURL(blob),link=make("a");link.href=url;link.download=`agent6-assessment-${shortId(currentReport.model_version)}.json`;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);toast("Assessment report exported");};
$("predict-form").onsubmit=async e=>{e.preventDefault();try{const result=await api("/api/predict","POST",{inputs:JSON.parse($("features").value)});$("prediction-result").textContent=`Prediction recorded · ${result.observation_id}`;toast("Diagnostic prediction recorded");await refresh();}catch(err){error(err instanceof SyntaxError ? "Enter valid transaction JSON in the diagnostic form." : err.message);}};
$("review-form").onsubmit=async e=>{e.preventDefault();try{await api(`/api/outcomes/${encodeURIComponent($("observation").value)}`,"POST",{actual:Number($("actual").value),note:$("note").value,verified:$("verified").checked,kind:dataCache?.configuration?.demo_mode && $("simulated").checked ? "SIMULATED" : "HUMAN"});$("review-form").reset();toast("Verified outcome saved");await refresh();}catch(err){error(err.message);}};
$("candidate-review-form").onsubmit=async e=>{e.preventDefault();if(!activeReview)return;$("candidate-review-submit").disabled=true;try{await api(`/api/lifecycle/${encodeURIComponent(activeReview.job_id)}/review`,"POST",{decision:activeReview.action,candidate_sha256:activeReview.candidate_sha256,note:$("candidate-note").value});$("review-dialog").close();toast("Candidate decision recorded");activeReview=null;await refresh();}catch(err){$("candidate-review-error").textContent=err.message;$("candidate-review-error").hidden=false;}finally{$("candidate-review-submit").disabled=false;}};
$("baseline").onclick=()=>{if(!currentReport)return;baselineVersion=currentReport.model_version;$("baseline-note").value="";$("baseline-error").hidden=true;$("baseline-dialog").showModal();};
$("baseline-form").onsubmit=async e=>{e.preventDefault();if(!baselineVersion)return;try{await api("/api/baselines","POST",{model_version:baselineVersion,note:$("baseline-note").value});$("baseline-dialog").close();toast("Drift reference recorded");await refresh();}catch(err){$("baseline-error").textContent=err.message;$("baseline-error").hidden=false;}};
$("today").textContent=new Date().toLocaleDateString([], {month:"short",day:"numeric",year:"numeric"});showTab("live");go("overview",false);setInterval(refresh,2000);refresh();
