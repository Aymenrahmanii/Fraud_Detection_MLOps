# Agent 6 workspace

The dashboard is organized around the next meaningful action, with separate views for detailed evidence. All displayed observations and decisions come from the authenticated monitoring API. Empty, insufficient-evidence and disconnected states do not contain invented metrics.

## Where to find things

| View | Purpose |
| --- | --- |
| Overview | Model status, observations, distribution changes, response time, connected services and the next step |
| Predictions | Recent observations, estimated fraud probability, confirmed outcomes, search and outcome filters |
| Model quality | Separate tabs for live checks, probability evaluation and historical benchmarks |
| Lifecycle | Actual preparation stages, candidate jobs and exceptional exact-candidate reviews |
| Insights | Optional advisor explanations with expandable supporting evidence |
| Activity & evidence | Recorded alerts, outcome-feed connection, the full assessment and optional diagnostic tools |

Choose the monitored model above the content. Model versions are never merged into a single score. Historical benchmarks are labeled separately from live correctness; verification sessions keep a visible origin banner. “Awaiting outcomes” means the model is being observed but correctness lacks sufficient independent confirmations.

The Open fraud app action is available when the backend has a configured browser-reachable loopback application URL. It respects custom native application ports. Internal Docker service names and health-check query strings are not presented as public application links. An already running older monitor may need a restart to expose this optional shortcut; the rest of the redesign works with its existing API.

## Interaction and access

- Auto-refresh retains open evidence disclosures and active filters when the underlying data is unchanged.
- Connection interruptions retain the last successful results and clearly label the interruption. Invalid credentials return to the access screen.
- Access tokens remain in memory, are removed from authenticated URL fragments, and are not stored in browser storage. Signing out clears the active session.
- Export report downloads the selected assessment JSON; it does not include the monitoring credential or private environment configuration.
- Probability curves use observed, confirmed scores. The threshold slider is a local comparison only and makes no mutation request.
- Candidate approval/rejection uses a focused dialog, an explicit evidence note and the exact candidate digest. Fixture-origin candidates do not expose an approval action.
- Manual feedback and reference tools remain restricted by backend configuration and authorization. They are tucked into diagnostic/evidence views.
- Mobile navigation scrolls horizontally; wide tables scroll within their cards. Keyboard focus, skip navigation, tab controls, reduced-motion support, text status indicators and labeled forms are included.

## Open the updated design

While the monitor is running, refresh `http://127.0.0.1:8026`. The HTML, CSS and JavaScript are served locally. A page reload clears the memory-only token: reconnect with Agent 6's monitoring token if prompted, or use an authenticated launch link. Stop the existing stack before starting another copy with `run --open-browser`.

## Browser verification

The optional test uses synthetic fixtures on an ephemeral local asset server. API requests and review actions are intercepted within the browser test; they never modify the fraud application's records or authorize a deployment.

```powershell
.\.venv-agent6\Scripts\python.exe -m pip install -e ".[ui-test]"
.\.venv-agent6\Scripts\python.exe -m playwright install chromium
.\.venv-agent6\Scripts\python.exe agent6_mlops_lifecycle/tests/browser/dashboard_smoke.py
```

On Windows with Edge already installed, use `--channel msedge` instead of installing Chromium. The script checks authentication, navigation, model switching, filtering, mobile overflow, chart/threshold behavior, historical benchmarks, persistent disclosures, report download, baseline identity, quality failure, stale data, exact-candidate review, fixture approval restrictions, connection recovery and logout.

Results and screenshots are written to ignored `artifacts/agent6/ui-review/`. Screenshots from this test depict declared verification fixtures, not live production performance. The implementation was checked in headless Edge at desktop and mobile viewport sizes, with no JavaScript runtime errors, and against the existing backend regression suite.
