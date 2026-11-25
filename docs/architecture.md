# API Revenant — Technical Architecture

Status: Stage 1 design, revision 6. Implementation status: slices 1 to 3 (configuration, logging, health endpoints, outbound guard, database layer, admin authentication, credential storage, redaction, projects API, evidence import) are implemented and tested; everything else in this document is still design. See `README.md` for what exists.

## 1. System overview

```
                         ┌──────────────────────────── one backend process ────────────────────────┐
                         │  HostDispatcher (outermost ASGI app)                                    │
 Legacy client ── HTTP ─▶│    Host is a registered legacy host ──────────────▶ RuntimeProxy        │
 Host: api.gravepay.demo │    management host ─▶ FastAPI app; any other host ─▶ 421                │
                         │                   /api/v1/*            management API                   │
 Browser ── HTTPS ──────▶│                   /runtime/{slug}/*    mount ─────────▶ RuntimeProxy    │
 (Next.js on Netlify)    │                                                                         │
                         │   RuntimeProxy ─▶ registry (immutable compiled adapters, in memory)     │
                         │                ─▶ transformation engine (constrained language)          │
                         │                ─▶ outbound guard ─▶ shared httpx.AsyncClient ───────────┼──▶ Replacement API
                         │                ─▶ trace pipeline (bounded queue ─▶ writer thread)       │
                         └───────────────┬─────────────────────────────────────────────────────────┘
                                         ▼
                              SQLite (local) / PostgreSQL (production)
```

One deployable backend process hosts both the management API and the compatibility runtime. The frontend is a separate deployment that only talks to `/api/v1`.

## 2. Components

| Package | Responsibility |
|---|---|
| `revenant.api` | FastAPI routers for the management API. Thin; no business logic. |
| `revenant.evidence` | Upload validation, OpenAPI/Swagger and fixture parsing, fact extraction with provenance. |
| `revenant.reconstruction` | Normalised contract model, legacy↔replacement diff, mapping proposals, invariants, gap detection. |
| `revenant.transform` | The constrained transformation language: schema, static validator, interpreter. |
| `revenant.adapter` | Compiles approved mappings into a versioned, immutable Adapter Document. |
| `revenant.runtime` | Host dispatcher, ASGI proxy, registry, forwarding, trace pipeline, health, instance lease. |
| `revenant.verification` | Planner, three test layers, independent oracles, scoring. |
| `revenant.security` | Outbound URL guard, credential store, redaction, upload limits, admin auth. |
| `revenant.db` | SQLAlchemy models, session management, Alembic migrations. |

Dependency direction: `api → {evidence, reconstruction, adapter, verification, runtime} → transform → (nothing)`. `transform` has no I/O and no database access. `verification.oracles` must not import `transform` or `adapter` (see §5.3).

## 3. The reconstruction pipeline

1. **Ingest.** Evidence files are validated, stored, and parsed into `endpoints` and `endpoint_facts`. Every fact row carries `evidence_file_id` plus a locator (JSON pointer or fixture index).
2. **Normalise.** Both sides are converted into one internal contract model: endpoints, parameters, flattened field paths with type/required/default/enum, status codes, auth scheme, pagination parameters, error shape.
3. **Pair endpoints.** Deterministic scoring on method, path-segment similarity, operation tags and schema overlap. Pairs below threshold are reported as gaps, never forced.
4. **Propose mappings.** Rule-based proposers, each emitting a mapping with a rule name and evidence references:
   - identical name and type;
   - normalised-name match (`customer` ↔ `customer_id`, case/underscore/suffix rules);
   - structural match (leaf name and type match under a different parent);
   - fixture value correlation (the same value appears at path A in a legacy fixture and at path B in a paired replacement recording);
   - enum cardinality and value-similarity match;
   - auth scheme, pagination parameter and error-shape detectors.
5. **Classify confidence.**
   - `observed` — the legacy side of the mapping is directly present in imported evidence **and** the correspondence is confirmed by at least one fixture pair or by exact name/type identity in both specs.
   - `inferred` — produced by a heuristic rule without direct confirmation. Always requires human approval.
   - `unknown` — evidence is insufficient; emitted as a **gap**, not as a mapping.
6. **Derive invariants.** Legacy-only behavioural invariants are derived from the legacy contract and fixtures (see §5.2). They do not reference replacement fields or mappings.
7. **Review.** Mappings start `proposed`. A human approves, rejects or edits each. Edits produce a new mapping row with `origin = human`; the proposal is retained for audit.
8. **Compile.** Only `approved` mappings are compiled into an Adapter Document. The compiler refuses to emit a route whose required replacement fields are not all satisfied.

No LLM participates in the first release.

## 4. Compatibility runtime

### 4.1 Entry points

The runtime is one ASGI application object, `RuntimeProxy`, created once at startup. It is reachable two ways, and both converge on the same handler `handle(project, method, raw_path, raw_query, headers, body)`:

- **Path entry:** FastAPI mounts it at `/runtime/{project_slug}`. The mount strips that prefix; what remains is the legacy path.
- **Host entry:** the outermost ASGI app, `HostDispatcher`, sends requests for registered legacy hostnames to it directly (§4.2).

No listening sockets are created in response to API calls. "Starting" a runtime means writing desired state to the database and loading a compiled adapter into the registry.

### 4.2 Legacy hostname routing

DNS only delivers the TCP connection to the backend container. Routing to the right runtime is done by host-aware ASGI dispatch inside the backend. No separate reverse proxy is required.

**Mechanism.** `HostDispatcher` wraps the FastAPI application and is the object Uvicorn serves. For every `http` scope it makes one decision, from the request's host and nothing else:

1. Extract and normalise the host (rules below). A malformed host gets `400` and is dispatched nowhere.
2. **Registered legacy host** → call `RuntimeProxy` with the ASGI scope **unmodified**, adding only the resolved project id under a private scope key. If that project's runtime is stopped or failed the response is `503`; the request never falls through to the management API.
3. **Management host** (listed in `REVENANT_PUBLIC_HOSTS`) → pass the scope, unmodified, to the FastAPI application.
4. **Any other host** → `421 Misdirected Request`. The one exception is `GET /healthz` and `GET /readyz`, which are answered for any well-formed host because platform health probes do not always send the public hostname; they return no sensitive information.

`lifespan` scopes go to FastAPI. `websocket` scopes on a legacy host are rejected; the first release proxies HTTP only.

**Strict normalisation.** One function, `normalise_host`, is used both when a host is registered and when a request is dispatched, so the two can never disagree.

- Exactly one `Host` header is required. None, or more than one, → `400`.
- The request target must be origin-form (the raw path begins with `/`). Absolute-form and authority-form targets → `400`, so a target URL can never contradict the `Host` header.
- Grammar is `hostname[:port]`. Rejected with `400`: an empty value; whitespace or control characters; a slash, backslash, `@`, `?`, `#`, comma or `%`; non-ASCII bytes; an underscore; a port that is not an integer from 1 to 65535; a name longer than 253 characters; a label that is empty, longer than 63 characters, contains anything other than letters, digits and hyphens, or begins or ends with a hyphen.
- Normalisation is exactly: ASCII-lowercase, remove the port, remove one trailing dot. Nothing is percent-decoded and no Unicode processing happens at dispatch (non-ASCII has already been rejected).
- IP literals (IPv4, bracketed IPv6) are well-formed but can never be legacy hosts; they are accepted only if listed as management hosts.
- Internationalised names are registered in A-label (`xn--`) form. Registration converts Unicode input with IDNA 2008 and stores the A-label; dispatch compares A-labels only.

**Exact matching.** Lookup is full-string equality between the normalised host and a key of the host table. There are no wildcards, no suffix or subdomain matching, no prefix matching and no default host. The port is ignored for matching because there is one listener. The host table is an immutable mapping replaced atomically when registrations change.

**What is preserved.** Because the scope is passed through untouched, the runtime receives exactly what the client sent:

| Element | Source | Guarantee |
|---|---|---|
| Method | `scope["method"]` | Unchanged. |
| Path | `scope["raw_path"]` | Original bytes, including percent-encoding; no prefix added or removed, no normalisation of `//` or `..`. |
| Query string | `scope["query_string"]` | Original bytes on entry; parameter order and repeated parameters retained. If the inbound credential is carried in a query parameter, that parameter is removed immediately (§4.7); every other parameter stays byte-identical and in order. |
| Headers | `scope["headers"]` | All headers, original order and duplicates, including the original `Host`. |
| Body | ASGI `receive` | Original bytes, subject to the size limit. |

