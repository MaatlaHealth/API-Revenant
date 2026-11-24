# Data Model

SQLAlchemy 2.x declarative models, Alembic migrations, portable between SQLite (local) and PostgreSQL (production). Primary keys are UUID strings. All tables have `created_at`; mutable tables have `updated_at`. Timestamps are UTC. JSON columns use SQLAlchemy `JSON` (JSONB on PostgreSQL through a type variant). Enumerations are stored as strings with `CHECK` constraints.

There is no `users` table: the first release is single-tenant behind one admin token. `audit_records.actor` records the token label.

## Entity overview

```
projects ─┬─< evidence_files ─┬─< api_specs ──< endpoints ──< endpoint_facts
          │                   └─< fixtures  (>── endpoints, nullable)
          ├─< reconstructions ─┬─< mappings ──< mapping_decisions
          │                    ├─< invariants
          │                    ├─< behaviours          (behaviour inventory)
          │                    └─< gaps
          ├─< adapter_versions
          ├─< inbound_keys            (legacy-client credentials, hashed)
          ├─< outbound_credentials    (replacement-provider credentials, encrypted, versioned)
          ├── verification_targets (1:1)
          ├─< verification_runs ─┬─< test_results          (>── behaviours)
          │                      └─< verification_behaviour_states (>── behaviours)
          ├── runtime_instances (1:1) ──< runtime_events
          ├─< runtime_hosts
          ├─< pagination_cursors
          └─< audit_records

instance_lease   (single row, not project-scoped)
```

## Tables

### projects
| Column | Type | Notes |
|---|---|---|
| id | uuid PK | |
| slug | string(64) | **unique**; `^[a-z0-9-]+$`; used in runtime URL |
| name, description | string, text | |
| legacy_name, replacement_name | string | Display names of the two providers |
| replacement_base_url | string | Validated by the outbound guard on write |
| is_demo | bool | Marks seeded demonstration data |
| status | string | `draft`, `evidence`, `reconstructed`, `adapter_ready`, `verified`, `running` — derived and stored on transition |
| settings | JSON | Timeouts, normalisation rules, sensitive paths |
| evidence_version | int | Incremented whenever an evidence file is imported or removed |
| evidence_fingerprint | string(64) | Hash over the content hashes of the current (non-deleted) evidence files |

### evidence_files
| Column | Type | Notes |
|---|---|---|
| id, project_id FK | | index `(project_id, created_at)` |
| side | string | `legacy`, `replacement` |
| kind | string | `openapi`, `swagger`, `json_schema`, `fixtures`, `markdown`, `manual_example` |
| filename, content_type, size_bytes | | |
| sha256 | string(64) | **unique** `(project_id, sha256)` prevents duplicate imports |
| content | large binary | Raw bytes as uploaded |
| parse_status | string | `parsed`, `partial`, `failed`, `unsupported` |
| parse_report | JSON | Counts, warnings, and every item that was rejected with the reason |
| deleted_at | timestamp, nullable | Soft delete. A removed file leaves the current evidence set but its row and content are retained so that historical reconstructions and runs remain reproducible |

Malformed or unsupported files are still stored with `failed`/`unsupported` and a report, so nothing is silently discarded.

### api_specs
One row per parsed specification: `evidence_file_id FK`, `project_id FK`, `side`, `title`, `version`, `spec_format`, `servers` JSON, `auth_schemes` JSON, `normalised` JSON (the internal contract model).

### endpoints
| Column | Notes |
|---|---|
| id, project_id FK, api_spec_id FK nullable | Nullable because an endpoint can be established by fixtures alone |
| side | `legacy`, `replacement` |
| method, path_template | **unique** `(project_id, side, method, path_template)` |
| operation_id, summary | |
| request_schema, response_schemas, parameters | JSON, normalised |

