# Revenant Transformation Language (RTL) — v1.0

The Adapter Document is the compiled output of approved mappings. It is plain JSON, validated by Pydantic models and a static validator, and interpreted by a fixed engine. There is no expression evaluator, no user-defined function, no dynamic import, and no code generation step. Anything the language cannot express is reported as a gap.

## 1. Document shape

```json
{
  "schema_version": "1.0",
  "project_slug": "gravepay",
  "upstream": { "base_url": "https://api.novapay.demo" },
  "auth": {
    "inbound":  { "type": "api_key_header", "header": "X-API-Key" },
    "outbound": { "type": "bearer", "secret_ref": "replacement_token" }
  },
  "error_envelope": { "rules": [ /* body rules producing the legacy error shape */ ] },
  "routes": [ /* Route */ ]
}
```

A **Route**:

```json
{
  "id": "create-charge",
  "match":    { "method": "POST", "path": "/v1/charges" },
  "upstream": { "method": "POST", "path": "/payments" },
  "effect": "state_changing",
  "request":  { "path": [], "query": [], "headers": [], "body": { "mode": "build", "rules": [] } },
  "response": {
    "status_map": [ { "from": 201, "to": 201 } ],
    "headers": [],
    "body": { "mode": "build", "rules": [] },
    "errors": [ /* ErrorRule */ ]
  },
  "pagination": null
}
```

Every rule carries a `mapping_id` linking it to the approved mapping (and through it to the evidence) that justifies it. The validator rejects rules without one.

`effect` records what the operation does to the replacement: `read_only`, `state_changing` or `unknown`. The runtime does not use it when serving legacy clients. Verification uses it, together with the HTTP methods as a floor, to decide whether a request may be sent to a test target (`architecture.md` §5.4).

## 2. Paths

A path addresses a value in a named scope: `<scope>:<pointer>`.

| Scope | Available in | Meaning |
|---|---|---|
| `body` | request, response | The message being read (legacy request body, or upstream response body). |
| `query`, `path`, `header` | request | Legacy request parts. Header names are case-insensitive. |
| `status` | response | Upstream status code. |
| `request.body`, `request.query`, `request.path` | response | The original legacy request, for echoing values the replacement does not return. |
| `item` | inside `each` | The current array element. |
| `var` | error envelope | The variables set by the matching error rule. |

Pointer grammar: `$` (root), `.name`, `[n]` (non-negative integer index). Wildcards are not allowed in paths; array iteration is done only with the `each` rule. Maximum depth 16.

## 3. Rules

All rules read from the source scopes and write to the output document. In `build` mode (the default) the output starts empty and only mapped values are emitted; source fields that no rule consumed are listed in the trace as `unmapped_fields`. `passthrough` mode starts from a copy of the source and must be approved explicitly per route.

| `op` | Fields | Behaviour |
|---|---|---|
| `move` | `from`, `to`, `required?` | Copy a value. Covers rename, nesting (`body:$.amount` → `$.transaction.value`) and flattening (the reverse). |
| `const` | `to`, `value` | Write a literal JSON value. |
| `default` | `to`, `value` | Write the value only if nothing has been written to `to`. |
| `convert` | `from`, `to`, `as`, `params?` | Type conversion. `as` ∈ `string`, `integer`, `number`, `boolean`, `iso8601_to_epoch_s`, `epoch_s_to_iso8601`, `epoch_ms_to_iso8601`, `iso8601_to_epoch_ms`, `scale` (`params.factor`, exact decimal arithmetic, error on precision loss). |
| `enum_map` | `from`, `to`, `map`, `on_unmapped` | Map enumerated values. `on_unmapped` ∈ `error`, `passthrough`, `{ "default": v }`. |
| `template` | `to`, `template`, `vars` | String interpolation of `{name}` placeholders from an explicit `vars` map of paths. No expressions, no format specifiers. |
| `each` | `from`, `to`, `rules` | Apply `rules` to every element of the array at `from`; inside, `item:` is the element scope. Nesting depth of `each` is limited to 3. |
| `drop` | `path` | Remove a value (meaningful in `passthrough` mode). |

