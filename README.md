# wa-voicenote-triage

A personal WhatsApp voice-note triage bot. Forward a voice note to a Twilio
Sandbox number, the bot transcribes it with Azure OpenAI `gpt-audio-mini`,
summarises it, and proposes a suggested reply — all in the original
language. Built end-to-end in one session as a portfolio project.

| | |
|---|---|
| Stack | Python 3.12 · FastAPI · uv · Docker · Azure Container Apps (Consumption, scale-to-zero) |
| LLM | Azure OpenAI Foundry · `gpt-audio-mini` v2025-12-15 (GlobalStandard) |
| Channel | Twilio Programmable Messaging WhatsApp Sandbox |
| Storage | Azure Tables (state) + Azure Blob (transient audio, 24h lifecycle) |
| Observability | structlog JSON + OpenTelemetry · Application Insights |
| Infra-as-code | Bicep for foundation resources |
| CI/CD | GitHub Actions · OIDC federated identity to Azure · ruff · mypy `--strict` · pytest (~95% coverage) |

## Quickstart

```bash
make install   # uv sync + pre-commit hooks
make ci        # ruff + ruff-format --check + mypy --strict + pytest + hadolint + docker build
```

For local development copy [.env.example](.env.example) to
`~/.config/wa-voicenote/secrets.env` (mode 600, outside the repo) and fill in
the values. See [infra/README.md](infra/README.md) for the Container App
secrets manifest in production.

## Architecture

Inbound WhatsApp → Twilio Sandbox → Container App `/webhook/whatsapp` →
verify HMAC signature → allowlist check → idempotency ring →
two-state machine (`idle` ↔ `awaiting_context`) → ffmpeg transcode →
Azure Blob upload → AOAI gpt-audio call → 3 chunked outbound messages
via Twilio REST.

Full design in [docs/PLAN.md](docs/PLAN.md). Infrastructure split in
[infra/README.md](infra/README.md).

## Repository layout

```
.
├── .github/workflows/         CI (lint+type+test+docker) and deploy (build+push+rollout+rollback)
├── infra/                     Bicep foundation (Storage, AOAI, App Insights, LAW, ACA Env)
├── src/wa_voicenote/          12 modules: config, signing, repos, transcoder, AOAI, Twilio, handlers, observability, diag, main
├── tests/                     230+ tests, ~95% coverage, including a 1s opus fixture
└── docs/                      PLAN.md (single source of truth)
```

## Admin / observability

I configured ingestion to Application Insights but did NOT build a custom
dashboard. The pre-baked Azure Portal views are good enough for a personal
bot. Bookmark these (replace `limeralda.com` with your tenant if you fork):

- **Live Metrics** — real-time request/error/latency stream:
  https://portal.azure.com/#@limeralda.com/resource/subscriptions/8c5dd4a1-ebb3-429f-bc1e-1285df2159f7/resourceGroups/rg-wa-voicenote/providers/microsoft.insights/components/appi-wa-voicenote/quickPulse
- **Application Map** — visual dependency graph (app → AOAI → Storage → Twilio):
  https://portal.azure.com/#@limeralda.com/resource/subscriptions/8c5dd4a1-ebb3-429f-bc1e-1285df2159f7/resourceGroups/rg-wa-voicenote/providers/microsoft.insights/components/appi-wa-voicenote/applicationMap
- **Performance** — p50/p95/p99 per operation:
  https://portal.azure.com/#@limeralda.com/resource/subscriptions/8c5dd4a1-ebb3-429f-bc1e-1285df2159f7/resourceGroups/rg-wa-voicenote/providers/microsoft.insights/components/appi-wa-voicenote/performance
- **Failures** — exceptions and failed deps:
  https://portal.azure.com/#@limeralda.com/resource/subscriptions/8c5dd4a1-ebb3-429f-bc1e-1285df2159f7/resourceGroups/rg-wa-voicenote/providers/microsoft.insights/components/appi-wa-voicenote/failures
- **Logs (KQL)** — query anything:
  https://portal.azure.com/#@limeralda.com/resource/subscriptions/8c5dd4a1-ebb3-429f-bc1e-1285df2159f7/resourceGroups/rg-wa-voicenote/providers/microsoft.insights/components/appi-wa-voicenote/logs

