"""
fetch_schemas.py — Build/refresh schema caches for the CIPP OAS Generator.

Outputs (written to schemas/ directory):
  schemas/graph-required.json  — Graph operation → required request body fields
  schemas/graph-responses.json — Graph operation → inline 200 response schema
  schemas/exo-required.json    — EXO cmdlet → mandatory parameter list (curated)

Usage:
  python fetch_schemas.py              # write all caches from built-in data
  python fetch_schemas.py --dry-run    # print what would be written
  python fetch_schemas.py --spec PATH  # augment built-in with a local Graph OAS JSON

The built-in curated data covers the ~60 Graph operations CIPP commonly calls and
the ~40 EXO cmdlets most frequently invoked via New-ExoRequest. If a local Graph
OAS JSON file is provided via --spec, it is merged *over* the built-in data so
any operations added to CIPP after this script was written are also covered.

Obtaining a local Graph OAS file (optional):
  The Microsoft Graph metadata team publishes OpenAPI descriptions at:
    https://github.com/microsoftgraph/msgraph-metadata
  Download openapi/beta/openapi.json from that repo and pass it via --spec.
  Warning: the full file is large (100 MB+); processing is slow but one-time.

The pipeline stages load these files if present and degrade gracefully if absent.
"""

import json
import re
import argparse
from pathlib import Path

from config import GRAPH_FIELD_TYPES

_SCHEMAS_DIR = Path(__file__).parent / "schemas"


# ── Built-in curated data: Graph required fields ──────────────────────────────
# Format: "{METHOD} {path}" → [required_property, ...]
# Paths use OAS-style single {id} parameter (normalized from verbose names).
# Only fields Graph will reject the request for (HTTP 400/422) are listed.
# PATCH operations list nothing — Graph accepts partial-update bodies.

_BUILTIN_GRAPH_REQUIRED: dict[str, list[str]] = {
    # Users
    "POST /users": [
        "accountEnabled", "displayName", "mailNickname",
        "passwordProfile", "userPrincipalName",
    ],
    "PATCH /users/{id}": [],
    "DELETE /users/{id}": [],
    # Groups
    "POST /groups": ["displayName", "mailEnabled", "mailNickname", "securityEnabled"],
    "PATCH /groups/{id}": [],
    "DELETE /groups/{id}": [],
    # Applications
    "POST /applications": ["displayName"],
    "PATCH /applications/{id}": [],
    "DELETE /applications/{id}": [],
    # Service principals
    "POST /servicePrincipals": ["appId"],
    "DELETE /servicePrincipals/{id}": [],
    # OAuth2 permission grants
    "POST /oauth2PermissionGrants": [
        "clientId", "consentType", "resourceId", "scope",
    ],
    "DELETE /oauth2PermissionGrants/{id}": [],
    # App role assignments
    "POST /servicePrincipals/{id}/appRoleAssignments": [
        "principalId", "resourceId", "appRoleId",
    ],
    # Conditional access policies
    "POST /identity/conditionalAccess/policies": [
        "displayName", "state", "conditions", "grantControls",
    ],
    "PATCH /identity/conditionalAccess/policies/{id}": [],
    "DELETE /identity/conditionalAccess/policies/{id}": [],
    # Named locations
    "POST /identity/conditionalAccess/namedLocations": [
        "@odata.type", "displayName",
    ],
    "PATCH /identity/conditionalAccess/namedLocations/{id}": [],
    "DELETE /identity/conditionalAccess/namedLocations/{id}": [],
    # Webhook subscriptions
    "POST /subscriptions": [
        "changeType", "notificationUrl", "resource", "expirationDateTime",
    ],
    "PATCH /subscriptions/{id}": ["expirationDateTime"],
    "DELETE /subscriptions/{id}": [],
    # Authentication phone methods
    "POST /users/{id}/authentication/phoneMethods": ["phoneNumber", "phoneType"],
    "DELETE /users/{id}/authentication/phoneMethods/{id}": [],
    # Device management — Intune apps
    "POST /deviceAppManagement/mobileApps": ["@odata.type", "displayName"],
    "PATCH /deviceAppManagement/mobileApps/{id}": [],
    "DELETE /deviceAppManagement/mobileApps/{id}": [],
    # Device management — configuration policies
    "POST /deviceManagement/deviceConfigurations": ["@odata.type", "displayName"],
    "PATCH /deviceManagement/deviceConfigurations/{id}": [],
    "DELETE /deviceManagement/deviceConfigurations/{id}": [],
    # Device management — compliance policies
    "POST /deviceManagement/deviceCompliancePolicies": ["@odata.type", "displayName"],
    "PATCH /deviceManagement/deviceCompliancePolicies/{id}": [],
    "DELETE /deviceManagement/deviceCompliancePolicies/{id}": [],
    # Autopilot device identities
    "POST /deviceManagement/importedWindowsAutopilotDeviceIdentities": [
        "serialNumber",
    ],
    "DELETE /deviceManagement/importedWindowsAutopilotDeviceIdentities/{id}": [],
    # Security audit log queries
    "POST /security/auditLog/queries": [
        "displayName", "filterStartDateTime", "filterEndDateTime",
    ],
    "DELETE /security/auditLog/queries/{id}": [],
    # Application templates instantiation
    "POST /applicationTemplates/{id}/instantiate": ["displayName"],
    # Teams
    "POST /teams": ["template@odata.bind"],
    "PATCH /teams/{id}": [],
    # Channels
    "POST /teams/{id}/channels": ["displayName"],
    "DELETE /teams/{id}/channels/{id}": [],
    # SharePoint sites
    "POST /sites/{id}/lists": ["displayName", "list"],
    # Privileged Identity Management
    "POST /identityGovernance/privilegedAccess/group/eligibilityScheduleRequests": [
        "action", "principalId", "groupId", "accessId",
    ],
    # Entitlement management
    "POST /identityGovernance/entitlementManagement/accessPackages": [
        "displayName", "isHidden",
    ],
}


