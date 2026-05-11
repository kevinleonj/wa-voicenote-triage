# HANDOFF.md

Last updated: 2026-05-11

---

## Current state

| Field | Value |
|---|---|
| Branch | `feat/c3-ci-workflows` (empty, ready for c3 work) |
| Latest commit on `main` | `5c5710a` — `chore(repo): add Dockerfile, docker-compose, hadolint config (#2)` |
| Commits merged to `main` | 3 (bootstrap PLAN, c1 scaffold, c2 Docker) |
| Open PR | None |
| Branch protection | Not yet active (lands after c3 merges) |

---

## What is merged on main

| SHA | Commit |
|---|---|
| `5c5710a` | c2 — Dockerfile (multi-stage uv + ffmpeg + non-root), docker-compose.yml, .hadolint.yaml, hadolint pre-commit hook v2.14.0, Makefile build + dockerlint targets |
| `ba4f492` | c1 — pyproject.toml (uv), ruff (PLR2004 on), mypy --strict, pytest cov gate 90%, pre-commit hooks (10), Makefile, README, smoke test |
| `52b372c` | Bootstrap — docs/PLAN.md (Phase 0 plan), bootstrap .gitignore |

---

## Build / test / lint state

| Check | Status |
|---|---|
| `make ci` (lint + format-check + type + test + dockerlint) | Green on `main` |
| `docker build` | Green |
| pre-commit (11 hooks incl. hadolint) | Green |
| gitleaks | Clean |
| `uv lock --check` | Clean |
| Coverage | 100% on 1-stmt package (gate 90%) |

---

## What is next: c3 — `chore(ci): add ci.yml and deploy.yml workflow skeletons`

Files:
- `.github/workflows/ci.yml` — real steps: setup-uv, uv sync, ruff, ruff-format, mypy, pytest --cov, hadolint, docker build (no push), trivy HIGH/CRITICAL gate
- `.github/workflows/deploy.yml` — skeleton with `if: false` until c14 (real deploy wiring lands then)

After c3 merges: enable branch protection on `main` (require ci.yml green, linear history, no force-push).

---

## Plan amendments since session start

[docs/PLAN.md](docs/PLAN.md) updated 2026-05-11 with new §11 (Observability and Monitoring):
- Logs: `structlog` JSON to stdout
- Metrics + traces: Azure Application Insights via OpenTelemetry SDK + `azure-monitor-opentelemetry`
- New commit **c10.5** — observability wiring (between c10 AOAI and c11 Twilio client)
- New commit **c13.5** — `GET /diag` endpoint with bearer-token auth for live status checks
- New Azure resources: App Insights `appi-wa-voicenote` + Log Analytics workspace `law-wa-voicenote` (both free-tier)
- New env vars: `APPLICATIONINSIGHTS_CONNECTION_STRING`, `OTEL_SERVICE_NAME`, `LOG_LEVEL`, `DIAG_TOKEN`, `ENV_NAME`
- 4 KQL alert rules (webhook 5xx, AOAI p95 latency, Twilio failures, cost guard at $15)
- Net cost impact: $0 (within free tiers)
- Total commits now: 18 (was 16)

---

## Time to first WhatsApp audio working end-to-end

| Milestone | Remaining commits |
|---|---|
| CI online + branch protection | c3 |
| All local code complete (mocked) | c3 → c13 (11 commits) |
| Azure resources provisioned | one provisioning sprint (RG, Storage, AOAI, Container App, App Insights, MI, OIDC) |
| First deploy, /health responds | c14 |
| Twilio Sandbox webhook pointed at Container App | post-c14 manual step |
| **First voice note → 3 replies back** | end of c14 |

Realistic: **15 more focused work blocks**.

---

## Live endpoints verified so far

| Service | Status | Notes |
|---|---|---|
| Twilio account | Verified | account active, Full tier, friendly_name "My first Twilio account" |
| Twilio Sandbox | Joined | code `join frighten-therefore` from `whatsapp:+34611779374` → success message at 20:54 |
| Twilio echo | Working | "Hi" → default echo reply ("You said: Hi. Configure your WhatsApp Sandbox's Inbound URL...") |
| Azure CLI | Authenticated | `kevin@limeralda.com`, default sub `8c5dd4a1-...2159f7`, tenant `bac379d9-...c40e` |
| Azure resource group `rg-wa-voicenote` | NOT YET PROVISIONED | will create at c10 for AOAI smoke test |
| AOAI deployment | NOT YET PROVISIONED | gpt-audio-1.5 v 2026-02-23 confirmed available in swedencentral |

---

## Tooling installed locally this session

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

Will add later: `APPLICATIONINSIGHTS_CONNECTION_STRING` (after App Insights provisioned), `DIAG_TOKEN` (generated at deploy time).

Never echo, never commit, never log. AOAI uses Managed Identity in production; API key only in local dev.

---

## Open decisions

None blocking c3. Kevin confirmed observability plan (App Insights + OpenTelemetry + structlog + /diag with bearer token).

---

## Provisioning still pending (c10–c14 boundary)

All scripted in [docs/PLAN.md §6](docs/PLAN.md) and amended in §11.4:

- GitHub Actions OIDC federated credential (Entra app `wa-voicenote-github-actions`)
- Azure resource group `rg-wa-voicenote` (region: swedencentral)
- Storage account `stwavoicenote` with table `convstate` and container `audio-staging` (24h lifecycle delete)
- Azure OpenAI Foundry resource `aoai-wa-voicenote` with deployment `gpt-audio-15` (model: gpt-audio-1.5, version 2026-02-23)
- Log Analytics workspace `law-wa-voicenote` + Application Insights `appi-wa-voicenote`
- Container App `wa-voicenote` in environment `cae-wa-voicenote` (min_replicas=0, max_replicas=2)
- Managed Identity role assignments: Cognitive Services OpenAI User, Storage Table Data Contributor, Storage Blob Data Contributor

---

## Hardcoding policy reminder

[docs/PLAN.md §10.2](docs/PLAN.md): no literal strings or magic numbers in `src/wa_voicenote/*.py` business logic. Everything via `config.py` (Pydantic Settings) + env vars. `config.py` lands in c4. Ruff PLR2004 enabled; `tests/**` exempted.

---

## How to resume in a fresh session

1. Read [docs/PLAN.md](docs/PLAN.md) end-to-end (it is the source of truth, including §10 decisions and §11 observability).
2. Read this `HANDOFF.md` for current branch, last green commit, and next commit ID.
3. Run `make ci` on the current branch to confirm green starting state before any new work.
4. Follow Red then Green then Refactor for the next commit; every commit must keep `make ci` green before push.
5. Verify-docs runs are required before c5 (Twilio signing), c7 (Azure Tables SDK), c8 (Azure Blob SDK), c10 (AOAI Chat Completions audio), c10.5 (azure-monitor-opentelemetry), c11 (Twilio Python helper library).
6. Live-endpoint ping discipline: ping Twilio and Azure at every touchpoint per Kevin's directive. Never trust documentation alone.