`required: true` on a rule makes a missing source value a transformation error (legacy-shaped `400` on the request side, `502` on the response side). Otherwise a missing source is skipped and recorded in the trace.

### Conditions

Any rule, and every `ErrorRule`, may carry `when`, a conjunction of predicates. Predicates are data, not expressions:

`{ "exists": path }`, `{ "equals": [path, literal] }`, `{ "in": [path, [literals]] }`, `{ "status_in": [codes] }`, `{ "not": predicate }`.

## 4. Query, path and header translation

`request.query`, `request.path` and `request.headers` are rule lists using the same operations with targets in the `query`, `path` or `header` scope of the upstream request.

- The upstream request starts with **no** headers. A header reaches the replacement only through an explicit rule, or through the outbound auth block.
- **Credential carriers are outside the language.** `authorization`, `proxy-authorization`, `cookie`, `set-cookie`, the carrier declared by `auth.inbound` and the carrier used by `auth.outbound` cannot be read or written by any rule; the validator rejects such rules. The inbound carrier is also deleted from the `header`/`query` scope before rules run, so it does not exist as far as the engine is concerned.
- Hop-by-hop headers, `Host` and `Content-Length` are set by the HTTP client, never by rules. The original `Host` is readable as `header:$.host` for rules that need it.
- Path parameters are percent-encoded by the engine after substitution; a substituted value can never introduce a new path segment or query string.

## 5. Authentication translation

Authentication is declared in the document but performed by the runtime, not by rules. The two sides are independent:

- `auth.inbound` — how the **legacy client** proves itself to the runtime: `{ "type", "header" | "param" }`. It names the carrier only. The accepted keys are the project's active inbound keys, stored as keyed hashes; the document holds no reference to any particular key.
- `auth.outbound` — how the runtime authenticates to the **replacement provider**: `{ "type", "secret_ref" }`. `secret_ref` names an outbound credential held in encrypted storage; the active version is resolved when the adapter is loaded.

Supported types: `api_key_header`, `api_key_query`, `bearer`, `basic`, `none`.

Order of operations: extract the inbound carrier from the header or query scope and delete it there → verify the extracted value → run rules → snapshot the upstream request for the trace → attach the outbound credential → pre-send check that the presented legacy key value appears nowhere in the upstream request → send.

For `api_key_query` the `query` scope seen by rules is built from the **sanitised query**: the carrier parameter is removed, and every other parameter keeps its original encoding, order and repetition. A carrier that appears more than once is rejected with `401` before any rule runs. Rules have no way to refer to the removed parameter. The legacy credential is therefore never forwarded, and neither credential value can appear in the Adapter Document, a rule's input, a trace, a log or an API response. `inbound.type: "none"` requires an approved mapping and the project setting `allow_unauthenticated_runtime`.

Failure behaviour is defined in `architecture.md` §4.7.

## 6. Pagination

```json
{
  "strategy": "computed_cursor",
  "legacy":  { "page_param": "page", "limit_param": "limit", "first_page": 1, "default_limit": 20, "max_limit": 100 },
  "upstream": { "cursor_param": "cursor", "size_param": "page_size" },
  "cursor":  { "encoding": "base64_json", "shape": { "page": "$page" } },
  "response": {
    "items_from": "body:$.items", "items_to": "$.data",
    "has_more_from_presence_of": "body:$.next_cursor", "has_more_to": "$.has_more",
    "echo": { "$.page": "$page", "$.limit": "$limit" }
  }
}
```

Two strategies are defined. Neither claims to translate arbitrary pagination schemes.

- **`computed_cursor`** — stateless. Valid only when the replacement's cursor is a documented deterministic function of the page number. The engine supports `base64_json` and `plain` encodings of a fixed shape with the placeholders `$page`, `$limit`, `$offset`. This is the NovaPay demonstration case: `page=2` → `cursor=eyJwYWdlIjoyfQ==`.
- **`cursor_cache`** — stateful, for opaque cursors. The runtime stores `(project, route, filter fingerprint, limit, page) → cursor` as responses are observed. A request for page *N* uses the cached cursor, or walks forward from the nearest cached page with a hard hop limit (default 10). If the limit is exceeded the adapter returns a legacy-shaped error instead of guessing. Cache entries expire.

