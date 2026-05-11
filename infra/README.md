# infra/

Bicep declarative source-of-truth for the Azure resources backing
wa-voicenote-triage.

## Files

| File | Purpose |
|---|---|
| `main.bicep` | All resources: Storage (table + blob + 24h lifecycle), Log Analytics, App Insights, Azure OpenAI account + `gpt-audio-mini` deployment, Container Apps Environment, Container App, role assignments for the Container App system-assigned identity |
| `main.parameters.json` | Non-sensitive parameters (region, names, SKU choices) |

Secrets (Twilio token, AOAI key, App Insights connection string, `DIAG_TOKEN`,
LLM system prompt) are NOT in Bicep. They are managed out-of-band as Container
App secrets via `az containerapp secret set` (or one-time during provisioning).
This avoids leaking them into the template or its parameter file.

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

## Outputs

- `containerAppFqdn` — the public FQDN of the Container App
- `containerAppPrincipalId` — system-assigned identity principal ID
- `appInsightsConnectionString` — used by the app's structlog + OTel
- `aoaiEndpoint` — base URL for the Azure OpenAI account

## What is intentionally NOT here

- The actual image tag — that is updated by the deploy GitHub Actions
  workflow on every push to `main` via `az containerapp update --image`.
- All secret values (see note above).
- Budget alerts — set imperatively once via `az rest` (see HANDOFF).
