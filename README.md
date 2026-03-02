# CIPP OAS Generator

**📄 [OpenAPI Spec (JSON)](https://raw.githubusercontent.com/KelvinTegelaar/CIPP-API/master/openapi.json)** | **📚 [Interactive Documentation](https://cipp-ashe.github.io/cipp-oas-generator/)**

---

Generates an OpenAPI 3.1 spec for the CIPP API via static analysis of both repos —
no runtime calls, no manual maintenance. Runs against the PowerShell API source and
the React frontend source simultaneously.

---

## How it works

```
Stage 1: API Scanner      Invoke-*.ps1 files → endpoint-index.json
Stage 2: Frontend Scanner JSX/JS call sites  → frontend-calls.json
Stage 3: Merger           reconcile + score  → merged-params.json
                                               mismatch-report.json
                                               coverage-report.json
Stage 4: OAS Emitter      merged output      → out/openapi.json
                                               out/domain/<name>-openapi.json
```

Each stage is independently runnable and testable. Outputs are plain JSON and feed
the next stage. Single-endpoint runs write to `out/*-EndpointName.json` and never
touch corpus output — safe to run at any time.

**Sidecars** (`sidecars/EndpointName.json`) are human-authored override files for
endpoints where static analysis can't fully resolve the contract. They take highest
precedence over both API and frontend sources. See `sidecars/README.md`.

---

## Setup

Install Python dependencies:

```bash
pip install -r requirements.txt
```

This installs `openapi-spec-validator` for OpenAPI 3.1 validation. The generator
uses only Python stdlib, but validation requires this external package.

**Optional:** For HTML documentation generation, install Node.js:
- macOS: `brew install node`
- Ubuntu: `sudo apt install nodejs npm`
- Fedora: `sudo dnf install nodejs`

---

## Quickstart

```bash
# Simplest — fetch KelvinTegelaar/CIPP-API@master + KelvinTegelaar/CIPP@main automatically
./run.sh --fetch

# With local clones (faster for repeated runs)
export CIPP_API_REPO=/path/to/CIPP-API
export CIPP_FRONTEND_REPO=/path/to/CIPP
./run.sh

# Single endpoint (spot-check, all stages)
./run.sh --fetch --endpoint AddUser

# After a CIPP release — verify assumptions before running
./run.sh --fetch --check-patterns

# Full parameter trace for one endpoint (see every decision the pipeline made)
./run.sh --fetch --validate-endpoint AddUser

# Trace one specific parameter end-to-end
./run.sh --fetch --validate-endpoint AddUser --param displayName

# CI mode — generate and diff against committed spec
./run.sh --fetch --validate-only
```

`run.sh` resolves repo paths in this order (first match wins):

1. **Env vars** — `CIPP_API_REPO` / `CIPP_FRONTEND_REPO` set explicitly
2. **Sibling directories** — `../cipp-api-master` and `../cipp-main` relative to the generator
3. **`--fetch` flag** — shallow-clones from GitHub into a temp directory, cleaned up on exit

Default remotes (used by `--fetch`):
- API: `https://github.com/KelvinTegelaar/CIPP-API` branch `master`
- Frontend: `https://github.com/KelvinTegelaar/CIPP` branch `main`

Override remotes or branches via env vars:
```bash
CIPP_API_REMOTE=https://github.com/yourfork/CIPP-API \
CIPP_API_BRANCH=dev \
CIPP_FRONTEND_REMOTE=https://github.com/yourfork/CIPP \
CIPP_FRONTEND_BRANCH=dev \
./run.sh --fetch
```

---

## Commands

### `./run.sh` (or `python3 pipeline.py`)

| Flag | Description |
|---|---|
| _(none)_ | Full corpus run — all endpoints, all stages |
| `--endpoint NAME` | One endpoint through all stages |
| `--stage N` | One stage only (1–4), full corpus |
| `--validate-only` | Generate + diff vs committed `openapi.json` (CI mode) |
| `--check-patterns` | Validate generator assumptions against live repos |
| `--check-sidecars` | Check for wizard endpoints needing sidecars |
| `--validate-endpoint NAME` | Full parameter trace for one endpoint |
| `--validate-endpoint NAME --param FIELD` | Trace one specific parameter |

### `--check-patterns`

Run this after every CIPP release before a corpus run. Checks 10 assumptions
using the **compiled patterns from the stage scripts directly** — not a parallel
re-implementation, so it validates the patterns actually in use:

- HTTP Functions path exists and has `Invoke-*.ps1` files
- `.FUNCTIONALITY` marker is in use (via Stage 1 `FUNCTIONALITY_RE`)
- `$Request.Query/Body` access pattern is present (via Stage 1 `QUERY_PARAM_RE` / `BODY_DIRECT_RE`)
- `ApiGetCall` / `ApiPostCall` / `.mutate` wrappers exist in frontend (via Stage 2 `POST_CONTEXT_RE`)
- `CippFormComponent name=` pattern is present (via Stage 2 `FORM_NAME_STATIC_RE`)
- Selector components (`Domain`, `License`, `User`) are present (via Stage 2 `SELECTOR_COMPONENT_RES`)
- `/api/` static URL prefix pattern is in use (via Stage 2 `STATIC_URL_RE`)
- `.mutate({ url: ... })` pattern is present (via Stage 2 `MUTATE_URL_RE`)
- `CippFormPage postUrl=` pattern is present (via Stage 2 `CIPP_FORM_PAGE_URL_RE`)

Exits 0 if all pass, 1 with a list of what drifted. If it fails, update the
relevant regex in the stage scripts before running the corpus — otherwise output
degrades silently.

> **Note:** The `.mutate({ url: ... })` and `CippFormPage postUrl=` checks sample the
> first 50 JSX files alphabetically. These files are all in `CippComponents/` and do not
> contain those patterns — causing false-positive failures. **This is a known sampling
> artifact, not real pattern drift.** Both patterns are confirmed present in the frontend
> (e.g. `CippFormPage.jsx`, `domains.js`). Verify against the full corpus before updating
> any regex.

### `--validate-endpoint`

Shows every decision the pipeline made for each parameter:

```
┌─ displayName  [body]  confidence=high  sources=['sidecar']
│ Stage 1: ✓ found — source=ast_blob (blob alias: ['UserObj'])  confidence=medium
│ Stage 2: — not scanned (sidecar-only param)
│ Stage 3: sidecar addition — bypasses merge logic
│ Sidecar: ✓ add_params entry: {'name': 'displayName', 'in': 'body', ...}
│ Stage 4: schema from sidecar type='string'
└─ final: in=body  conf=high  sources=['sidecar']
```

Also shows:
- Active filter sets (PS_NOISE, FRONTEND_NOISE, PARAM_NOISE) and what they suppressed
- Which params were dropped and why
- Known static analysis gaps for the endpoint
- Call sites found by Stage 2

Use `--param FIELD` when a parameter is **absent** from output to trace exactly
which filter suppressed it or why the scanner missed it.

### `--check-sidecars`

Identifies endpoints that use wizard components with potentially undocumented parameters:

```bash
./run.sh --fetch --check-sidecars
```

Checks:
- **Wizard endpoints** — Finds endpoints using `CippWizard*` components (e.g., `CippWizardOffboarding`) that hide fields from static analysis
- **Sparse parameters** — Flags POST endpoints with < 5 parameters that may need review
- **Sidecar coverage** — Reports which wizard endpoints lack sidecars

Example output:
```
1. Wizard-based endpoints:
  ✓ ExecOffboardUser (CippWizardOffboarding) — has sidecar
  ✗ AddAPDevice (CippWizardAutopilotOptions) — needs sidecar
```

**Auto-generate missing sidecars:**

Instead of manually creating sidecars, use the auto-generation script to extract fields from wizard components:

```bash
# Generate all missing wizard sidecars
./generate_wizard_sidecars.py

# Preview without writing files
./generate_wizard_sidecars.py --dry-run

# Regenerate specific sidecar (force overwrite)
./generate_wizard_sidecars.py --endpoint AddAPDevice --force
```

The script:
1. Detects which wizard components each endpoint uses (from Stage 2 output)
2. Locates the wizard component JSX files in the frontend repo
3. Extracts form fields using brace-aware parsing (handles nested API props)
4. Infers OpenAPI types from `type=` attributes (switch→boolean, autoComplete→object, etc.)
5. Generates properly formatted sidecar JSON files

Auto-generated sidecars include a `_comment` field documenting the source wizard components and field counts.

**Manual creation (if auto-generation fails):**
```bash
cp sidecars/_template.json sidecars/AddAPDevice.json
# Edit to add all wizard fields manually
```

Run this after CIPP releases to catch new wizard pages before they reach production.

---

## Validation

### OpenAPI Spec Validation

Validate generated specs for OAS 3.1 structural correctness:

```bash
# Validate the unified spec
python3 validate_spec.py out/openapi.json

# Validate all domain specs
python3 validate_spec.py out/domain/*.json

# Quiet mode for CI (only show errors)
python3 validate_spec.py --quiet out/openapi.json
```

The `--validate-only` mode (CI) automatically validates before diffing against the
committed spec. Validation failures block the pipeline.

Exit codes:
- `0` — All specs are valid
- `1` — Validation failed (structural errors)
- `2` — File not found or read error

### Documentation Preview

Generate static HTML documentation with Redocly:

```bash
# Generate docs from unified spec
./generate_docs.sh

# Generate from specific spec
./generate_docs.sh out/domain/identity-openapi.json
```

Output: `out/docs/index.html` — standalone HTML file, no dependencies.

**Requirements:** Node.js and npx (optional). If not installed, validation still works
(Python-only), but docs generation is skipped.

**Online alternative:** Drag `out/openapi.json` into:
- [Swagger Editor](https://editor.swagger.io/) — interactive editing + preview
- [Redoc Viewer](https://redocly.github.io/redoc/) — read-only documentation view

### GitHub Pages Deployment

Automatically deploy documentation to GitHub Pages for easy sharing:

**Setup (one-time):**
1. Go to your repository **Settings → Pages**
2. Under **Source**, select **GitHub Actions**
3. Save

**Deployment:**
- **Automatic:** Docs deploy when `out/openapi.json` is updated on `main` branch
- **Manual:** Go to **Actions → Deploy Documentation → Run workflow**

Once deployed, docs are available at:
```
https://<owner>.github.io/<repo>/
```

Example: `https://cipp-ashe.github.io/cipp-oas-generator/`

The workflow validates the spec and generates fresh HTML on every deployment.

---

## Coverage tiers

Every endpoint is assigned a coverage tier in the output:

| Tier | Meaning |
|---|---|
| `FULL` | Both API and frontend sources present; all params high confidence |
| `PARTIAL` | One source only, or mixed confidence, or blob-access params |
| `BLIND` | API function found but zero params extracted — pure blob passthrough |
| `ORPHAN` | Frontend calls endpoint but no `Invoke-*.ps1` found |
| `STUB` | Neither source found — sidecar-only documentation |

`BLIND` endpoints need sidecar files. The corpus run produces `out/coverage-report.json`
with the full list of BLIND endpoints sorted by name.

`coverage-report.json` also includes a `type_inference_coverage` block tracking the
percentage of params with a non-default type (`string`). Use this as a quality metric
over time — it increases as sidecars and `TYPE_HINTS` improve type fidelity.

---

## Configuration (`config.py`)

Single source of truth for all tunable constants. Nothing is hardcoded in stage scripts.

| Constant | Purpose |
|---|---|
| `API_REPO` / `FRONTEND_REPO` | Repo paths (env var overrides) |
| `SHARED_PARAM_REFS` | Params that become `$ref` in OAS (e.g. `tenantFilter`) |
| `FRONTEND_NOISE` | UI-only fields that are never sent to the API |
| `PS_NOISE` | PowerShell auto-members and LabelValue sub-fields to suppress |
| `PARAM_NOISE` | Call-option keys that appear in data objects but aren't params |
| `DOWNSTREAM_PATTERNS` | Regex patterns for detecting downstream service usage |
| `TYPE_HINTS` | Field name → OAS schema fragment (richer than default `string`) |
| `ALWAYS_REQUIRED_BODY/QUERY` | Params that are required on every endpoint |
| `DYNAMIC_EXTENSION_PARENTS` | Dotted paths that collapse to `DynamicExtensionFields $ref` |
| `KNOWN_FRONTEND_GAPS` | Documented static analysis limits (in output, never silent) |
| `COVERAGE_TIERS` | Tier definitions (in output) |

---

## What Stage 1 detects

Stage 1 (`stage1_api_scanner.py`) walks all `Invoke-*.ps1` HTTP entrypoints and extracts:

| Pattern | Source tag | Confidence |
|---|---|---|
| `$Request.Query.Name` | `ast_direct` | high |
| `$Request.Body.Name` | `ast_direct` | high |
| `$Alias = $Request.Body` then `$Alias.Name` | `ast_blob` | medium |
| `foreach ($x in $Request.Body.array)` then `$x.Field` | `ast_blob` | medium |
| `foreach` array container param | `ast_foreach_array` | high |

Also flags:
- `has_scheduled_branch` — body branches on `Scheduled.enabled`
- `has_dynamic_options` — `Select-Object * -ExcludeProperty` passthrough
- `dynamic_fields` — `$x.defaultAttributes` / `$x.customData` access
- `is_passthrough` — function delegates entirely to another CIPP function
- `downstream` — which services the endpoint touches (Graph, EXO, Scheduler, etc.)
- HTTP method inference from access pattern and explicit Graph call type hints

Non-entrypoints (Activity Triggers, helpers) are filtered via `.FUNCTIONALITY Entrypoint`.
Files without a `.FUNCTIONALITY` block are included with a warning in output.

## What Stage 2 detects

Stage 2 (`stage2_frontend_scanner.py`) scans all `.jsx`/`.js` files and extracts:

| Pattern | Method | Notes |
|---|---|---|
| `ApiGetCall({ url: "/api/Name", data: {...} })` | GET | Inline data keys = query params |
| `ApiPostCall({ url: "/api/Name" })` | POST | Form fields from same file |
| `ApiPostCall({ urlFromData: true })` + `.mutate({ url: "/api/Name" })` | POST | `MUTATE_URL_RE` |
| `` url: `/api/Name?key=${val}` `` | GET | Template param extraction |
| `<CippFormPage postUrl="/api/Name" />` | POST | `CIPP_FORM_PAGE_URL_RE`; inline fields captured; external child components flagged, not followed |
| `<CippFormComponent name="field" />` | — | Form field → body param |
| `<CippFormDomainSelector name="field" />` | — | LabelValue shape |
| `<CippFormLicenseSelector name="field" />` | — | Array of SKU IDs |
| `<CippFormUserSelector name="field" />` | — | LabelValue shape |

**Call record flags** (preserved through `consolidate()` into Stage 3 output):

| Flag | Meaning | Action |
|---|---|---|
| `has_external_form_component` | Fields live in a child component; not extracted | Add sidecar |
| `has_data_transform` | `customDataformatter` present; field names/types may differ | Add sidecar |
| `has_constructed_body` | Submit handler builds its own data object | Add sidecar |
| `url_patterns` | Set of scan methods that found this endpoint | Informational |

**Known static analysis gaps** (documented in output, never silent):

1. Shared form components (e.g. `CippAddEditUser`) render different fields based on
   `formType`. All fields are attributed to all endpoints using the component.
   Sidecar resolves this.
2. CippFormPage with an external child form component: the child import is not followed.
   Endpoint is discovered and flagged `has_external_form_component=true`; add a sidecar.
3. `customDataformatter` on CippFormPage rewrites field names/types before POST.
   Endpoint is flagged `has_data_transform=true`; add a sidecar.
4. Handler-constructed POST bodies: submit handler builds its own object rather than
   submitting form values directly. Endpoint is flagged `has_constructed_body=true`; add sidecar.
5. New `*Selector` components not in `SELECTOR_COMPONENT_RES` are invisible until added.
6. New wrapper functions (`ApiPatchCall`, etc.) classify as GET unless added to
   `POST_CONTEXT_RE`.
7. Direct `axios`/`fetch` calls not wrapped in `ApiGetCall`/`ApiPostCall`.
8. Computed endpoint names (`` url: `/api/${name}` ``).

## What Stage 3 does

Stage 3 (`stage3_merger.py`) reconciles Stage 1 + Stage 2 + sidecar:

- Params in both sources → `confidence=high`, API name casing wins
- API only → param's own confidence, `api_only` mismatch note
- Frontend only → `confidence=medium`, `frontend_only` mismatch note
- Location conflict (API says query, frontend says body) → mismatch note, API wins
- Sidecar additions → replace existing if same name, append if new
- Sidecar `remove_params` → case-insensitive, silently ignored if not present

Sidecar validation runs before merge. Fatal errors (missing required fields, bad types)
skip the endpoint and log it as an error — they don't abort the full run.

**`trust_level` validation:** If a sidecar sets `trust_level: "tested"`, a warning is
emitted when `tested_version` is absent or null. Set `tested_version` to the CIPP release
the sidecar was validated against (e.g. `"v10.1.0"`) to enable regression tracking.

## What Stage 4 emits

Stage 4 (`stage4_emitter.py`) produces OAS 3.1 JSON:

- `out/openapi.json` — unified spec, all endpoints
- `out/domain/<name>-openapi.json` — per-domain specs (Identity, Email-Exchange, etc.)
- Single-endpoint mode prints the OAS path snippet to stdout, writes nothing

OAS 3.1 correctness enforced:
- `$ref` is never combined with sibling keys — wrapped in `allOf` when extensions needed
- `required[]` only lists names present in `properties{}`
- `x-cipp-*` extensions are always legal in OAS 3.1

Extensions added per operation:

| Extension | When present |
|---|---|
| `x-cipp-role` | RBAC role required |
| `x-cipp-confidence` | When not `high` |
| `x-cipp-coverage-tier` | When not `FULL` |
| `x-cipp-downstream` | Services touched (graph, exo, scheduler, etc.) |
| `x-cipp-scheduled-branch` | Endpoint branches on `Scheduled.enabled` |
| `x-cipp-dynamic-options` | `Select-Object *` passthrough detected |
| `x-cipp-trust-level` | Sidecar provenance (`reversed`/`tested`/`inferred`) |
| `x-cipp-tested-version` | CIPP version sidecar was validated against |
| `x-cipp-raw-body` | `true` when `raw_request_body` sidecar escape hatch used |
| `x-cipp-warnings` | Consumer warnings (destructive ops, silent queuing, etc.) |
| `x-cipp-analysis-version` | Generator version (in `info`) |

---

## Outputs

| File | Stage | Description |
|---|---|---|
| `out/endpoint-index.json` | 1 | All API endpoints with AST params |
| `out/endpoint-index-{Name}.json` | 1 | Single-endpoint run (never overwrites corpus) |
| `out/frontend-calls.json` | 2 | All frontend call sites |
| `out/frontend-calls-{Name}.json` | 2 | Single-endpoint |
| `out/merged-params.json` | 3 | Reconciled, confidence-scored |
| `out/merged-params-{Name}.json` | 3 | Single-endpoint |
| `out/mismatch-report.json` | 3 | Frontend/backend drift per endpoint |
| `out/coverage-report.json` | 3 | Coverage tier breakdown + BLIND endpoint list |
| `out/openapi.json` | 4 | Unified OAS 3.1 spec |
| `out/domain/` | 4 | Per-domain specs |

---

## Adding a sidecar for a BLIND endpoint

```bash
# 1. Check the coverage report
cat out/coverage-report.json | python3 -m json.tool | grep blind_endpoints -A 50

# 2. Create a sidecar
cp sidecars/_template.json sidecars/MyEndpoint.json

# 3. Edit it — see sidecars/README.md for full schema including raw_request_body
#    for complex nested schemas, trust_level/tested_version for provenance,
#    and x_cipp_warnings for consumer safety notes

# 4. Validate it against the endpoint
./run.sh --validate-endpoint MyEndpoint

# 5. Verify the OAS snippet looks right
python3 stage4_emitter.py --endpoint MyEndpoint
```

---

## Evergreen operation

Re-run after any commit touching:
- `Modules/CIPPCore/Public/Entrypoints/HTTP Functions/**` (API repo)
- `src/components/**`, `src/pages/**`, `src/api/**` (frontend repo)

After a CIPP release: run `--check-patterns` first. If any check fails, update the
relevant regex in the stage scripts before running the corpus.

CI: `.github/workflows/validate-openapi.yml` runs `--validate-only` on every PR.

---

## Known limits

This tool uses regex-based static analysis, not a real AST parser. It handles the
patterns CIPP actually uses. Patterns it cannot resolve:

- Params consumed only inside downstream functions (e.g. `New-CIPPUserTask` internals)
- Fields rendered conditionally by shared form components without `postUrl` tracing
- Dynamic computed endpoint URLs
- Direct `axios`/`fetch` calls not using CIPP's wrapper functions
- PS1 parameter default values (not extracted)
- Response body structure (auto-detected only where sidecars exist; blind elsewhere)
- Usage examples (not generated — add manually via sidecar `add_responses`)

For all of these: write a sidecar. The `raw_request_body` escape hatch handles cases
where the param structure is too complex for `add_params`.

Future enhancement: `@babel/parser` AST for JSX would eliminate the shared-form
attribution gap by tracing `<CippFormPage postUrl="/api/Name">` to its exact field tree.
