# Which model each agent uses, and why

Status: **chosen and checked, not enabled.** Every LLM role is optional. The
deterministic path (the current default) stays the fallback, and the defense
compares each LLM role against it on the same scenarios before it is kept.

## What was verified (2026-11-08, on the provided free-tier keys)

- **Model names exist:** the Gemini and Groq model lists contain every name below.
- **Tool calling works** for every chosen model: each returned a tool call for a one-function test.
- **Gemini 2.5 is closed to new users.** Calls to `gemini-2.5-flash` return HTTP 404: "no longer available to new users", and the error recommends `gemini-3.8-flash`. The 2.5 names in the earlier draft are withdrawn.
- **Gemini Pro is not available on this key.** `gemini-pro-latest` returns a quota error. Pro needs billing enabled, so Agent 2 uses the newest Flash instead (see below).
- **Groq needs a User-Agent header.** Without one, requests fail with HTTP 403, error code 1010. Any Groq client in the code must send one.

Prices are not verified. The sources disagree and the 3.x prices were not found, so no cost figure is claimed here.

## The roles

| Agent | LLM role | Provider | Model | Why this model | Fallback |
|---|---|---|---|---|---|
| 1 Monitoring | triage: is this symptom real or noise? short summary | Google | `gemini-3.5-flash-lite` | Newest Flash-Lite on the list, tool calling verified. It runs on every symptom, so the cheapest tier fits. The decision is a short classification. | the rule-based detector |
| 2 Diagnosis | investigates with tools, forms and tests hypotheses, asks for more evidence | Google | `gemini-3.8-flash` | Newest Flash on the list, tool calling verified. Pro would be the stronger choice, but this key cannot reach it. The one-call-per-investigation volume keeps cost bounded. **Upgrade to `gemini-pro-latest` once billing is enabled, then re-run the comparison.** | the current rules and retrieval |
| 3 Security and quality | explains and ranks the release findings | Groq | `openai/gpt-oss-20b` | Tool calling verified. Fast and small. Already the default for this role in our code. The release decision stays deterministic. | no rationale sentence, as now |
| 5 Resources | reviews a scaling recommendation against usage history | Groq | `openai/gpt-oss-120b` | Tool calling verified. Judging a number against history needs more reasoning than summarising, at one call per recommendation. The replica bounds stay deterministic. | the current recommendation |
| 6 MLOps | investigates drift causes, writes the review | Google | `gemini-3.5-flash` | Tool calling verified. Mid-tier: reads metric tables and data-quality results and calls tools over them. The promotion decision stays a statistical test. | the current lifecycle policy |
| 4 Deployment | none | — | — | It executes a signed, exact patch. A model must not decide what changes production. | — |
| Verification (Agent 1) | none | — | — | A model must not grade its own change. | — |
| Orchestrator | none | — | — | Routing is a state machine. The spec forbids LLM routing in the MVP. | — |

## Why two providers

Google runs Agents 1, 2 and 6; Groq runs Agents 3 and 5. Different providers and
model families make different mistakes, so when they disagree the disagreement
is informative. That feeds the human escalation rule directly.

## Why these are not larger models

Each role has a narrow decision. A larger model adds cost and latency to every
incident without a decision the smaller one cannot make. Upgrade a role only when
its benchmark shows it is the bottleneck.

## Code that must change before enabling

- `agent1_monitoring/labelers/gemini.py` defaults to `gemini-2.5-flash`, which new users cannot call. It needs the new name.
- Any Groq client must send `User-Agent`.

## What the defense has to show for each role

For every LLM role, run the same labelled scenarios with and without the model:
- correct root cause or decision,
- unsafe proposals (should stay at zero),
- escalation rate,
- latency,
- cost per incident.

A role is kept only if it beats its fallback on these measures. The baseline in
`experiments/baselines/deterministic-v1.md` is the comparison point.

## Verify before enabling

Keep keys in environment variables or in a `.env` file. `.env` is git-ignored in
this repository. Never commit a key or paste one into a document.

Gemini model list:

```powershell
curl "https://generativelanguage.googleapis.com/v1beta/models?key=$env:GEMINI_API_KEY"
```

Groq model list (needs the User-Agent header):

```powershell
curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $env:GROQ_API_KEY" -H "User-Agent: aiops-platform/1.0"
```

## Security note

The keys were pasted into a chat during this check, so treat them as exposed.
Revoke both and create new ones before enabling anything.

## Sources

- Groq models page: https://console.groq.com/docs/models.md (lists `openai/gpt-oss-120b` and `openai/gpt-oss-20b` as production models; Llama entries disputed in third-party sources)
- Groq provider notes: https://docs.cline.bot/provider-config/groq
- Gemini pricing, third-party: https://www.morphllm.com/gemini-api-pricing and https://benchlm.ai/blog/posts/gemini-api-pricing
- Gemini quota page (older snapshot): https://ai.google.dev/gemini-api/docs/quota?hl=zh-cn
