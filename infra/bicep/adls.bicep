// SPEC-M9 §G: Azure Data Lake Storage Gen2 for the multi-project workspace
// (ServiceContainer.get_project, apps/api/app/services.py). Deployed after
// platform.bicep (needs the API's managed identity to exist) and
// independently of apps.bicep, the same layering as data.bicep/
// evidence.bicep.
//
// ADLS Gen2 (isHnsEnabled: true), not plain Blob Storage -- the opposite
// choice from evidence.bicep (D-020), and deliberately so (D-023): that
// milestone's need was opaque-filename lookup with no hierarchy; this
// milestone's need is real per-project directory organization
// (`projects/<project_id>/{ifc,pdf}/...`) with real directory ACLs, which
// only Gen2's hierarchical namespace provides.
//
// COST NOTE: Standard LRS for a handful of small IFC/PDF files (this
// milestone's two synthetic projects) is negligible, the same order of
// magnitude as evidence.bicep's own cost note.
targetScope = 'resourceGroup'

@minLength(3)
@maxLength(11)
param namePrefix string = 'armiem3'

param location string = resourceGroup().location

@description('The API app\'s managed identity (platform.bicep output: identityPrincipalId/identityName) -- must be able to read any project a caller selects, so no narrower scope than the whole filesystem is possible for the identity that actually serves requests. Read-only: the app never writes to ADLS, only downloads (SPEC-M9 §C).')
param apiIdentityPrincipalId string

param apiIdentityName string

// Storage account names must be globally unique, lowercase, no hyphens,
// 3-24 characters -- uniqueString() keeps this deployable more than once
// across subscriptions without a manual name collision. take(...,24)
// guarantees the length bound holds even at namePrefix's declared maximum
// (11 chars), matching evidence.bicep's own BCP335 fix.
var storageAccountName = take(toLower('${namePrefix}proj${uniqueString(resourceGroup().id)}'), 24)
var storageBlobDataReaderRoleId = subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '2a2b9908-6ea1-4ae2-8e65-a410df84e7d1')

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
    // Managed Identity only (OD-23 extended, D-023): no API-key path
    // exists in apps/api/app/adls.py at all -- disabling shared-key
    // access here makes that a platform guarantee, mirroring
    // evidence.bicep/D-014/D-018's own precedent for every other Azure
    // data resource in this project.
    allowSharedKeyAccess: false
    // The defining feature this milestone actually needs (D-023's
    // Rationale): a hierarchical namespace, real directory semantics, and
    // real directory-level ACLs -- none of which plain Blob Storage
    // (evidence.bicep) has.
    isHnsEnabled: true
  }
}

resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-01-01' = {
  parent: storage
  name: 'default'
}

// A Data Lake Gen2 "filesystem" is the same underlying resource as a Blob
// container -- ARM still models it as one, addressed here as
// `projects/<project_id>/{ifc,pdf}/<file>` by the app's own download logic
// (ServiceContainer._download_and_publish).
resource projectsFilesystem 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-01-01' = {
  parent: blobService
  name: 'projects'
  properties: {
    publicAccess: 'None'
  }
}

resource projectsBlobDataReader 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(storage.id, apiIdentityName, storageBlobDataReaderRoleId)
  scope: storage
  properties: {
    roleDefinitionId: storageBlobDataReaderRoleId
    principalId: apiIdentityPrincipalId
    principalType: 'ServicePrincipal'
  }
}

output storageAccountName string = storage.name
// azure-storage-file-datalake's DataLakeServiceClient expects the `.dfs.`
// endpoint, not `.blob.` -- confirmed against the SDK's own documented
// account_url contract, not assumed from the blob-endpoint pattern
// evidence.bicep uses.
output dfsEndpoint string = storage.properties.primaryEndpoints.dfs
output filesystemName string = projectsFilesystem.name
