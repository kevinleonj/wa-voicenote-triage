# HANDOFF.md

Last updated: 2026-05-11

---

## Current state

| Field | Value |
|---|---|
| Branch | `main` |
| Latest commit | `5576bfa` — `fix(deploy): make GHCR package public during workflow (#23)` |
| Commits merged | 17 PRs in total (c1-c14 + fix + docs refreshes) |
| Open PR | None |
| Branch protection | Active on `main` (status check `lint + type + test + docker`, linear history, no force-push) |
| **BLOCKING** | First production deploy stuck — Container App cannot pull GHCR image because the GHCR package is private and the workflow's `GITHUB_TOKEN` lacks `admin:packages` scope required to flip visibility programmatically. Kevin to flip the package to Public manually one time via the GitHub UI. |

---

## Distance to first WhatsApp voice note

**One Kevin click + one workflow re-run = end-to-end working.**

Steps remaining after Kevin flips GHCR visibility:

1. Re-trigger deploy.yml on main (`gh workflow run deploy.yml --ref main`)
2. Wait for /health smoke to pass on the revision-specific FQDN
3. Set Twilio Sandbox "When a message comes in" webhook URL to `https://wa-voicenote.bravecliff-0c370c26.swedencentral.azurecontainerapps.io/webhook/whatsapp`
4. Kevin sends a real voice note → expects 3 replies (transcript, summary, suggested reply)

After end-to-end works: c15 (.env.example) + c16 (docs) close the plan.

---

## What is merged on main (chronological)

