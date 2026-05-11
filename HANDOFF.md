# HANDOFF.md

Last updated: 2026-05-11

---

## Current state

| Field | Value |
|---|---|
| Branch | `main` |
| Latest commit | `60c341d` — `fix(aoai): make max_tokens + http timeout configurable; raise defaults (#29)` |
| Commits merged | 25 PRs total (c1-c14 + 8 fixes + doc refreshes) |
| Open PR | None |
| Branch protection | Active on `main` |
| Production | **LIVE** at `https://wa-voicenote.bravecliff-0c370c26.swedencentral.azurecontainerapps.io` |
| Twilio webhook URL | Configured — `POST /webhook/whatsapp` |
| End-to-end | Confirmed working for short audio (1 min); user retesting for long audio (3+ min) |

---

## What just changed (5 files)

c14 hotfix: `fix(aoai): make max_tokens + http timeout configurable; raise defaults`

Live 3-minute voice note crashed at AOAI: response truncated mid-string
(`Unterminated string starting at: line 2 column 17`). Root cause:
`max_tokens` was hardcoded to 200 in `aoai_client.py`. A 3-min transcript
alone is ~700 tokens, plus summary + suggested_reply.

Files changed:
- `src/wa_voicenote/aoai_client.py` — `max_tokens` is now a constructor parameter (default 4000). No magic constant in `_build_body`.
- `src/wa_voicenote/config.py` — new `aoai_max_tokens` field with `AOAI_MAX_TOKENS` env var. Validated positive. Default 4000. `http_timeout_seconds` default bumped 45s → 180s.
- `src/wa_voicenote/main.py` — both `AoaiClient` construction paths (api-key and Managed Identity) pass `max_tokens=settings.aoai_max_tokens`.
- `tests/test_aoai_client.py` — assertion for new 4000 default.
- `tests/test_config.py` — 3 new tests (default, override, positive-validator).

Production env vars on Container App (set via `az containerapp update`):
- `AOAI_MAX_TOKENS=4000`
- `HTTP_TIMEOUT_SECONDS=180`

Prod reads from env, not from module defaults. Defaults exist only for local dev / CI.

---

## Build / test / lint state

| Check | Status |
|---|---|
| `make ci` on main | Green |
| GitHub Actions ci.yml | Green |
| GitHub Actions deploy.yml | Green (last run on `60c341d`) |
| Tests | **222 passing**, 3 skipped, **95.37% coverage** (gate 90%) |
| Modules with 100% module coverage | `__init__`, `blob_repo`, `transcoder`, `twilio_signing`, `observability`, `diag` |
| Other module coverage | `state_repo` 92%, `config` 98%, `aoai_client` 90%, `twilio_client` 92%, `main` 97%, `handlers` 94% |
| pre-commit | Green |
| gitleaks | Clean across all commits |
| `hadolint Dockerfile` | Clean |

---

## Live verifications (production endpoints exercised)

| Surface | Status | When |
|---|---|---|
| `GET /health` on Container App | ✓ HTTP 200, 233ms | c14 |
| `GET /diag` with bearer token | ✓ HTTP 200 (aoai ok, blob ok; table ping cosmetic 403) | c14 |
| `POST /webhook/whatsapp` with signed payload | ✓ HTTP 200 | c14 hotfix (signature fix) |
| Real Twilio inbound text | ✓ `event=hint_idle` sent | post-c14 |
| Real Twilio inbound audio (short, ~1 min) | ✓ `event=ack_idle` then `event=ok_proc` — 3 replies sent | post-c14 |
| Real Twilio inbound audio (3 min) | ✗ AOAI truncated JSON; user-facing `msg_llm_error` sent | post-c14, NOW FIXED |
| Real Twilio inbound audio (3 min) RETRY | ⏳ user to retry now |
| Twilio outbound send | ✓ live SID delivered | c11 |
| AOAI gpt-audio-mini | ✓ HTTP 200, valid JSON | c10 |
| App Insights ingest | ✓ structlog + OTel spans visible | c10.5 |

---

## Azure resources live

| Resource | Name | Status |
|---|---|---|
| Resource group | `rg-wa-voicenote` (swedencentral) | Live |
| Storage account | `stwavoicenote` (Standard_LRS, Hot, TLS1.2) | Live |
| Table `convstate`, container `audio-staging` (24h lifecycle) | | Live |
| Log Analytics | `law-wa-voicenote` (PerGB2018, 30d) | Live |
| App Insights | `appi-wa-voicenote` (linked to LAW) | Live |
| AOAI | `aoai-wa-voicenote` + deployment `gpt-audio-mini` v2025-12-15 (GlobalStandard, 30 TPM) | Live |
| Container Apps Environment | `cae-wa-voicenote` | Live |
| Container App | `wa-voicenote` with system MI `25ee4ae8-...fdfa0` | Live, revision `wa-voicenote--0000006`+ |
| Container App FQDN | `https://wa-voicenote.bravecliff-0c370c26.swedencentral.azurecontainerapps.io` | Reachable |
| MI roles | Cognitive Services OpenAI User on AOAI, Storage Table+Blob Data Contributor on Storage | Active |
| Entra app OIDC | `wa-voicenote-github-actions` (app ID `9751e543-...97fc`) | Live |
| Federated credentials | `github-actions-main` (ref=main), `github-actions-pr` (pull_request, vestigial) | Live |
| GitHub Actions secrets | `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID` | Set |
| Budget alert | `budget-wa-voicenote-20eur` €20/mo, 50/80/100% notifications | Active |

