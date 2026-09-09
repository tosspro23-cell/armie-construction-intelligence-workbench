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

@description('Object ID of the principal that deploys/administers this server -- the same OIDC service principal azure-deploy.yml already authenticates as (azure/login, secrets.AZURE_CLIENT_ID), which already holds Contributor + Role Based Access Control Administrator on this resource group (D-012). Becomes the Flexible Server\'s sole Microsoft Entra administrator. NOT the API\'s runtime managed identity (see apiIdentityPrincipalId below) -- an earlier version of this template made the runtime identity itself the AAD admin, which is full database-admin privilege for a container whose actual job is two INSERT/SELECT/UPDATE statements; an independent review correctly flagged that a compromised API process would then be able to modify or delete audit records outright, defeating this project\'s own independent-verification/audit-integrity invariant (D-004). Fixed by separating "who can administer this server" from "what the running app can do to it."')
param deployPrincipalId string

@description('Display name of the deploy principal above, required by the Flexible Server AAD administrator resource alongside its object ID.')
param deployPrincipalName string

@description('\'User\' for a human deploying by hand (e.g. via az login), \'ServicePrincipal\' for azure-deploy.yml\'s OIDC app registration (the expected case).')
@allowed(['User', 'ServicePrincipal'])
param deployPrincipalType string = 'ServicePrincipal'

@description('The API app\'s managed identity (platform.bicep output: identityId/identityPrincipalId/identityName) -- NOT granted admin here. Used only as documentation of which identity apps/api/migrations/0002_grant_api_runtime_role.sql must be run for after this template deploys: that script creates a plain (non-admin) AAD-mapped Postgres role for exactly this identity and grants it SELECT/INSERT/UPDATE on the two SPEC-M4 tables only -- nothing else, not even DELETE (matching AuditStore\'s append-only interface). Kept as an explicit parameter, not just a comment, so a future automation step has something to bind the migration script\'s target identity to without re-deriving it.')
param apiIdentityPrincipalId string

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
    // Server over its public endpoint -- see the firewall rule below for
    // exactly what that means, corrected after an independent review
    // pointed out the previous wording overstated it.
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

// NOT "reachable only from your Azure resources" -- an independent review
// correctly caught an earlier version of this comment overstating that.
// Microsoft's own documentation for this exact 0.0.0.0-0.0.0.0 rule name
// (https://learn.microsoft.com/azure/postgresql/security/security-firewall-rules)
// says it allows connections from *any* Azure resource in *any*
// subscription, including other customers' -- not a network boundary
// scoped to this resource group or this project. With passwordAuth
// disabled server-wide (authConfig above), a network path here is not by
// itself a valid credential -- a caller still needs a real Entra ID token
// for a principal this server actually recognizes -- but this rule should
// not be described as private network isolation, because it is not.
// Tightening this to a real private endpoint / VNet integration is
// deferred (SPEC-M3's scope-control precedent: no VNet in this milestone).
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
  name: deployPrincipalId
  properties: {
    principalType: deployPrincipalType
    principalName: deployPrincipalName
    tenantId: tenantId
  }
}

output serverName string = postgres.name
output serverFqdn string = postgres.properties.fullyQualifiedDomainName
// Fixed, not parameterized: one application, one logical database,
// matching this milestone's narrow scope. Callers build the full
// postgresql:// DSN by combining this with serverFqdn.
output databaseName string = database.name