The end-of-list condition is declared explicitly (`has_more_from_presence_of`). If evidence does not establish how the legacy API terminated pagination, that is a gap in the reconstruction report, not a default in the engine.

## 7. Error normalisation

```json
{
  "when": { "status_in": [422], "equals": ["body:$.code", "CURRENCY_NOT_SUPPORTED"] },
  "status": 400,
  "vars": { "code": { "const": "invalid_currency" }, "message": { "from": "body:$.detail" } }
}
```

`ErrorRule`s are evaluated in order for any upstream status ≥ 400. The first match sets the legacy status and supplies variables to the document-level `error_envelope` rules, which build the legacy error body.

If no rule matches, the upstream status is preserved, the envelope is populated with a generic code only if the envelope itself is established by evidence, and the response carries `X-Revenant-Unmapped: error`. The trace marks the error as unmapped, and verification reports it as unresolved. The adapter never fabricates a specific legacy error code.

## 8. Static validation

An Adapter Document is rejected at compile time and again at load time if any of these fail:

1. Schema validation (discriminated union on `op`; unknown fields forbidden).
2. Path grammar, scope availability and depth limits.
3. Target collisions — two unconditional rules writing the same target.
4. Type compatibility where source and target types are known from the contracts. *(Not implemented yet.)*
5. Every `mapping_id` refers to an approved mapping (checked at compile time). Every `secret_ref` must name an outbound credential with an active version when the runtime loads the adapter; at compile time a missing credential is reported as a warning, so an adapter can be compiled and dry-run before the credential is stored.
5a. No rule reads from or writes to a credential carrier (§4). No rule writes the outbound credential's header or parameter.
5b. Every route has `effect` set to `read_only`, `state_changing` or `unknown`. A route with `effect: read_only` whose `match.method` or `upstream.method` is not `GET`, `HEAD` or `OPTIONS` is rejected. `read_only` must be backed by an approved `effect` mapping; without one the compiler writes `state_changing` (unsafe method on either side) or `unknown`.
6. Every required replacement request field is produced by some rule.
7. Limits: ≤ 200 routes, ≤ 200 rules per list, document ≤ 512 KB.
8. Upstream base URL passes the outbound guard.

### As implemented

- Timestamps are converted only in the form `YYYY-MM-DDTHH:MM:SSZ` (UTC, whole seconds). Any other form is a transformation error, not a best-effort parse.
- Numbers are converted with exact decimal arithmetic. A result that a JSON number cannot hold exactly, a fraction where an integer is required, and a magnitude beyond 10^30 are errors.
- A substituted template value is never scanned for placeholders again.
- When no error rule matches, the legacy response has the upstream status and no body; nothing is invented.
- A compiled route that fails the required-field checks is left out of the document and reported; it is never emitted incomplete.

## 9. Execution limits

Request and response bodies ≤ 1 MB (configurable), arrays ≤ 10 000 elements for `each`, string values ≤ 64 KB, deterministic evaluation order (document order). The engine is a pure function `(document, input scopes) → (output, trace)` with no I/O.

## 10. Trace format

Each executed rule appends `{ rule_index, op, mapping_id, from, to, outcome }` where `outcome` ∈ `applied`, `skipped_missing`, `skipped_condition`, `error`. Values are **not** recorded in rule traces.

A runtime event additionally stores:

- the original method, host and raw path, and the **sanitised** query (inbound credential parameter removed), with `query_credential_removed` set when one was removed;
- a legacy request snapshot taken **after** the inbound credential carrier was removed;
- an upstream request snapshot taken **before** the outbound credential was attached;
- `auth: { inbound: "verified" | "rejected" | "none", key_label, outbound: "<type> attached", credential_version }` — labels and version numbers only.

Snapshots then pass through the redactor (configured sensitive paths and any value equal to a credential are replaced with `[REDACTED]`), and are truncated to the trace size cap with `truncated: true`. Delivery of traces to storage is best-effort and bounded; see `architecture.md` §4.9.
