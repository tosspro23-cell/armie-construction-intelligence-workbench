// SPEC-M8 §D: Blob Storage for cited PDF evidence-crop persistence
// (DocumentAnalyzer.crop_evidence, apps/api/app/tools/document/analyzer.py).
// Deployed after platform.bicep (needs the API's managed identity to exist)
// and independently of apps.bicep, the same layering as data.bicep.
//
// Plain Blob Storage, not ADLS Gen2 (OD-37, D-020): this project's actual
// need is opaque-filename PNG lookup, no folder hierarchy, no analytics
// workload -- ADLS Gen2's hierarchical namespace has no use here.
//
// COST NOTE: Standard LRS blob storage for a handful of PNG crops a few KB
// each is negligible (well under $0.01/month at this project's scale) --
// unlike data.bicep's Postgres server, there is no meaningful always-on
// cost consideration here.
targetScope = 'resourceGroup'

@minLength(3)
@maxLength(11)
param namePrefix string = 'armiem3'

param location string = resourceGroup().location

@description('The API app\'s managed identity (platform.bicep output: identityPrincipalId/identityName) -- the sole writer of evidence crops and the sole reader for GET /api/v1/evidence/{filename}, so it (not a separate operator identity, unlike SPEC-M7\'s Azure AI Search setup) gets Storage Blob Data Contributor directly.')
param apiIdentityPrincipalId string

param apiIdentityName string

// Storage account names must be globally unique, lowercase, no hyphens,
// 3-24 characters -- uniqueString() keeps this deployable more than once
// across subscriptions without a manual name collision. take(...,24)
// guarantees the length bound holds even at namePrefix's declared
// maximum (11 chars), not only for the "armiem3" value actually used.
var storageAccountName = take(toLower('${namePrefix}evid${uniqueString(resourceGroup().id)}'), 24)
var storageBlobDataContributorRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'ba92f5b4-2d11-453d-a403-e96b0029c9fe')

resource storage 'Microsoft.Storage/storageAccounts@2023-01-01' = {
  name: storageAccountName
  location: location
  sku: {
    name: 'Standard_LRS'
  }
  kind: 'StorageV2'
  properties: {
    minimumTlsVersion: 'TLS1_2'
    allowBlobPublicAccess: false
    // Managed Identity only (OD-23 extended, D-020): no API-key/
    // connection-string code path exists in apps/api/app/evidence_storage.py
    // at all -- disabling shared-key access here makes that a platform
    // guarantee, not merely an application convention (mirrors D-014's
    // passwordAuth: 'Disabled' on the Postgres server and D-018's
    // disableLocalAuth on the Azure AI Search service).
    allowSharedKeyAccess: false
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

resource evidenceContainer 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'evidence'
  properties: {
    publicAccess: 'None'
  }
}

resource evidenceBlobDataContributor 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, apiIdentityName, storageBlobDataContributorRoleId)
  scope: storage
  properties: {
    roleDefinitionId: storageBlobDataContributorRoleId
    principalId: apiIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output storageAccountName string = storage.name
output blobEndpoint string = storage.properties.primaryEndpoints.blob
output containerName string = evidenceContainer.name
