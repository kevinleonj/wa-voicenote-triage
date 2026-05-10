# PLAN.md — wa-voicenote-triage Phase 0

_Generated: 2026-05-10. Status: pre-implementation planning only. No code written._

---

## 1. Overview

`wa-voicenote-triage` is a personal, single-user FastAPI service that acts as a WhatsApp voice note processor via Twilio Sandbox. When a voice note arrives, the bot acknowledges receipt and optionally solicits a brief context message before processing. All natural-language work — transcription, summarization, and suggested-reply generation — is performed in a single multimodal Azure OpenAI call using `gpt-audio-1.5`. The service replies with three sequential WhatsApp messages: the raw transcript, a concise summary, and a suggested reply. Conversation state is tracked per-sender in Azure Table Storage using a two-state machine (`idle` / `awaiting_context`). Audio is transiently staged in Azure Blob Storage (auto-deleted after 24 hours). The service is deployed as a Container App on Azure (scale-to-zero, min_replicas=0) and is gated by a strict CI pipeline that mirrors the local `make ci` target exactly. The entire stack is Python 3.12 + FastAPI, with no Node or Express anywhere.

---

## 2. Final File Tree

```
wa-voicenote-triage/
├── .github/workflows/{ci.yml,deploy.yml}
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── Makefile
├── src/wa_voicenote/
│   ├── __init__.py
│   ├── main.py
│   ├── config.py
│   ├── twilio_signing.py
│   ├── twilio_client.py
│   ├── state_repo.py
│   ├── blob_repo.py
│   ├── transcoder.py
│   ├── aoai_client.py
│   └── handlers.py
├── tests/
│   ├── fixtures/sample.ogg
│   └── (mirrors src/ — see test list below)
├── docs/
│   ├── PLAN.md
│   ├── ARCHITECTURE.md
│   ├── DEPLOY.md
│   └── CHANGELOG.md
└── README.md
```

---

## 3. TDD Test List

All tests live under `tests/`. Each module mirrors its `src/wa_voicenote/` counterpart. Tests are written before the implementation (Red phase), then implementation makes them pass (Green phase).

### `tests/test_config.py`

| Test name | What it asserts |
|---|---|
| `test_all_required_vars_load` | When all required env vars are set, `Settings()` constructs without error and each field has the correct type and value. |
| `test_missing_required_raises` | When `TWILIO_ACCOUNT_SID` is absent, `Settings()` raises `ValidationError`. Repeat for each of the 12 required vars. |
| `test_type_coercion_allowlist` | `TWILIO_ALLOWLIST="whatsapp:+1111,whatsapp:+2222"` is parsed into `list[str]` with two elements. |
| `test_type_coercion_expected_languages` | `EXPECTED_LANGUAGES="ES,EN,DE"` is parsed into `list[str]` of length 3. |
| `test_api_version_default` | `AZURE_OPENAI_API_VERSION` defaults to `"2025-04-01-preview"` when not set. |
| `test_secrets_env_file_not_in_repo` | Asserts that `~/.config/wa-voicenote/secrets.env` is NOT inside the project directory (path check only — does not require the file to exist in CI). |

### `tests/test_twilio_signing.py`