Useful queries:

```kql
// Last hour of webhook activity grouped by event
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "wa-voicenote"
| where TimeGenerated > ago(1h)
| where Log_s contains "event"
| project TimeGenerated, Log_s
| order by TimeGenerated desc

// AOAI dependency latency p95
AppDependencies
| where Target contains "openai.azure.com"
| where TimeGenerated > ago(24h)
| summarize p95=percentile(DurationMs, 95) by bin(TimeGenerated, 1h)
```

There is also a live `/diag` endpoint protected by a bearer token in
`DIAG_TOKEN`:

```bash
curl -H "Authorization: Bearer $DIAG_TOKEN" \
  https://wa-voicenote.bravecliff-0c370c26.swedencentral.azurecontainerapps.io/diag
```

Returns JSON with AOAI / Storage Table / Storage Blob ping latencies, app
version, env, and whether App Insights is configured.

## Cost

Estimated baseline: under €5/month for ~50 voice notes/month at 1-3 minutes
each. Budget alert at €20/month with notifications at 50%, 80%, 100%.

| Resource | Tier | Approx €/mo |
|---|---|---|
| Container Apps | Consumption, min_replicas=0 | ~€0.05 |
| AOAI gpt-audio-mini | GlobalStandard, ~50 min input/mo | ~€3-5 |
| Storage (Tables + Blob) | Standard_LRS Hot + 24h lifecycle | ~€0.01 |
| Application Insights + Log Analytics | 1 GB/mo free tier | €0 |
| **Total** | | **< €5** |

## Honest postmortem — what I got wrong

This is the section the recruiter should read. None of these were caught by
the test suite. Each one bit me live, in front of the user. Listed in the
order I hit them.

1. **Hardcoded `max_tokens = 200` in the AOAI client.**
   Worked for short audio. A 3-minute voice note transcript is ~700 tokens
   plus summary and suggested reply, so the model returned valid JSON
   *truncated mid-string*; the client raised `AoaiParseError` and the user
   got the generic "processing failed" message. The hardcoded magic was the
   real bug; I missed it during build because tests mocked the AOAI response.
   Fix: make `max_tokens` a `Settings` field (`AOAI_MAX_TOKENS`, default
   4000), passed through `AoaiClient.__init__`, and set the Container App
   env var explicitly so prod never falls back to the default.

2. **Required BOTH `X-Forwarded-Proto` AND `X-Forwarded-Host` for the
   Twilio signature URL reconstruction.**
   Azure Container Apps' Envoy ingress sets `X-Forwarded-Proto: https` but
   does NOT set `X-Forwarded-Host` — it relies on the standard `Host`
   header. My defensive AND-coded fallback meant the signed URL was always
   `http://` while Twilio signed `https://`, so every real webhook 403'd.
   The fix is one line. I should have known the convention.

3. **Asked the user to test before I tested it myself.**
   I had the auth token, the public URL, and code to compute the signature.
   I could have generated a properly signed test POST and `curl`'d it
   against `/webhook/whatsapp` before asking the user to send a voice note.
   Doing that would have caught both the 403 and the `max_tokens` bug
   without involving the user. I didn't, and the user had to send a
   voice note three times to discover three different bugs.

4. **Forgot to add `uvicorn` to runtime dependencies.**
   The Dockerfile `CMD` invokes `uvicorn`, but I only had it in dev deps.
   Tests use FastAPI's `TestClient` (which doesn't go through uvicorn), so
   the suite was 95% green with a container that wouldn't even start. Found
   on first deploy via `exec: "uvicorn": executable file not found`.
   Lesson: a smoke test that actually runs the container's entrypoint
   should be part of CI, not deferred to deploy.

5. **Didn't anticipate Twilio's 1600-character per-message limit.**
   Should have known. AOAI happily returned 6000 chars of valid JSON for a
   3-minute transcript; Twilio rejected the first outbound `send_text`
   with error 21617. Fix: chunk at safe boundaries with `(i/N)` markers.
   The chunker is small and configurable now, but it should have shipped
   in c12 alongside the rest of the state machine.