### endpoint_facts — provenance
| Column | Notes |
|---|---|
| id, endpoint_id FK | index `(endpoint_id, fact_type)` |
| fact_type | `field`, `parameter`, `header`, `status_code`, `auth`, `pagination`, `error_shape`, `default`, `enum` |
| subject | Field path or parameter name |
| value | JSON: type, required, default, enum values, etc. |
| evidence_file_id FK | **not null** |
| locator | JSON pointer into the spec, or fixture id + path |
| basis | `declared` (from a spec), `exemplified` (seen in a fixture) |

Every extracted fact has exactly one provenance row; the same fact supported by two sources has two rows.

### fixtures
`id`, `project_id FK`, `evidence_file_id FK`, `endpoint_id FK nullable`, `side` (`legacy` = historical fixture; `replacement` = paired recording), `pair_with` FK nullable to a legacy fixture (set on replacement recordings), `name`, `request` JSON (method, path, query, headers, body), `expected_response` JSON (status, headers, body), `content_sha256`, `recorded_at nullable`, `normalisation` JSON (per-fixture overrides). Credential headers are redacted at import; the original value is never stored. Fixtures are immutable after import: verification has no write path to this table.

### reconstructions
One row per analysis run: `project_id FK`, `status`, `engine_version`, `summary` JSON (counts by confidence and category), `evidence_version` and `evidence_fingerprint` (the evidence set it analysed), `evidence_file_ids` JSON (the exact files), `inventory_size`, `inventory_fingerprint`, `previous_reconstruction_id FK nullable`, `inventory_diff` JSON (behaviours added and removed relative to the previous reconstruction).

A reconstruction is **stale** when its `evidence_fingerprint` differs from the project's current one. Staleness is computed, not stored. A reconstruction and everything under it (mappings, invariants, gaps, behaviours) is immutable once completed, apart from review decisions recorded in their own append-only tables. A reconstruction referenced by a verification run is never deleted.

### mappings
| Column | Notes |
|---|---|
| id, project_id FK, reconstruction_id FK | index `(project_id, status)` |
| legacy_endpoint_id FK, replacement_endpoint_id FK | nullable for document-level mappings (auth, error envelope) |
| category | `endpoint`, `request_field`, `response_field`, `query`, `header`, `auth`, `pagination`, `status_code`, `error`, `enum`, `type`, `effect` |
| legacy_ref, replacement_ref | Path or behaviour description |
| transformation | JSON — one RTL rule or block |
| confidence | `observed`, `inferred` |
| rule_name | Which proposer produced it |
| rationale | Human-readable explanation generated from the rule |
| evidence_refs | JSON list of `endpoint_facts` / fixture ids |
| requires_approval | bool; always true for `inferred` |
| status | `proposed`, `approved`, `rejected`, `superseded` |
| origin | `engine`, `human` |
| supersedes_id FK nullable | Set when a human edits a proposal |

### mapping_decisions
Append-only: `mapping_id FK`, `decision` (`approved`/`rejected`/`edited`), `actor`, `note`, `created_at`.

### gaps — unresolved behaviour
`project_id FK`, `reconstruction_id FK`, `endpoint_id FK nullable`, `category`, `title`, `description`, `evidence_refs` JSON (what *is* known), `missing` (what evidence would resolve it), `severity` (`blocking` if it prevents compiling a route, else `advisory`), `resolution` (`open`, `accepted_risk`, `resolved_by_evidence`).

### adapter_versions
`project_id FK`, `version` int — **unique** `(project_id, version)`, `document` JSON (the RTL document), `document_sha256`, `mapping_ids` JSON, `compiled_by`, `notes`. Immutable after insert. Each route in the document carries `effect` (`read_only`, `state_changing`, `unknown`), compiled from the approved `effect` mapping for that route; a route with no approved declaration is `state_changing` if either method is unsafe and `unknown` otherwise. Verification reads operation effects only from the adapter version a run is pinned to.

