# infra/

Bicep declarative source-of-truth for the **foundation** Azure resources
behind wa-voicenote-triage.

## Split: what Bicep owns vs. what the deploy workflow owns

| Layer | Owner | Why |
|---|---|---|
| Storage account, table, blob container, lifecycle policy | **Bicep** | Stable shape; safe to re-apply |
| Log Analytics + Application Insights | **Bicep** | Stable shape; safe to re-apply |
| Azure OpenAI account + `gpt-audio-mini` deployment | **Bicep** | Stable shape; safe to re-apply |
| Container Apps Environment | **Bicep** | Stable shape; safe to re-apply |
| Container App itself (image, env vars, secrets, MI role assignments) | **deploy workflow** (`az containerapp`) | Mutable surface; if Bicep owned this, every `az deployment group create` without the complete secrets/env-vars list would wipe them |

If we ever need to recreate the Container App from scratch, the
imperative commands used at c14 are documented in `docs/PLAN.md` §6.

## Files

| File | Purpose |
|---|---|
| `main.bicep` | Foundation resources (everything except the Container App) |
| `main.parameters.json` | Non-sensitive parameters (region, names, SKU choices) |

Secrets (Twilio token, AOAI key, App Insights connection string, `DIAG_TOKEN`,
`LLM_SYSTEM_PROMPT`) are managed via `az containerapp secret set` and never
appear in this directory.

## Deploy

Dry-run (what-if diff):

```bash
az deployment group what-if \
  --resource-group rg-wa-voicenote \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json
```

Apply:

```bash
az deployment group create \
  --resource-group rg-wa-voicenote \
  --template-file infra/main.bicep \
  --parameters infra/main.parameters.json
```

Idempotent: re-running updates resources in place when properties drift.
Safe to re-apply since the Container App is not in scope.

## Outputs

- `appInsightsConnectionString` — used by the app's structlog + OTel
- `aoaiEndpoint` — base URL for the Azure OpenAI account
- `containerAppsEnvId` — resource ID the Container App attaches to

## Container App secrets manifest

The Container App requires these secrets to be set before its image will
boot successfully. Values are NEVER committed. Use:

```bash
az containerapp secret set --name wa-voicenote --resource-group rg-wa-voicenote \
  --secrets twilio-auth-token=<value> aoai-api-key=<value> ...
```

| Secret name | Maps to env var | Purpose |
|---|---|---|
| `twilio-auth-token` | `TWILIO_AUTH_TOKEN` | Twilio Programmable Messaging Basic auth |
| `aoai-api-key` | `AZURE_OPENAI_API_KEY` | Optional — when set, AOAI client uses api-key auth instead of Managed Identity. Recommended in prod: leave unset and rely on MI. |
| `appinsights-conn` | `APPLICATIONINSIGHTS_CONNECTION_STRING` | App Insights telemetry exporter connection string |
| `diag-token` | `DIAG_TOKEN` | Bearer token for the `/diag` endpoint |
| `llm-system-prompt` | `LLM_SYSTEM_PROMPT` | LLM system prompt (template, not actually a secret — moved here only because Bicep does not manage env vars on the Container App; safe to migrate to a plain env var later) |

## Container App env vars (non-secret)

| Env var | Value |
|---|---|
| `TWILIO_ACCOUNT_SID` | Twilio account SID (begins `AC...`) |
| `TWILIO_FROM` | `whatsapp:+14155238886` (Sandbox sender) |
| `TWILIO_ALLOWLIST` | `whatsapp:+<your-E.164>` (comma-list) |
| `AZURE_OPENAI_ENDPOINT` | `https://aoai-wa-voicenote.openai.azure.com/` |
| `AZURE_OPENAI_DEPLOYMENT` | `gpt-audio-mini` |
| `AZURE_OPENAI_API_VERSION` | `2025-04-01-preview` |
| `AZURE_STORAGE_ACCOUNT` | `stwavoicenote` |
| `AZURE_STORAGE_TABLE` | `convstate` |
| `AZURE_STORAGE_CONTAINER` | `audio-staging` |
| `LOG_LEVEL` | `INFO` |
| `ENV_NAME` | `prod` |
| `OTEL_SERVICE_NAME` | `wa-voicenote-triage` |
| `CONTEXT_TIMEOUT_SECONDS` | `120` |

## What is intentionally NOT here

- Container App resource definition (see split table above)
- Image tag — owned by the deploy workflow (`az containerapp update --image`)
- Budget alerts — set imperatively once via `az rest`
- OIDC federated identity for GitHub Actions — set imperatively once
