# HANDOFF.md

Last updated: 2026-05-11

---

## Current state

| Field | Value |
|---|---|
| Branch | `main` |
| Latest commit | `11134d3` — `fix(handlers): chunk WhatsApp messages over the 1600-char limit (#31)` |
| Commits merged | 27 PRs total (c1-c14 + 10 hotfixes + doc refreshes) |
| Open PR | None |
| Branch protection | Active on `main` |
| Production | **LIVE** at `https://wa-voicenote.bravecliff-0c370c26.swedencentral.azurecontainerapps.io`, revision `wa-voicenote--0000010` |
| Twilio webhook URL | Configured |
| End-to-end | Short audio: ✓ working. Long audio (3+ min): retry pending after chunking fix. |

---

## Live production timeline (today)

| Time | Event | Outcome |
|---|---|---|
| 12:33 | First Twilio webhook from Kevin | 403 — signature mismatch |
| 12:50 | Diagnostic logging deployed | Revealed `X-Forwarded-Host=None` (Container Apps doesn't set it) |
| 12:55 | **Signature fix** — use Host header when X-Forwarded-Host missing | 403s gone |
| 12:58 | Real 1-min audio inbound | ✓ 3 replies sent successfully |
| 13:00 | Real 3-min audio inbound | ✗ AOAI returned truncated JSON (`max_tokens=200` too small) |
| 13:10 | **Max-tokens fix** — `AOAI_MAX_TOKENS=4000`, `HTTP_TIMEOUT_SECONDS=180`, both env-configurable | Deployed |
| 13:22 | Real 3-min audio retry | ✗ AOAI returned valid 4000-token JSON, **Twilio rejected** with error 21617 (>1600 char message) |
| 13:30 | **Chunking fix** — split messages at safe boundaries, prepend `(i/N)` markers | Deployed (current revision) |
| → | Kevin to retry 3-min audio v3 | pending |

---

## What just changed (3 files in last commit)

`fix(handlers): chunk WhatsApp messages over the 1600-char limit`:

- `src/wa_voicenote/handlers.py` — new `_chunk_message(body, max_chars)` module helper; new `WebhookHandler._send_chunked(to, body)` method. Splits at paragraph → line → sentence → space → hard-cut, reserves 16 chars for `(i/N) ` page marker.
- `src/wa_voicenote/config.py` — new `whatsapp_max_chars_per_message` field (default 1500, env var `WHATSAPP_MAX_CHARS_PER_MESSAGE`). Validated positive.
- `tests/test_handlers.py` — 7 new chunker tests, `_StubSettings` gained `whatsapp_max_chars_per_message = 1500`.

Container App env var also set explicitly: `WHATSAPP_MAX_CHARS_PER_MESSAGE=1500`.

---

## Build / test / lint state

| Check | Status |
|---|---|
| `make ci` on main | Green |
| GitHub Actions ci.yml | Green |
| GitHub Actions deploy.yml | Green (last run on `11134d3`) |
| Tests | **229 passing**, 3 skipped, **94.76% coverage** (gate 90%) |
| Modules with 100% module coverage | `__init__`, `blob_repo`, `transcoder`, `twilio_signing`, `observability`, `diag` |
| Other module coverage | `state_repo` 92%, `config` 98%, `aoai_client` 90%, `twilio_client` 92%, `main` 97%, `handlers` 93% |
| pre-commit | Green |
| gitleaks | Clean |
| `hadolint Dockerfile` | Clean |

---

## Container App env vars (all configurable, no hardcoded values in prod)

Non-secret:

| Var | Value | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | `ACeec...45da` | |
| `TWILIO_FROM` | `whatsapp:+14155238886` | Sandbox sender |
| `TWILIO_ALLOWLIST` | `whatsapp:+34611779374` | Kevin |
| `AZURE_OPENAI_ENDPOINT` | `https://aoai-wa-voicenote.openai.azure.com/` | |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-audio-mini` | (gpt-audio-1.5 quota=0 portal request pending) |
| `AZURE_OPENAI_API_VERSION` | `2025-04-01-preview` | |
| `AZURE_STORAGE_ACCOUNT` | `stwavoicenote` | |
| `AZURE_STORAGE_TABLE` | `convstate` | |
| `AZURE_STORAGE_CONTAINER` | `audio-staging` | |
| `LOG_LEVEL` | `INFO` | |
| `ENV_NAME` | `prod` | |
| `OTEL_SERVICE_NAME` | `wa-voicenote-triage` | |
| `CONTEXT_TIMEOUT_SECONDS` | `120` | |
| `AOAI_MAX_TOKENS` | `4000` | c14 hotfix — was implicit 200, truncated 3-min audio JSON |
| `HTTP_TIMEOUT_SECONDS` | `180` | c14 hotfix — was 45s, too tight |
| `WHATSAPP_MAX_CHARS_PER_MESSAGE` | `1500` | c14 hotfix — chunk to avoid Twilio error 21617 |

Container App secrets (values set via `az containerapp secret set`, never in repo):
`twilio-auth-token`, `aoai-api-key`, `appinsights-conn`, `diag-token`, `llm-system-prompt`.

---

## Azure resources live (unchanged)

| Resource | Name |
|---|---|
| Resource group | `rg-wa-voicenote` (swedencentral) |
| Storage | `stwavoicenote` + table `convstate` + container `audio-staging` (24h lifecycle) |
| Log Analytics | `law-wa-voicenote` (PerGB2018, 30d) |
| App Insights | `appi-wa-voicenote` |
| AOAI | `aoai-wa-voicenote` + deployment `gpt-audio-mini` v2025-12-15 (GlobalStandard, 30 TPM) |
| Container Apps Env | `cae-wa-voicenote` |
| Container App | `wa-voicenote`, MI `25ee4ae8-...fdfa0`, revision `wa-voicenote--0000010` |
| Budget | `budget-wa-voicenote-20eur` €20/mo, 50/80/100% notifications |

---

## What is next

| Priority | Item |
|---|---|
| **NOW** | User to retry 3-minute voice note. Expect ACK then chunked transcript `(1/N) (2/N) ...` then summary then suggested reply. |
| High | Quiet OpenTelemetry SDK debug-level chatter in console logs (the QuickPulse exporter floods stdout) |
| High | Pin an Application Insights workbook for at-a-glance observability |
| Medium | Fix `/diag` `storage_table` ping — switch from `get_table_access_policy` (needs management plane) to `list_entities(top=1)` (data plane, already authorized) |
| Medium | Codex follow-up: narrow Contributor scope, remove dead `pull_request` OIDC subject, fix role assignment GUID pattern |
| Medium | Open AOAI gpt-audio-1.5 quota support ticket if Kevin wants the upgrade |
| Low | c15 — `.env.example` with all env vars documented |
| Low | c16 — `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`, `docs/CHANGELOG.md` |

---

## Active blockers

None. System is end-to-end live for short audio. 3-min audio retry pending after chunking fix.

---

## Hardcoding policy reminder

[docs/PLAN.md §10.2](docs/PLAN.md): no literal user-facing strings or magic numbers in `src/wa_voicenote/*.py` business logic. Module-level Pydantic Settings field defaults are explicitly allowed because they are env-overridable. All operational knobs (timeouts, token budgets, message char limits) are now both env-driven AND explicitly set on the Container App, so prod never falls back to a code default.

---

## How to resume in a fresh session

1. Read [docs/PLAN.md](docs/PLAN.md) end-to-end.
2. Read this `HANDOFF.md` for current state.
3. Read [infra/README.md](infra/README.md) for Bicep / Container App split.
4. `make ci` on `main` to confirm green starting state.
5. Watch live logs:
   ```bash
   WORKSPACE_ID=$(az monitor log-analytics workspace show -g rg-wa-voicenote -n law-wa-voicenote --query customerId -o tsv)
   az monitor log-analytics query --workspace "$WORKSPACE_ID" \
     --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'wa-voicenote' | where TimeGenerated > ago(10m) | where Log_s contains 'event' or Log_s contains 'ERROR' or Log_s contains 'POST' | order by TimeGenerated desc | take 30" -o tsv
   ```
6. /diag for live ping (bearer token from `~/.config/wa-voicenote/secrets.env`):
   ```bash
   curl -H "Authorization: Bearer $DIAG_TOKEN" https://wa-voicenote.bravecliff-0c370c26.swedencentral.azurecontainerapps.io/diag
   ```