# ── Built-in curated data: Graph response schemas ─────────────────────────────
# Format: "GET {path}" → inline OAS 3.1 schema dict (no $ref — fully inlined).
# Only GET operations are included (POST/PATCH/DELETE responses are StandardResults).
# Schemas are abbreviated: top-level shape + most-used properties + additionalProperties.

_BUILTIN_GRAPH_RESPONSES: dict[str, dict] = {
    # Users collection
    "GET /users": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph user object.",
                    "properties": {
                        "id":                     {"type": "string"},
                        "displayName":            {"type": "string"},
                        "userPrincipalName":       {"type": "string"},
                        "mail":                   {"type": "string"},
                        "accountEnabled":         {"type": "boolean"},
                        "givenName":              {"type": "string"},
                        "surname":                {"type": "string"},
                        "jobTitle":               {"type": "string"},
                        "department":             {"type": "string"},
                        "usageLocation":          {"type": "string"},
                        "city":                   {"type": "string"},
                        "country":                {"type": "string"},
                        "mobilePhone":            {"type": "string"},
                        "businessPhones":         {"type": "array", "items": {"type": "string"}},
                        "assignedLicenses":       {"type": "array", "items": {"type": "object"}},
                        "onPremisesSyncEnabled":  {"type": "boolean"},
                        "onPremisesImmutableId":  {"type": "string"},
                        "createdDateTime":        {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": True,
                },
            },
            "@odata.nextLink": {"type": "string"},
        },
    },
    # Single user
    "GET /users/{id}": {
        "type": "object",
        "description": "Microsoft Graph user object.",
        "properties": {
            "id":                     {"type": "string"},
            "displayName":            {"type": "string"},
            "userPrincipalName":       {"type": "string"},
            "mail":                   {"type": "string"},
            "accountEnabled":         {"type": "boolean"},
            "givenName":              {"type": "string"},
            "surname":                {"type": "string"},
            "jobTitle":               {"type": "string"},
            "department":             {"type": "string"},
            "usageLocation":          {"type": "string"},
            "assignedLicenses":       {"type": "array", "items": {"type": "object"}},
            "onPremisesSyncEnabled":  {"type": "boolean"},
        },
        "additionalProperties": True,
    },
    # Groups collection
    "GET /groups": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph group object.",
                    "properties": {
                        "id":              {"type": "string"},
                        "displayName":     {"type": "string"},
                        "mailEnabled":     {"type": "boolean"},
                        "securityEnabled": {"type": "boolean"},
                        "groupTypes":      {"type": "array", "items": {"type": "string"}},
                        "mail":            {"type": "string"},
                        "description":     {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
            "@odata.nextLink": {"type": "string"},
        },
    },
    # Single group
    "GET /groups/{id}": {
        "type": "object",
        "description": "Microsoft Graph group object.",
        "properties": {
            "id":              {"type": "string"},
            "displayName":     {"type": "string"},
            "mailEnabled":     {"type": "boolean"},
            "securityEnabled": {"type": "boolean"},
            "groupTypes":      {"type": "array", "items": {"type": "string"}},
        },
        "additionalProperties": True,
    },
    # Applications
    "GET /applications": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph application registration.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "appId":                {"type": "string"},
                        "displayName":          {"type": "string"},
                        "keyCredentials":       {"type": "array", "items": {"type": "object"}},
                        "passwordCredentials":  {"type": "array", "items": {"type": "object"}},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Devices
    "GET /devices": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph device object.",
                    "properties": {
                        "id":                {"type": "string"},
                        "displayName":       {"type": "string"},
                        "deviceId":          {"type": "string"},
                        "operatingSystem":   {"type": "string"},
                        "isCompliant":       {"type": "boolean"},
                        "isManaged":         {"type": "boolean"},
                        "registrationDateTime": {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Conditional access policies
    "GET /identity/conditionalAccess/policies": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Conditional access policy.",
                    "properties": {
                        "id":          {"type": "string"},
                        "displayName": {"type": "string"},
                        "state":       {
                            "type": "string",
                            "enum": ["enabled", "disabled", "enabledForReportingButNotEnforced"],
                        },
                        "conditions":     {"type": "object", "additionalProperties": True},
                        "grantControls":  {"type": "object", "additionalProperties": True},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Security alerts
    "GET /security/alerts_v2": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph security alert (v2).",
                    "properties": {
                        "id":                  {"type": "string"},
                        "title":               {"type": "string"},
                        "severity":            {"type": "string"},
                        "status":              {"type": "string"},
                        "classification":      {"type": "string"},
                        "createdDateTime":     {"type": "string", "format": "date-time"},
                        "lastUpdateDateTime":  {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Managed devices (Intune)
    "GET /deviceManagement/managedDevices": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Intune managed device.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "deviceName":           {"type": "string"},
                        "operatingSystem":      {"type": "string"},
                        "complianceState":      {"type": "string"},
                        "managementState":      {"type": "string"},
                        "userPrincipalName":    {"type": "string"},
                        "enrolledDateTime":     {"type": "string", "format": "date-time"},
                        "lastSyncDateTime":     {"type": "string", "format": "date-time"},
                        "isEncrypted":          {"type": "boolean"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Autopilot devices
    "GET /deviceManagement/windowsAutopilotDeviceIdentities": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Windows Autopilot device identity.",
                    "properties": {
                        "id":                 {"type": "string"},
                        "serialNumber":       {"type": "string"},
                        "model":              {"type": "string"},
                        "manufacturer":       {"type": "string"},
                        "groupTag":           {"type": "string"},
                        "deploymentProfileAssignmentStatus": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Service principals
    "GET /servicePrincipals": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph service principal.",
                    "properties": {
                        "id":          {"type": "string"},
                        "appId":       {"type": "string"},
                        "displayName": {"type": "string"},
                        "accountEnabled": {"type": "boolean"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Subscribed SKUs (licences)
    "GET /subscribedSkus": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Subscribed licence SKU.",
                    "properties": {
                        "id":               {"type": "string"},
                        "skuId":            {"type": "string"},
                        "skuPartNumber":    {"type": "string"},
                        "consumedUnits":    {"type": "integer"},
                        "prepaidUnits":     {"type": "object", "additionalProperties": True},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Single managed device (Intune)
    "GET /deviceManagement/managedDevices/{id}": {
        "type": "object",
        "description": "Intune managed device.",
        "properties": {
            "id":                   {"type": "string"},
            "deviceName":           {"type": "string"},
            "operatingSystem":      {"type": "string"},
            "complianceState":      {"type": "string"},
            "managementState":      {"type": "string"},
            "userPrincipalName":    {"type": "string"},
            "enrolledDateTime":     {"type": "string", "format": "date-time"},
            "lastSyncDateTime":     {"type": "string", "format": "date-time"},
            "isEncrypted":          {"type": "boolean"},
            "serialNumber":         {"type": "string"},
            "model":                {"type": "string"},
            "manufacturer":         {"type": "string"},
        },
        "additionalProperties": True,
    },
    # Sign-in audit logs
    "GET /auditLogs/signIns": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph sign-in log entry.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "createdDateTime":       {"type": "string", "format": "date-time"},
                        "userDisplayName":       {"type": "string"},
                        "userPrincipalName":     {"type": "string"},
                        "appDisplayName":        {"type": "string"},
                        "ipAddress":             {"type": "string"},
                        "clientAppUsed":         {"type": "string"},
                        "conditionalAccessStatus": {"type": "string"},
                        "riskDetail":            {"type": "string"},
                        "riskLevelAggregated":   {"type": "string"},
                        "status": {
                            "type": "object",
                            "properties": {
                                "errorCode":    {"type": "integer"},
                                "failureReason": {"type": "string"},
                            },
                        },
                        "location": {
                            "type": "object",
                            "properties": {
                                "city":        {"type": "string"},
                                "state":       {"type": "string"},
                                "countryOrRegion": {"type": "string"},
                            },
                        },
                    },
                    "additionalProperties": True,
                },
            },
            "@odata.nextLink": {"type": "string"},
        },
    },
    # Directory audit logs
    "GET /auditLogs/directoryAudits": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Azure AD directory audit entry.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "category":             {"type": "string"},
                        "activityDisplayName":  {"type": "string"},
                        "activityDateTime":     {"type": "string", "format": "date-time"},
                        "operationType":        {"type": "string"},
                        "result":               {"type": "string"},
                        "resultReason":         {"type": "string"},
                        "initiatedBy":          {"type": "object", "additionalProperties": True},
                        "targetResources":      {"type": "array", "items": {"type": "object"}},
                    },
                    "additionalProperties": True,
                },
            },
            "@odata.nextLink": {"type": "string"},
        },
    },
    # Single service principal
    "GET /servicePrincipals/{id}": {
        "type": "object",
        "description": "Microsoft Graph service principal.",
        "properties": {
            "id":               {"type": "string"},
            "appId":            {"type": "string"},
            "displayName":      {"type": "string"},
            "accountEnabled":   {"type": "boolean"},
            "appRoles":         {"type": "array", "items": {"type": "object"}},
            "oauth2PermissionScopes": {"type": "array", "items": {"type": "object"}},
        },
        "additionalProperties": True,
    },
    # User's transitive group memberships
    "GET /users/{id}/transitiveMemberOf": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Group or directory role membership.",
                    "properties": {
                        "id":          {"type": "string"},
                        "displayName": {"type": "string"},
                        "@odata.type": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Group members
    "GET /groups/{id}/members": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Group member (user, device, or service principal).",
                    "properties": {
                        "id":                   {"type": "string"},
                        "displayName":          {"type": "string"},
                        "userPrincipalName":     {"type": "string"},
                        "@odata.type":           {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # User's managed devices
    "GET /users/{id}/managedDevices": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Intune managed device.",
                    "properties": {
                        "id":               {"type": "string"},
                        "deviceName":       {"type": "string"},
                        "operatingSystem":  {"type": "string"},
                        "complianceState":  {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # User's owned devices
    "GET /users/{id}/ownedDevices": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Device owned by the user.",
                    "properties": {
                        "id":            {"type": "string"},
                        "displayName":   {"type": "string"},
                        "deviceId":      {"type": "string"},
                        "operatingSystem": {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Autopilot deployment profiles
    "GET /deviceManagement/windowsAutopilotDeploymentProfiles": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Windows Autopilot deployment profile.",
                    "properties": {
                        "id":           {"type": "string"},
                        "displayName":  {"type": "string"},
                        "description":  {"type": "string"},
                        "language":     {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Device enrollment configurations
    "GET /deviceManagement/deviceEnrollmentConfigurations": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Device enrollment configuration.",
                    "properties": {
                        "id":               {"type": "string"},
                        "displayName":      {"type": "string"},
                        "description":      {"type": "string"},
                        "priority":         {"type": "integer"},
                        "@odata.type":      {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Single assignment filter
    "GET /deviceManagement/assignmentFilters/{id}": {
        "type": "object",
        "description": "Intune assignment filter.",
        "properties": {
            "id":           {"type": "string"},
            "displayName":  {"type": "string"},
            "description":  {"type": "string"},
            "platform":     {"type": "string"},
            "rule":         {"type": "string"},
        },
        "additionalProperties": True,
    },
    # SharePoint site lists
    "GET /sites/{id}/lists": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "SharePoint list.",
                    "properties": {
                        "id":           {"type": "string"},
                        "name":         {"type": "string"},
                        "displayName":  {"type": "string"},
                        "description":  {"type": "string"},
                        "webUrl":       {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # SharePoint list items
    "GET /sites/{id}/lists/{id}/items": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "SharePoint list item.",
                    "properties": {
                        "id":           {"type": "string"},
                        "createdDateTime": {"type": "string", "format": "date-time"},
                        "fields":       {"type": "object", "additionalProperties": True},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Single team
    "GET /teams/{id}": {
        "type": "object",
        "description": "Microsoft Teams team.",
        "properties": {
            "id":           {"type": "string"},
            "displayName":  {"type": "string"},
            "description":  {"type": "string"},
            "isArchived":   {"type": "boolean"},
            "webUrl":       {"type": "string"},
        },
        "additionalProperties": True,
    },
    # Team channels
    "GET /teams/{id}/channels": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Teams channel.",
                    "properties": {
                        "id":           {"type": "string"},
                        "displayName":  {"type": "string"},
                        "description":  {"type": "string"},
                        "membershipType": {"type": "string"},
                        "webUrl":       {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Team members
    "GET /teams/{id}/members": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Teams member.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "displayName":          {"type": "string"},
                        "userId":               {"type": "string"},
                        "email":                {"type": "string"},
                        "roles":                {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Team installed apps
    "GET /teams/{id}/installedApps": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "App installed in a Teams team.",
                    "properties": {
                        "id":   {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # GDAP delegated admin relationship access assignments
    "GET /tenantRelationships/delegatedAdminRelationships/{id}/accessAssignments": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "GDAP delegated admin access assignment.",
                    "properties": {
                        "id":               {"type": "string"},
                        "status":           {"type": "string"},
                        "accessContainer": {
                            "type": "object",
                            "properties": {
                                "accessContainerId":   {"type": "string"},
                                "accessContainerType": {"type": "string"},
                            },
                        },
                        "accessDetails": {
                            "type": "object",
                            "properties": {
                                "unifiedRoles": {
                                    "type": "array",
                                    "items": {
                                        "type": "object",
                                        "properties": {"roleDefinitionId": {"type": "string"}},
                                    },
                                },
                            },
                        },
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Multitenant management — managed device compliances
    "GET /tenantRelationships/managedTenants/managedDeviceCompliances": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Managed tenant device compliance record.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "tenantId":             {"type": "string"},
                        "tenantDisplayName":    {"type": "string"},
                        "deviceName":           {"type": "string"},
                        "complianceStatus":     {"type": "string"},
                        "osDescription":        {"type": "string"},
                        "lastSyncDateTime":     {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Tenant information lookup
    "GET /tenantRelationships/findTenantInformationByTenantId/{id}": {
        "type": "object",
        "description": "Basic tenant identity information.",
        "properties": {
            "tenantId":             {"type": "string"},
            "displayName":          {"type": "string"},
            "defaultDomainName":    {"type": "string"},
            "federationBrandName":  {"type": "string"},
        },
        "additionalProperties": True,
    },
    # Service health issues
    "GET /admin/serviceAnnouncement/issues": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft 365 service health issue.",
                    "properties": {
                        "id":               {"type": "string"},
                        "title":            {"type": "string"},
                        "service":          {"type": "string"},
                        "status":           {"type": "string"},
                        "classification":   {"type": "string"},
                        "startDateTime":    {"type": "string", "format": "date-time"},
                        "endDateTime":      {"type": "string", "format": "date-time"},
                        "lastModifiedDateTime": {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # App consent requests — user consent requests for a specific app
    "GET /identityGovernance/appConsent/appConsentRequests/{id}/userConsentRequests": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "User consent request for an app.",
                    "properties": {
                        "id":           {"type": "string"},
                        "status":       {"type": "string"},
                        "reason":       {"type": "string"},
                        "createdBy":    {"type": "object", "additionalProperties": True},
                        "createdDateTime": {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Detected apps (Intune software inventory)
    "GET /deviceManagement/detectedApps": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "App detected on managed devices.",
                    "properties": {
                        "id":               {"type": "string"},
                        "displayName":      {"type": "string"},
                        "version":          {"type": "string"},
                        "sizeInByte":       {"type": "integer"},
                        "deviceCount":      {"type": "integer"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Managed devices with a specific detected app
    "GET /deviceManagement/detectedApps/{id}/managedDevices": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Managed device with the detected app installed.",
                    "properties": {
                        "id":               {"type": "string"},
                        "deviceName":       {"type": "string"},
                        "userPrincipalName": {"type": "string"},
                        "operatingSystem":  {"type": "string"},
                        "complianceState":  {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Detected apps on a specific managed device
    "GET /deviceManagement/managedDevices/{id}/detectedApps": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "App detected on a specific managed device.",
                    "properties": {
                        "id":           {"type": "string"},
                        "displayName":  {"type": "string"},
                        "version":      {"type": "string"},
                        "sizeInByte":   {"type": "integer"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Contacts (personal contacts via Graph)
    "GET /contacts": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph contact object.",
                    "properties": {
                        "id":               {"type": "string"},
                        "displayName":      {"type": "string"},
                        "givenName":        {"type": "string"},
                        "surname":          {"type": "string"},
                        "emailAddresses":   {"type": "array", "items": {"type": "object"}},
                        "businessPhones":   {"type": "array", "items": {"type": "string"}},
                        "mobilePhone":      {"type": "string"},
                        "jobTitle":         {"type": "string"},
                        "companyName":      {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Organization / tenant info
    "GET /organization": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Azure AD organization (tenant) object.",
                    "properties": {
                        "id":                    {"type": "string"},
                        "displayName":           {"type": "string"},
                        "verifiedDomains": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name":         {"type": "string"},
                                    "type":         {"type": "string"},
                                    "isDefault":    {"type": "boolean"},
                                    "isInitial":    {"type": "boolean"},
                                },
                            },
                        },
                        "onPremisesSyncEnabled": {"type": "boolean"},
                        "onPremisesLastSyncDateTime": {"type": "string", "format": "date-time"},
                        "dirSyncEnabled":        {"type": "boolean"},
                        "technicalNotificationMails": {"type": "array", "items": {"type": "string"}},
                        "assignedPlans": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "servicePlanId": {"type": "string"},
                                    "capabilityStatus": {"type": "string"},
                                    "service":       {"type": "string"},
                                },
                            },
                        },
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Domains
    "GET /domains": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Azure AD verified domain.",
                    "properties": {
                        "id":           {"type": "string"},
                        "isDefault":    {"type": "boolean"},
                        "isInitial":    {"type": "boolean"},
                        "isVerified":   {"type": "boolean"},
                        "authenticationType": {"type": "string"},
                        "supportedServices": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Named locations (Conditional Access)
    "GET /identity/conditionalAccess/namedLocations": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Conditional Access named location (IP range or country).",
                    "properties": {
                        "id":           {"type": "string"},
                        "displayName":  {"type": "string"},
                        "@odata.type":  {"type": "string"},
                        "createdDateTime":  {"type": "string", "format": "date-time"},
                        "modifiedDateTime": {"type": "string", "format": "date-time"},
                        "isTrusted":    {"type": "boolean"},
                        "ipRanges": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "cidrAddress": {"type": "string"},
                                    "@odata.type": {"type": "string"},
                                },
                            },
                        },
                        "countriesAndRegions": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Directory roles (built-in Azure AD roles)
    "GET /directoryRoles": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Azure AD directory role.",
                    "properties": {
                        "id":               {"type": "string"},
                        "displayName":      {"type": "string"},
                        "description":      {"type": "string"},
                        "roleTemplateId":   {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Security alerts V1 (legacy)
    "GET /security/alerts": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Microsoft Graph security alert.",
                    "properties": {
                        "id":               {"type": "string"},
                        "title":            {"type": "string"},
                        "description":      {"type": "string"},
                        "severity":         {"type": "string", "enum": ["unknown", "informational", "low", "medium", "high"]},
                        "status":           {"type": "string"},
                        "category":         {"type": "string"},
                        "createdDateTime":  {"type": "string", "format": "date-time"},
                        "lastModifiedDateTime": {"type": "string", "format": "date-time"},
                        "vendorInformation": {"type": "object", "additionalProperties": True},
                        "userStates":       {"type": "array", "items": {"type": "object"}},
                        "hostStates":       {"type": "array", "items": {"type": "object"}},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Intune Device Management Intents
    "GET /deviceManagement/Intents": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Intune device management intent (security baseline).",
                    "properties": {
                        "id":               {"type": "string"},
                        "displayName":      {"type": "string"},
                        "description":      {"type": "string"},
                        "isAssigned":       {"type": "boolean"},
                        "lastModifiedDateTime": {"type": "string", "format": "date-time"},
                        "templateId":       {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # SharePoint admin settings
    "GET /admin/sharepoint/settings": {
        "type": "object",
        "description": "SharePoint Online tenant-level settings.",
        "properties": {
            "sharingCapability":            {"type": "string"},
            "allowedDomainGuidsForSyncApp": {"type": "array", "items": {"type": "string"}},
            "availableManagedPathsForSiteCreation": {"type": "array", "items": {"type": "string"}},
            "deletedUserPersonalSiteRetentionPeriodInDays": {"type": "integer"},
            "excludedFileExtensionsForSyncApp": {"type": "array", "items": {"type": "string"}},
            "isGuestUserSharingLimitedToSelectedDomainList": {"type": "boolean"},
            "isResharingByExternalUsersEnabled":  {"type": "boolean"},
            "isSiteCreationEnabled":              {"type": "boolean"},
        },
        "additionalProperties": True,
    },
    # Intune organizational message details
    "GET /deviceManagement/organizationalMessageDetails": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Intune organizational message.",
                    "properties": {
                        "id":               {"type": "string"},
                        "status":           {"type": "string"},
                        "frequency":        {"type": "string"},
                        "startDateTime":    {"type": "string", "format": "date-time"},
                        "endDateTime":      {"type": "string", "format": "date-time"},
                        "surface":          {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Managed tenant operations (GDAP / Lighthouse)
    "GET /tenantRelationships/managedTenants/managedTenantOperations": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Managed tenant async operation.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "operationId":          {"type": "string"},
                        "status":               {"type": "string"},
                        "createdDateTime":       {"type": "string", "format": "date-time"},
                        "lastActionDateTime":    {"type": "string", "format": "date-time"},
                        "tenantId":             {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Assignment filters collection
    "GET /deviceManagement/assignmentFilters": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Intune assignment filter.",
                    "properties": {
                        "id":           {"type": "string"},
                        "displayName":  {"type": "string"},
                        "description":  {"type": "string"},
                        "platform":     {"type": "string"},
                        "rule":         {"type": "string"},
                        "roleScopeTags": {"type": "array", "items": {"type": "string"}},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Multitenant management — managed tenants (Lighthouse)
    "GET /tenantRelationships/managedTenants/managedTenants": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Managed tenant record in Microsoft Lighthouse.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "tenantId":             {"type": "string"},
                        "tenantDisplayName":    {"type": "string"},
                        "onboardingStatus":     {"type": "string"},
                        "onboardingDateTime":   {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # GDAP delegated admin relationships
    "GET /tenantRelationships/delegatedAdminRelationships": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "GDAP delegated admin relationship.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "displayName":          {"type": "string"},
                        "status":               {"type": "string"},
                        "customer": {
                            "type": "object",
                            "properties": {
                                "tenantId":     {"type": "string"},
                                "displayName":  {"type": "string"},
                            },
                        },
                        "createdDateTime":      {"type": "string", "format": "date-time"},
                        "activatedDateTime":    {"type": "string", "format": "date-time"},
                        "endDateTime":          {"type": "string", "format": "date-time"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # User's inbox messages
    "GET /me/mailFolders/{id}/messages": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Mail message.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "subject":              {"type": "string"},
                        "bodyPreview":          {"type": "string"},
                        "isRead":               {"type": "boolean"},
                        "receivedDateTime":     {"type": "string", "format": "date-time"},
                        "from":                 {"type": "object", "additionalProperties": True},
                        "toRecipients":         {"type": "array", "items": {"type": "object"}},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Current user (me)
    "GET /me": {
        "type": "object",
        "description": "Currently authenticated user.",
        "properties": {
            "id":                   {"type": "string"},
            "displayName":          {"type": "string"},
            "userPrincipalName":    {"type": "string"},
            "mail":                 {"type": "string"},
            "jobTitle":             {"type": "string"},
        },
        "additionalProperties": True,
    },
    # OAuth2 permission grants
    "GET /oauth2PermissionGrants": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "OAuth2 delegated permission grant.",
                    "properties": {
                        "id":               {"type": "string"},
                        "clientId":         {"type": "string"},
                        "consentType":      {"type": "string"},
                        "principalId":      {"type": "string"},
                        "resourceId":       {"type": "string"},
                        "scope":            {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
    # Groups with teams filter — covers ListTeams (groups with resourceProvisioningOptions containing 'Team')
    "GET /groups/{id}/members": {
        "type": "object",
        "properties": {
            "value": {
                "type": "array",
                "items": {
                    "type": "object",
                    "description": "Group member.",
                    "properties": {
                        "id":                   {"type": "string"},
                        "displayName":          {"type": "string"},
                        "userPrincipalName":    {"type": "string"},
                        "@odata.type":          {"type": "string"},
                    },
                    "additionalProperties": True,
                },
            },
        },
    },
}


# ── Built-in curated data: EXO mandatory parameters ───────────────────────────
# Format: "CmdletName" → [mandatory_param, ...]
# Only parameters Exchange Online will reject without (HTTP 400 / PS terminating error).
# Parameters that accept pipeline input or have defaults are excluded.

_BUILTIN_EXO_REQUIRED: dict[str, list[str]] = {
    # Contacts
    "New-MailContact":              ["Name", "ExternalEmailAddress"],
    "Remove-MailContact":           ["Identity"],
    # Mailboxes
    "Enable-Mailbox":               ["Identity"],
    "Remove-Mailbox":               ["Identity"],
    "Get-Mailbox":                  [],
    "Set-Mailbox":                  [],
    # Distribution groups
    "New-DistributionGroup":        ["Name"],
    "Set-DistributionGroup":        [],
    "Remove-DistributionGroup":     ["Identity"],
    "Add-DistributionGroupMember":  ["Identity", "Member"],
    "Remove-DistributionGroupMember": ["Identity", "Member"],
    "Get-DistributionGroupMember":  ["Identity"],
    # Microsoft 365 groups (via EXO)
    "New-UnifiedGroup":             ["DisplayName"],
    "Set-UnifiedGroup":             ["Identity"],
    "Remove-UnifiedGroup":          ["Identity"],
    "Add-UnifiedGroupLinks":        ["Identity", "LinkType", "Links"],
    "Remove-UnifiedGroupLinks":     ["Identity", "LinkType", "Links"],
    # Mailbox permissions
    "Add-MailboxPermission":        ["Identity", "User", "AccessRights"],
    "Remove-MailboxPermission":     ["Identity", "User", "AccessRights"],
    "Add-RecipientPermission":      ["Identity", "Trustee", "AccessRights"],
    "Remove-RecipientPermission":   ["Identity", "Trustee", "AccessRights"],
    # Folder permissions
    "Add-MailboxFolderPermission":  ["Identity", "User", "AccessRights"],
    "Set-MailboxFolderPermission":  ["Identity", "User", "AccessRights"],
    "Remove-MailboxFolderPermission": ["Identity", "User"],
    # Transport rules
    "New-TransportRule":            ["Name"],
    "Set-TransportRule":            ["Identity"],
    "Remove-TransportRule":         ["Identity"],
    # Connectors
    "New-InboundConnector":         ["Name", "ConnectorType"],
    "Set-InboundConnector":         ["Identity"],
    "Remove-InboundConnector":      ["Identity"],
    "New-OutboundConnector":        ["Name", "ConnectorType"],
    "Set-OutboundConnector":        ["Identity"],
    "Remove-OutboundConnector":     ["Identity"],
    # Spam / content filter
    "New-HostedContentFilterPolicy":  ["Name"],
    "Set-HostedContentFilterPolicy":  ["Identity"],
    "Remove-HostedContentFilterPolicy": ["Identity"],
    "New-HostedContentFilterRule":    ["Name", "HostedContentFilterPolicy"],
    "Set-HostedContentFilterRule":    ["Identity"],
    "Set-HostedConnectionFilterPolicy": ["Identity"],
    # Safe links / attachments
    "New-SafeLinksPolicy":          ["Name"],
    "Set-SafeLinksPolicy":          ["Identity"],
    "Remove-SafeLinksPolicy":       ["Identity"],
    "New-SafeAttachmentPolicy":     ["Name"],
    "Set-SafeAttachmentPolicy":     ["Identity"],
    "Remove-SafeAttachmentPolicy":  ["Identity"],
    # Quarantine policies
    "New-QuarantinePolicy":         ["Name"],
    "Set-QuarantinePolicy":         ["Identity"],
    "Remove-QuarantinePolicy":      ["Identity"],
    # Mobile devices
    "Remove-MobileDevice":          ["Identity"],
    # Audit log
    "Search-UnifiedAuditLog":       ["StartDate", "EndDate"],
    "Search-unifiedAuditLog":       ["StartDate", "EndDate"],
    # No-required-param cmdlets
    "Get-BlockedSenderAddress":     [],
    "Get-QuarantineMessage":        [],
    "Get-MailboxStatistics":        [],
    "Get-MobileDeviceStatistics":   ["Mailbox"],
    "Get-MessageTrace":             [],
    "Get-Recipient":                [],
    "Get-EXOMailbox":               [],
    "Get-EXORecipient":             [],
    "New-MailContact":              ["Name", "ExternalEmailAddress"],
}


# ── Spec file processing ──────────────────────────────────────────────────────

def _normalize_oas_path(path: str) -> str:
    """
    Normalize OAS verbose path params to a single {id} token.
    /users/{user-id}/memberOf → /users/{id}/memberOf
    This matches how CIPP normalizes its $($Alias.field) interpolations.
    """
    return re.sub(r'\{[^}]+\}', '{id}', path)


def _extract_from_spec(spec_path: Path) -> tuple[dict, dict]:
    """
    Extract required fields and GET response schemas from a local Graph OAS JSON file.
    Returns (graph_required, graph_responses) dicts to merge into built-in data.
    """
    size_mb = spec_path.stat().st_size // (1024 * 1024)
    print(f"Parsing Graph OAS spec: {spec_path} ({size_mb} MB) …")
    spec = json.loads(spec_path.read_text(encoding="utf-8"))

    graph_required:  dict[str, list[str]] = {}
    graph_responses: dict[str, dict]      = {}

    for path, path_item in spec.get("paths", {}).items():
        norm_path = _normalize_oas_path(path)
        for method, operation in path_item.items():
            method_upper = method.upper()
            if method_upper not in {"GET", "POST", "PATCH", "PUT", "DELETE"}:
                continue
            key = f"{method_upper} {norm_path}"

            # Required request body fields
            rb      = operation.get("requestBody", {})
            content = rb.get("content", {})
            schema  = (
                content.get("application/json", {}).get("schema", {})
                or content.get("*/*", {}).get("schema", {})
            )
            required_fields = schema.get("required", [])
            if required_fields:
                graph_required[key] = required_fields

            # GET 200 response schema (inline only — skip $ref-only schemas)
            if method_upper == "GET":
                resp_200    = operation.get("responses", {}).get("200", {})
                resp_content = resp_200.get("content", {})
                resp_schema  = (
                    resp_content.get("application/json", {}).get("schema")
                    or resp_content.get("*/*", {}).get("schema")
                )
                # Only store if schema has actual properties (skip bare $ref)
                if resp_schema and "properties" in resp_schema:
                    graph_responses[key] = resp_schema

    print(f"  Extracted {len(graph_required)} required mappings, "
          f"{len(graph_responses)} response schemas")
    return graph_required, graph_responses


# ── Main ──────────────────────────────────────────────────────────────────────

def run(spec_file: Path | None = None, dry_run: bool = False) -> None:
    _SCHEMAS_DIR.mkdir(exist_ok=True)

    graph_required  = dict(_BUILTIN_GRAPH_REQUIRED)
    graph_responses = dict(_BUILTIN_GRAPH_RESPONSES)
    exo_required    = dict(_BUILTIN_EXO_REQUIRED)

    if spec_file:
        spec_req, spec_resp = _extract_from_spec(spec_file)
        # Merge: spec file (Microsoft's authoritative metadata) wins over built-in curated data.
        added_req  = sum(1 for k in spec_req  if k not in graph_required)
        added_resp = sum(1 for k in spec_resp if k not in graph_responses)
        graph_required.update(spec_req)
        graph_responses.update(spec_resp)
        print(f"Added/updated {added_req} new + {len(spec_req) - added_req} existing required mappings from spec file.")
        print(f"Added/updated {added_resp} new + {len(spec_resp) - added_resp} existing response schemas from spec file.")

    if dry_run:
        print(f"[dry-run] Would write {len(graph_required)} entries → schemas/graph-required.json")
        print(f"[dry-run] Would write {len(graph_responses)} entries → schemas/graph-responses.json")
        print(f"[dry-run] Would write {len(exo_required)} cmdlets  → schemas/exo-required.json")
        return

    (_SCHEMAS_DIR / "graph-required.json").write_text(
        json.dumps(graph_required,  indent=2), encoding="utf-8"
    )
    (_SCHEMAS_DIR / "graph-responses.json").write_text(
        json.dumps(graph_responses, indent=2), encoding="utf-8"
    )
    (_SCHEMAS_DIR / "exo-required.json").write_text(
        json.dumps(exo_required,    indent=2), encoding="utf-8"
    )
    print(f"schemas/graph-required.json  — {len(graph_required)} operations")
    print(f"schemas/graph-responses.json — {len(graph_responses)} operations")
    print(f"schemas/exo-required.json    — {len(exo_required)} cmdlets")

    registry = _build_registry(graph_required, graph_responses, exo_required)
    registry_path = _SCHEMAS_DIR / "schema-registry.json"
    registry_path.write_text(json.dumps(registry, indent=2) + "\n")
    print(f"schema-registry.json         — {len(registry['graph'])} graph + {len(registry['exo'])} exo entries")


def _strip_odata_fields(schema: dict) -> dict:
    """Remove @odata.* properties from response schemas. CIPP handles pagination internally."""
    import copy
    schema = copy.deepcopy(schema)

    def _clean_props(obj: dict) -> None:
        if "properties" in obj:
            obj["properties"] = {
                k: v for k, v in obj["properties"].items()
                if not k.startswith("@odata")
            }
        # Recurse into nested structures
        if "items" in obj and isinstance(obj["items"], dict):
            _clean_props(obj["items"])
        if "properties" in obj:
            for v in obj["properties"].values():
                if isinstance(v, dict) and "properties" in v:
                    _clean_props(v)

    _clean_props(schema)
    return schema


def _build_registry(
    graph_required: dict[str, list[str]],
    graph_responses: dict[str, dict],
    exo_required: dict[str, list[str]],
) -> dict:
    """Build unified schema-registry.json from the three legacy caches."""
    registry: dict = {"version": "1.0", "graph": {}, "exo": {}}

    # Collect all Graph operation keys from both sources
    all_graph_keys = set(graph_required.keys()) | set(graph_responses.keys())
    for key in sorted(all_graph_keys):
        req_fields = graph_required.get(key, [])
        resp_schema = graph_responses.get(key)

        # Build request schema from required fields list
        request_schema = None
        if req_fields:
            request_schema = {
                "required": req_fields,
                "properties": {f: {"type": "string"} for f in req_fields},
            }

        # Build select_fields from response schema + GRAPH_FIELD_TYPES
        select_fields = {}
        if resp_schema:
            # Extract property names from response schema
            items = resp_schema
            if items.get("type") == "object" and "properties" in items:
                value_prop = items["properties"].get("value", {})
                if value_prop.get("type") == "array" and "items" in value_prop:
                    items = value_prop["items"]
            if items.get("type") == "array" and "items" in items:
                items = items["items"]
            props = items.get("properties", {})
            for field_name in props:
                # Skip @odata metadata fields — CIPP handles pagination internally
                if field_name.startswith("@odata"):
                    continue
                field_lower = field_name.lower()
                if field_lower in GRAPH_FIELD_TYPES:
                    ft = GRAPH_FIELD_TYPES[field_lower]
                    select_fields[field_name] = ft
                else:
                    select_fields[field_name] = "string"

        # Strip @odata metadata fields from response schemas
        clean_resp = _strip_odata_fields(resp_schema) if resp_schema else None

        registry["graph"][key] = {
            "source": "curated",
            "request": request_schema,
            "response": clean_resp,
            "select_fields": select_fields if select_fields else None,
        }

    # EXO entries
    for cmdlet, req_params in sorted(exo_required.items()):
        request_schema = None
        if req_params:
            request_schema = {
                "required": req_params,
                "properties": {p: {"type": "string"} for p in req_params},
            }
        registry["exo"][cmdlet] = {
            "source": "curated",
            "request": request_schema,
            "response": {"type": "array", "items": {"type": "object", "additionalProperties": True}},
        }

    return registry


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Build schema caches for the CIPP OAS Generator.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--spec", type=Path, metavar="PATH",
        help="Path to a locally downloaded Graph OAS JSON file (augments built-in data)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would be written without writing files",
    )
    args = parser.parse_args()
    run(spec_file=args.spec, dry_run=args.dry_run)
