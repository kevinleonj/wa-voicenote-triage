# CLAUDE.md — wa-voicenote-triage

## Project identity

`wa-voicenote-triage` is a personal, single-user FastAPI service that processes WhatsApp voice notes via Twilio Sandbox. It transcribes, summarises, and generates a suggested reply using a single multimodal Azure OpenAI call (gpt-audio-1.5), then sends three sequential WhatsApp messages back to the sender.

---

## Source of truth

[docs/PLAN.md](docs/PLAN.md) is the authoritative design document. Never deviate from it without amending PLAN.md first and noting the change in the relevant §10.x decision block.

---

## Stack (locked, no alternatives)

| Layer | Technology |
|---|---|
| Language | Python 3.12 |
| Web framework | FastAPI + uvicorn |
| Deployment | Azure Container Apps (min_replicas=0, scale-to-zero) |
| LLM | Azure OpenAI gpt-audio-1.5 (deployment: `gpt-audio-15`, API version: `2025-04-01-preview`) |
| Messaging | Twilio Sandbox WhatsApp (sender: `whatsapp:+14155238886`, join code: `join frighten-therefore`) |
| State storage | Azure Table Storage (`convstate` table) |
| Audio staging | Azure Blob Storage (`audio-staging` container, 24h lifecycle delete) |
| Audio transcode | ffmpeg subprocess (OGG/Opus to 16kHz mono PCM16 WAV) |
| Packaging | uv |
| Lint/format | Ruff |
| Type checking | mypy --strict |
| Testing | pytest + pytest-asyncio + pytest-cov |
| HTTP client (tests) | httpx with ASGI transport |

No Node.js. No Express. No alternatives to the above.

---

## Hardcoding rule

Per PLAN.md §10.2: no literal user-facing strings, no magic numbers in `src/wa_voicenote/*.py`. Every configurable value goes through `config.py` (Pydantic Settings) backed by environment variables. This includes all user-facing messages, the LLM system prompt, timeouts, sender numbers, model names, API versions, container names, and the idempotency ring size.

Ruff PLR2004 (magic-value-comparison) is enabled. `tests/**` is exempted from PLR2004 and S101.

`config.py` lands in c4. Before c4, there is no business logic to hardcode.

---

## Commit discipline

Conventional Commits format. Atomic commits per PLAN.md §4 (16 total). Every commit must leave `make ci` green before pushing. Branch protection on `main` is enabled after c3 lands (require CI green, require linear history, no force push).

Never use `--no-verify`.

---

## Pre-commit hooks

10 hooks active from c1 (hadolint is commented out until c2):

- `pre-commit-hooks`: check-yaml, check-added-large-files (500KB), check-merge-conflict, end-of-file-fixer, trailing-whitespace, mixed-line-ending
- `ruff-check` (with `--fix`)
- `ruff-format`
- `mypy --strict`
- `gitleaks`

---

## Secrets

- Twilio credentials: `~/.config/wa-voicenote/secrets.env` (mode 600, outside repo)
- Never write secret values to the repo, commit messages, PR descriptions, or logs
- Azure OpenAI uses Managed Identity in production; `AZURE_OPENAI_API_KEY` is used only for local development
- Allowed phone number (only Kevin's): `TWILIO_ALLOWLIST=whatsapp:+34611779374`

---

## Test discipline (TDD)

Red then Green then Refactor per feature. Tests are written before the implementation. Coverage gate: 90% on `src/wa_voicenote`. Every new source file must be accompanied by a matching test file (Kevin's global rule). Full test list is in PLAN.md §3.

---

## verify-docs gate

The following commits require a verify-docs run before any code is written:

| Commit | What to verify |
|---|---|
| c5 | Twilio signature algorithm (header name, body-sort order, HMAC-SHA1 encoding) |
| c7 | `azure-data-tables` async API: `TableServiceClient`, `upsert_entity`, `get_entity`, `ResourceNotFoundError` |
| c8 | `azure-storage-blob` async SDK: `BlobServiceClient`, `upload_blob`, SAS generation, `aio` module |
| c10 | AOAI Chat Completions audio-input shape: `input_audio`, `format`, `modalities`; Managed Identity token scope |
| c11 | Twilio Python helper: `messages.create(from_, to, body)` signature and Sandbox sender format |

---

## Allowed phone numbers

`TWILIO_ALLOWLIST=whatsapp:+34611779374` (Kevin's number, Spain). Any inbound message from a number not in the allowlist is dropped silently with an empty `<Response/>` TwiML body. Exact match only, no prefix matching.

---

## WhatsApp Sandbox

Sender: `whatsapp:+14155238886`. Join code: `join frighten-therefore`. Kevin's number (`+34`) receives messages from a US sender (`+1`). Twilio warns that international Sandbox delivery may be flaky; documented risk in PLAN.md §10.7. Escalate to registered sender if delivery becomes unreliable.

---

## Cost ceiling

$20/month. Enforced by: Container Apps min_replicas=0 (scale-to-zero), AOAI TPM cap (set in Azure Portal after deployment), lifecycle delete on audio blobs older than 24h. Estimated baseline: ~$5.05/month (see PLAN.md §7).

---

## Subagent delegation

Follow Kevin's global rules: builder implements code (must run verify-docs before coding), qa tests and validates (must update test files when source files change), reviewer is read-only final gate, docs-updater updates README.md, HANDOFF.md, CLAUDE.md. Use codex-consultant or gemini-consultant for second opinions on planning or risky surgical edits.