---

## Container App env vars (non-secret, in deployment manifest)

| Var | Value | Notes |
|---|---|---|
| `TWILIO_ACCOUNT_SID` | `ACeec...45da` | |
| `TWILIO_FROM` | `whatsapp:+14155238886` | Sandbox sender |
| `TWILIO_ALLOWLIST` | `whatsapp:+34611779374` | Kevin's number |
| `AZURE_OPENAI_ENDPOINT` | `https://aoai-wa-voicenote.openai.azure.com/` | |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-audio-mini` | Was meant to be `gpt-audio-15`; quota 0 forced swap |
| `AZURE_OPENAI_API_VERSION` | `2025-04-01-preview` | |
| `AZURE_STORAGE_ACCOUNT` | `stwavoicenote` | |
| `AZURE_STORAGE_TABLE` | `convstate` | |
| `AZURE_STORAGE_CONTAINER` | `audio-staging` | |
| `LOG_LEVEL` | `INFO` | |
| `ENV_NAME` | `prod` | |
| `OTEL_SERVICE_NAME` | `wa-voicenote-triage` | |
| `CONTEXT_TIMEOUT_SECONDS` | `120` | Passive drop of stale awaiting_context |
| `AOAI_MAX_TOKENS` | `4000` | **NEW** (hotfix) — was implicit 200, broke on 3-min audio |
| `HTTP_TIMEOUT_SECONDS` | `180` | **NEW** (hotfix) — was 45s, too tight for long audio |

Container App secrets (values set via `az containerapp secret set`, never in repo):
- `twilio-auth-token` → `TWILIO_AUTH_TOKEN`
- `aoai-api-key` → `AZURE_OPENAI_API_KEY` (local-dev only; prod should use MI)
- `appinsights-conn` → `APPLICATIONINSIGHTS_CONNECTION_STRING`
- `diag-token` → `DIAG_TOKEN`
- `llm-system-prompt` → `LLM_SYSTEM_PROMPT`

---

## Source files merged (12 modules)

`config.py`, `twilio_signing.py`, `handlers.py`, `state_repo.py`, `blob_repo.py`, `transcoder.py`, `aoai_client.py`, `twilio_client.py`, `observability.py`, `diag.py`, `main.py`, `__init__.py`.

Bicep foundation in `infra/main.bicep` (Storage, AOAI, App Insights, Log Analytics, Container Apps Environment). Container App resource itself is managed imperatively by the deploy workflow + `az containerapp` commands.

---

## What is next

| Priority | Item |
|---|---|
| **NOW** | User to retry the 3-minute voice note; watch logs for full AOAI → 3-replies flow |
| High | Quiet down OpenTelemetry SDK debug noise in console logs (real app logs are buried under QuickPulse exporter chatter) |
| High | Pin an Application Insights workbook / dashboard for at-a-glance observability |
| Medium | Fix `/diag` `storage_table` ping — currently 403s because `get_table_access_policy` needs management-plane permissions that Table Data Contributor lacks. Switch to `list_entities(top=1)` instead. |
| Medium | Address Codex follow-up findings: Contributor RG-scope too broad, dead `pull_request` OIDC subject, role assignment GUID pattern |
| Medium | Open AOAI gpt-audio-1.5 quota support ticket (portal-only) if Kevin wants the upgrade |
| Low | c15 — `.env.example` with all env vars documented |
| Low | c16 — `docs/ARCHITECTURE.md`, `docs/DEPLOY.md`, `docs/CHANGELOG.md` |

---

## Active blockers

None. System is end-to-end live for short audio. 3-min audio retry pending user.

---

## Open decisions

None blocking. Kevin to confirm the 3-min retry works; then we tackle observability polish.

---

## Hardcoding policy reminder

[docs/PLAN.md §10.2](docs/PLAN.md): no literal user-facing strings or magic numbers in `src/wa_voicenote/*.py` business logic. Everything via `config.py` (Pydantic Settings) + env vars. Ruff PLR2004 enforced; `tests/**` exempted. Module-level constants like `_DEFAULT_AOAI_MAX_TOKENS = 4000` are allowed AS Pydantic Settings field defaults — they are env-overridable, and the c14 hotfix proves it (Container App now overrides via `AOAI_MAX_TOKENS=4000`).

---

## How to resume in a fresh session

1. Read [docs/PLAN.md](docs/PLAN.md) end-to-end (source of truth).
2. Read this `HANDOFF.md` for current state.
3. Read [infra/README.md](infra/README.md) for Bicep / Container App split.
4. Run `make ci` on `main` to confirm green starting state.
5. Production is live; webhook URL is configured in Twilio Sandbox.
6. Watch logs:
   ```
   WORKSPACE_ID=$(az monitor log-analytics workspace show -g rg-wa-voicenote -n law-wa-voicenote --query customerId -o tsv)
   az monitor log-analytics query --workspace "$WORKSPACE_ID" \
     --analytics-query "ContainerAppConsoleLogs_CL | where ContainerAppName_s == 'wa-voicenote' | where TimeGenerated > ago(10m) | where Log_s contains 'event' or Log_s contains 'ERROR' or Log_s contains 'POST' | order by TimeGenerated desc | take 30" -o tsv
   ```
7. /diag for live ping (bearer token from `~/.config/wa-voicenote/secrets.env`):
   ```
   curl -H "Authorization: Bearer $DIAG_TOKEN" https://wa-voicenote.bravecliff-0c370c26.swedencentral.azurecontainerapps.io/diag
   ```
