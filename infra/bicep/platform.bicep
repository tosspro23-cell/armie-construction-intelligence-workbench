// SPEC-M3 §4F: Phase 1 platform prerequisites -- deployed BEFORE any
// container image exists.
//
// Split from apps.bicep specifically to solve a bootstrap ordering problem:
// `az acr build` needs the registry to already exist, but the Container
// Apps in apps.bicep need the built image tags as parameters. The deploy
// workflow (.github/workflows/azure-deploy.yml) runs this file first,
// builds and pushes both images into the registry it creates here, then
// runs apps.bicep with those image tags.
//
// Scope discipline (SPEC-M3 §5 explicitly excludes AKS, Azure AI Search,
// ADLS Gen2, PostgreSQL/Cosmos DB, and Key Vault from Phase 1): this
// provisions only what the vertical slice needs -- a registry, monitoring,
// and the identity/role wiring so no secret is ever stored in the app
// config or either template.
targetScope = 'resourceGroup'

@description('Short name used to derive resource names. Lowercase alphanumeric, no spaces.')
@minLength(3)
@maxLength(11)
param namePrefix string = 'armiem3'

@description('Azure region for every resource in this template.')
param location string = resourceGroup().location

@description('Name of an EXISTING Azure OpenAI (Cognitive Services) account in this resource group. Not created by this template: model/region availability for a brand-new subscription is too variable to safely automate before this has been run against a real one -- a Phase 1 simplifying default, not an owner decision. Create it out-of-band first (portal or `az cognitiveservices account create`).')
param azureOpenAiAccountName string

var uniqueSuffix = uniqueString(resourceGroup().id)
var acrName = '${namePrefix}acr${uniqueSuffix}'
var identityName = '${namePrefix}-identity'
var webIdentityName = '${namePrefix}-web-identity'
var logAnalyticsName = '${namePrefix}-logs'
var appInsightsName = '${namePrefix}-insights'

// Built-in role definition IDs (fixed GUIDs, documented by Microsoft):
// AcrPull, and Cognitive Services OpenAI User. Verified directly against a
// real subscription (`az role definition list --name "AcrPull"`) during
// SPEC-M3's first real deployment: the AcrPull GUID originally written here
// was wrong (a transcription slip, not a formatting issue) and failed
// deployment with RoleDefinitionDoesNotExist -- not assumed correct from
// memory a second time.
var acrPullRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '7f951dda-4ed3-4680-a7ca-43fe172d538d')
var openAiUserRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')

resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2022-10-01' = {
  name: logAnalyticsName
  location: location
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
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-07-01' = {
  name: acrName
  location: location
  sku: {
    name: 'Basic'
  }
  properties: {
    // No admin user: Container Apps pulls using the managed identity's
    // AcrPull role assignment below, never a stored registry password.
    adminUserEnabled: false
  }
}

// Two identities, not one (independent review finding, SPEC-M3): the web
// app's nginx only ever pulls its own image and reverse-proxies HTTP -- it
// never calls Azure OpenAI -- so it must not hold `Cognitive Services
// OpenAI User`. Sharing one identity between both apps meant a code-
// execution bug in the (public-facing) web container could use that
// identity to call the model directly, bypassing the API's planning,
// verification, and any future rate limiting entirely. `identity` (api)
// gets both roles below; `webIdentity` gets only AcrPull.
resource identity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: identityName
  location: location
}

resource webIdentity 'Microsoft.ManagedIdentity/userAssignedIdentities@2023-01-31' = {
  name: webIdentityName
  location: location
}

resource acrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, identity.id, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource webAcrPullAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(acr.id, webIdentity.id, acrPullRoleId)
  scope: acr
  properties: {
    roleDefinitionId: acrPullRoleId
    principalId: webIdentity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

resource azureOpenAi 'Microsoft.CognitiveServices/accounts@2023-05-01' existing = {
  name: azureOpenAiAccountName
}

resource openAiUserAssignment 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(azureOpenAi.id, identity.id, openAiUserRoleId)
  scope: azureOpenAi
  properties: {
    roleDefinitionId: openAiUserRoleId
    principalId: identity.properties.principalId
    principalType: 'ServicePrincipal'
  }
}

output acrName string = acr.name
output acrLoginServer string = acr.properties.loginServer
output identityId string = identity.id
output identityClientId string = identity.properties.clientId
output webIdentityId string = webIdentity.id
output webIdentityClientId string = webIdentity.properties.clientId
// The workspace *name* only, never its keys: apps.bicep looks the workspace
// up again with `existing` and calls listKeys() itself, so a Log Analytics
// shared key never has to cross a template boundary as a plain output.
output logAnalyticsName string = logAnalytics.name
output appInsightsConnectionString string = appInsights.properties.ConnectionString
output azureOpenAiEndpoint string = azureOpenAi.properties.endpoint