### inbound_keys — legacy-client credentials
| Column | Notes |
|---|---|
| id, project_id FK | index `(project_id, status)` |
| label | **unique** `(project_id, label)`; shown in traces instead of the key |
| verifier | binary — HMAC-SHA-256 of the key under a key derived from the master key. Not reversible. |
| key_id | Which master key the verifier was derived from (for master-key rotation) |
| status | `active`, `revoked` |
| created_at, revoked_at | |

No column holds the key itself. Several rows may be `active` per project.

### outbound_credentials — replacement-provider credentials
| Column | Notes |
|---|---|
| id, project_id FK | |
| name | The `secret_ref` used by adapters |
| version | int; **unique** `(project_id, name, version)` |
| ciphertext | binary (Fernet) |
| key_id | Master key identifier |
| status | `active`, `previous`, `retired`; a partial unique index (emulated on SQLite by a service-layer check in the same transaction) allows one `active` per `(project_id, name)` |
| created_at, retired_at | |

No plaintext column. The API never returns values. The two credential tables are deliberately separate: they have different storage forms and no code path reads both for the same purpose.

### invariants — legacy-only behavioural invariants
`project_id FK`, `reconstruction_id FK`, `endpoint_id FK`, `kind` (`echo`, `schema`, `pagination`, `error`), `definition` JSON (legacy paths only — never a replacement path or mapping id), `confidence` (`observed`, `inferred`), `evidence_refs` JSON, `status` (`active`, `proposed`, `rejected`). An `inferred` invariant must be approved before verification uses it.

### behaviours — the behaviour inventory
| Column | Notes |
|---|---|
| id, project_id FK, reconstruction_id FK | index `(project_id, reconstruction_id)` |
| endpoint_id FK nullable | |
| kind | `response` (endpoint + status code), `auth`, `pagination_traversal`, `pagination_termination`, `gap`, `unsupported` |
| key | Stable deterministic key, e.g. `POST /v1/charges → 201`; **unique** `(reconstruction_id, key)` |
| description | |
| evidence_refs | JSON; empty when nothing establishes the behaviour |
| scope | `in_scope`, `out_of_scope` (with `scope_note`, required). Out-of-scope behaviours stay in the inventory. Changes are recorded in `audit_records`; each run snapshots the scope it used. |
| unsupported_reason | Set for `kind = unsupported` |

Enumerated deterministically from evidence when reconstruction runs. The set of rows for a reconstruction never changes afterwards: evidence imported later produces behaviours only in a new reconstruction. `endpoint_id` null marks a project-level behaviour.

### verification_behaviour_states
One row per (run, behaviour): `run_id FK`, `behaviour_id FK` — **primary key** `(run_id, behaviour_id)`; `behaviour_key` and `scope` (copied at run start, so later changes cannot alter the run); `state` (`verified`, `failed`, `not_executed`, `unresolved`, `not_assessed`); `partial` bool (true for a verified behaviour with at least one skipped or errored check); `reason` (for `not_executed`: `skipped` / `error`; for `unresolved` and `not_assessed`: the specific reason); `checks_planned_executable`, `checks_executed`, `checks_unresolved`. A run has exactly one row for every behaviour in the inventory it started with, so the five state counts always sum to the inventory size. Rows are written once and never updated.

