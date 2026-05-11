# HANDOFF.md

Last updated: 2026-05-11

---

## Current state

| Field | Value |
|---|---|
| Branch | `main` (no open work; ready for c7) |
| Latest commit on `main` | `668409e` — `feat(allowlist): implement From allowlist guard (#11)` |
| Commits merged | 6 of 18 (PLAN §4 + §11.2 fractional commits) |
| Open PR | None |
| Branch protection | Active on `main` (status check `lint + type + test + docker`, linear history, no force-push) |

---

## Merged commits on main

| SHA | Commit | Notes |
|---|---|---|
| `668409e` | c6 — From allowlist guard | `is_sender_allowed` + 8 tests + reactivated AST no-hardcoded-strings check on `handlers.py` |
| `93cd154` | c5 — Twilio HMAC signature validation | canonical algorithm per Twilio docs; FastAPI dependency honors X-Forwarded-Proto/Host; constant-time compare; 16 tests at 100% coverage |
| `596df5d` | c4 — Pydantic Settings | 28 fields (Twilio + Azure + LLM prompt + 9 message templates + 5 observability vars); SecretStr for tokens; ENV_FILE override; lru_cache |
| `6bc1e17` | c3 — CI + branch protection | ci.yml (uv, ruff, mypy, pytest+cov, hadolint, docker buildx, trivy HIGH/CRITICAL); deploy.yml gated `if: false` until c14; dependabot weekly |
| `5c5710a` | c2 — Docker + hadolint | multi-stage Dockerfile (uv builder + ffmpeg + non-root runtime + healthcheck); docker-compose mounts host secrets read-only |
| `ba4f492` | c1 — uv scaffold | pyproject.toml, ruff (PLR2004), mypy --strict, pytest cov gate 90%, pre-commit hooks |
| `52b372c` | bootstrap | docs/PLAN.md (Phase 0 plan including §10 decisions and §11 observability) |

---

## Build / test / lint state (as of 668409e on main)

| Check | Status |
|---|---|
| `make ci` (lint + format-check + type + test + dockerlint) | Green |
| GitHub Actions CI on `main` | Green |
| pre-commit hooks | Green |
| gitleaks | Clean across 12 commits |
| `uv lock --check` | Clean |
| Tests | 65 pass, 0 skipped |
| Coverage | 98.88% project-wide (gate 90%) |
| Modules with 100% coverage | `__init__.py`, `handlers.py`, `twilio_signing.py` |
| `config.py` coverage | 98% (1 unreachable-from-env branch) |

---

## Source files merged

| File | Purpose |
|---|---|
| `src/wa_voicenote/__init__.py` | Package init, exports `__version__` |
| `src/wa_voicenote/config.py` | Pydantic Settings model, 28 fields, lru_cached |
| `src/wa_voicenote/twilio_signing.py` | `compute_signature`, `is_valid_signature`, `require_valid_twilio_signature` FastAPI dependency |
| `src/wa_voicenote/handlers.py` | Currently only `is_sender_allowed` — full state machine in c12 |

---

## What is next: c7 — `feat(state-repo): implement Azure Table Storage state store`

Files:
- `src/wa_voicenote/state_repo.py` — `StateRecord(state, blob_url, awaiting_context_since)`, `get_state`, `set_state`, `check_and_record_sid` (last-100 SID ring per phone)
- `tests/test_state_repo.py` — written first (Red); mocks `azure-data-tables` async SDK

Verify-docs required before c7: `azure-data-tables` async API — `TableServiceClient`, `upsert_entity`, `get_entity`, `ResourceNotFoundError`, entity schema constraints.

---

## Time to first WhatsApp audio working end-to-end

| Milestone | Commits remaining |
|---|---|
| c7 state repo (mocked) | 1 |
| c8 blob staging (mocked) | 1 |
| c9 ffmpeg transcoder (real subprocess) | 1 |
| **Azure provisioning sprint at c10 boundary** (RG, Storage, AOAI, App Insights, Container Apps, MI, OIDC) | — |
| c10 AOAI client + live AOAI smoke test | 1 |
| c10.5 observability (structlog + OTel + App Insights) | 1 |
| c11 Twilio REST client | 1 |
| c12 full state machine | 1 |
| c13 FastAPI main + /health | 1 |
| c13.5 /diag bearer-token endpoint | 1 |
| c14 deploy wiring + first Container App deploy | 1 |
| **First WhatsApp voice note → 3 replies back** | end of c14 |
| c15 .env.example | 1 |
| c16 docs (ARCHITECTURE, DEPLOY, CHANGELOG) | 1 |

**10 more commits + 1 provisioning sprint to first live WhatsApp audio.**

