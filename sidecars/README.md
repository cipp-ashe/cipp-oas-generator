# Sidecars

Sidecars are JSON override files for endpoints where static analysis can't fully resolve
the contract. Create one per endpoint that needs it: `EndpointName.json`.

The pipeline applies these during Stage 3 (merger). Sidecars have highest precedence.

---

## Schema

### Core overrides

```json
{
  "override_synopsis":     "Better one-line description (replaces auto-extracted)",
  "override_description":  "Longer description (optional)",
  "override_methods":      ["POST"],
  "override_confidence":   "high",
  "override_role":         "Identity.User.ReadWrite"
}
```

### Param-level additions and removals

```json
{
  "add_params": [
    {
      "name":        "paramName",
      "in":          "body",
      "required":    true,
      "type":        "string",
      "description": "What this param does",
      "confidence":  "high"
    }
  ],
  "remove_params": ["noisyParam", "uiOnlyField"],
  "add_responses": {
    "404": "User not found"
  }
}
```

Valid `type` values:
- OAS scalars: `string`, `number`, `integer`, `boolean`, `array`, `object`
- Named schemas: `LabelValue`, `LabelValueNumber`, `GroupRef`, `PostExecution`,
  `ScheduledTask`, `StandardResults`, `DynamicExtensionFields`

### Raw escape hatches

Use these when `add_params` can't express the schema — complex nested objects,
`oneOf`, discriminators, array-of-objects with full inline schemas, etc.

**`raw_request_body`** replaces the *entire* generated requestBody with a hand-written
OAS 3.1 requestBody object:

```json
{
  "raw_request_body": {
    "required": true,
    "content": {
      "application/json": {
        "schema": {
          "type": "object",
          "properties": {
            "bulkItems": {
              "type": "array",
              "items": {
                "type": "object",
                "properties": {
                  "siteName":    { "type": "string" },
                  "siteOwner":   { "type": "string" },
                  "templateName":{ "type": "string", "enum": ["TeamSite", "CommunicationSite"] }
                },
                "required": ["siteName", "siteOwner"]
              }
            },
            "tenantFilter": { "type": "string" }
          },
          "required": ["bulkItems", "tenantFilter"]
        }
      }
    }
  }
}
```

**`raw_response_body`** replaces standard responses for specific status codes:

```json
{
  "raw_response_body": {
    "200": {
      "description": "Success",
      "content": {
        "application/json": {
          "schema": {
            "type": "object",
            "properties": {
              "Results": { "type": "array", "items": { "type": "string" } },
              "password": { "type": "string", "description": "Only present for new users" }
            }
          }
        }
      }
    }
  }
}
```

Note: 401 and 403 are always added even if `raw_response_body` is present — auth
failures apply to every endpoint.

### Provenance and trust

```json
{
  "trust_level":    "reversed",
  "tested_version": "v10.1.2"
}
```

`trust_level` values:
- `"reversed"` — reconstructed from source code (default for all sidecars)
- `"verified"` — human spot-checked (code review + logic confirmed), not live-tested
- `"tested"` — validated against a live CIPP tenant
- `"inferred"` — auto-generated, best-guess from partial information; treat with caution

`tested_version` — the CIPP version this sidecar was last validated against.
When CIPP updates and the version drifts, this is how you know to re-verify.

### Consumer warnings

```json
{
  "x_cipp_warnings": [
    "This endpoint permanently deletes data with no undo.",
    "Scheduled=true path queues silently — check the task scheduler for status."
  ]
}
```

Emitted as `x-cipp-warnings` on the OAS operation. SDK generators and Swagger UI
can surface these to callers.

### Deprecation

```json
{
  "deprecated": true,
  "notes": "Replaced by ExecNewEndpoint in v10.2.0"
}
```

---

## When to add a sidecar

| Situation | What to use |
|---|---|
| Endpoint uses `$Request.Body` as a blob (BLIND tier) | `add_params` for simple shapes; `raw_request_body` for complex |
| Body has nested arrays of objects | `raw_request_body` |
| Frontend sends different field shape than API reads | `notes` to explain; `add_params` to correct |
| Scanner attributed shared-form fields wrongly | `remove_params` for wrong ones; `add_params` for correct ones |
| Response schema is non-standard | `raw_response_body` |
| Endpoint is dangerous / has side effects | `x_cipp_warnings` |
| You reviewed the source and logic but didn't live-test | `trust_level: "verified"` |
| You tested this against a real tenant | `trust_level: "tested"` + `tested_version` |
| Endpoint is deprecated | `deprecated: true` |

---

## Existing sidecars

| Endpoint | Coverage | Trust | Notes |
|---|---|---|---|
| AddUser | PARTIAL → sidecar | reversed | Body blob to New-CIPPUserTask |
| AddUserBulk | PARTIAL → sidecar | reversed | urlFromData pattern; BulkUser array |

---

## Safety rules

1. `raw_request_body` and `add_params` body entries cannot coexist — raw wins,
   add_params body entries are silently ignored. The validator warns if you try.
2. Unknown `type` values in `add_params` produce a validation warning — the
   pipeline will continue but the schema defaults to `string`. Check the spelling.
3. `remove_params` names are case-insensitive. Removing a param that doesn't
   exist is silently ignored (not an error).
4. Stage 3 validates all sidecars before running. Fatal errors abort the endpoint
   entirely (not the full run). The endpoint will show as an error in the output.
