// SPEC-M3 §4F: Phase 1 Container Apps -- deployed AFTER platform.bicep and
// after both images have been built into the registry it created (see that
// file's header and azure-deploy.yml for the full bootstrap order).
targetScope = 'resourceGroup'

@minLength(3)
@maxLength(11)
param namePrefix string = 'armiem3'

param location string = resourceGroup().location

@description('platform.bicep output: acr.properties.loginServer')
param acrLoginServer string

@description('platform.bicep output: the user-assigned managed identity resource ID')
param identityId string

@description('platform.bicep output: that identity\'s client ID. DefaultAzureCredential needs AZURE_CLIENT_ID set to this value to pick the right identity for a user-assigned Managed Identity (SPEC-M3 §4A/OD-23).')
param identityClientId string

@description('platform.bicep output: the web-only identity (AcrPull only, no Azure OpenAI role) -- independent review finding: the web and API apps must not share an identity, since the web container never calls Azure OpenAI and a code-execution bug in it should not be able to.')
param webIdentityId string

@description('platform.bicep output: the Log Analytics workspace name. Referenced here via `existing` and listKeys() so its shared key never has to cross a template boundary as a plain parameter value.')
param logAnalyticsName string

param appInsightsConnectionString string

param azureOpenAiEndpoint string

@description('Name of an EXISTING deployment on the Azure OpenAI account. No default on purpose (independent review finding): a prior version defaulted to \'gpt-4o-mini\', which does not exist on every account, and Bicep applied that default silently when a caller forgot to pass this -- the deployment "succeeded" while the model-backed answer path was actually broken.')
param azureOpenAiTextDeployment string

@description('Name of an EXISTING deployment on the Azure OpenAI account, used for vision-grounded calls. No default, for the same reason as azureOpenAiTextDeployment.')
param azureOpenAiVisionDeployment string

param azureOpenAiApiVersion string = '2024-10-21'

@description('Full ACR image reference for the API container, e.g. <acrLoginServer>/armie-api:<tag>. No default: azure-deploy.yml supplies it after building the image.')
param apiImage string

@description('Full ACR image reference for the web container, e.g. <acrLoginServer>/armie-web:<tag>.')
param webImage string

@description('SPEC-M4: postgresql:// DSN for ConversationStore/AuditStore, with no password (AAD-only, OD-26) -- e.g. postgresql://<identityName>@<data.bicep serverFqdn>:5432/<databaseName>?sslmode=require. Empty (the default) keeps the in-memory conversation store + local JSONL audit file, exactly like every deployment before this milestone; the Postgres data tier (infra/bicep/data.bicep) is deployed as a deliberate, separate step, not automatically by this template, because unlike Container Apps it bills continuously once created (SPEC-M4 cost note) -- mirrors how azureOpenAiAccountName above is never auto-created either.')
param databaseUrl string = ''

var containerAppsEnvName = '${namePrefix}-env'
var apiAppName = '${namePrefix}-api'
var webAppName = '${namePrefix}-web'

// Conditionally appended (not a fixed-length env array with an empty
// value): an empty DATABASE_URL env var would still satisfy Settings'
// `str | None = None` field as the *string* "", not None, which is not the
// same opt-out this project's other opt-in settings rely on (compare
// otel_exporter_connection_string, which is genuinely absent, not "").
var databaseEnv = empty(databaseUrl) ? [] : [
  { name: 'DATABASE_URL', value: databaseUrl }
  // Always true when a URL is supplied: this project's only supported
  // Postgres auth path in Azure is Entra ID/Managed Identity (OD-26) --
  // there is no scenario in this deployment template where a databaseUrl
  // is set but should be treated as password-based.
  { name: 'DATABASE_USE_MANAGED_IDENTITY', value: 'true' }
]

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' existing = {
  name: logAnalyticsName
}

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: containerAppsEnvName
  location: location
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
  }
}

// Internal-only: the API is never exposed to the public internet directly
// (a design decision, not an OD -- reversible by editing this file). Only
// the web app's nginx reverse proxy reaches it, over the Container Apps
// environment's internal DNS, computable here without a second deployment
// pass because Container Apps' internal hostname pattern is deterministic
// from the app name and the environment's default domain.
//
// https, not http (verified live during SPEC-M3's first real deployment,
// not assumed): Container Apps' Envoy-based ingress edge terminates TLS
// for both internal and external apps uniformly -- the internal FQDN is
// only reachable over HTTPS at the ingress layer regardless of this app's
// own `ingress.transport`/`allowInsecure` settings below, which control
// the separate hop from that edge to the container's own port 8000, not
// what scheme a caller (nginx, here) must use to reach the edge itself.
// Using http here produced Azure's own "stopped or does not exist" 404
// page instead of a connection error -- confirmed the request reached
// Azure's platform routing, which then rejected the plain-HTTP internal
// request rather than failing to resolve/connect.
var apiInternalUrl = 'https://${apiAppName}.internal.${containerAppsEnv.properties.defaultDomain}'

resource apiApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: apiAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acrLoginServer
          identity: identityId
        }
      ]
      ingress: {
        external: false
        targetPort: 8000
        transport: 'http'
        allowInsecure: true
      }
    }
    template: {
      // Pinned to exactly one replica (OD-22, SPEC-M3 §11). SPEC-M4 (OD-25)
      // makes conversation context and audit events durable via Postgres
      // when databaseUrl is set above, but does not lift this pin:
      // app.state.requests still holds live, in-process asyncio.Task
      // references used for request cancellation, which have no
      // serializable cross-replica representation and would still silently
      // fragment across replicas.
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json('0.5')
            memory: '1Gi'
          }
          env: concat([
            { name: 'LLM_PROVIDER', value: 'azure' }
            { name: 'AZURE_OPENAI_ENDPOINT', value: azureOpenAiEndpoint }
            { name: 'AZURE_OPENAI_API_VERSION', value: azureOpenAiApiVersion }
            { name: 'AZURE_OPENAI_TEXT_DEPLOYMENT', value: azureOpenAiTextDeployment }
            { name: 'AZURE_OPENAI_VISION_DEPLOYMENT', value: azureOpenAiVisionDeployment }
            { name: 'OTEL_EXPORTER_CONNECTION_STRING', value: appInsightsConnectionString }
            // Managed Identity only (OD-23): no API-key env var exists
            // anywhere in this template.
            { name: 'AZURE_CLIENT_ID', value: identityClientId }
          ], databaseEnv)
        }
      ]
    }
  }
}

resource webApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: webAppName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${webIdentityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      activeRevisionsMode: 'Single'
      registries: [
        {
          server: acrLoginServer
          identity: webIdentityId
        }
      ]
      ingress: {
        external: true
        targetPort: 80
        transport: 'auto'
      }
    }
    template: {
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
      containers: [
        {
          name: 'web'
          image: webImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            // Same-origin reverse proxy target (SPEC-M3 §4E/§7): keeps the
            // browser talking to one origin, so apps/api's CORS policy
            // never needs to change for this deployment.
            { name: 'API_BACKEND_URL', value: apiInternalUrl }
          ]
        }
      ]
    }
  }
  dependsOn: [
    apiApp
  ]
}

output webAppFqdn string = webApp.properties.configuration.ingress.fqdn
output apiInternalUrl string = apiInternalUrl