---

## Live endpoints status

| Service | Status |
|---|---|
| Twilio account | Verified active (Full tier) |
| Twilio Sandbox | Joined from `whatsapp:+34611779374` with code `join frighten-therefore` (2026-05-10 20:54) |
| Twilio Sandbox echo | Working (default reply observed on test message "Hi") |
| Twilio Sandbox webhook URL | NOT YET SET — will configure post-c14 to point at Container App `/webhook/whatsapp` |
| Azure CLI | Authenticated as `kevin@limeralda.com`, default sub `8c5dd4a1-...2159f7`, tenant `bac379d9-...c40e` |
| Azure resource group `rg-wa-voicenote` | NOT YET PROVISIONED — create at c10 |
| AOAI deployment `gpt-audio-15` | NOT YET PROVISIONED — `gpt-audio-1.5` v2026-02-23 confirmed available in swedencentral |
| Storage account `stwavoicenote` | NOT YET PROVISIONED |
| App Insights `appi-wa-voicenote` | NOT YET PROVISIONED — wires in at c10.5 |
| Container App `wa-voicenote` | NOT YET PROVISIONED — first deploy at c14 |

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

Contains: `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`.

Will append later (after Azure provisioning at c10/c10.5):
- `APPLICATIONINSIGHTS_CONNECTION_STRING`
- `AZURE_OPENAI_ENDPOINT` (only for local dev; prod uses Managed Identity)
- `AZURE_OPENAI_API_KEY` (local dev only)
- `DIAG_TOKEN` (generated at deploy time)

Never echo, never commit, never log. Pre-commit `gitleaks` hook gates every commit.

---

## Open decisions

None blocking. PLAN §10.1 captured all Phase 0 answers. PLAN §11 (observability) captured Kevin's logging/monitoring directive.

**Next decision point:** Kevin's go/no-go on Azure provisioning at the c10 boundary. Estimated monthly cost stays ~$5 (within $20 ceiling); App Insights and Log Analytics use free tiers.

---

## Provisioning still pending

All scripted in [docs/PLAN.md §6](docs/PLAN.md) and amended in §11.4:

- GitHub Actions OIDC federated credential (Entra app `wa-voicenote-github-actions`)
- Resource group `rg-wa-voicenote` in `swedencentral`
- Storage account `stwavoicenote` (Standard_LRS) + table `convstate` + container `audio-staging` (24h lifecycle delete)
- Log Analytics workspace `law-wa-voicenote` (PerGB2018, 30-day retention)
- Application Insights `appi-wa-voicenote` (web type, linked to law-wa-voicenote)
- AOAI Foundry `aoai-wa-voicenote` + deployment `gpt-audio-15` (model `gpt-audio-1.5` v2026-02-23, Standard tier, low TPM cap)
- Container Apps environment `cae-wa-voicenote`
- Container App `wa-voicenote` (min_replicas=0, max_replicas=2, system-assigned identity)
- Role assignments to Container App MI: Cognitive Services OpenAI User, Storage Table Data Contributor, Storage Blob Data Contributor

---

## Hardcoding policy reminder

[docs/PLAN.md §10.2](docs/PLAN.md): no literal user-facing strings or magic numbers in `src/wa_voicenote/*.py` business logic. Everything via `config.py` (Pydantic Settings) + env vars. Ruff PLR2004 enforced; `tests/**` exempted. `handlers.py` AST scan is active and passing.

---

## Live-endpoint discipline (Kevin's directive)

Ping the real endpoint at every touchpoint:
- c10: provision AOAI, run a real `gpt-audio-1.5` call against a 1s WAV fixture
- c10.5: send a structured log line and an OTel span to App Insights, verify in Live Metrics
- c11: send a real Twilio Sandbox message from the CLI before code changes ship
- c14: full end-to-end (Twilio webhook → Container App → AOAI → Storage → Twilio REST reply)

---

## How to resume in a fresh session

1. Read [docs/PLAN.md](docs/PLAN.md) end-to-end (source of truth, including §10 decisions and §11 observability).
2. Read this `HANDOFF.md` for current branch, last green commit, and next commit ID.
3. Run `make ci` on `main` to confirm green starting state before any new work.
4. Follow Red then Green then Refactor for the next commit; every commit must keep `make ci` green before push.
5. Verify-docs runs are required before c7 (Azure Tables), c8 (Azure Blob), c10 (AOAI Chat Completions audio), c10.5 (azure-monitor-opentelemetry), c11 (Twilio Python helper library).
6. Live-endpoint ping discipline per Kevin's directive: ping real Twilio and Azure at every touchpoint. Never trust documentation alone.
