// SPEC-M4 §G: Azure Database for PostgreSQL Flexible Server for
// ConversationStore/AuditStore (apps/api/app/persistence/postgres_store.py).
// Deployed after platform.bicep (needs the API's managed identity to exist)
// and independently of apps.bicep -- the Container Apps in that file take
// this template's outputs as parameters the same way they take
// platform.bicep's.
//
// COST NOTE (SPEC-M4 "Owner decisions"): unlike Container Apps, which this
// project already scales via minReplicas, a Postgres Flexible Server bills
// continuously once created, even on the cheapest Burstable tier -- do not
// run this against a real subscription without the owner's explicit
// go-ahead on that always-on cost.
targetScope = 'resourceGroup'

@minLength(3)
@maxLength(11)
param namePrefix string = 'armiem3'

param location string = resourceGroup().location

@description('The API app\'s managed identity (platform.bicep output: identityId) -- becomes this server\'s sole Microsoft Entra administrator. A dedicated, least-privilege AAD role for the API identity is not set up here: this is a single-app, single-identity deployment (no multi-tenant scenario yet), and doing least-privilege properly needs a post-deploy SQL step (CREATE ROLE ... IN ROLE azure_ad_user) Bicep cannot express. A simplifying default for this milestone, not an owner decision -- flagged the same way platform.bicep flags azureOpenAiAccountName not being created by that template.')
param apiIdentityPrincipalId string

@description('The API app managed identity\'s display name, required by the Flexible Server AAD administrator resource alongside its principal ID.')
param apiIdentityName string

param tenantId string = subscription().tenantId

var serverName = '${namePrefix}-pg-${uniqueString(resourceGroup().id)}'

resource postgres 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  sku: {
    // Cheapest tier (SPEC-M4 cost note): Burstable B1ms. Still bills
    // 24/7 unlike Container Apps -- see the file header before deploying.
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    storage: {
      storageSizeGB: 32
    }
    authConfig: {
      // AAD-only (OD-26): passwordAuth disabled outright means there is no
      // password-based connection path to this server at all, not merely
      // one this project chooses not to use -- continuing OD-23's
      // zero-stored-secret posture onto this project's second Azure data
      // resource as a platform guarantee, not an application convention.
      activeDirectoryAuth: 'Enabled'
      passwordAuth: 'Disabled'
      tenantId: tenantId
    }
    // No VNet/private-endpoint integration in this milestone (SPEC-M3's
    // scope-control precedent: defer infrastructure a capability doesn't
    // yet require). The Container Apps environment in apps.bicep has no
    // VNet integration either, so it can only reach a Postgres Flexible
    // Server over its public endpoint; the firewall rule below is Azure's
    // documented mechanism for "reachable from Azure services," not the
    // open internet, and with passwordAuth disabled a network path alone
    // is not a valid credential -- a caller still needs a real Entra ID
    // token for apiIdentityPrincipalId.
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource allowAzureServices 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2024-08-01' = {
  parent: postgres
  name: 'AllowAllAzureServicesAndResourcesWithinAzureIps'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgres
  name: 'armie'
}

resource aadAdmin 'Microsoft.DBforPostgreSQL/flexibleServers/administrators@2024-08-01' = {
  parent: postgres
  name: apiIdentityPrincipalId
  properties: {
    principalType: 'ServicePrincipal'
    principalName: apiIdentityName
    tenantId: tenantId
  }
}

output serverName string = postgres.name
output serverFqdn string = postgres.properties.fullyQualifiedDomainName
// Fixed, not parameterized: one application, one logical database,
// matching this milestone's narrow scope. Callers build the full
// postgresql:// DSN by combining this with serverFqdn.
output databaseName string = database.name
