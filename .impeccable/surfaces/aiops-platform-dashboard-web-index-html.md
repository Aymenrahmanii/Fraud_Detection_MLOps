---
version: 1
slug: "aiops-platform-dashboard-web-index-html"
primary_target: "aiops_platform/dashboard/web/index.html"
related_targets: ["aiops_platform/dashboard/web/style.css","aiops_platform/dashboard/web/app.js"]
---

# Surface brief: AIOps Control Room (operator console)

Scope: `aiops_platform/dashboard/web` (index.html, style.css, app.js), the
single-page operator console served by `aiops_platform/dashboard/app.py`.
Visitor mode: **Operate**.

Audience and job: presenter acting as on-call SRE/MLOps operator in a live
demo or project defense, watched by a jury on a laptop or desktop screen.
Task: see what broke and where each workflow is in the loop, judge the exact
proposed change, approve or reject with a comment, show the verified outcome
and the audit trail. Constraints: static files, no build step, no CDN; data
only from the console API (PRODUCT.md).

Memorable moment: the decision sheet. A red-ruled block that states the
exact patch, the risk, why a human is needed, and the two controls.

## Direction contract

THESIS: The closed loop is the layout. Every workflow is a numbered process
set on one rigorous Swiss grid, so a viewer reads position in the loop before
reading any record. Refuses the category default of a dark sidebar, KPI cards
and status pills scattered over tables.

OWN-WORLD: International Typographic Style. Cool white paper ground, near-black
ink, a second neutral layer for the frame; one signal red reserved for "a human
must decide" and for failure; a quiet green for verified. One self-hosted
grotesque at three sizes, rank by weight, case and reversal (black bands with
white type). Tabular figures everywhere. 1px hairlines are the only dividers:
no shadows, no rounded cards, no pills. Monospace only for patches, hashes, IDs.

STORY: The viewer sees the loop's seven numbered stages, sees which workflows
sit at which stage, sees the red decision band when the platform waits for a
human, reads exactly what will change and why, watches approval move the row
through Execute and Verify, and ends on a verified audit chain.

FIRST VIEWPORT: Top: black title band with product name, live loop totals per
stage (01 Detect ... 07 Audit) as a numbered strip, operator identity and
scenario controls. Left 7/12: the workflow register, one row per workflow,
each row carrying the identical 7-cell stage rail (signature move), status,
service, priority, age; selected row reversed, others recede. Right 5/12:
the selected workflow's sheet: when approval is pending, the red-ruled
Decision required block first (action, target, exact patch, risk factors,
triggers, comment, Approve / Reject); then diagnosis, candidates, policy,
agent results, audit timeline with chain verdict.

FORM: Swiss Operating Manual (my grounded candidate 7 of 7, assigned by the
roll). Raises: three type sizes only (timetable); hairline grid only
(design annual); two inks plus one signal red (data sublime); stage rails
registered at identical scale (botanical folio); selection recedes the rest
(streaming wall). Seed key 43f9e1e3. Signature move: the registered 7-step
stage rail on every workflow row, enlarged as the sheet's header.

FINISH: unreviewed and undocumented is unfinished; this build ends with the finish review, the verdict, DESIGN.md, and every shipping raster carrying its provenance