### verification_runs
| Column | Notes |
|---|---|
| id, project_id FK, adapter_version_id FK | |
| reconstruction_id FK | The reconstruction, and therefore the inventory, the run used. Must not be stale at start |
| evidence_version, evidence_fingerprint | Copied from the reconstruction at start |
| inventory_fingerprint | Hash of the behaviour keys and scopes at start |
| config | JSON — normalisation rules, target label, `allow_replay_traffic`, `allow_state_changing_traffic`, `allow_generated_traffic`, sandbox headers and reset path, as they were at start |
| state_changing_traffic | JSON — `{ enabled, requests_sent: { recorded: { read_only, state_changing, unknown }, generated: { … } }, checks_blocked: { state_changing, unknown }, unknown_operations: [ … ], target_reset: { declared, performed, outcome } }`. Always written; every `state_changing` and `unknown` count under `requests_sent` must be 0 when `enabled` is false (service-layer assertion) |
| status | `queued`, `running`, `completed`, `errored` |
| layers | JSON list of layers requested (`offline`, `independent`, `live`) |
| target | JSON — the declared test target used, or null |
| started_at, finished_at | |
| formula_version, weights | Stored so past runs stay interpretable |
| check_totals | JSON — check outcomes (passed / failed / skipped / error / unresolved), overall and per category and endpoint |
| inventory_size | int — N, fixed when the run starts |
| state_counts | JSON — `{ verified: { fully, partially }, failed, not_executed: { skipped, error }, unresolved, not_assessed }`; CHECK in the service layer that they sum to `inventory_size` |
| checks_executed, checks_planned_executable, checks_unresolved | ints — executed = passed + failed; planned executable = passed + failed + skipped + error |
| behavioural_compatibility | float, **nullable** (null when no check executed) |
| behavioural_compatibility_independent | float, nullable — computed over checks with `evidence_overlap = false` |
| tested_share | float, nullable — (verified + failed) / N; the display denominator for behavioural compatibility |
| evidence_coverage | float, nullable — (verified + failed + not_executed) / N; null when N = 0 |
| incomplete | bool — true if any result is `error` |
| evidence_fingerprint | Hash of fixtures, contracts and invariants at start; re-checked at finish to prove verification did not alter evidence |
| hypothesis_seed, error | |

A run in status `completed` or `errored` is immutable: no column of the run, its `test_results` or its `verification_behaviour_states` is ever updated, and none is deleted except by deleting the project. Whether evidence has changed since the run is computed by comparing `evidence_fingerprint` with the project's current value and is never written back.

### verification_targets
One row per project (`project_id` **unique**):

| Column | Notes |
|---|---|
| base_url | Validated by the outbound guard |
| label | |
| allow_replay_traffic | bool, default false — recorded and scripted requests may be sent |
| allow_generated_traffic | bool, default false — Hypothesis-generated requests may be sent |
| allow_state_changing_traffic | bool, default false — operations not classified read-only may be exercised, from any permitted source. Has no effect on its own |
| sandbox_headers | JSON, nullable — fixed non-secret headers added to verification traffic only |
| reset_path | string, nullable — `POST` path that restores known state on the target |
| updated_at | |

Every change to a permission writes an `audit_records` row. No constraint links the three columns: `allow_state_changing_traffic` is a modifier that is consulted only together with a source permission.

### test_results
| Column | Notes |
|---|---|
| id, run_id FK, endpoint_id FK nullable | index `(run_id, outcome)`, `(run_id, endpoint_id)` |
| behaviour_id FK | **not null** — every check belongs to exactly one behaviour |
| layer | `offline`, `independent`, `live` |
| category | `replay_offline`, `contract`, `property`, `integration` |
| group_key | The legacy endpoint id, or `project` for checks of project-level behaviours; the grouping used for behavioural compatibility |
| traffic_source | `offline`, `recorded`, `generated`, or null for checks that send nothing (contract) |
| effect | `read_only`, `state_changing`, `unknown` — the effective classification made by the planner before execution; null for offline checks |
| name | |
| outcome | `passed`, `failed`, `unresolved`, `skipped`, `error` |
| oracle | `fixture_response`, `replacement_request_schema`, `legacy_contract`, `invariant`, `none` |
| evidence_overlap | bool — the fixture replayed is evidence for a mapping it exercises |
| ignored_fields | int — fields excluded by declared normalisation |
| detail | JSON: field-level diffs, failing example (shrunk), reason for unresolved / skipped / error |
| evidence_refs | JSON |
| duration_ms | |

Every planned check has exactly one row, including those that end `unresolved`, `skipped` or `error`. Behaviours with no check at all (for example an open gap) have no row here; they appear in `verification_behaviour_states`.

