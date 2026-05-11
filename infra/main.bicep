// wa-voicenote-triage infrastructure as code.
//
// Declarative source-of-truth for the FOUNDATION resources behind the app:
// Storage account (table + blob + lifecycle), Log Analytics, Application
// Insights, Azure OpenAI account + model deployment, and the Container Apps
// Environment.
//
// IMPORTANT: The Container App itself is NOT declared here. It is managed
// imperatively by `az containerapp create` + `az containerapp update` via the
// deploy workflow. Reason: declaring the Container App in Bicep without ALSO
// declaring every secret and env var would silently wipe them on any
// `az deployment group create`. Splitting the Container App out keeps Bicep
// idempotent and safe to re-run, while the deploy workflow owns the mutable
// app surface (image tag, env vars, secrets, MI role assignments).
//
// Scope: resource group. Deploy with:
//   az deployment group create \
//     --resource-group rg-wa-voicenote \
//     --template-file infra/main.bicep \
//     --parameters infra/main.parameters.json

targetScope = 'resourceGroup'

@description('Azure region for all resources.')
param location string = resourceGroup().location

@description('Project tag applied to every resource.')
param projectTag string = 'wa-voicenote'

@description('Storage account name. Must be globally unique, 3-24 chars, lowercase letters and digits.')
param storageAccountName string = 'stwavoicenote'

@description('Storage table name for conversation state.')
param storageTableName string = 'convstate'

@description('Storage blob container for staged voice notes.')
param storageContainerName string = 'audio-staging'

@description('Log Analytics workspace name.')
param logAnalyticsName string = 'law-wa-voicenote'

@description('Application Insights component name.')
param appInsightsName string = 'appi-wa-voicenote'

@description('Azure OpenAI account name.')
param aoaiAccountName string = 'aoai-wa-voicenote'

@description('Azure OpenAI deployment name for the audio model.')
param aoaiDeploymentName string = 'gpt-audio-mini'

@description('Azure OpenAI model name.')
param aoaiModelName string = 'gpt-audio-mini'

@description('Azure OpenAI model version.')
param aoaiModelVersion string = '2025-12-15'

@description('Azure OpenAI deployment SKU.')
param aoaiSkuName string = 'GlobalStandard'

@description('Azure OpenAI deployment capacity (TPM thousands).')
param aoaiSkuCapacity int = 30

@description('Container Apps Environment name.')
param containerAppsEnvName string = 'cae-wa-voicenote'

var commonTags = {
  project: projectTag
}

// -----------------------------------------------------------------------------
// Log Analytics + Application Insights
// -----------------------------------------------------------------------------

resource law 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: logAnalyticsName
  location: location
  tags: commonTags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: appInsightsName
  location: location
  kind: 'web'
  tags: commonTags
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: law.id
  }
}

// -----------------------------------------------------------------------------
// Storage account + table + blob container with 24h lifecycle delete
// -----------------------------------------------------------------------------

resource storage 'Microsoft.Storage/storageAccounts@2024-01-01' = {
  name: storageAccountName
  location: location
  tags: commonTags
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    accessTier: 'Hot'
    allowBlobPublicAccess: false
    minimumTlsVersion: 'TLS1_2'
    supportsHttpsTrafficOnly: true
  }
}

resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2024-01-01' = {
  parent: storage
  name: 'default'
}

resource convstateTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2024-01-01' = {
  parent: tableService
  name: storageTableName
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2024-01-01' = {
  parent: storage
  name: 'default'
}

resource audioStagingContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2024-01-01' = {
  parent: blobService
  name: storageContainerName
  properties: {
    publicAccess: 'None'
  }
}

resource lifecyclePolicy 'Microsoft.Storage/storageAccounts/managementPolicies@2024-01-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'delete-old-audio'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: ['blockBlob']
              prefixMatch: ['${storageContainerName}/']
            }
            actions: {
              baseBlob: {
                delete: {
                  daysAfterModificationGreaterThan: 1
                }
              }
            }
          }
        }
      ]
    }
  }
}

// -----------------------------------------------------------------------------
// Azure OpenAI
// -----------------------------------------------------------------------------

resource aoai 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: aoaiAccountName
  location: location
  kind: 'OpenAI'
  tags: commonTags
  sku: {
    name: 'S0'
  }
  properties: {
    customSubDomainName: aoaiAccountName
    publicNetworkAccess: 'Enabled'
  }
}

resource aoaiDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: aoai
  name: aoaiDeploymentName
  sku: {
    name: aoaiSkuName
    capacity: aoaiSkuCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: aoaiModelName
      version: aoaiModelVersion
    }
  }
}

// -----------------------------------------------------------------------------
// Container Apps Environment + Container App
// -----------------------------------------------------------------------------

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2024-10-02-preview' = {
  name: containerAppsEnvName
  location: location
  tags: commonTags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: law.properties.customerId
        sharedKey: law.listKeys().primarySharedKey
      }
    }
  }
}

// NOTE: The Container App and its role assignments live outside Bicep — see
// the header comment for the rationale. Use `az containerapp create` /
// `az containerapp update` via the deploy workflow.

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

output appInsightsConnectionString string = appInsights.properties.ConnectionString
output aoaiEndpoint string = aoai.properties.endpoint
output containerAppsEnvId string = containerAppsEnv.id
