// wa-voicenote-triage infrastructure as code.
//
// Declarative source-of-truth for the resources that were provisioned
// imperatively at the start of c14. Re-running this deployment is idempotent:
// existing resources are updated in place when their properties drift from
// what this file declares.
//
// Scope: resource group. Deploy with:
//   az deployment group create \
//     --resource-group rg-wa-voicenote \
//     --template-file infra/main.bicep \
//     --parameters infra/main.parameters.json
//
// Secrets are NOT in this file. They live as Container App secrets configured
// via `az containerapp secret set` (or the workflow). See HANDOFF.md for the
// list of required secret names.

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

@description('Container App name.')
param containerAppName string = 'wa-voicenote'

@description('Container App image (tag updated by the deploy workflow on each push to main).')
param containerImage string = 'mcr.microsoft.com/k8se/quickstart:latest'

@description('Min replicas (scale-to-zero supported).')
param minReplicas int = 0

@description('Max replicas.')
param maxReplicas int = 2

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

resource containerApp 'Microsoft.App/containerApps@2024-10-02-preview' = {
  name: containerAppName
  location: location
  tags: commonTags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        traffic: [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      // Secrets are managed out-of-band via `az containerapp secret set` so
      // their values never appear in the template or its parameter file.
      // Listed here as references that the image's env vars consume.
    }
    template: {
      containers: [
        {
          name: containerAppName
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
      }
    }
  }
}

// -----------------------------------------------------------------------------
// Role assignments for Container App system-assigned identity
// -----------------------------------------------------------------------------

// Built-in role definition IDs
var aoaiUserRoleId = '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd'              // Cognitive Services OpenAI User
var tableDataContributorRoleId = '0a9a7e1f-b9d0-4cc4-a60d-0319b160aaa3'  // Storage Table Data Contributor
var blobDataContributorRoleId = 'ba92f5b4-2d11-453d-a403-e96b0029c9fe'   // Storage Blob Data Contributor

resource aoaiRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: aoai
  name: guid(aoai.id, containerApp.id, aoaiUserRoleId)
  properties: {
    principalId: containerApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', aoaiUserRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource tableRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, containerApp.id, tableDataContributorRoleId)
  properties: {
    principalId: containerApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', tableDataContributorRoleId)
    principalType: 'ServicePrincipal'
  }
}

resource blobRoleAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: storage
  name: guid(storage.id, containerApp.id, blobDataContributorRoleId)
  properties: {
    principalId: containerApp.identity.principalId
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', blobDataContributorRoleId)
    principalType: 'ServicePrincipal'
  }
}

// -----------------------------------------------------------------------------
// Outputs
// -----------------------------------------------------------------------------

output containerAppFqdn string = containerApp.properties.configuration.ingress.fqdn
output containerAppPrincipalId string = containerApp.identity.principalId
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output aoaiEndpoint string = aoai.properties.endpoint