| Commit | Description |
|---|---|
| c1 | uv scaffold (pyproject, ruff PLR2004, mypy --strict, pytest cov 90%, pre-commit hooks) |
| c2 | Docker (multi-stage uv + ffmpeg + non-root + healthcheck), docker-compose, hadolint |
| c3 | CI workflows (ci.yml + deploy.yml skeleton), branch protection on main |
| c4 | Pydantic Settings (28 fields, SecretStr for tokens, lru_cache, ENV_FILE override) |
| c5 | Twilio HMAC signature validation (canonical algorithm, FastAPI dep, X-Forwarded-Proto aware) |
| c6 | Allowlist guard (`is_sender_allowed`) + reactivated AST no-hardcoded-strings check |
| c7 | Azure Tables StateRepo (two-state machine, SID ring dedupe, timezone-aware datetimes) |
| c8 | Azure Blob staging (BlobRepo with phone-hash names, typed errors, idempotent delete) |
| c9 | ffmpeg WAV transcoder (async subprocess, RIFF/WAVE validation, timeout-killed) |
| c10 | AOAI gpt-audio client (two-mode auth, retry-once-on-parse-error, live verified) |
| c10.5 | Observability (structlog JSON + OpenTelemetry FastAPI/httpx + Azure Monitor exporter) |
| c11 | Twilio REST client (Basic auth, retry on 429/5xx, live message sent to Kevin's WhatsApp) |
| c12 | Full state machine in `WebhookHandler` (all transitions, passive timeout, idempotency) |
| c13 | FastAPI app + `/health` + `/webhook/whatsapp` route, lifespan builds shared clients |
| c13.5 | `/diag` endpoint with bearer-token auth, hmac.compare_digest, live AOAI/Storage pings |
| c14 | deploy.yml (build+push+update+rollback) + Bicep foundation (Storage, LAW, AppInsights, AOAI, ACA Env) |
| c14 fix | GHCR package visibility step in workflow (currently a no-op because GITHUB_TOKEN scope insufficient) |

---

## Build / test / lint state

| Check | Status |
|---|---|
| `make ci` on main | Green (lint, format-check, mypy --strict, pytest+cov, hadolint, docker build, trivy) |
| GitHub Actions ci.yml | Green on every PR |
| Tests | 218 passing, 3 skipped, **95.35% coverage** (gate 90%) |
| Modules with 100% module coverage | `__init__`, `blob_repo`, `handlers` 94%, `transcoder`, `twilio_signing`, `observability`, `diag` 99% |
| `state_repo`, `config`, `aoai_client`, `twilio_client`, `main` | 92%, 98%, 90%, 92%, 97% (all above gate) |
| Pre-commit hooks | Green |
| gitleaks | Clean across all commits |
| `uv lock --check` | Clean |
| `hadolint Dockerfile` | Clean |

---

## Source files merged

| File | Purpose |
|---|---|
| `src/wa_voicenote/__init__.py` | Package init, `__version__ = "0.1.0"` |
| `src/wa_voicenote/config.py` | Pydantic Settings (28 fields), `get_settings()` lru_cached |
| `src/wa_voicenote/twilio_signing.py` | `compute_signature`, `is_valid_signature`, `require_valid_twilio_signature` FastAPI dep |
| `src/wa_voicenote/handlers.py` | Full `WebhookHandler` state machine + `InboundMessage` + `is_sender_allowed` |
| `src/wa_voicenote/state_repo.py` | Async Azure Tables StateRepo + SID ring |
| `src/wa_voicenote/blob_repo.py` | Async Azure Blob staging + typed errors |
| `src/wa_voicenote/transcoder.py` | Async ffmpeg subprocess → WAV PCM16 16kHz mono |
| `src/wa_voicenote/aoai_client.py` | Async AOAI client (api-key or token_provider) with parse retry |
| `src/wa_voicenote/twilio_client.py` | Async Twilio REST client (Basic auth, retry 429/5xx) |
| `src/wa_voicenote/observability.py` | structlog + OTel + Azure Monitor wiring (idempotent) |
| `src/wa_voicenote/diag.py` | `/diag` endpoint helpers + ping functions for AOAI/Tables/Blob |
| `src/wa_voicenote/main.py` | FastAPI app + lifespan + route registration |

---

## Live verifications (production endpoints already exercised)

| Surface | Result | When |
|---|---|---|
| Twilio account (REST API) | HTTP 200 — account active, Full tier | c0 |
| Twilio Sandbox join | Confirmed — `join frighten-therefore` accepted from `whatsapp:+34611779374` | c0 |
| Twilio outbound send | HTTP 201 — SID `SM4ded9442e3168b47961a16e4786dc93c` delivered to Kevin's phone | c11 |
| Azure CLI authentication | `kevin@limeralda.com`, sub `8c5dd4a1-...2159f7`, tenant `bac379d9-...c40e` | c0 |
| AOAI gpt-audio-mini Chat Completions | HTTP 200 — valid JSON with transcript/summary/suggested_reply keys, 1.5s latency | c10 |
| Application Insights ingest | OTel span + custom properties visible in Log Analytics workspace, AppRoleName=`wa-voicenote-triage` | c10.5 |
| GHCR image push | Built and pushed `ghcr.io/kevinleonj/wa-voicenote-triage:<sha>` and `:latest` | c14 |
| `az containerapp create` | Container App `wa-voicenote` live at FQDN | c14 |
| `az containerapp update` (first deploy) | **FAILED** — UNAUTHORIZED at GHCR pull (private package) | c14 |
| `/health` endpoint | Not yet reachable (container running placeholder image) | pending |
| `/webhook/whatsapp` | Not yet reachable | pending |

---

## Azure resources live

| Resource | Name | Status |
|---|---|---|
| Resource group | `rg-wa-voicenote` (swedencentral) | Live |
| Storage account | `stwavoicenote` (Standard_LRS, Hot, TLS1.2, no public blob) | Live |
| Table | `convstate` | Live |
| Blob container | `audio-staging` (24h lifecycle delete) | Live |
| Log Analytics workspace | `law-wa-voicenote` (PerGB2018, 30-day retention) | Live |
| Application Insights | `appi-wa-voicenote` (web, linked to workspace) | Live |
| AOAI account | `aoai-wa-voicenote` (S0, custom domain) | Live |
| AOAI deployment | `gpt-audio-mini` v2025-12-15 (GlobalStandard, 30 TPM) | Live (verified, returns JSON) |
| Container Apps Environment | `cae-wa-voicenote` | Live |
| Container App | `wa-voicenote` with system MI principal `25ee4ae8-...fdfa0` | Live (placeholder image, /health not yet responding) |
| Container App FQDN | `https://wa-voicenote.bravecliff-0c370c26.swedencentral.azurecontainerapps.io` | Reachable but 404 (placeholder) |
| MI role: Cognitive Services OpenAI User on AOAI | Assigned | Active |
| MI role: Storage Table Data Contributor | Assigned | Active |
| MI role: Storage Blob Data Contributor | Assigned | Active |
| Entra app for GitHub Actions OIDC | `wa-voicenote-github-actions` (app ID `9751e543-...97fc`) | Live |
| Federated credentials | `github-actions-main` (ref=main) + `github-actions-pr` (pull_request) | Live |
| Budget alert | `budget-wa-voicenote-20eur` €20/mo, 50%/80%/100% notifications to kevin@limeralda.com | Active |

GitHub Actions secrets set: `AZURE_CLIENT_ID`, `AZURE_TENANT_ID`, `AZURE_SUBSCRIPTION_ID`.

---

## Current blocker (action required from Kevin)

**GHCR package is private. Container Apps cannot pull it.**

Workflow's `GITHUB_TOKEN` has `packages:write` (sufficient to push) but NOT `admin:packages` (required to flip visibility). The `gh api -X PATCH .../visibility` step in the workflow silently fails 404.

**One-click manual fix:**

1. Open https://github.com/users/kevinleonj/packages/container/wa-voicenote-triage/package_settings
2. Scroll to the "Danger Zone"
3. Click "Change visibility" → "Public" → type the package name to confirm

After that, re-trigger the deploy: `gh workflow run deploy.yml --ref main`

Alternative (heavier, deferred): wire a dedicated read-only PAT as a Container App registry credential. Adds a secret to manage. Only worth it if we ever need the image to stay private.

---

## Plan amendments since session start

[docs/PLAN.md](docs/PLAN.md):
- §10.1: 8 Phase-0 decisions captured (allowlist, language match-inbound, concise tone, 120s timeout option A, no transcript persistence)
- §10.4: LLM system prompt locked verbatim
- §11: Observability stack (structlog + OTel + App Insights, /diag endpoint, KQL alerts) — landed as c10.5 + c13.5

**Deviation:** Deployed `gpt-audio-mini` instead of `gpt-audio-1.5` because the 1.5 quota in swedencentral is 0 and quota increase requires a portal-only support request. Mini has identical API shape. Swap-back is one env var (`AZURE_OPENAI_DEPLOYMENT`) if quota is granted.

---

## Codex consultant review applied (PR #22 → c14)

Codex flagged 11 issues. Resolved in the c14 PR before merge:

| # | Finding | Severity | Resolution |
|---|---|---|---|
| 3 | GHCR pull will fail | Blocker | Workflow step added to flip visibility; currently silently failing due to token scope — see blocker above |
| 5 | Bicep would wipe secrets on redeploy | Blocker | Container App removed from Bicep; deploy workflow owns it imperatively. Bicep covers foundation only. |
| 2B | Smoke could pass on old revision | High | Now probes revision-specific FQDN (`<rev_suffix>.<base_fqdn>`); 10x15s retry budget |
| 2A | 75s cold-start budget tight | Medium | Raised to 150s |

Non-blocking findings tracked for follow-up:
- Contributor at RG scope too broad (could narrow to `Container Apps Contributor` + scoped `RBAC Administrator`)
- Dead `pull_request` federated credential subject (workflow doesn't run on PRs)
- Role assignment GUID pattern uses resource.id instead of principalId
- Secrets manifest documented in infra/README.md, no separate secrets.example.json yet
- `LLM_SYSTEM_PROMPT` stored as Container App secret unnecessarily (could be plain env var)

---

## Tooling installed locally

| Tool | Version |
|---|---|
| uv | 0.11.12 |
| pre-commit | 4.6.0 |
| gitleaks | 8.30.1 |
| hadolint | 2.14.0 |
| Python | 3.12.13 |
| az CLI | 2.84.0 |
| gh CLI | 2.89.0 |
| docker | 29.3.1 |
| ffmpeg | 8.1 |

---

## Secrets location

`~/.config/wa-voicenote/secrets.env` (mode 600, outside repo).

Contains:
- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`
- `APPLICATIONINSIGHTS_CONNECTION_STRING` (added 2026-05-11)
- `AZURE_OPENAI_ENDPOINT`, `AZURE_OPENAI_DEPLOYMENT`, `AZURE_OPENAI_API_VERSION`, `AZURE_OPENAI_API_KEY` (local dev only — prod uses MI)
- `DIAG_TOKEN` (random 32 bytes, also set as Container App secret)
- `LLM_SYSTEM_PROMPT`

Container App secrets (set via `az containerapp secret set` during provisioning):
- `twilio-auth-token`, `aoai-api-key`, `appinsights-conn`, `diag-token`, `llm-system-prompt`

Never echo, never commit, never log. Pre-commit `gitleaks` hook gates every commit.

---

## Open decisions

None blocking. Awaiting Kevin's one-click flip of GHCR visibility.

---

## Hardcoding policy reminder

[docs/PLAN.md §10.2](docs/PLAN.md): no literal user-facing strings or magic numbers in `src/wa_voicenote/*.py` business logic. Everything via `config.py` (Pydantic Settings) + env vars. Ruff PLR2004 enforced; `tests/**` exempted. AST check on `handlers.py` is active and passing.

---

## How to resume in a fresh session

1. Read [docs/PLAN.md](docs/PLAN.md) end-to-end (source of truth).
2. Read this `HANDOFF.md` for current state.
3. Read [infra/README.md](infra/README.md) for Bicep / Container App split.
4. Run `make ci` on `main` to confirm green starting state.
5. Current blocker is the GHCR visibility flip — see "Current blocker" section above.
6. Once unblocked: `gh workflow run deploy.yml --ref main`, watch with `gh run watch`, verify /health on the new revision FQDN, set Twilio Sandbox webhook URL, send voice note.
7. Live-endpoint ping discipline per Kevin's directive: ping real Twilio and Azure at every touchpoint.