These are the **inputs** to the adapter. What is sent upstream is determined only by approved rules (the upstream `Host` is the replacement's; headers are forwarded by explicit rule only — see `transformation-language.md` §4). The original host, method and path, and the **sanitised** query (§4.7), are recorded in the trace. The unsanitised query string is never stored or logged.

**Management endpoints are not reachable through legacy hostnames.** The dispatch decision is made once per request from the host, and `RuntimeProxy` holds no reference to the FastAPI application. On a legacy hostname, `/api/v1/*`, `/docs`, `/openapi.json`, `/healthz`, `/readyz` and `/runtime/{slug}/…` are therefore ordinary legacy paths and receive the adapter's `404`. The only reserved path on a legacy host is `/__revenant/health`. Conversely, on a management host the runtime is reachable only under `/runtime/{slug}`.

**Host routing is not authentication.** The `Host` header is chosen by the client. It selects *which surface answers*, never *whether the caller is authorised*: the runtime always requires a legacy-client key and the management API always requires the admin token. Sending a spoofed host gains a caller nothing that addressing that surface directly would not.

**Registration and collisions.** `PATCH /projects/{id}/runtime` sets `legacy_hosts`. Each value is passed through `normalise_host` and rejected if it is malformed, contains a wildcard, is an IP literal, is `localhost`, is a management host, or is already registered. Uniqueness is a primary key on the normalised form, so variants that differ only in case, trailing dot, port, or Unicode versus A-label spelling collide and are refused. If configuration later changes so that a registered legacy host also appears in `REVENANT_PUBLIC_HOSTS`, the management host wins: at start-up that host is left out of the host table, an error lifecycle event is written, and health reports the conflict. In production the application refuses to start if `REVENANT_PUBLIC_HOSTS` is empty.

**Forwarded headers are never trusted by default.** `X-Forwarded-Host`, `Forwarded`, `X-Original-Host`, `X-Host` and `X-Forwarded-Server` play no part in dispatch. The only opt-in is `REVENANT_TRUST_FORWARDED_HOST=true`, which additionally requires `REVENANT_TRUSTED_PROXIES` (a CIDR list; start-up fails if it is empty). With both set, `X-Forwarded-Host` — and only that header — is used in place of `Host` when the directly connected peer address is inside a trusted range, the header has exactly one value, and that value passes `normalise_host`; otherwise the request gets `400`. From any other peer the header is ignored. A reverse proxy placed in front must otherwise pass the original `Host` through.

**Docker demonstration, end to end.**

1. The client container resolves `api.gravepay.demo` through Docker's embedded DNS. In the *after* profile that name is a network alias of the backend container.
2. The client opens a TCP connection to the backend on port 8000 and sends, for example, `POST /v1/charges` with `Host: api.gravepay.demo:8000`.
3. Uvicorn passes the request to `HostDispatcher`, which normalises the host to `api.gravepay.demo`, finds it registered to the GravePay project, and calls `RuntimeProxy` with the untouched scope.
4. `RuntimeProxy` matches `POST /v1/charges`, transforms, forwards to `http://api.novapay.demo:8000/payments`, transforms the response, and replies.

**Limitations.** Serving a legacy hostname over HTTPS requires a certificate for that hostname at whatever terminates TLS; this is outside the first release and the demonstration uses HTTP on a private Docker network. The hosted deployment uses path entry.

### 4.3 Activation state machine

`runtime_instances` holds **desired** state (`desired_state`, `adapter_version_id`, `generation`) and **reported** state (`status`, `last_error`, `last_loaded_at`).

```
stopped ──activate──▶ starting ──load ok──▶ running ──deactivate──▶ stopped
                          │
                          └──validation / credential failure──▶ failed   (desired_state stays 'running';
                                                                          last_error recorded; retry on demand)
```

- Activation is one database transaction that sets `desired_state='running'`, pins `adapter_version_id` and increments `generation`.
- The registry then loads the adapter: validate the document, resolve credentials (§4.7), build the immutable compiled adapter. A swap is a single reference assignment, so in-flight requests finish on the version they started with.
- Activation is refused (`409 runtime_not_ready`) if no adapter version exists, the outbound credential is missing, or inbound authentication is declared but no inbound key is active.
- Deactivation sets `desired_state='stopped'`, increments `generation`, removes the registry entry. Subsequent requests receive `503` with `X-Revenant-Runtime: stopped`.
- `generation` gives an ordering for concurrent control calls and an audit trail; with one process the registry is authoritative, so the request path never reads it from the database.

### 4.4 Restart recovery

On process start (FastAPI lifespan), in order:

1. Run the configuration checks (§4.5). Fail fast on violation.
2. Check database connectivity with bounded retry and backoff.
3. Acquire the instance lease (§4.5), waiting in standby for a handover if another process holds it.
4. Load the host table from `runtime_hosts`.
5. Select every `runtime_instances` row with `desired_state='running'`. For each: load the pinned adapter version, re-validate it, resolve credentials, load into the registry, set `status='running'`, write a `lifecycle` event.
6. Any failure in step 5 sets that instance to `status='failed'` with `last_error`; other projects still recover.
7. Mark verification runs left in `running` as `errored: interrupted`.
8. Start the trace writer, then report ready.

The pinned `adapter_version_id` guarantees the runtime resumes on exactly the version that was active, not "latest".

### 4.5 Single-worker constraint

**Requirement.** The first release must run as exactly one process with one worker. The runtime registry, host table, resolved credentials and trace counters live in process memory; a second worker would hold an independent copy and could serve a stopped runtime or an old adapter version.

**Configuration check (fail fast).** At startup the application refuses to start, with exit code 1 and the error `unsupported_multi_worker`, if any of these indicates more than one worker:

- `WEB_CONCURRENCY` is set to a value other than `1`;
- `REVENANT_WORKERS` is set to a value other than `1`;
- the process detects it was started by a multi-worker supervisor (Uvicorn `--workers N` with N > 1, or Gunicorn), by inspecting the parent process command line where the platform allows it.

Every supplied start command (`render.yaml`, Dockerfile, documentation) pins `--workers 1`, and the deployment configs set `WEB_CONCURRENCY=1` explicitly because some platforms default it from the CPU count.

**Instance lease (runtime check).** Environment variables cannot catch every case (a platform scaled to two instances, an overlapping deployment), so a single serving process is also enforced with a lease held in the application's own database. It uses one table row and no additional infrastructure. Its purpose is narrow: **under the assumptions listed at the end of this section, at most one process serves runtime traffic at a time.** It is not a clustering mechanism, it does not make more than one instance supported, and it is not an unconditional guarantee.

*State.* Table `instance_lease` has exactly one row, created by the migration: `holder_id` (a random id per process start), `host`, `parent_pid`, `epoch`, `heartbeat_at`, `released`, `handover_requested_at`, `handover_requested_by`. All timestamps written to and compared in this row use the **database clock**, so processes never compare their own wall clocks with each other.

*Timing* (defaults; validated at start-up):

| Setting | Symbol | Default | Meaning |
|---|---|---|---|
| Renew interval | R | 5 s | How often the holder renews. |
| Grace | G | 45 s | How long the holder may keep **admitting** runtime requests without a confirmed renewal. |
| Request lifetime | L | 30 s | Hard upper bound on the total lifetime of any runtime request, including reading the request body, the upstream call and writing the response (§4.6). |
| Clock margin | M | 15 s | Allowance for clock-rate divergence between the process and the database, and for scheduling delay. |
| TTL | — | 90 s | How old `heartbeat_at` must be, by the database clock, before another process may take the lease. |

Start-up fails unless **`TTL ≥ G + L + M`**. The lease calculation uses the complete request lifetime, not the upstream timeout alone.

*Why that inequality.* Let `t0` be the moment the holder *sent* its last successful renewal. The holder keeps two in-memory deadlines:

```
admit_until = t0 + G          no runtime request is admitted after this
stop_by     = admit_until + L every admitted request has ended by this
```

The database wrote `heartbeat_at` at or after `t0`, so a successor cannot acquire before `t0 + TTL ≥ t0 + G + L + M = stop_by + M`. The old process has therefore stopped serving every request at least M before a successor is permitted to acquire, provided the assumptions below hold. Deadlines are measured with `CLOCK_BOOTTIME` where the operating system provides it, so time spent suspended counts; otherwise with the monotonic clock.

*Acquisition.* A single atomic statement — `UPDATE instance_lease SET holder_id = :me, epoch = epoch + 1, heartbeat_at = now, released = false, handover_requested_at = null, handover_requested_by = null WHERE released = true OR heartbeat_at < now − TTL` — succeeds only if exactly one row changes. A process serves runtime traffic only after this succeeds. Until then it is in `standby`: `/readyz` fails and runtime requests receive `503` with `X-Revenant-Runtime: standby`.

*Renewal.* Every R seconds, on a dedicated thread: `UPDATE … SET heartbeat_at = now WHERE holder_id = :me AND epoch = :my_epoch`, returning the row. One row changed extends `admit_until` and `stop_by` from the send time, and reveals any pending handover request. Zero rows changed means ownership was lost.

*New requests and requests already in progress.*

| Situation | New runtime requests | Requests already in progress |
|---|---|---|
| Lease confirmed | Admitted | Run, each bounded by L. |
| Renewal failing, before `admit_until` | Admitted | Run, each bounded by L. |
| Past `admit_until` (lease expired locally) | `503`, `X-Revenant-Runtime: lease_expired` | Continue only until their own deadline, which is no later than `stop_by`. A watchdog cancels anything still running at `stop_by`. |
| Ownership lost (renewal changed zero rows) | `503`, `X-Revenant-Runtime: superseded` | Cancelled **immediately**. Upstream connections are closed. The client receives `503` if no response byte has been sent; otherwise the connection is aborted. |
| Handover or graceful shutdown | `503`, `X-Revenant-Runtime: handover`, `Retry-After` | Drained: allowed to finish, each bounded by L; anything left at the drain deadline is cancelled. |
| Suspected pause (watchdog tick late by more than R) | Not admitted until the next successful renewal | Requests that have not yet sent their upstream call are aborted with `503`; the rest are bounded by `stop_by`. |

The lease is checked at three points in a request (§4.6): on admission; again immediately before the upstream call is sent; and continuously through the lifetime deadline. All three are in-memory checks with no database access.

*Handover protocol (overlapping start, e.g. a deployment).* H is the current holder, S the starting process.

1. S passes its configuration checks, connects to the database and reads the lease row.
2. If the lease is released or older than the TTL, S acquires it (above) and the protocol ends.
3. If the lease is live and H has the same `host` and `parent_pid` as S, S is a sibling worker and exits with `unsupported_multi_worker`.
4. Otherwise S records a request: `UPDATE … SET handover_requested_at = now, handover_requested_by = :S WHERE holder_id = :H AND epoch = :e AND released = false`. S stays in `standby` and retries acquisition every second. **S never takes a live lease.**
5. H sees the request at its next renewal (within R). H keeps renewing during the following steps, so the lease cannot expire underneath a drain.
6. H stops accepting runtime requests (`503`, `X-Revenant-Runtime: handover`) and fails `/readyz`.
7. H waits for in-flight requests to finish. Each is already bounded by L, so the wait is at most L; anything still running then is cancelled.
8. H drains the trace queue for at most the trace drain deadline (§4.9).
9. H releases: `UPDATE … SET released = true WHERE holder_id = :H AND epoch = :e`. The lease is never released while H can still serve.
10. S's next acquisition attempt succeeds (within one second), `epoch` increases, S runs start-up recovery (§4.4) and becomes ready.
11. H remains up only as a non-ready `superseded` process (`503`, `X-Revenant-Runtime: superseded`) until the platform stops it. It never requests the lease back.

Expected duration is R + (in-flight time) + a few seconds; the worst case is R + L + trace drain + 1 s ≈ 41 s with defaults. Legacy clients see `503` with `Retry-After` during that window. This is the accepted cost of not overlapping.

*Graceful shutdown* (`SIGTERM` with no successor waiting) is steps 6–9 without the request.

*Timeouts and stale-process recovery.*

| Case | Behaviour |
|---|---|
| H crashed or was killed without releasing | `heartbeat_at` stops advancing. S acquires once it is older than the TTL (at most 90 s with defaults). No manual step. |
| H is completely hung (no renewals, no drain) | Same as a crash for S. If H later resumes, its `admit_until` and `stop_by` deadlines have passed and its next renewal changes zero rows, so it serves nothing. |
| H's event loop is hung but its renewal thread is alive, so it never drains | The renewal thread enforces a **handover deadline** of L + trace drain + 5 s from seeing the request. At that deadline it sets the in-memory fenced flag (checked on admission and before every upstream call), stops renewing and releases the lease itself. S then acquires within a second. |
| S crashes while waiting, or after H released | The lease is released or expires; the next process to start acquires it. H does not resume. |
| Several successors start at once | Exactly one acquisition succeeds. A standby process that sees the lease acquired by a holder newer than the one it asked exits with `lease_unavailable`; it does not issue another handover request, so instances cannot take the lease from each other in turn. |
| S cannot acquire within `REVENANT_LEASE_WAIT_SECONDS` (default 120; must be at least TTL + 10 s) | S exits non-zero with `lease_unavailable`. |

*Database outage.* See §4.10.

*Development.* `REVENANT_LEASE_FORCE_ACQUIRE=true` takes the lease immediately at start-up, for recovering from a hard-killed local process without waiting for the TTL. It bypasses the protections in this section and is refused when `REVENANT_ENV=production`.

*Assumptions and limitations.* The single-server property holds only under these assumptions. They are stated so the mechanism is not mistaken for a stronger guarantee.

1. **Clock rates.** The process clock and the database clock advance at rates that differ by less than M over an interval of G + L. Sustained clock-rate divergence larger than the margin can let a successor acquire before the old process has stopped.
2. **Pauses.** A process pause is detected only where the clock in use keeps counting during it (`CLOCK_BOOTTIME` across an operating-system suspend) or where the watchdog tick is visibly late. A whole-machine freeze in which every guest clock stops is not detectable from inside the process until the next renewal attempt.
3. **No upstream fencing token.** The replacement API cannot tell a current holder from a stale one. Once a request has been transmitted upstream it cannot be recalled; the replacement may act on it even if the lease is lost a moment later. The design bounds this window with L and re-checks the lease immediately before sending, but cannot eliminate it.
4. **One consistent database.** The lease row lives in a single database that applies each `UPDATE` atomically and does not lose acknowledged writes. A database failover that discards the latest lease write, or two deployments pointed at different databases, is outside the model.
5. **Cooperating processes.** Every process runs this code with the same timing settings and without the development force-acquire flag.

When an assumption is violated, two processes may serve for a bounded period. The consequence is that a client may be served by an instance with stale adapter state, and — because of limitation 3 — a request may reach the replacement from each. The product therefore describes this as *designed to prevent* simultaneous serving under stated assumptions, never as an absolute guarantee, and it does not support more than one instance.

Supporting multiple workers later requires moving registry invalidation to a shared mechanism; it is out of scope for this release.

### 4.6 Request path

**Total request lifetime.** Every runtime request has one hard deadline, L (`REVENANT_REQUEST_LIFETIME_SECONDS`, default 30 s), measured from the moment the request is accepted to the last response byte written. It covers reading the request body, transformation, the upstream call and writing the response. It is implemented as a single deadline around the whole handler; the upstream timeout for a request is the smaller of the configured upstream timeout and the time remaining. Upstream responses are buffered up to the size limit and transformed, not streamed, so writing the response is short and inside the same deadline.

1. **Admission.** Check the lease in memory (§4.5): accepting, not fenced, before `admit_until`. Otherwise `503`. Start the lifetime deadline.
2. Resolve the project (host or path entry) and its compiled adapter. Unknown project → `404`; stopped → `503`.
3. **Credential extraction** (§4.7). Remove the inbound credential carrier from the headers or the query string and keep its value in a local variable. From this point the only request that exists for rules, traces and logs is the sanitised one.
4. Read the body within the size limit and the deadline. A client that has not finished sending by the deadline gets `408`, outcome `request_timeout`.
5. **Inbound authentication.** Verify the extracted value. Failure → `401`; nothing is sent upstream.
6. Match the route by method and legacy path template. No match → `404`.
7. Run the request transformation (path, query, headers, body, pagination).
8. **Second lease check.** If ownership has been lost, the process is fenced, or the deadline has passed, stop with `503` and send nothing upstream. Then snapshot the upstream request for the trace, attach the **outbound credential**, and run the pre-send leak check.
9. Connect under the outbound rules (`security.md`): resolve, validate every address, connect to a validated address, no redirects. Forward with the remaining lifetime as the timeout.
10. Run the response transformation (status map, headers, body or error normalisation, pagination conversion).
11. Offer the trace to the trace pipeline without waiting (§4.9). Write the legacy response.

Outcomes generated by the runtime itself: upstream timeout or lifetime deadline during the upstream call → `504` (`upstream_error` / `request_deadline_exceeded`); connection error → `502`; upstream rejects the outbound credential (`401`) → `502` with outcome `upstream_auth_failed`. When a request is cancelled, its upstream connection is closed, not returned to the pool. Legacy-shaped bodies are used when the legacy error envelope is established by evidence; runtime-generated error bodies never contain request data.

Credential extraction happens before the body is read and before authentication, so a request that is rejected, times out or fails later is still recorded only in sanitised form. A request that fails at step 1 or 2 has no adapter, so the carrier is not known: for those requests the query string and headers are **not recorded at all** — only method, path, host and outcome.

### 4.7 Authentication separation

There are two unrelated credential domains. They are stored differently, used at different steps, and never mixed.

| | Legacy-client (inbound) | Replacement-provider (outbound) |
|---|---|---|
| Purpose | Prove the caller is an authorised legacy client | Authenticate API Revenant to the replacement API |
| Who knows the value | The legacy client and nobody else after registration | The backend only |
| Storage | **Keyed hash** (HMAC-SHA-256 with a key derived from `REVENANT_SECRET_KEY`). Not recoverable. | **Encrypted** (Fernet) because the value must be sent upstream |
| Cardinality | Any number of active keys per project, each with a label | One active version per named credential |
| Used at | Step 3 of the request path | Step 7 of the request path |

**Never forwarding legacy credentials.** Four independent controls:

1. Before anything else is done with the request, the inbound credential carrier (the header or query parameter named by the adapter's inbound auth block) is removed from the request scopes, so no rule, trace or log can see it.
2. The static validator rejects any rule that reads or writes `authorization`, `proxy-authorization`, `cookie`, `set-cookie`, the inbound carrier or the outbound carrier.
3. The upstream request is built from an empty header set; only explicit rules add headers, and the outbound credential is attached last by the runtime, not by a rule.
4. Immediately before sending, the runtime checks that the presented legacy credential value does not occur anywhere in the upstream URL, headers or body. If it does, the request is aborted with `500`, outcome `credential_leak_blocked`, and nothing is sent.

**Query-parameter credentials.** The inbound carrier may be a query parameter (`api_key_query`) because some legacy APIs worked that way. The same route also promises to preserve the query string, so extraction is defined exactly:

1. The raw query string is split on `&` into an ordered list of raw `key=value` segments. Nothing is decoded in place.
2. A segment belongs to the carrier if its key, after percent-decoding and `+`-to-space decoding, equals the declared parameter name exactly (case-sensitive).
3. If the carrier appears more than once the request is rejected with `401`; an ambiguous credential is never guessed.
4. The credential value is the decoded value of the single carrier segment. It is held in a local variable for verification and the pre-send leak check, and discarded when the request ends.
5. The **sanitised query** is the remaining segments joined with `&`, in their original order and byte-identical: the same encoding, the same repeated parameters, the same empty segments.
6. The sanitised query replaces the original in the request scopes. The original string is not retained anywhere.

Where the legacy credential is kept out, and by what:

| Place | Control |
|---|---|
| Upstream URL and query string | Rules read only the sanitised query; the validator rejects any rule naming the carrier; the pre-send leak check scans the final upstream URL, headers and body for the value, in raw and percent-decoded form. |
| Request traces | The trace stores the sanitised query and sets `query_credential_removed: true`. Snapshots are taken after extraction. |
| Response traces | The upstream never received the value. The redactor still replaces any occurrence of the presented value in response snapshots as a second layer. |
| Application logs | The server's built-in access log is disabled (`--no-access-log`). The application's own request log records method, path **without query**, status, duration and request id; for runtime requests the sanitised query is added only after extraction. Exception text is never logged raw. |
| Error responses | Runtime-generated error bodies contain no request data. Legacy-shaped errors are built by rules, which cannot read the carrier. |
| Other persisted data | `runtime_events.legacy_query` holds the sanitised query. Audit records, health output and metrics contain no query strings. |

The same care applies in the other direction: if the **outbound** credential type is `api_key_query`, the upstream URL contains the replacement credential. Upstream URLs are logged and traced without their query string, the trace snapshot is taken before the credential is attached, and HTTP client exceptions are recorded by type with a query-free URL, never by their raw message.

*Limitation.* Infrastructure in front of the backend that the product does not control (a platform router, a load balancer, a corporate proxy) may log full request URLs, including a query-parameter credential. This cannot be prevented from inside the application. The product marks query-parameter authentication as weaker than header authentication in the review screen and recommends header carriers where the legacy contract allows.

**Lookup.** Credentials are resolved when an adapter is loaded (activation or recovery) into an in-memory object held beside the compiled adapter. That object is never serialised, has a redacted `repr`, and is not part of the Adapter Document. The request path performs no database reads for credentials.

**Rotation.**

- *Inbound:* add a new key (it becomes active immediately), move clients over, revoke the old key. Several keys may be active at once, so rotation needs no downtime. Revocation takes effect on the next request.
- *Outbound:* writing a new value creates a new version, makes it active and reloads the running adapter atomically. The previous version is kept as `previous` and can be re-activated (rollback) until it is retired. There is no automatic fallback to a previous version on upstream failure.
- *Encryption key:* `REVENANT_SECRET_KEY` may be rotated by supplying the old key as `REVENANT_SECRET_KEY_PREVIOUS`; decryption tries both, and a maintenance command re-encrypts stored outbound values. Inbound key hashes cannot be recomputed without the plaintext, so while the previous key is supplied verification checks against both derivations and upgrades a stored hash to the current key the next time that key is presented successfully. Inbound keys not used during the transition must be re-registered before the previous key is removed; the maintenance command lists them.

**Failure behaviour.**

| Condition | Behaviour |
|---|---|
| Inbound credential missing or wrong | `401`; no upstream call; outcome `auth_rejected`. The response does not reveal whether any key exists. Comparison is constant-time. |
| Adapter declares inbound auth but no key is active | Activation refused. Deleting the last active key while running is refused. |
| Adapter declares no inbound auth | Allowed only with an explicitly approved mapping and the project setting `allow_unauthenticated_runtime=true`; the UI warns that anyone who can reach the runtime can use the replacement credential. |
| Outbound credential missing or cannot be decrypted at load | Runtime status `failed` with `credential_unavailable`; it does not serve. |
| Upstream returns `401` for the outbound credential | Client receives `502`, never `401`, so a legacy client is not told its own key is wrong. Outcome `upstream_auth_failed`; health reports it. |

**Traces.** Credentials cannot enter a trace: the inbound carrier is removed before any snapshot, and the upstream snapshot is taken before the outbound credential is attached. The redactor (`security.md`) is applied as a second layer. A trace records only `auth: { inbound: "verified", key_label, outbound: "<type> attached", credential_version }`.

### 4.8 Concurrency

- Request handling is `async`. The transformation engine is pure CPU over small JSON documents with hard size limits.
- One shared `httpx.AsyncClient` with `max_connections` and `max_keepalive_connections`.
- Database access uses synchronous SQLAlchemy sessions **off the event loop**: management endpoints run in the thread pool, and the trace writer and lease heartbeat each run in their own thread. The proxy request path performs no database I/O at all.
- Per-project concurrency cap; excess requests receive `503` with `Retry-After`.
- SQLite runs in WAL mode with a busy timeout for local use; PostgreSQL uses a pooled engine with `pool_pre_ping`.

### 4.9 Trace pipeline

Design rule: tracing is best-effort observability. It must never delay or fail a proxy request, never grow without bound, and never claim a trace was stored when it was not.

**Bounds.**

- Queue: `asyncio.Queue(maxsize=REVENANT_TRACE_QUEUE_SIZE)`, default 1 000.
- Each trace is capped at 32 KB; larger request/response snapshots are truncated and marked `truncated: true`.
- Worst-case memory is therefore queue size × 32 KB plus one in-flight batch (default ≈ 35 MB).

**Enqueue.** The request path calls `put_nowait`. If the queue is full the trace is discarded and `dropped_queue_full` is incremented. There is no waiting, no retry, and no fallback buffer.

**Writer.** One background task takes up to 100 traces or waits 500 ms, then persists the batch in one transaction on a dedicated thread.

- On a persistence error the batch is retried up to 3 times with backoff (0.5 s, 1 s, 2 s). Only one batch is ever held for retry.
- If all retries fail, the batch is discarded, `dropped_persist_failed` increases by its size, and an error is logged with the count. While the writer is retrying, new traces keep arriving until the queue is full and are then dropped at enqueue — memory stays bounded.
- When persistence succeeds again after losses, the writer inserts one `trace_loss` lifecycle event recording how many traces were dropped and why, so the loss is visible in the event history, not only in live counters.
- The writer task is supervised: if it exits unexpectedly it is restarted with backoff, and health reports `writer: "stopped"` meanwhile.

**Counters** (per process, since start, exposed in health):

```
generated            = accepted + dropped_queue_full
accepted             = persisted + dropped_persist_failed + dropped_shutdown + queued + in_flight
```

Tests assert these identities hold after every scenario.

**Not claiming persistence.** A trace has three possible fates and the system reports only what it knows:

- The proxy response carries `X-Revenant-Request-Id` (a correlation id, always present) and `X-Revenant-Trace: queued` or `X-Revenant-Trace: dropped`. It never says "recorded".
- `GET …/runtime/events/{id}` returns the event only if it exists in the database; otherwise `404`.
- Request counts shown in the product come from persisted rows; the health counters show how many were lost alongside them.

**Shutdown.** On lifespan shutdown: the runtime stops accepting requests and in-flight requests finish; the writer drains the queue until empty or until `REVENANT_TRACE_DRAIN_SECONDS` (default 5) elapses; anything left is counted as `dropped_shutdown` and the count is written to the log (the counters die with the process). The instance lease is released after the drain (§4.5), and the writer thread and database connections are then closed. A hard kill loses whatever was queued; this is accepted and documented.

### 4.10 Database loss and recovery

Two goals pull in opposite directions here: keep serving through a database outage, and avoid two processes serving at once. A process that cannot reach the database cannot prove it still holds the lease, so serving through an outage is **bounded by the lease grace period**. This is deliberate and replaces the earlier unbounded "keep serving" behaviour.

| Phase | Proxy traffic | Lease | Traces | Management API | Health |
|---|---|---|---|---|---|
| Outage begins; within grace (default 45 s) | Served normally from in-memory state | Renewals fail and are retried every interval; `serve_until` is not extended | Queue to the bound, then drop and count | `503 database_unavailable` | `degraded`; `lease.state: held`, `valid_for_seconds` counting down |
| Outage exceeds grace | New requests: `503`, `X-Revenant-Runtime: lease_expired`. Requests in progress end by their own deadline, at most L later | Self-fenced; adapters stay loaded in memory | As above | `503` | `not_serving`; `lease.state: fenced`; `/readyz` fails |
| Database returns, lease row still names this process | Resumes immediately | Renewal succeeds (same `holder_id` and `epoch`); `serve_until` extended | Writer resumes; a `trace_loss` event is written if anything was dropped | Available | `ok` |
| Database returns, another process acquired meanwhile | Stays `503`, `X-Revenant-Runtime: superseded` | Lost; this process does not request it back | Remaining queue is dropped and counted | Available but the process is not ready | `not_serving`; `lease.state: standby` |
| Process starts while the database is down | Not served | Cannot acquire; bounded retry, then exit non-zero | — | — | `/readyz` fails |

Short interruptions — a database restart, a failover, a brief network fault — are shorter than the grace period and do not interrupt proxy traffic. The grace period and TTL are configurable for operators who prefer a different trade-off, subject to the start-up validation in §4.5.

### 4.11 Health

| Endpoint | Meaning |
|---|---|
| `GET /healthz` | Process liveness. No dependencies. |
| `GET /readyz` | Configuration checks passed, lease held, database reachable, startup recovery finished. |
| `GET /runtime/{slug}/__revenant/health` (or `/__revenant/health` on a legacy host) | Per-runtime health, computed at call time. |

Runtime health body:

```json
{
  "status": "ok | degraded | not_serving | stopped | failed",
  "adapter": { "loaded": true, "version": 3, "generation": 7 },
  "database": "ok | unavailable",
  "lease": { "state": "held | fenced | handover | standby", "epoch": 4, "last_renewed_at": "…", "valid_for_seconds": 41 },
  "requests": { "accepting": true, "in_flight": 3, "lifetime_seconds": 30 },
  "upstream": { "last_outcome": "ok | timeout | connect_error | auth_failed | none", "last_checked_at": "…" },
  "traces": {
    "writer": "running | retrying | stopped",
    "queue_depth": 0, "queue_capacity": 1000,
    "accepted": 1532, "persisted": 1532,
    "dropped": { "queue_full": 0, "persist_failed": 0, "shutdown": 0 },
    "last_persist_at": "…", "last_error": null,
    "counters_since": "…"
  }
}
```

`status` is `not_serving` when the lease is fenced or not held. It is `degraded` when the database is unavailable, a lease renewal is failing, the writer is not `running`, any trace was dropped in the last 60 s, or the last upstream outcome was `auth_failed`. `?deep=true` additionally sends a bounded probe to the upstream. The body contains no credentials and no request data.

## 5. Verification

Verification answers one question with evidence: *does the legacy interface served by the adapter behave the way the historical evidence says the legacy API behaved?* It is organised into three layers with different dependencies, so that a weakness in one is not hidden by another.

### 5.1 Layers

| | Layer A — Offline historical replay | Layer B — Independent contract and property tests | Layer C — Live runtime integration |
|---|---|---|---|
| Needs network / upstream | No | Contract: no. Property: a test target that allows **generated** traffic | Yes: a test target that allows **replay** traffic; operations classified state-changing or unknown additionally need **state-changing traffic** permission |
| System under test | Transformation engine with the compiled adapter | The legacy-facing output of A and C, and generated cases through the runtime | The full ASGI runtime: routing, auth, guard, HTTP, pagination, errors |
| Input | Historical legacy fixtures and paired replacement recordings | Outputs from A and C; Hypothesis-generated legacy requests | Historical legacy requests and scripted sequences |
| Oracle (source of the expected result) | The recorded legacy response in the fixture; the replacement OpenAPI request schema | The legacy contract compiled to JSON Schema; legacy-only invariants | The recorded legacy response; legacy-only invariants |
| Result category | `replay_offline` | `contract`, `property` | `integration` |

**Layer A — offline historical replay.** Runs with no network.

- *Request side:* each legacy fixture request is transformed. The produced upstream request is validated against the replacement specification's request schema by a third-party JSON Schema validator. If a paired replacement request recording exists, it is also compared field by field.
- *Response side:* a **paired replacement recording** (a response captured from the replacement for the equivalent operation, imported as evidence and linked to the legacy fixture by `pair_with`) is transformed, and the result is compared with the legacy fixture's recorded response after the declared normalisation.
- A legacy fixture with no paired recording yields a response-side result of `unresolved` ("no replacement-side response evidence"). It is not synthesised.
- A fixture for a legacy endpoint with no route in the adapter is `failed`.

**Layer B — independent contract and property tests.**

- *Contract:* every legacy-shaped response produced in layers A and C, and by property cases, is validated against the legacy contract (required fields, structure, types, enum membership, status code in the evidence-established set, required headers) using a third-party JSON Schema validator.
- *Property:* Hypothesis generates legacy requests from the legacy request schemas and sends them through the runtime to a **declared test target** (`verification_target.allow_generated_traffic = true`). Each invariant is checked on the legacy request and legacy response only. One invariant is one test, however many examples Hypothesis runs. Property tests require a test target with `allow_generated_traffic = true` (§5.4), and `allow_state_changing_traffic` as well for operations that are not read-only; otherwise they are `skipped` with the reason.

**Layer C — live runtime integration.** Requires a test target with `allow_replay_traffic = true` (§5.4); it does not require permission for generated traffic. With that permission alone only operations classified **read-only** are exercised; operations classified state-changing or unknown additionally require `allow_state_changing_traffic`. Real HTTP through the mounted runtime to the replacement simulator: live replay of historical requests compared with recorded legacy responses, stateful sequences (create → retrieve → list → refund), error triggers, pagination walks, and authentication negatives. This is the only layer that exercises auth translation, the outbound guard and upstream error handling.

### 5.2 How expected outputs are established

Expected results come from exactly three places, all of them imported evidence, all immutable and hash-stamped at import:

1. **Recorded legacy responses** in historical fixtures.
2. **The legacy contract** (specification), compiled to JSON Schema.
3. **Legacy-only invariants** derived deterministically from 1 and 2, and stored with evidence references:
   - *Echo invariants:* response field Y equals request field X. Derived only when at least two fixtures for the endpoint show equality with distinct values; classified `observed`. With a single fixture it is `inferred` and needs approval before use.
   - *Schema invariants:* required fields, types and enum membership from the legacy contract.
   - *Pagination invariants:* when legacy pagination is established — no record appears on two pages; a page holds at most `limit` records.
   - *Error invariants:* for each legacy error with a fixture — the input class that triggers it yields that status and code.

If none of these establishes an expectation for a behaviour, the check is `unresolved`. Normalisation (ignoring or type-matching variable fields such as generated ids and timestamps) must be declared in the fixture or project settings before the run and is listed in the report.

### 5.3 Preventing circular verification

A verification is circular when the thing being tested also supplies the answer. The design rules that prevent it:

1. **The adapter never supplies an expectation.** There is no "record current output as expected" or snapshot-update feature. Verification cannot write to fixtures, contracts or invariants.
2. **Oracles are independent code.** `verification.oracles` (schema validation, diffing, invariant checking, value lookup) does not import `revenant.transform` or `revenant.adapter` and does not read the Adapter Document. An architecture test fails the build if such an import appears. Schema validation uses a third-party validator, not the engine.
3. **Invariants are stated in legacy terms only.** They never mention replacement fields or mappings, so they cannot pass merely because the engine did what a mapping said.
4. **Engine correctness is tested separately.** Whether the engine executes rules as specified is covered by the project's own unit and property tests (`testing-strategy.md`), not by project verification, so it cannot inflate a compatibility result.
5. **Evidence overlap is disclosed.** A mapping proposed from fixture F will tend to pass replay of F. Each replay result records `evidence_overlap = true` when the fixture is among the evidence of a mapping it exercises. The report shows results with and without overlap, and behavioural compatibility is published for both sets (§6.5).
6. **Normalisation cannot erase a test.** If normalisation leaves no compared field, the result is `unresolved`, not `passed`. The number of ignored fields is reported per test.
7. **The simulator is independent.** The NovaPay simulator is written from the NovaPay specification and imports no API Revenant code. Results are labelled "against simulator"; fidelity of a simulator to a real provider is not claimed.
8. **The verifier is shown to fail.** The test suite runs verification against deliberately broken adapters and requires failures.

### 5.4 Traffic permissions and operation classification

Verification can send requests to a replacement test target. That is a capability to cause effects on someone else's system, so nothing is sent unless it is authorised, and what may be sent depends on two things: **where the request comes from** (the traffic source) and **what the operation does** (its effect).

#### Traffic sources

| Source | Layer | What it is |
|---|---|---|
| **Offline fixture replay** | A | Recorded requests and paired recordings run through the transformation engine in memory. Nothing leaves the process. |
| **Recorded / scripted (live)** | C | Historical fixture requests, and scripted sequences built from them, sent through the runtime to the target. A fixed, finite, inspectable set. |
| **Generated (live)** | B | Hypothesis-generated requests sent through the runtime to the target: arbitrary valid inputs, in volume, never recorded. |

#### Permissions

Three permissions per target. **All are disabled by default.**

| Permission | Meaning |
|---|---|
| `allow_replay_traffic` | Recorded and scripted requests may be sent. |
| `allow_generated_traffic` | Hypothesis-generated requests may be sent. |
| `allow_state_changing_traffic` | Operations that are not classified read-only may be exercised, **from whichever sources are themselves permitted**. It applies to every traffic source. On its own it sends nothing. |

#### Permission matrix

| Traffic | Operation effect | Required |
|---|---|---|
| Offline fixture replay | any | No traffic permission |
| Live recorded / scripted | read-only | `allow_replay_traffic` |
| Live recorded / scripted | state-changing or unknown | `allow_replay_traffic` **and** `allow_state_changing_traffic` |
| Hypothesis-generated | read-only | `allow_generated_traffic` |
| Hypothesis-generated | state-changing or unknown | `allow_generated_traffic` **and** `allow_state_changing_traffic` |

#### Operation classification

HTTP methods are not enough: an API may have side effects behind a `GET`. Every operation therefore carries an explicit **effect** with three values.

| Effect | Meaning |
|---|---|
| `read_only` | A person has declared, and approved, that calling this operation does not change data on the replacement. |
| `state_changing` | The operation creates, updates or deletes, or has any other side effect. |
| `unknown` | Nobody has established which it is. **Treated as state-changing for every permission decision**, and reported separately so it can be resolved. |

*Where the effect comes from.* The effect belongs to an adapter route (one legacy operation mapped to one replacement operation). It is a mapping of category `effect`, reviewed like any other mapping, compiled into the Adapter Document as `route.effect`, and therefore versioned and immutable with the adapter version a run uses.

- The engine proposes `state_changing` whenever either side's method is not safe.
- For a route whose methods are both safe, the engine proposes **`unknown`**, never `read_only`. A safe method alone is not evidence of safety.
- `read_only` exists only as an explicit human declaration, approved with an actor and a note, and recorded in the audit log.
- A specification may carry the vendor extension `x-revenant-effect` on an operation. It is treated as evidence for the proposal's rationale; it never makes an operation read-only without approval.

*Method semantics are a floor, not the rule.* A declaration can make an operation stricter than its method suggests, never looser:

```
effective_effect(request) =
    state_changing   if legacy method ∉ {GET, HEAD, OPTIONS}
    state_changing   if replacement method ∉ {GET, HEAD, OPTIONS}
    state_changing   if route.effect = state_changing
    read_only        if route.effect = read_only          (both methods safe, by the lines above)
    unknown          otherwise                             (no route, no declaration, or declared unknown)
```

*Conflicting declarations.* The most restrictive value always wins, and a looser declaration is rejected rather than ignored:

- Declaring `read_only` on a route where either method is state-changing is rejected when the declaration is made (`400`), again when the adapter is compiled (`422 adapter_invalid`), and — as a last line — overridden by the method floor at send time.
- Declaring `read_only` where evidence explicitly marks the operation state-changing (`x-revenant-effect: state_changing` on either side) is rejected.
- If two sources disagree, the stricter one applies.
- A `GET` with a side effect is handled by declaring it `state_changing`; that is always accepted.

*Scripted sequences.* A sequence takes the strictest effect of its steps. If **any** step is state-changing or unknown and `allow_state_changing_traffic` is absent, the **entire** sequence is blocked: no step is sent, including its read-only steps, because they depend on the writes.

#### Enforcement: planner and dispatcher

1. **Planner (before execution).** For every check it classifies the request or sequence with `effective_effect`, combines it with the traffic source, and consults the permission matrix against the permissions snapshotted at run start. An unauthorised check is never scheduled. It is recorded as `skipped` with a reason and the effect that caused it.
2. **Dispatcher (immediately before each send).** The component that hands a verification request to the runtime repeats the decision independently: it looks up the route's declared effect in the run's pinned adapter version, applies the method floor to the request's actual methods, takes the traffic source from the check, and consults the same snapshotted permissions. If the request is not authorised it is not sent, whatever the plan said. That refusal is recorded as `error` with the reason `blocked_unauthorised_request`, because it means planner and dispatcher disagreed.

The dispatcher does not trust any classification stored on the check; it recomputes from the adapter version and the request.

**Skip reasons.** `no_test_target`, `replay_traffic_not_authorised`, `generated_traffic_not_authorised`, `state_changing_traffic_not_authorised` (with `effect: state_changing` or `effect: unknown`), `target_reset_failed`.

#### Disclosure and audit

Every run records and reports, as part of its immutable record:

- the three permissions as snapshotted;
- whether state-changing traffic was enabled;
- requests sent, broken down by source (recorded, generated) and effect (read-only, state-changing, unknown);
- checks blocked, by effect;
- the operations whose effect is `unknown`, so they can be declared;
- whether the target's state was reset first.

An audit record is written whenever a permission changes and whenever an operation's effect is declared, approved or changed.

#### Isolated, resettable target state

A target may declare two optional settings, used only for verification traffic and never for the runtime's normal traffic:

- `sandbox_headers` — fixed, non-secret headers added to every verification request, so a target that supports it can keep verification data apart from other data.
- `reset_request` — a `POST` path on the target that restores a known state. When state-changing traffic is enabled and a reset is declared, it is called before the run and its outcome is recorded; if it fails, checks that are not read-only are `skipped` with the reason `target_reset_failed`.

The demonstration simulator implements both (see `demo-environment.md`).

#### Limits

- `read_only` is a human assertion. The product records who made it and enforces the method floor, but it cannot prove that an operation has no side effects.
- Volume, rate limits and cost on a real sandbox are the operator's responsibility.

## 6. Scoring

Two metrics are published and kept separate. **Behavioural compatibility** says how well the adapter matched where it was tested. **Evidence coverage** says how much of the legacy behaviour has evidence behind it at all. Neither is ever shown without its denominators and the full state breakdown, so a good compatibility figure cannot hide behaviour that is unknown or untested.

### 6.1 Two units: behaviours and checks

- A **behaviour** is one item of legacy behaviour that matters to a client. The **behaviour inventory** is enumerated deterministically from imported evidence when reconstruction runs, before any test exists:
  - one per legacy (endpoint, status code) that is documented in the contract or seen in a fixture;
  - one per endpoint for its authentication requirement;
  - one per paginated endpoint for page traversal and one for termination;
  - one per open gap;
  - one per legacy endpoint or feature that the product cannot analyse (for example a non-JSON body), so that it is counted instead of silently omitted.
- A **check** is one executable test in one layer (§5.1). Every check belongs to exactly one behaviour. A behaviour may have several checks, or none.

Most behaviours belong to one legacy endpoint. A behaviour that belongs to none (for example the response to an unknown path, or a gap about the API as a whole) is a **project-level behaviour**.

### 6.2 Evidence versions, inventories and historical runs

- **Evidence version.** A project has an integer `evidence_version` and an `evidence_fingerprint` (a hash over the content hashes of its current evidence files). Importing or removing an evidence file increments the version and changes the fingerprint.
- **Reconstruction.** Each reconstruction records the evidence version and fingerprint it analysed. Its mappings, invariants, gaps and behaviour inventory describe exactly that evidence.
- **Stale.** A reconstruction is stale as soon as the project's current evidence fingerprint differs from the one it recorded — that is, when any evidence file is imported or removed afterwards. Staleness is computed by comparison, not stored, so it cannot be forgotten.
- **Evidence imported after reconstruction does not enter the existing inventory.** An inventory is immutable once its reconstruction completes. New evidence becomes behaviours only through a new reconstruction, which creates a new inventory. Until then the product shows that the reconstruction is stale and how many evidence files are not yet analysed, and it does not present the old metrics as current.
- **Verification needs a current reconstruction.** Starting a verification run against a stale reconstruction is refused with `409 stale_reconstruction`.
- **Inventory changes are visible.** When a new reconstruction replaces an old one, the product reports the difference between the two inventories: behaviours added, behaviours removed and N before and after. A denominator cannot shrink unnoticed.
- **Historical runs are frozen.** A verification run records, at start: the reconstruction id, evidence version and fingerprint, adapter version, inventory size N, an inventory fingerprint, the scope of every behaviour, the formula version, the weights, the normalisation rules, the target and its permissions, and the layers requested. When it completes, its check results, behaviour states and metrics are written once. **A completed run is never modified:** later evidence, reconstructions, scope changes or formula versions do not alter its inventory, denominators or scores.
- **Retention.** Evidence files are soft-deleted, and a reconstruction referenced by a run is retained, so the exact evidence and inventory behind every historical run remain available.
- **Labelling old runs.** A run whose evidence fingerprint differs from the project's current one is labelled "evidence has changed since this run". The label is computed when the run is displayed; the stored numbers are untouched.

### 6.3 Check outcomes

| Outcome | Meaning | Executable? |
|---|---|---|
| `passed` | Executed; matched an evidence-established expectation. | Yes |
| `failed` | Executed; did not match an evidence-established expectation. Includes fixtures for endpoints with no route. | Yes |
| `skipped` | An expectation exists, but the check was not executed because of configuration (permission not granted, no target, layer deselected). | Yes |
| `error` | An expectation exists, but the check could not be executed because of an infrastructure fault. | Yes |
| `unresolved` | The check was planned, but evidence does not establish its expected result (for example an offline response check with no paired recording, or a comparison left empty by normalisation). | No |

Every planned check ends in exactly one outcome and is stored. The **planned executable checks** of a run are those with an evidence-established expectation: `passed + failed + skipped + error`. The **executed checks** are `passed + failed`.

### 6.4 Behaviour states

Every behaviour in a run's inventory is in exactly one of five states. The states are mutually exclusive and their counts always sum to N.

| State | Count | Definition | Rule |
|---|---|---|---|
| **Verified** — evidence available and successfully tested | P | At least one check passed and none failed. | Split into *fully verified* (every planned executable check passed) and *partially verified* (at least one planned executable check was skipped or errored). |
| **Failed** — evidence available but the test failed | F | At least one check failed. | A failure is never masked by passes on the same behaviour. |
| **Not executed** — evidence available but the test could not execute | X | No check passed or failed, and at least one check with an established expectation ended `skipped` or `error`. | Shown split into skipped and error. |
| **Unresolved** — behaviour planned but unsupported by evidence | U | It is in the inventory and in scope, but no evidence establishes what the legacy API did: it has no check, or all its checks are `unresolved`. Every open gap and every documented status code without an example is here. | Marking a gap `accepted_risk` annotates it; the state stays Unresolved. |
| **Not assessed** — behaviour not yet assessed | A | It is in the inventory but was not evaluated: the product cannot analyse it, or the user marked it out of scope. Before a project's first run, every behaviour is in this state. | Carries a reason. Out-of-scope behaviours stay in the inventory. |

```
N = P + F + X + U + A            P = P_full + P_partial
```

Each behaviour also reports `checks executed / planned executable checks`, so partial verification is visible on the behaviour itself as well as in the totals.

### 6.5 Behavioural compatibility (BC)

*Where evidence exists and tests ran, how much matched?* Computed over **checks**, grouped so that no endpoint dominates.

```
Groups g:  each legacy endpoint, plus one "project-level" group holding the checks
           of behaviours that belong to no endpoint.

For group g and category c ∈ { replay_offline, integration, contract, property }:

    rate(g, c) = passed(g, c) / ( passed(g, c) + failed(g, c) )        defined only if the denominator > 0

    BC(g) = Σ_c  w_c · rate(g, c)  /  Σ_c  w_c                         over categories where rate(g, c) is defined

    BC    = arithmetic mean of BC(g) over groups where BC(g) is defined

Weights:  replay_offline 0.30 · integration 0.30 · contract 0.20 · property 0.20
```

- `failed` lowers BC.
- `skipped`, `error` and `unresolved` checks, and behaviours that are Unresolved or Not assessed, are **not** in BC — numerator or denominator. They cannot raise it.
- Project-level behaviours are **not** excluded: their checks form one group that counts like an endpoint. If that group has no executed check it is simply undefined and left out of the mean, like any endpoint with none. Project-level behaviours always count in N, the states, the tested share and evidence coverage.
- Rates are per category, so a category with many checks cannot swamp another.
- If no check was executed, BC is `null` and displayed as "—", never as 0 % or 100 %.
- BC is published twice: over all executed checks, and over checks with `evidence_overlap = false` (§5.3 rule 5).
- If any check ended in `error`, the run is flagged `incomplete`.

**Display denominators.** BC is always displayed with **both** of the following:

- *Behaviours tested:* `(P + F) of N`. This fraction is the **tested share**, `TS = (P + F) / N`.
- *Checks executed:* `(passed + failed) of (passed + failed + skipped + error)`, the planned executable checks. The number of `unresolved` checks is shown beside it.

### 6.6 Evidence coverage (EC)

*For how much of the legacy behaviour does evidence establish what to expect?* Computed over **behaviours**, unweighted.

```
EC = ( P + F + X ) / N
```

- Unresolved (U) and Not assessed (A) are in the denominator only. They always lower EC.
- Not executed (X) counts toward EC, because evidence exists; it is exposed instead by the tested share, which excludes it.
- EC does not depend on whether anything passed. It cannot be raised by the adapter, only by importing evidence and reconstructing.
- If N = 0, EC is `null`.

**Display denominator.** EC is always displayed as `(P + F + X) of N`.

### 6.7 Why the two metrics cannot hide unknown or untested behaviour

| Situation | Where it shows |
|---|---|
| Behaviour with no evidence | Counted in U; lowers EC and TS; listed by name with the evidence that would resolve it |
| Behaviour the product cannot assess, or marked out of scope | Counted in A; lowers EC and TS; listed with a reason |
| Evidence exists but the test did not run | Counted in X; lowers TS; lowers "checks executed of planned"; run flagged `incomplete` if any were errors |
| A behaviour passed some checks while others were skipped or errored | Counted as *partially verified*; its own `executed / planned` is shown; "checks executed of planned" is below 100 % |
| Few behaviours tested, all passing | BC is high, but it is displayed with "(P + F) of N" and labelled "low tested share" when TS < 0.5 |
| Evidence imported but not yet reconstructed | Reconstruction shown as stale with the count of unanalysed evidence files; metrics from the old run are labelled, not presented as current; a new run is refused until reconstruction is repeated |
| A new reconstruction has a smaller inventory | The inventory difference lists every behaviour removed and N before and after |
| Someone edits a past run | Not possible: completed runs are immutable |

### 6.8 Presentation rules

The product and the API always present these together; none may be shown alone:

1. **Tested behavioural compatibility:** BC as a percentage, followed by "based on (P + F) of N behaviours" **and** "(passed + failed) of K planned checks executed".
2. **Evidence coverage:** EC as a percentage, followed by "(P + F + X) of N behaviours".
3. **State breakdown:** the five counts P · F · X · U · A, summing to N, with P split into fully and partially verified and X split into skipped and error.
4. **The lists** of Unresolved and Not assessed behaviours, each with its reason, and of partially verified behaviours with the checks that did not run.
5. **Provenance of the numbers:** the evidence version and reconstruction the run used, and the "evidence has changed since this run" label when it applies.

Additional rules: BC is labelled "low tested share" when TS < 0.5; an undefined metric is "—"; the run's `incomplete` flag and the overlap-excluded BC are shown when present; the wording is "tested behavioural compatibility", with the statement that it is not a guarantee of equivalence.

### 6.9 Worked example

Inventory N = 20 behaviours, evidence version 7.

| State | Count | Detail |
|---|---|---|
| Verified (P) | 12 | 11 fully; 1 partially (its replay and contract checks passed, its property check was skipped) |
| Failed (F) | 1 | One offline replay check failed |
| Not executed (X) | 2 | Two behaviours whose only checks are property tests; generated traffic not authorised, so skipped |
| Unresolved (U) | 3 | Two documented status codes without fixtures; one open gap |
| Not assessed (A) | 2 | One endpoint with a non-JSON body; one behaviour marked out of scope |

Checks, all in one endpoint group — offline replay: 3 passed, 1 failed; integration: 4 passed; contract: 5 passed; property: 3 skipped; plus 2 `unresolved` checks.

```
BC = (0.30·0.75 + 0.30·1.00 + 0.20·1.00) / (0.30 + 0.30 + 0.20) = 0.725 / 0.80 = 0.906
TS = (12 + 1) / 20                                              = 0.65
EC = (12 + 1 + 2) / 20                                          = 0.75
Checks executed = (12 + 1) of (12 + 1 + 3 + 0)                  = 13 of 16
```

Displayed as:

- Tested behavioural compatibility **90.6 %** — based on 13 of 20 behaviours; 13 of 16 planned checks executed (2 checks unresolved)
- Evidence coverage **75.0 %** — 15 of 20 behaviours
- 12 verified (11 fully, 1 partially) · 1 failed · 2 not executed (2 skipped, 0 errors) · 3 unresolved · 2 not assessed
- Evidence version 7

## 7. Technical decisions

| Decision | Choice | Reason |
|---|---|---|
| Runtime hosting model | ASGI app mounted in FastAPI | No dynamic listeners; activation is data, so it survives restarts and is testable. |
| Legacy hostname routing | Host-aware ASGI dispatcher in the same process | Preserves the request exactly; no extra component to deploy or misconfigure. |
| Adapter representation | Declarative JSON interpreted by a fixed engine | No generated code is executed; documents are diffable, versioned and statically validated. |
| Workers | Exactly one; enforced at startup and by a database lease with self-fencing | In-memory registry must have a single owner; no extra infrastructure. The lease is designed to prevent overlap under stated assumptions, not an absolute guarantee. |
| Request lifetime | One hard deadline per runtime request, covering body read, upstream call and response | Bounds how long an old process can still be serving; used in the lease timing. |
| Query-parameter credentials | Removed at extraction; only the sanitised query exists afterwards | Lets the query string be preserved without persisting or forwarding the credential. |
| Inventory versioning | Inventories immutable per reconstruction; runs frozen with their evidence version | Historical scores and denominators never change silently. |
| Traffic permissions | Source permissions (`allow_replay_traffic`, `allow_generated_traffic`) and an effect permission (`allow_state_changing_traffic`) that applies to every source; all off by default | Recorded reads can be sent without authorising writes or arbitrary generated input. |
| Operation classification | Explicit effect per route: read-only, state-changing or unknown; unknown treated as state-changing; HTTP method is a floor | A safe method does not prove an operation is safe; read-only must be declared and approved. |
| Outbound HTTP client | `httpx` on an `httpcore` pool with a custom network backend (`httpcore.AsyncNetworkBackend`) inside a custom `httpx` transport | Demonstrated by an executable spike (`spikes/outbound_transport`, 78 tests) to resolve at connect, validate every address, connect by IP and keep hostname-based TLS, using public interfaces only. |
| Serving through a database outage | Bounded by the lease grace period | A process that cannot renew cannot prove it is the only server. |
| Unknown hosts | Rejected with `421`; management API only on allowlisted hosts | A host that is neither a legacy host nor a management host is unexpected. |
| Metrics | Behavioural compatibility over checks; evidence coverage over a fixed behaviour inventory | Keeps "how well it matched" separate from "how much is known". |
| Trace delivery | Best-effort, bounded, counted | Observability must not affect customer traffic. |
| Inbound credentials | Keyed hash | They only need verifying, so they are not stored recoverably. |
| Database | SQLite locally, PostgreSQL in production | No local Postgres/Docker on the dev machine; one model set and migration history. |
| Database access | Synchronous SQLAlchemy off the event loop | Simple and portable; the proxy path does no database I/O. |
| Mapping proposals | Deterministic rules only | Reproducible, explainable, auditable. |
| Verification oracles | Independent of the engine; third-party schema validator | Prevents circular verification. |
| Tenancy | Single tenant, one admin token | Avoids overengineering. |
| Backend host | Render (the only supported backend target) | See `docs/deployment.md`. |
| Frontend host | Netlify | Next.js is supported natively. |

## 8. Repository layout

```
.kiro/                 Kiro specs, steering, agents, hooks
backend/
  pyproject.toml
  alembic.ini, alembic/
  revenant/{api,evidence,reconstruction,transform,adapter,runtime,verification,security,db}/
  tests/{unit,property,integration,security,resilience,architecture,e2e}/
frontend/              Next.js app (Stage 4)
demo/
  gravepay/            legacy OpenAPI spec + historical fixtures
  novapay/             replacement OpenAPI spec + paired recordings + simulator service
  legacy-client/       the original client; never modified after it is written
docs/                  architecture, data model, API contracts, security, testing, operations
spikes/                feasibility spikes; not application code, not imported by backend/
docker-compose.yml     demonstration topology
render.yaml, netlify.toml
```

## 9. Demonstration topology

The legacy client's source contains its original default base URL, `http://api.gravepay.demo:8000`. An environment variable may override it for non-Docker runs, but the Docker demonstration does not use the override.

| Phase | What answers `api.gravepay.demo` | Expected result |
|---|---|---|
| Before | Docker network alias on the **NovaPay simulator** | The unchanged client fails: wrong paths, wrong auth header, wrong body shape. |
| After | Docker network alias on the **API Revenant backend**; `HostDispatcher` maps the hostname to the GravePay project's runtime (§4.2) | The unchanged client succeeds. |

The two phases are Compose profiles, so the only thing that changes between them is network routing. A checksum of the client source is printed in both phases to demonstrate it is identical.

A non-Docker runner (`demo/run_demo.py`) starts the same services on localhost ports and sets the client's base URL through the environment. Host routing itself is still tested without Docker by integration tests that send an explicit `Host` header through the ASGI app.
