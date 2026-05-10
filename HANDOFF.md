# HANDOFF.md

Last updated: 2026-05-10

---

## Current state

| Field | Value |
|---|---|
| Branch | `feat/c1-scaffold` |
| Latest commit | `381d5b2` — `chore(repo): scaffold pyproject, ruff, mypy, pytest, pre-commit` |
| PR | https://github.com/kevinleonj/wa-voicenote-triage/pull/1 (open, awaiting Kevin's merge) |
| Merge status | Not merged. Kevin must merge before c2 starts. |

---

## What just changed (c1 scaffold)

| File | Purpose |
|---|---|
| `pyproject.toml` | uv-managed project; ruff, mypy, pytest, pytest-cov, pytest-asyncio, httpx; coverage gate 90%; PLR2004 enabled |
| `.pre-commit-config.yaml` | 10 hooks: check-yaml, check-added-large-files, check-merge-conflict, end-of-file-fixer, trailing-whitespace, mixed-line-ending, ruff-check, ruff-format, mypy --strict, gitleaks |
| `Makefile` | Targets: `install`, `lint`, `format`, `format-check`, `type`, `test`, `ci` |
| `README.md` | Skeleton: project title, requirements, quickstart, structure pointer |
| `.gitignore` | Python build artifacts, venvs, caches, secrets, OS noise, IDE files |
| `.python-version` | Pins Python 3.12 for uv and pyenv |
| `src/wa_voicenote/__init__.py` | Package init; exports `__version__ = "0.1.0"` |
| `tests/__init__.py` | Marks tests as a package |
| `tests/test_smoke.py` | Single smoke test: asserts `__version__` is a non-empty string |
| `docs/PLAN.md` | Full Phase 0 design: file tree, TDD test list, 16-commit plan, CI/CD, Azure provisioning, cost model, decisions |

---

## Build / test / lint state

| Check | Status |
|---|---|
| `make lint` | Green |
| `make format-check` | Green |
| `make type` | Green |
| `make test` | Green (1 test; coverage 100% on 1-stmt package; gate 90% passes) |
| `make ci` | Green |
| pre-commit (10 hooks) | Green |
| gitleaks | Clean |
| `uv lock --check` | Clean |

---

## Tooling installed this session

| Tool | Version |
|---|---|
| uv | 0.11.12 |
| pre-commit | 4.6.0 |
| gitleaks | 8.30.1 |
| hadolint | 2.14.0 (hook commented out in `.pre-commit-config.yaml` until Dockerfile lands in c2) |
| Python | 3.12.13 (already present) |

---

## Secrets location

Twilio credentials: `~/.config/wa-voicenote/secrets.env` (mode 600, outside the repo).
Never echo, never commit, never log. AOAI uses Managed Identity in production; API key only in local dev.

---

## What is next

**c2** — `chore(repo): add Dockerfile, docker-compose, hadolint config`

Files: `Dockerfile`, `docker-compose.yml`, `.hadolint.yaml`. Enables the commented-out hadolint hook in `.pre-commit-config.yaml`.

Full commit order (16 total) is in [docs/PLAN.md §4](docs/PLAN.md).

---

## Blockers

Kevin must merge PR #1 (`feat/c1-scaffold` into `main`) before c2 starts. No other blockers.

---

## Open decisions

None for c2. All Phase 0 answers are captured in [docs/PLAN.md §10.1](docs/PLAN.md).

---

## Provisioning not yet done (c14+ work)

All scripted in [docs/PLAN.md §6](docs/PLAN.md):

- GitHub Actions OIDC federated credential (Entra app registration)
- Azure resource group `rg-wa-voicenote` (region: swedencentral)
- Storage account `stwavoicenote` with table `convstate` and container `audio-staging`
- Azure OpenAI Foundry resource `aoai-wa-voicenote` with deployment `gpt-audio-15` (model: gpt-audio-1.5)
- Container App `wa-voicenote` in environment `cae-wa-voicenote` (min_replicas=0)
- Managed Identity role assignments: Cognitive Services OpenAI User, Storage Table Data Contributor, Storage Blob Data Contributor

---

## Hardcoding policy reminder

Per [docs/PLAN.md §10.2](docs/PLAN.md): no literal strings, numbers, or magic values in `src/wa_voicenote/*.py` business logic. Everything goes through `config.py` (Pydantic Settings) backed by env vars. `config.py` lands in c4. Ruff PLR2004 is already enabled; `tests/**` is exempted.

---

## How to resume in a fresh session

Read [docs/PLAN.md](docs/PLAN.md) first to get full project context, then check this HANDOFF.md for the current branch and last green commit. Find the next commit ID in PLAN.md §4 (currently c2 if PR #1 is merged, or still c1 if not). Verify `make ci` is green on the current branch before writing any code. Follow Red then Green then Refactor for each commit; every commit must leave `make ci` green before pushing. Verify-docs runs are required before c5, c7, c8, c10, and c11 as noted in PLAN.md §4.