### runtime_instances
One row per project (`project_id` **unique**): `desired_state` (`running`/`stopped`), `adapter_version_id FK nullable`, `generation` int (orders control operations; not read on the request path), `status` (`stopped`, `starting`, `running`, `failed`), `last_error`, `last_loaded_at`, `config` JSON (timeouts, limits).

### runtime_hosts
`host` **primary key** (normalised: lowercase, no port, no trailing dot, IDNA), `project_id FK`, `created_at`. The primary key guarantees one hostname cannot route to two projects. This table is the source of the in-memory host table used by `HostDispatcher`.

### instance_lease
Single row (`id = 1`, CHECK), created by the initial migration:

| Column | Notes |
|---|---|
| holder_id | Random id generated at each process start; null before first acquisition |
| host, parent_pid | Used only to recognise a sibling worker |
| epoch | int; incremented on every acquisition |
| heartbeat_at | Written with the **database** clock on acquisition and renewal |
| released | bool; set true by a graceful shutdown or handover, after the holder has stopped serving |
| handover_requested_at, handover_requested_by | Set by a starting process that found a live lease held by another process; cleared on acquisition |

Acquisition, renewal, handover request and release are single compare-and-set `UPDATE` statements on this row; no locks are held across statements. Used to prevent more than one process serving at a time, under the assumptions stated in `architecture.md` §4.5.

### runtime_events
| Column | Notes |
|---|---|
| id, project_id FK | index `(project_id, created_at desc)` |
| request_id | **unique**, nullable — the correlation id returned as `X-Revenant-Request-Id` |
| kind | `request`, `lifecycle`, `error`, `trace_loss` |
| entry | `host`, `path` — how the request reached the runtime |
| original_host, method, legacy_path, upstream_path | Original values as received. `upstream_path` never includes a query string |
| legacy_query | The **sanitised** query string: the inbound credential parameter removed, everything else as received. Null for requests that failed before an adapter was resolved |
| query_credential_removed | bool |
| route_id | |
| legacy_status, upstream_status, duration_ms, upstream_ms | |
| outcome | `ok`, `transform_error`, `upstream_error`, `upstream_auth_failed`, `auth_rejected`, `credential_leak_blocked`, `no_route`, `unmapped_error`, `request_timeout`, `request_deadline_exceeded`, `lease_lost`, `cancelled_drain` |
| trace | JSON — rule trace, redacted snapshots, auth labels (never credential values); capped at 32 KB |
| adapter_version_id FK | |

A row exists only for traces that were actually persisted. `trace_loss` rows record how many traces were dropped and why (`queue_full`, `persist_failed`, `shutdown` when known). Retention: capped per project (default 5 000 rows), pruned by the trace writer.

### pagination_cursors
For the `cursor_cache` strategy: `project_id`, `route_id`, `fingerprint`, `limit`, `page`, `cursor`, `expires_at`; **unique** `(project_id, route_id, fingerprint, limit, page)`.

### audit_records
Append-only: `project_id FK nullable`, `actor`, `action` (e.g. `mapping.approve`, `runtime.activate`, `credential.inbound.add`, `credential.outbound.rotate`), `target_type`, `target_id`, `metadata` JSON (never secret values), `created_at`. Index `(project_id, created_at)`.

## Integrity rules

- Foreign keys enforced (`PRAGMA foreign_keys=ON` for SQLite). Project deletion cascades.
- `adapter_versions` and `audit_records` are never updated.
- Completed reconstructions, their behaviour inventories, and completed verification runs with their result and state rows are never updated.
- A verification run cannot be created for a stale reconstruction (service-layer check in the creating transaction).
- No table stores an unsanitised query string or a credential value.
- A `runtime_instances` row with `desired_state='running'` must have a non-null `adapter_version_id` (CHECK).
- A mapping cannot be `approved` while it has `confidence='inferred'` and no `mapping_decisions` row (enforced in the service layer inside the same transaction).