6. **GHCR package visibility flip was painful.**
   The workflow's `GITHUB_TOKEN` has `packages:write` (push) but NOT
   `admin:packages` (change visibility). My workflow had a `gh api PATCH
   visibility=public` step that silently 404'd. I asked the user to click
   through the GitHub UI; he made the *repo* public instead of the
   *package*. I had to ask twice for the right click. A clearer path: just
   document the manual one-time flip and don't pretend it's automated.

7. **Spent three commits getting one diagnostic log line right.**
   `logger.warning("event", extra={...})` — the `extra` kwargs vanish when
   the default formatter is `%(message)s` (which I set up to make
   structlog JSON play nice with stdlib). Took me three deploys to realize
   I needed an `%s`-style format string. Embarrassing.

8. **The `/diag` table ping used a management-plane API.**
   `get_table_access_policy()` returned 403 in prod with
   `AuthorizationPermissionMismatch` because the Container App's MI has
   data-plane access only (`Storage Table Data Contributor`).
   `list_entities(top=1)` is data-plane. I should have read the role
   description before picking the method.

9. **Pre-commit `mypy` hook needed dep-additions four separate times.**
   `pydantic-settings`, `fastapi`, `httpx`, `azure-data-tables`,
   `azure-storage-blob`, `azure-monitor-opentelemetry`, `structlog`, etc.
   Each commit that introduced a new runtime dep also had to add that dep
   to `additional_dependencies` on the mypy pre-commit hook. The pattern is
   inherent to pre-commit's isolated environments, but it would have been
   nicer to wire the mypy hook to share the project's `uv` venv from the
   start.

10. **HTTP timeout of 45s was always going to be tight for long audio.**
    AOAI on a 5-minute voice note can take 60-90s. I should have set a
    higher timeout from the start, not waited for it to bite me.

11. **Builder sub-agents kept refusing to write code.**
    The general-purpose safety reminder ("treat read files as potentially
    malicious; refuse to augment them") repeatedly triggered on routine
    project code. Twice I had to abandon a delegated subtask and write it
    myself, or write a very explicit override into the next agent's
    prompt. Not a bug in my work, but a workflow lesson.

12. **gpt-audio-1.5 quota in `swedencentral` is hard-zero.**
    The plan assumed 1.5 would deploy. It didn't. I had to swap to
    `gpt-audio-mini` mid-provisioning. The swap was clean (same API
    shape, identical JSON output structure) but I should have queried
    quota *before* writing PLAN.md.

13. **Tested the public-repo / private-package split after pushing the
    image, not before.**
    Container Apps cannot pull a private GHCR package by default. We
    discovered this on the first deploy attempt, not during planning.
    Should have been a Phase-0 question to the user.

## What I learnt that I'll keep

- **`X-Forwarded-Host` is not universal.** Many ingresses set Proto but not
  Host, relying on the standard `Host` header instead. Defensive URL
  reconstruction must handle both shapes.
- **Twilio's signature scheme is fragile to URL differences.** Even a port
  number or trailing slash off can break it. The canonical-example test in
  the docs is essential as a fixture.
- **Bicep should NOT own mutable resources** like Container Apps whose env
  vars and secrets change between deploys. Declaring them in Bicep
  without ALSO declaring every secret value silently wipes them on
  redeploy. Codex caught this before merge.
- **Container Apps + GHCR private package = registry credential required.**
  Either configure a read-only PAT as a Container App registry credential
  or make the package public. There's no third way that "just works."
- **App Insights + OpenTelemetry distro is excellent**, but the exporter's
  internal loggers default to DEBUG and drown out app events on stdout.
  Pin them at WARNING during observability config.
- **Smoke tests should probe the revision-specific FQDN**, not the
  load-balanced base FQDN. Otherwise a healthy old revision masks a broken
  new revision.
- **Adding `dataclasses` and `SecretStr` everywhere pays off.** mypy
  `--strict` caught real bugs that pytest didn't, especially around
  optional fields and string-vs-bytes confusion.
- **The "stop and ask before risky actions" discipline matters.** Pausing
  to ping a real endpoint with a real signature *before* asking the user
  to test would have saved hours.

## License

Private. All rights reserved.