Uses the canonical example from [Twilio docs: Validating Requests](https://www.twilio.com/docs/usage/security) — exact URL, POST body, and X-Twilio-Signature from the official example.

| Test name | What it asserts |
|---|---|
| `test_valid_signature_passes` | `validate_twilio_signature(token, url, params, sig)` returns `True` for the canonical Twilio docs example. |
| `test_invalid_signature_raises` | A mutated signature (last byte changed) returns `False` (or raises `HTTPException(403)`). |
| `test_tampered_param_raises` | Same signature but one POST param value changed — returns `False`. |
| `test_replay_different_sid_allowed` | A fresh MessageSid with a valid signature is accepted (replay guard is a separate layer in `state_repo`). |
| `test_empty_body_valid_signature` | A GET-like webhook with no POST params but a valid HMAC passes (edge case for health-check-style pings). |

### `tests/test_allowlist.py`

| Test name | What it asserts |
|---|---|
| `test_allowlisted_from_passes` | A request from a number in `TWILIO_ALLOWLIST` is not dropped; handler is invoked. |
| `test_non_allowlisted_from_drops` | A request from a number NOT in `TWILIO_ALLOWLIST` returns HTTP 200 with an empty `<Response/>` TwiML body and the handler is never called. |
| `test_allowlist_exact_match` | `whatsapp:+491234` is NOT matched by `whatsapp:+49123` — prefix matching is rejected; only exact equality. |
| `test_allowlist_whitespace_trimmed` | `TWILIO_ALLOWLIST=" whatsapp:+1111 , whatsapp:+2222 "` — whitespace around entries is stripped before comparison. |

### `tests/test_state_repo.py`

Uses `azure-data-tables` with a mocked `TableServiceClient` (via `unittest.mock.AsyncMock`).

| Test name | What it asserts |
|---|---|
| `test_get_state_idle_default` | When the table entity does not exist, `get_state(phone)` returns `StateRecord(state="idle", blob_url=None)`. |
| `test_set_and_get_idle` | `set_state(phone, "idle")` followed by `get_state(phone)` returns `state="idle"`. |
| `test_set_and_get_awaiting_context` | `set_state(phone, "awaiting_context", blob_url="https://...")` persists both fields. |
| `test_blob_url_cleared_on_idle` | Transitioning back to `idle` clears `blob_url` to `None`. |
| `test_sid_dedupe_first_time_accepted` | `check_and_record_sid(phone, sid)` returns `False` (not a duplicate) for a new SID. |
| `test_sid_dedupe_replay_rejected` | The same SID passed again returns `True` (is a duplicate). |
| `test_sid_ring_evicts_oldest` | After inserting 101 distinct SIDs, the 1st SID is evicted and no longer triggers dedupe; SID 101 does. |
| `test_sid_ring_persisted_as_json` | The ring buffer is stored as a JSON-serialized list in the same table entity. |

### `tests/test_blob_repo.py`

Mocks `azure.storage.blob.aio.BlobServiceClient`.

| Test name | What it asserts |
|---|---|
| `test_upload_bytes_returns_blob_url` | `upload_audio(phone, wav_bytes)` calls `upload_blob` with the correct container name and returns a URL containing the blob name. |
| `test_download_by_sas_returns_bytes` | `download_audio(blob_url)` makes an authenticated GET and returns the raw bytes. |
| `test_blob_name_includes_phone_and_timestamp` | The blob name follows the pattern `{phone_hash}/{iso_timestamp}.wav` (no PII in plain text). |
| `test_container_name_from_config` | The container used matches `AZURE_STORAGE_CONTAINER` from config, not a hardcoded string. |

### `tests/test_transcoder.py`

Uses the real `ffmpeg` subprocess. Requires `ffmpeg` installed in the test environment (CI installs it via apt).

| Test name | What it asserts |
|---|---|
| `test_ogg_to_wav_produces_file` | `transcode_to_wav(ogg_bytes)` does not raise and returns non-empty `bytes`. |
| `test_output_is_pcm_wav` | The returned bytes start with the RIFF/WAVE header (`b"RIFF"` at offset 0, `b"WAVE"` at offset 8). |
| `test_output_sample_rate_16khz` | Parsed WAV header reports sample rate = 16000 Hz. |
| `test_output_mono` | Parsed WAV header reports num_channels = 1. |
| `test_output_pcm16` | Parsed WAV header reports bits_per_sample = 16 (PCM, not float). |
| `test_corrupt_input_raises` | Passing `b"not an audio file"` raises `TranscodeError`. |

### `tests/test_aoai_client.py`

Mocks `httpx.AsyncClient.post` to avoid real API calls.

| Test name | What it asserts |
|---|---|
| `test_builds_correct_chat_completions_payload` | The JSON body sent to the endpoint contains `modalities=["text"]`, an `input_audio` content block with `format="wav"` and a base64-encoded WAV, and the system + user prompt. |
| `test_api_version_in_url` | The request URL contains `api-version=2025-04-01-preview` (or the configured version). |
| `test_parses_well_formed_json_response` | A mock response containing valid JSON in `choices[0].message.content` is parsed into `AoaiResult(transcript=..., summary=..., suggested_reply=...)`. |
| `test_retries_once_on_non_json` | First call returns malformed JSON; second call (with stricter prompt) returns valid JSON — `call_count == 2` and result is returned. |
| `test_raises_after_second_non_json` | Both retry calls return malformed JSON — `AoaiParseError` is raised. |
| `test_managed_identity_header_absent_in_local_mode` | When `AZURE_OPENAI_API_KEY` is set, request uses `api-key` header; no `Authorization: Bearer` header. |
| `test_managed_identity_token_used_in_prod_mode` | When no API key is set and a mock token provider is injected, request carries `Authorization: Bearer <token>`. |

### `tests/test_handlers.py`

Tests each state-machine transition. Uses mocked `state_repo`, `blob_repo`, `transcoder`, `aoai_client`, and `twilio_client`.

| Test name | State transition | What it asserts |
|---|---|---|
| `test_idle_audio_inbound` | `idle + audio_inbound -> awaiting_context` | State written as `awaiting_context`; blob uploaded; ack message sent: "Voice note received. Reply with extra context, or send 'no' to skip." |
| `test_awaiting_context_text_triggers_process` | `awaiting_context + text -> idle` | `aoai_client.process()` called with blob bytes + context text; state reset to `idle`; exactly 3 messages sent in order (transcript, summary, suggested reply). |
| `test_awaiting_context_text_no_skips_context` | `awaiting_context + text="no" -> idle` | Same flow but context passed to AOAI is empty string / None. |
| `test_awaiting_context_audio_replaces` | `awaiting_context + audio -> idle then re-enter awaiting_context` | Old blob replaced with new blob URL; reply: "Replaced previous voice note. Send context or 'no'."; state is `awaiting_context` after. |
| `test_idle_text_only` | `idle + text-only -> reply` | No AOAI call; single reply: "Send me a voice note to start."; state remains `idle`. |
| `test_non_allowlisted_drops` | `any + non-allowlisted From -> drop` | Handler returns empty TwiML; no state written; no messages sent. |
| `test_three_messages_sent_in_order` | `awaiting_context -> idle` | `twilio_client.send_message` called three times; first call carries transcript text, second summary, third suggested reply (order verified via `call_args_list`). |

### `tests/test_main.py`

Integration tests using `httpx.AsyncClient` with ASGI transport against the real FastAPI app. All external I/O (Twilio, Azure) mocked via `pytest-mock` or dependency-override.

| Test name | What it asserts |
|---|---|
| `test_health_returns_200` | `GET /health` returns 200 with `{"status": "ok"}`. |
| `test_webhook_idle_to_awaiting_context` | Full POST to `/webhook/whatsapp` with a valid Twilio signature and audio payload transitions state and returns TwiML 200. |
| `test_webhook_awaiting_context_to_reply` | Full POST with text context; mocked AOAI returns valid JSON; response is 200 TwiML; `send_message` mock called 3 times. |
| `test_webhook_replay_is_idempotent` | Second POST with the same `MessageSid` returns 200 empty TwiML; AOAI not called; state not mutated. |
| `test_webhook_non_allowlisted_dropped` | POST from unlisted number returns 200 with `<Response/>` (empty); handler not entered. |
| `test_webhook_invalid_signature_rejected` | POST with bad `X-Twilio-Signature` returns 403. |
| `test_webhook_missing_signature_header_rejected` | POST with no `X-Twilio-Signature` header returns 403. |

---

## 4. Atomic Commit Order

All commits must keep CI green. Follow Red -> Green -> Refactor. No mixed concerns per commit. Conventional Commits format enforced.

---

### c1 — `chore(repo): scaffold pyproject, ruff, mypy, pytest, pre-commit`

Files touched:
- `pyproject.toml` (uv-managed; ruff, mypy, pytest config; coverage gate 90%)
- `.pre-commit-config.yaml` (ruff, ruff-format, mypy, gitleaks, hadolint)
- `Makefile` (targets: `lint`, `format`, `type-check`, `test`, `ci`)
- `README.md` (skeleton)
- `.gitignore`
- `.python-version` (3.12)

What becomes green: `make lint`, `make format`, `make type-check` all pass on empty src. Pre-commit hooks install cleanly.

---

### c2 — `chore(repo): add Dockerfile, docker-compose, and hadolint config`

Files touched:
- `Dockerfile` (python:3.12-slim base, apt ffmpeg, uv install, non-root user)
- `docker-compose.yml` (local dev with secrets.env volume mount)
- `.hadolint.yaml` (ignore list if needed)

What becomes green: `hadolint Dockerfile` passes. `docker build` succeeds locally.

---

### c3 — `chore(ci): add ci.yml and deploy.yml workflow skeletons`

Files touched:
- `.github/workflows/ci.yml` (lint, format-check, mypy, pytest, hadolint, docker-build, trivy — all stubbed with `echo` placeholders until src exists)
- `.github/workflows/deploy.yml` (push-to-main trigger — stubbed)

What becomes green: Workflows are valid YAML; GitHub Actions parses them without error.

---

### c4 — `feat(config): implement Settings with pydantic-settings and env loading`

Files touched:
- `src/wa_voicenote/__init__.py`
- `src/wa_voicenote/config.py`
- `tests/test_config.py` (written first — Red)

What becomes green: All `test_config.py` tests pass. mypy passes on `config.py`.

---

### c5 — `feat(twilio-signing): implement HMAC signature validation middleware`

**verify-docs required before this commit:** Confirm canonical Twilio signature algorithm from https://www.twilio.com/docs/usage/security#validating-signatures — exact header name, body-sort order, and HMAC-SHA1 encoding.

Files touched:
- `src/wa_voicenote/twilio_signing.py`
- `tests/test_twilio_signing.py` (written first — Red)

What becomes green: All `test_twilio_signing.py` tests pass. mypy clean.

---

### c6 — `feat(allowlist): implement From allowlist guard`

Files touched:
- `src/wa_voicenote/handlers.py` (allowlist check stub only — full handler in later commit)
- `tests/test_allowlist.py` (written first — Red)

What becomes green: All `test_allowlist.py` tests pass.

---

### c7 — `feat(state-repo): implement Azure Table Storage state store`

**verify-docs required before this commit:** Confirm `azure-data-tables` SDK async API — `TableServiceClient`, `upsert_entity`, `get_entity`, exception type for missing entity (`ResourceNotFoundError`), and entity schema constraints.

Files touched:
- `src/wa_voicenote/state_repo.py`
- `tests/test_state_repo.py` (written first — Red)

What becomes green: All `test_state_repo.py` tests pass including the 101-SID ring eviction test.

---

### c8 — `feat(blob-repo): implement Azure Blob Storage staging`

**verify-docs required before this commit:** Confirm `azure-storage-blob` async SDK — `BlobServiceClient`, `upload_blob`, container-level and blob-level SAS generation, and `aio` module availability.

Files touched:
- `src/wa_voicenote/blob_repo.py`
- `tests/test_blob_repo.py` (written first — Red)

What becomes green: All `test_blob_repo.py` tests pass.

---

### c9 — `feat(transcoder): implement ffmpeg WAV transcode subprocess`

Files touched:
- `src/wa_voicenote/transcoder.py`
- `tests/fixtures/sample.ogg` (committed binary — 1-2 second synthetic OGG, no PII)
- `tests/test_transcoder.py` (written first — Red)

What becomes green: All `test_transcoder.py` tests pass including real ffmpeg subprocess invocation. CI apt-installs ffmpeg.

---

### c10 — `feat(aoai-client): implement Azure OpenAI gpt-audio-1.5 call`

**verify-docs required before this commit:** Confirm the Chat Completions audio input shape at https://learn.microsoft.com/en-us/azure/ai-services/openai/reference — exact field names for `input_audio`, `format`, `modalities`, `audio` content block; confirm that `gpt-audio-1.5` on API version `2025-04-01-preview` accepts `input_audio` in the messages array; confirm Managed Identity token scope (`https://cognitiveservices.azure.com/.default`).

Files touched:
- `src/wa_voicenote/aoai_client.py`
- `tests/test_aoai_client.py` (written first — Red)

What becomes green: All `test_aoai_client.py` tests pass. mypy clean under `--strict`.

---

### c11 — `feat(twilio-client): implement Twilio message sender`

**verify-docs required before this commit:** Confirm Twilio Python helper library `send` vs `create` API for `messages.create(from_, to, body)`, and the Twilio Sandbox sender number format.

Files touched:
- `src/wa_voicenote/twilio_client.py`
- (No separate test file — tested indirectly via `test_handlers.py` mocks)

What becomes green: mypy clean. No regressions.

---

### c12 — `feat(handlers): implement full state machine`

Files touched:
- `src/wa_voicenote/handlers.py` (full implementation replacing the stub from c6)
- `tests/test_handlers.py` (written first — Red)

What becomes green: All `test_handlers.py` tests pass including all 7 state-machine transition cases.

---

### c13 — `feat(main): wire FastAPI app, middleware, and routes`

Files touched:
- `src/wa_voicenote/main.py`
- `tests/test_main.py` (written first — Red)

What becomes green: All `test_main.py` integration tests pass. Coverage gate 90% on `src/` met. Full `make ci` passes end-to-end.

---

### c14 — `chore(ci): wire ci.yml and deploy.yml to real steps`

Files touched:
- `.github/workflows/ci.yml` (replace stubs with real steps — see Section 5)
- `.github/workflows/deploy.yml` (replace stubs with real deploy steps)

What becomes green: GitHub Actions CI passes on push. Deploy workflow validates on push to `main`.

---

### c15 — `chore(env): add .env.example and docker-compose env wiring`

Files touched:
- `.env.example` (all 12 env vars, no real values, comments explaining each)
- `docker-compose.yml` (update to reference `.env.example` schema)

What becomes green: No secrets in repo (gitleaks passes). docker-compose up works locally.

---

### c16 — `docs: add ARCHITECTURE.md, DEPLOY.md, CHANGELOG.md`

Files touched:
- `docs/ARCHITECTURE.md`
- `docs/DEPLOY.md`
- `docs/CHANGELOG.md`

What becomes green: Docs present. README updated with links.

---

## 5. CI/CD Pipeline

### `.github/workflows/ci.yml`

Trigger: `push` and `pull_request` on all branches.

Steps (in order):

1. `actions/checkout@v4`
2. `astral-sh/setup-uv@v4` — install uv, use `.python-version`
3. `uv sync --frozen` — install dependencies from lockfile
4. `uv run ruff check src/ tests/` — lint gate
5. `uv run ruff format --check src/ tests/` — format gate
6. `uv run mypy --strict src/` — type gate
7. `apt-get install -y ffmpeg` (or pre-installed runner image) — required for `test_transcoder.py`
8. `uv run pytest --cov=src/wa_voicenote --cov-fail-under=90 --cov-report=xml tests/` — test + coverage gate
9. `hadolint/hadolint-action@v3` — Dockerfile lint
10. `docker build -t wa-voicenote-triage:ci .` — build gate (no push)
11. `aquasecurity/trivy-action@master` — scan the built image, fail on HIGH or CRITICAL CVEs

Branch protection rules (set in GitHub repo settings, not in YAML):
- Require `ci.yml` to be green before merge
- Require linear history (no merge commits)
- No force push to `main`

### `.github/workflows/deploy.yml`

Trigger: `push` to `main` only.

Steps (in order):

1. `actions/checkout@v4`
2. Log in to GHCR: `docker login ghcr.io -u kevinleonj --password ${{ secrets.GHCR_PAT }}`
3. `docker build -t ghcr.io/kevinleonj/wa-voicenote-triage:${{ github.sha }} .`
4. `docker push ghcr.io/kevinleonj/wa-voicenote-triage:${{ github.sha }}`
5. `docker tag ... :latest && docker push ... :latest`
6. `azure/login@v2` — OIDC federated credential (no client secret stored in GitHub)
7. `az containerapp update --name wa-voicenote --resource-group rg-wa-voicenote --image ghcr.io/kevinleonj/wa-voicenote-triage:${{ github.sha }}`
8. Smoke test: `curl --fail https://<containerapp-fqdn>/health` (retry 3x, 10s apart)
9. On smoke failure: `az containerapp revision list ... | jq` to find previous revision; `az containerapp ingress traffic set ... --revision-weight <prev>=100` — rollback to previous revision

---

## 6. Azure Resources to Provision (One-Time, Manual or Scripted)

All commands target subscription `8c5dd4a1-ebb3-429f-bc1e-1285df2159f7`, region `swedencentral`.

### Resource Group

```
az group create \
  --name rg-wa-voicenote \
  --location swedencentral \
  --subscription 8c5dd4a1-ebb3-429f-bc1e-1285df2159f7
```

### Storage Account (Table + Blob)

Suggested name: `stwavoicenote` (globally unique; adjust suffix if taken).

```
az storage account create \
  --name stwavoicenote \
  --resource-group rg-wa-voicenote \
  --location swedencentral \
  --sku Standard_LRS \
  --kind StorageV2 \
  --access-tier Hot \
  --allow-blob-public-access false

az storage table create \
  --name convstate \
  --account-name stwavoicenote

az storage container create \
  --name audio-staging \
  --account-name stwavoicenote \
  --public-access off
```

Lifecycle rule (delete blobs older than 24h in `audio-staging`):

```
az storage account management-policy create \
  --account-name stwavoicenote \
  --resource-group rg-wa-voicenote \
  --policy '{
    "rules": [{
      "name": "delete-old-audio",
      "enabled": true,
      "type": "Lifecycle",
      "definition": {
        "filters": {"blobTypes": ["blockBlob"], "prefixMatch": []},
        "actions": {"baseBlob": {"delete": {"daysAfterModificationGreaterThan": 1}}}
      }
    }]
  }'
```

### Azure OpenAI Foundry Resource + Deployment

```
az cognitiveservices account create \
  --name aoai-wa-voicenote \
  --resource-group rg-wa-voicenote \
  --location swedencentral \
  --kind OpenAI \
  --sku S0

az cognitiveservices account deployment create \
  --name aoai-wa-voicenote \
  --resource-group rg-wa-voicenote \
  --deployment-name gpt-audio-15 \
  --model-name gpt-audio-1.5 \
  --model-version 2026-02-23 \
  --model-format OpenAI \
  --sku-capacity 1 \
  --sku-name Standard
```

Note: Set a low TPM (tokens-per-minute) quota cap in the Azure Portal after deployment to enforce the cost ceiling. 1K TPM is sufficient for personal use.

### Container Apps Environment + Container App

```
az containerapp env create \
  --name cae-wa-voicenote \
  --resource-group rg-wa-voicenote \
  --location swedencentral

az containerapp create \
  --name wa-voicenote \
  --resource-group rg-wa-voicenote \
  --environment cae-wa-voicenote \
  --image ghcr.io/kevinleonj/wa-voicenote-triage:latest \
  --ingress external \
  --target-port 8000 \
  --min-replicas 0 \
  --max-replicas 2 \
  --system-assigned \
  --secrets twilio-auth-token=<value> \
  --env-vars \
    TWILIO_ACCOUNT_SID=<value> \
    TWILIO_AUTH_TOKEN=secretref:twilio-auth-token \
    TWILIO_FROM=whatsapp:+14155238886 \
    TWILIO_ALLOWLIST=<kevin_number> \
    AZURE_OPENAI_ENDPOINT=https://aoai-wa-voicenote.openai.azure.com/ \
    AZURE_OPENAI_DEPLOYMENT=gpt-audio-15 \
    AZURE_OPENAI_API_VERSION=2025-04-01-preview \
    AZURE_STORAGE_ACCOUNT=stwavoicenote \
    AZURE_STORAGE_TABLE=convstate \
    AZURE_STORAGE_CONTAINER=audio-staging \
    EXPECTED_LANGUAGES=ES,EN,DE
```

### Managed Identity Role Assignments

After the Container App is created, assign roles to its system-assigned identity:

```
# Get the principal ID of the Container App identity
PRINCIPAL_ID=$(az containerapp show \
  --name wa-voicenote \
  --resource-group rg-wa-voicenote \
  --query identity.principalId -o tsv)

# AOAI: Cognitive Services OpenAI User
AOAI_ID=$(az cognitiveservices account show \
  --name aoai-wa-voicenote \
  --resource-group rg-wa-voicenote \
  --query id -o tsv)

az role assignment create \
  --assignee $PRINCIPAL_ID \
  --role "Cognitive Services OpenAI User" \
  --scope $AOAI_ID

# Storage: Table Data Contributor + Blob Data Contributor
STORAGE_ID=$(az storage account show \
  --name stwavoicenote \
  --resource-group rg-wa-voicenote \
  --query id -o tsv)

az role assignment create \
  --assignee $PRINCIPAL_ID \
  --role "Storage Table Data Contributor" \
  --scope $STORAGE_ID

az role assignment create \
  --assignee $PRINCIPAL_ID \
  --role "Storage Blob Data Contributor" \
  --scope $STORAGE_ID
```

### GHCR Pull Secret

The Container App pulls from GHCR using a GitHub Personal Access Token (PAT) with `read:packages` scope. Store it as a Container App secret and pass as registry credentials on `az containerapp create` (add `--registry-server ghcr.io --registry-username kevinleonj --registry-password <PAT>`). This is simpler than mirroring to ACR and avoids an additional Azure resource.

### Federated Credential for GitHub Actions OIDC

```
# Create an Entra App registration (or use an existing one)
APP_ID=$(az ad app create --display-name "wa-voicenote-github-actions" --query appId -o tsv)
SP_ID=$(az ad sp create --id $APP_ID --query id -o tsv)

# Assign Contributor on the resource group (scoped; adjust if narrower role suffices)
az role assignment create \
  --assignee $SP_ID \
  --role Contributor \
  --scope /subscriptions/8c5dd4a1-ebb3-429f-bc1e-1285df2159f7/resourceGroups/rg-wa-voicenote

# Add federated credential for GitHub Actions
az ad app federated-credential create \
  --id $APP_ID \
  --parameters '{
    "name": "github-actions-main",
    "issuer": "https://token.actions.githubusercontent.com",
    "subject": "repo:kevinleonj/wa-voicenote-triage:ref:refs/heads/main",
    "audiences": ["api://AzureADTokenExchange"]
  }'
```

Add `AZURE_CLIENT_ID`, `AZURE_TENANT_ID` (`bac379d9-f41c-4963-ab69-80807fd5c40e`), and `AZURE_SUBSCRIPTION_ID` as GitHub Actions secrets (not federated — these are not sensitive, but keep in secrets for cleanliness).

---

## 7. Cost Model

Target: $20/month ceiling. Assumptions: personal use, roughly 50 voice notes/month averaging 60 seconds each.

| Resource | Pricing basis | Estimated monthly cost |
|---|---|---|
| Azure Container Apps (Consumption) | min_replicas=0 (scale-to-zero). Billed only per request: ~0.000024 USD/vCPU-s, ~0.000003 USD/GB-s. 50 requests x 30s active = 1500 vCPU-s. | ~$0.04 |
| Azure OpenAI gpt-audio-1.5 | Audio input ~$0.100/min (check current Foundry pricing). 50 notes x 1 min = 50 min. Text output token cost negligible. | ~$5.00 |
| Azure Storage (Table + Blob) | Storage: <1 MB state + <50 MB audio (lifecycle-deleted in 24h so near-zero persistent). Table operations: <10K/month at $0.00036/10K. | ~$0.01 |
| Azure OpenAI Foundry resource | No per-resource charge; pay-as-you-go on Standard tier. | $0.00 |
| Container Apps environment | Shared environment base: $0.00 on Consumption plan. | $0.00 |
| **Total** | | **~$5.05** |

The $20 ceiling provides a 4x headroom buffer over baseline usage. The TPM cap on the AOAI deployment prevents runaway cost if the allowlist is ever misconfigured. Scale-to-zero on Container Apps ensures zero compute cost during idle periods (which is the vast majority of time for a single-user bot).

---

## 8. Open Questions for Kevin

1. **WhatsApp Sandbox join:** Kevin's personal phone must send the Sandbox join code (e.g., `join <word>-<word>`) to `whatsapp:+14155238886` before the bot can reply to it. This is a one-time manual step per phone number. Confirmed required — call this out in DEPLOY.md. Is this already done, or does it need to be done before first test?

2. **TWILIO_ALLOWLIST initial value:** What is Kevin's personal WhatsApp number in `whatsapp:+E.164` format (e.g., `whatsapp:+46701234567`)? This must be set before the first deploy; otherwise all messages are silently dropped.

3. **Suggested reply language:** Should the suggested reply always be generated in the same language as the inbound voice note (auto-detected by the LLM), or should it always be in one of Kevin's preferred languages (e.g., always EN, or always match the voice note)? Default assumption: match the inbound language.

4. **Tone for suggested reply:** Should the suggested reply be concise/casual (as if Kevin is replying from his phone), formal/professional, or should the prompt include no tone instruction and let Kevin edit freely? Default assumption: concise and casual.

5. **Transcript retention:** Should transcripts be stored anywhere (Table, Log Analytics, app logs) or only appear transiently in the WhatsApp reply? Default assumption per the brief: DEBUG log only, no persistence. Confirm this is acceptable.

6. **Context timeout:** If Kevin sends a voice note (entering `awaiting_context`) but never sends context or "no", the bot stays in `awaiting_context` indefinitely. Should there be a timeout (e.g., 1 hour of inactivity auto-resets to `idle`)? Not in scope for Phase 0 — but worth noting.

7. **Multiple voice notes in sequence:** The state machine currently handles `awaiting_context + audio` as "replace and stay in awaiting_context." If Kevin sends two voice notes rapidly before replying, only the second is processed. Is this the intended behavior?

8. **Twilio Sandbox limits:** The Twilio WhatsApp Sandbox has a 72-hour session window — if Kevin hasn't messaged the bot in 72 hours, Twilio may refuse outbound messages. This is a Sandbox limitation, not a bug. Worth noting in DEPLOY.md. Is Kevin planning to upgrade to a production WhatsApp Business number eventually?

---

## 9. Risks and Mitigations

### gpt-audio-1.5 cold-start latency

**Risk:** The model may take 10-30 seconds to produce a response, especially on first call after a cold period. Twilio's webhook has a default 15-second response timeout for TwiML. If the AOAI call exceeds this, Twilio may retry the webhook, creating a duplicate-processing scenario.

**Mitigation:** The webhook must return a TwiML `<Response/>` immediately (acknowledging receipt), then use Twilio's REST API (`twilio_client.py`) to send the three messages asynchronously after the AOAI call completes. This decouples Twilio's timeout from the AOAI latency. The idempotency SID ring guard (in `state_repo.py`) prevents the Twilio retry from double-processing.

### Twilio media URL authentication

**Risk:** Twilio voice note URLs (the `MediaUrl0` parameter in the webhook POST) are short-lived authenticated URLs. If the service fetches the URL more than a few minutes after the webhook arrives, the URL may have expired.

**Mitigation:** Download the media immediately inside the webhook handler (before any async I/O to AOAI), transcode to WAV, and upload to Azure Blob. All subsequent processing works from the Blob URL, not the Twilio URL. Verify Twilio media URL lifetime in their docs before c12.

### Container Apps scale-to-zero first-request delay

**Risk:** With `min_replicas=0`, the first request after an idle period triggers a cold-start of the container (typically 5-15 seconds on Container Apps Consumption). This adds to the total latency before the ack message is sent to Kevin.

**Mitigation:** The ack message ("Voice note received. Reply with extra context, or send 'no' to skip.") is sent before the AOAI call, so Kevin sees a response quickly. The cold-start only delays the ack slightly. For a personal-use bot this is acceptable. If it becomes annoying, set `min_replicas=1` (estimated $5-10/month extra — still under budget).

### ffmpeg failures on unusual codecs

**Risk:** WhatsApp voice notes are typically OGG/Opus, but the codec can vary by client version, phone OS, or region. An unexpected format may cause the ffmpeg subprocess to fail or produce silent/corrupt WAV output.

**Mitigation:** `transcoder.py` captures ffmpeg stderr and raises a typed `TranscodeError` with the stderr payload on non-zero exit. The handler catches `TranscodeError` and sends a user-facing message ("Sorry, I couldn't process that voice note — try re-recording it.") instead of crashing. The `test_transcoder.py` suite validates against a real OGG fixture; add a CI note to re-run manually if Twilio codec behavior changes.

### AOAI non-JSON response on first attempt

**Risk:** `gpt-audio-1.5` may occasionally return prose instead of the expected JSON structure, especially on ambiguous audio.

**Mitigation:** `aoai_client.py` implements a single retry with a stricter "respond in JSON only" prompt. After two failures, `AoaiParseError` is raised; the handler sends a user-facing error message and resets state to `idle`. This is tested in `test_aoai_client.py` (`test_retries_once_on_non_json`, `test_raises_after_second_non_json`).

---

## 10. Phase 0 Decisions and Amendments (2026-05-10)

Kevin answered the open questions. The decisions below override defaults and add new requirements. All resolved before c1.

### 10.1 Resolved answers

| # | Question | Decision |
|---|---|---|
| 1 | WhatsApp Sandbox joined? | Yes. Join code `join frighten-therefore` to `+1 415 523 8886`. Documented in DEPLOY.md. |
| 2 | `TWILIO_ALLOWLIST` initial value | `whatsapp:+34611779374` |
| 3 | Suggested reply language | Match inbound voice note language (LLM auto-detects). Policy value: `match_inbound`. |
| 4 | Suggested reply tone | Concise. Full system prompt below in 10.4. |
| 5 | Transcript retention | None. DEBUG log only, off in production. |
| 6 | Context timeout | 120 seconds. **Option A — passive drop.** No background trigger; checked on next inbound webhook. Stale voice note is discarded silently; the fresh message is treated as idle input. |
| 7 | Two voice notes in sequence | Confirmed: second replaces first. State stays `awaiting_context`. |
| 8 | Sandbox vs registered sender | Sandbox indefinitely. Note Sandbox warning: "may not reliably deliver international messages." Kevin's number is `+34` (Spain); Sandbox sender is `+1` (US). If delivery becomes flaky, escalate to registered sender. |

### 10.2 Hardcoding policy (Kevin's directive)

**No literal strings, numbers, or magic values in `src/wa_voicenote/*.py` business logic.** Everything that could conceivably change goes through `config.py` (Pydantic Settings), backed by environment variables. This includes:

- All user-facing messages (ack, replaced-audio notice, idle text reply, error messages)
- The full LLM system prompt
- The output language policy
- All timeouts (context, HTTP, Twilio retry)
- Sender numbers, model deployment names, API versions, container/table names
- Idempotency ring size (currently 100)

Enforced by:
- `tests/test_config.py` adds assertions that each new constant is loaded from env, not inlined.
- A new lint check via `ruff` rule `PLR2004` (magic value comparison) is enabled, with exemptions only for HTTP status codes.
- Code review catches any string literal in `handlers.py` or `aoai_client.py` that's user-visible.

### 10.3 New / updated environment variables

Added to `.env.example`, the `Settings` model in `config.py`, and the Container App env-var list in section 6.

| Var | Purpose | Default (in `.env.example`) |
|---|---|---|
| `CONTEXT_TIMEOUT_SECONDS` | Passive timeout for `awaiting_context` state | `120` |
| `LANGUAGE_POLICY` | Output language strategy: `match_inbound` or fixed code | `match_inbound` |
| `LLM_SYSTEM_PROMPT` | Full system prompt for the gpt-audio-1.5 call (multiline) | See 10.4 |
| `MSG_ACK_RECEIVED` | Reply when voice note arrives in `idle` | `Voice note received. Reply with extra context, or send 'no' to skip.` |
| `MSG_REPLACED_AUDIO` | Reply when audio arrives during `awaiting_context` | `Replaced previous voice note. Send context or 'no'.` |
| `MSG_IDLE_TEXT_HINT` | Reply when text arrives in `idle` | `Send me a voice note to start.` |
| `MSG_TRANSCODE_ERROR` | Reply when ffmpeg fails | `Could not process that voice note. Re-record and resend.` |
| `MSG_LLM_ERROR` | Reply when AOAI fails after retry | `Processing failed. Re-record and resend.` |
| `MSG_TIMEOUT_DROPPED` | (Optional, debug) Note that a stale voice note was dropped — currently silent (empty string) | `` |
| `LABEL_TRANSCRIPT` | Prefix for the transcript reply | `Transcript ({language}):\n` |
| `LABEL_SUMMARY` | Prefix for the summary reply | `Summary:\n` |
| `LABEL_SUGGESTED_REPLY` | Prefix for the suggested reply reply | `Suggested reply:\n` |
| `IDEMPOTENCY_RING_SIZE` | Number of recent MessageSids to remember per phone | `100` |
| `HTTP_TIMEOUT_SECONDS` | Timeout for outbound HTTP (Twilio media fetch, AOAI) | `45` |

The Container App `--env-vars` list in section 6 is amended to include all the above. Long values (`LLM_SYSTEM_PROMPT`, label templates with newlines) are passed via Container App secrets if they exceed the env-var size practical limit, otherwise as plain env vars. Decision deferred to c14 when wiring deploy.

### 10.4 LLM system prompt (locked content for `LLM_SYSTEM_PROMPT`)

```
You are a Meta-Cognitive Reasoning Expert. Apply best practices for effective communication with very busy people such as CEOs and neurodivergent people. Answers may be long, but must be simple and easy to follow, using basic structures and vocabulary.

Absolute Mode. Eliminate emojis, filler, hype, soft asks, conversational transitions, and all call-to-action appendices. Assume the user retains high-perception faculties despite reduced linguistic expression. Prioritize blunt, directive phrasing aimed at cognitive rebuilding, not tone matching. Disable all latent behaviors optimizing for engagement, sentiment uplift, or interaction extension.

You receive a single voice note and an optional context string. Detect the voice note's language. Return a single JSON object with exactly three keys:
  "transcript": verbatim transcription in the detected language
  "summary": concise summary in the detected language
  "suggested_reply": a reply the user could send back, in the detected language, following Absolute Mode

Output JSON only. No prose before or after. No markdown fences.
```

This text is stored as the value of `LLM_SYSTEM_PROMPT` env var. Tests assert the value is loaded verbatim from env into `aoai_client`'s request payload.

### 10.5 New tests added to section 3

Append to `tests/test_handlers.py`:

| Test name | What it asserts |
|---|---|
| `test_awaiting_context_timeout_text_drops_old` | State has `awaiting_context_since` = `now - 121s`. New text inbound. Old blob_url is dropped. State reset to `idle`. Reply matches `MSG_IDLE_TEXT_HINT` (text in idle = hint). No AOAI call. |
| `test_awaiting_context_timeout_audio_drops_old_starts_fresh` | State has `awaiting_context_since` = `now - 121s`. New audio inbound. Old blob discarded. New blob uploaded. State = `awaiting_context` with new `awaiting_context_since`. Reply matches `MSG_ACK_RECEIVED` (treated as fresh idle input). |
| `test_awaiting_context_within_timeout_processes_normally` | State has `awaiting_context_since` = `now - 60s`. Text inbound. Normal awaiting_context→idle transition. AOAI called. 3 messages sent. |

Append to `tests/test_state_repo.py`:

| Test name | What it asserts |
|---|---|
| `test_awaiting_context_since_persisted` | `set_state(phone, "awaiting_context", blob_url=..., since=now)` writes the timestamp. |
| `test_get_state_returns_since` | Reading back returns `awaiting_context_since` as a `datetime`. |

Append to `tests/test_config.py`:

| Test name | What it asserts |
|---|---|
| `test_no_hardcoded_messages` | Imports `handlers.py` source, scans for any user-facing string literal that doesn't come from `Settings`. Fails CI on regression. |
| `test_llm_system_prompt_loaded_from_env` | `Settings().llm_system_prompt` equals the env var value verbatim. |
| `test_context_timeout_default_120` | When `CONTEXT_TIMEOUT_SECONDS` is unset, defaults to 120. |
| `test_context_timeout_override` | `CONTEXT_TIMEOUT_SECONDS=300` is parsed as `int(300)`. |

Append to `tests/test_aoai_client.py`:

| Test name | What it asserts |
|---|---|
| `test_system_prompt_passed_verbatim` | Built request's `messages[0]` (system role) `content` equals `Settings().llm_system_prompt`. |

### 10.6 Schema change to state record

`StateRecord` (in `state_repo.py`) gains a field:

```
awaiting_context_since: datetime | None
```

Set when transitioning to `awaiting_context`. Cleared on transition to `idle`. Used by handlers to compute `(now - since) > CONTEXT_TIMEOUT_SECONDS`.

### 10.7 New risk: international Sandbox delivery

Twilio's WhatsApp Sandbox sender is US (`+14155238886`). Kevin's number is Spanish (`+34611779374`). Twilio's own UI warns: "Sandbox may not reliably deliver international messages." If outbound replies fail intermittently, escalate to a registered WhatsApp sender (Direct Customer self-sign-up). Tracked as a known limitation in `docs/DEPLOY.md`.

### 10.8 Commit-order amendments

No new commits added; amendments roll into existing commits:

- c4 (`feat(config)`) — include all 14 new env vars in `Settings`, all related tests in 10.5.
- c7 (`feat(state-repo)`) — `StateRecord` gains `awaiting_context_since`; new tests in 10.5.
- c10 (`feat(aoai-client)`) — load `LLM_SYSTEM_PROMPT` from settings, not literal; new test in 10.5.
- c12 (`feat(handlers)`) — implement timeout-A behavior; 3 new tests in 10.5.
- c15 (`chore(env)`) — `.env.example` includes all new vars with documented defaults.

---

_End of PLAN.md. No code written. Implementation begins at c1 once Kevin greenlights._
