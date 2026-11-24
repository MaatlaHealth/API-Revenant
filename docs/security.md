# Security Design

## Threat model

| Asset | Threat | Control |
|---|---|---|
| Replacement API credentials | Disclosure through API, logs, traces, exported adapters, frontend bundle | Encrypted at rest; write-only API; `secret_ref` indirection; attached after trace snapshot; redaction; no secrets in `NEXT_PUBLIC_*` |
| Legacy client credentials | Forwarded to the replacement provider; disclosed from storage, traces, logs or a preserved query string | Stored as keyed hashes; carrier (header or query parameter) removed before transformation, tracing and logging; validator and pre-send leak check; access log disabled |
| Runtime consistency | Two processes serving different adapter state | Single-worker start-up check; database lease with self-fencing and a bounded request lifetime (prevents overlap under stated assumptions; see Process model) |
| Management API | Unauthorised use; reached through a legacy hostname or a spoofed host header | Admin token always required; CORS allowlist; three-way host dispatch; management hosts allowlisted; forwarded headers untrusted |
| Internal network / cloud metadata | SSRF through `replacement_base_url`, redirects, DNS rebinding or mixed DNS answers | Outbound guard: resolve at connect, validate every address, connect to the validated address, no redirects, no environment proxies (below) |
| Backend host | Code execution through imported evidence or adapters | No `eval`/`exec`/`subprocess`/template engines; declarative adapters; safe parsers |
| Service availability | Oversized uploads, deeply nested or self-referential specs, slow upstreams | Size, depth and count limits; `$ref` cycle detection; timeouts; bounded queues |
| Data on the replacement test target | Verification creating, changing or deleting data unintentionally | All traffic permissions off by default; explicit read-only / state-changing / unknown classification per operation with unknown treated as state-changing and HTTP method as a floor; state-changing and unknown operations blocked from every source without `allow_state_changing_traffic`; planner and dispatcher both check; disclosure in every report; sandbox headers and reset for isolated state |
| Integrity of results | Fabricated or overstated compatibility; unknown behaviour hidden | Metrics computed from stored results only; fixed behaviour inventory as denominator; unresolved and unassessed behaviour always counted and listed; provenance required for every fact |

Imported specifications, documentation and fixtures are **untrusted data**. Text inside them is never interpreted as instructions and is always escaped when rendered.

## Controls

### Uploads
- Allowed extensions and sniffed content: `.json`, `.yaml`, `.yml`, `.md`, `.txt`. Maximum 5 MB per file (configurable), 50 files per project.
- YAML is parsed with `yaml.safe_load` only. JSON nesting depth is capped at 64.
- OpenAPI `$ref` resolution is **local to the document**. Remote and file references are not fetched; they are recorded in the parse report as unresolved.
- Filenames are stored as metadata only and never used to build filesystem paths. Content is stored in the database.

### Outbound requests (SSRF and DNS rebinding)

`revenant.security.outbound` is the only code path allowed to create outbound connections. It is implemented as a custom network backend for the HTTP client, so the rules below apply to every connection the client opens and cannot be bypassed by a caller.

**URL rules (checked when a URL is configured and again on every request).**
- Scheme must be `http` or `https`; `https` is required when `REVENANT_ENV=production`.
- No credentials in the URL; port must be in the allowed set or explicitly allowlisted.
- The runtime forwards only to the origin configured on the project (or the declared verification target); nothing in a client request can change the destination host.
- Redirects are never followed. A `3xx` from upstream is returned through response mapping like any other status.
- Proxy settings from the environment (`HTTP_PROXY`, `HTTPS_PROXY`, `ALL_PROXY`) are ignored (`trust_env=False`), so the destination cannot be redirected through an unvalidated intermediary.

**Connection rules (applied each time a new connection is opened).**
1. **Resolve immediately before connecting.** The hostname is resolved at connection time, for both IPv4 (A) and IPv6 (AAAA). A result from configuration time is never reused.
2. **Validate every resolved address.** Each address in the answer is checked. Rejected: loopback, private (RFC 1918 and unique-local IPv6), link-local (including `169.254.169.254` and `fe80::/10`), CGNAT (`100.64.0.0/10`), multicast, unspecified, reserved and documentation ranges, and IPv4-mapped, IPv4-compatible and NAT64 (`64:ff9b::/96`) forms of any of those, after unwrapping.
3. **Mixed answers are refused.** If any address in the answer is disallowed the connection is refused outright; the guard does not pick the "good" address from a mixed public/private answer.
4. **Connect to a validated address, with no second resolution.** The socket is opened to one of the validated IP addresses directly. The hostname is not handed to the operating system or the HTTP library to resolve again, which is what closes the DNS-rebinding window. After connecting, the peer address of the socket is compared with the address that was validated.
5. **TLS still uses the hostname.** The TLS handshake sends the original hostname as SNI and verifies the certificate against that hostname with the system trust store. Connecting by IP does not weaken certificate verification, and the `Host` header is the original hostname.
6. **Every new connection is revalidated.** Steps 1–5 run for each new connection. A pooled keep-alive connection is reused only for the same origin and is already bound to a validated peer; idle connections are closed after 60 s and connections after a bounded lifetime, so a change in DNS or in the allowlist takes effect promptly.
7. A URL whose host is an IP literal goes through the same validation.

**Explicit exceptions.** `REVENANT_UPSTREAM_ALLOWLIST` (comma-separated hosts) permits named hosts whose addresses would otherwise be rejected. `REVENANT_ALLOW_PRIVATE_UPSTREAMS=true` disables the private-address rejection for the local demonstration only; the application refuses to start with this flag when `REVENANT_ENV=production`. Neither exception disables resolution-at-connect, redirect refusal or TLS verification.

**Implementation and feasibility.** The client is `httpx` (async) on an `httpcore` connection pool. The connection rules are implemented as an `httpcore.AsyncNetworkBackend`, which `httpcore` calls once for every new connection, inside a custom `httpx` transport; only public interfaces are used. `httpcore` performs the TLS handshake itself with the origin hostname, so connecting by IP does not change SNI or certificate verification, and the transport removes the `sni_hostname` request extension so callers cannot override it. An executable spike (`spikes/outbound_transport`) demonstrates all of the rules above with 78 passing tests, including a local TLS server whose certificate names the hostname and contains no IP address. It has been run on Windows only so far. A CI workflow runs the full suite and its mutation check on Linux with pinned dependencies; the transport is not accepted as an application dependency until that run has passed, and it has not yet been executed.

### Credentials

Three credential domains exist and are kept separate. Full behaviour is in `architecture.md` §4.7.

| Domain | What it protects | Storage | Presented by |
|---|---|---|---|
| Admin token | Management API | Environment variable only | Operator's browser |
| Legacy-client (inbound) keys | The compatibility runtime | Keyed hash (HMAC-SHA-256, key derived from `REVENANT_SECRET_KEY` with HKDF); plaintext is not stored and cannot be recovered | Legacy client |
| Replacement-provider (outbound) credential | The replacement API | Fernet ciphertext, versioned | API Revenant only |

- **Legacy credentials are never forwarded.** The inbound carrier is removed from the request scopes before anything else is done with the request; the validator rejects rules that touch credential carriers; the upstream request starts from an empty header set; and a final check aborts the request if the presented legacy key value appears anywhere in the upstream URL, headers or body, in raw or percent-decoded form.
- **Query-parameter credentials.** When the inbound carrier is a query parameter it is extracted by an exact procedure (`architecture.md` §4.7): split on `&`, match the decoded key exactly, reject duplicates with `401`, and rebuild a *sanitised query* from the remaining segments, byte-identical and in order. Only the sanitised query exists afterwards. The credential therefore cannot appear in upstream URLs, request or response traces, application logs, error responses or any persisted data. The unsanitised query string is never stored or logged. Requests that fail before an adapter is resolved have their query string and headers left out of all records, because the carrier is not yet known.
- **Outbound query credentials.** If the replacement credential is sent as a query parameter, upstream URLs are logged and traced without their query string, and HTTP client exceptions are recorded by type with a query-free URL, never by raw message.
- **Lookup.** Credentials are resolved when an adapter is loaded and held in memory in an object that is never serialised and has a redacted representation. The request path does not read credentials from the database.
- **Rotation.** Inbound: several keys may be active at once; add the new key, migrate clients, revoke the old one. Outbound: a new version becomes active atomically and the running adapter reloads; the previous version is retained for rollback until retired. Master key: supply the old key as `REVENANT_SECRET_KEY_PREVIOUS`; a maintenance command re-encrypts outbound values and lists inbound keys that still need re-registration.
- **Failure behaviour.** Wrong or missing legacy key → `401`, no upstream call, no indication of whether keys exist. No active inbound key or no usable outbound credential → the runtime does not start or is marked `failed`. Upstream rejects the outbound credential → the client receives `502`, never `401`.
- **Write-only API.** Credential values are accepted and never returned. An inbound key generated by the server is shown exactly once, in the creation response.
- **Unauthenticated runtimes** require an approved mapping and an explicit project setting, and are flagged in the UI.
- **Demonstration credentials** (`legacy-demo-key`, `replacement-demo-token`) are fictional, supplied through environment variables documented in `.env.example`, and work only against the bundled simulator.
- **Production start-up fails** if `REVENANT_SECRET_KEY` or `REVENANT_ADMIN_TOKEN` is missing or is a documented example value.

### Logging and traces
- Structured JSON logs (`structlog`), one event per request with a request id.
- The HTTP server's built-in access log is disabled (`--no-access-log`) because it prints full request targets including query strings. The application's own request log records method, path without query, status, duration and request id; a runtime request's sanitised query is added only after credential extraction.
- Exception messages are never logged raw; errors are logged by type with fields that have passed the redactor.
- **Credentials cannot reach a trace by construction:** the inbound carrier is deleted before any snapshot is taken, and the upstream request snapshot is taken before the outbound credential is attached. Traces record only that authentication was verified and attached, the inbound key's label, and the outbound credential version.
- A single redactor is then applied to log events and trace snapshots as a second layer: `Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`, `X-API-Key`, any carrier named by an adapter auth block, any body path listed in project `sensitive_paths`, and any value equal to a loaded outbound credential or to the legacy key presented on that request.
- Rule traces record paths and outcomes, not values.
- The trace pipeline is bounded and best-effort (`architecture.md` §4.9); trace failures cannot block requests or exhaust memory, and health never reports a dropped trace as stored.

### Hostname routing

Full rules: `architecture.md` §4.2.

- **Strict normalisation.** Exactly one `Host` header; origin-form request target; `hostname[:port]` grammar with a restricted character set, label and length limits; ASCII only. Normalisation is lowercase, strip port, strip one trailing dot — nothing else. One function serves both registration and dispatch.
- **Exact matching.** Full-string equality against registered hosts. No wildcard, suffix, subdomain, prefix or default-host matching.
- **Malformed hosts are rejected** with `400` and dispatched nowhere.
- **Unexpected hosts are rejected** with `421`. The management API is served only on hosts listed in `REVENANT_PUBLIC_HOSTS`; production refuses to start if that list is empty. Liveness and readiness probes are the only exception.
- **Forwarded-host headers are never trusted by default.** `X-Forwarded-Host` is honoured only with `REVENANT_TRUST_FORWARDED_HOST=true`, a non-empty `REVENANT_TRUSTED_PROXIES`, a directly connected peer inside those ranges, and a single well-formed value. `Forwarded`, `X-Original-Host`, `X-Host` and `X-Forwarded-Server` are never used.
- **Management endpoints are not exposed on legacy hostnames.** The runtime proxy has no reference to the management application; every path on a legacy host is a legacy path.
- **Collisions are impossible by constraint.** Legacy hosts are unique on their normalised form and cannot be management hosts, `localhost`, IP literals or wildcards. If configuration later creates an overlap, the management host wins and the conflict is reported.
- **Host routing is not an authentication boundary.** The `Host` header is client-controlled. The runtime always requires a legacy-client key and the management API always requires the admin token, whatever host was sent.

### Process model
- Exactly one worker and one instance. The application refuses to start under a multi-worker configuration.
- A single-row database lease with self-fencing is **designed to prevent** two processes serving runtime traffic at once (`architecture.md` §4.5). It is not an unconditional guarantee. It relies on stated assumptions: process and database clock rates diverge by less than the configured margin; pauses are detectable by the clock in use; the database applies lease updates atomically and does not lose them; and all processes run the same code and settings.
- Every runtime request has a hard total lifetime (default 30 s) covering body read, upstream call and response. The lease time-to-live must be at least grace + request lifetime + clock margin, so an old process has ended every request before a successor may acquire.
- There is **no upstream fencing token**: a request already transmitted to the replacement cannot be recalled if the lease is lost a moment later. The lease is re-checked immediately before each upstream call, which narrows but does not remove that window.
- No additional coordination service is used.

### Verification traffic

Verification can send requests to a replacement test target. That is a capability to cause effects on someone else's system, so it is denied by default and granted in separate steps (`architecture.md` §5.4).

- **Three permissions, all disabled by default.** `allow_replay_traffic` and `allow_generated_traffic` each authorise a traffic *source*. `allow_state_changing_traffic` authorises a kind of *operation* and applies to every source; on its own it sends nothing.
- **Permission matrix.** Recorded read-only: replay. Recorded state-changing: replay and state-changing. Generated read-only: generated. Generated state-changing: generated and state-changing. Offline fixture replay: none, and it sends nothing.
- **Explicit operation classification.** Every operation is `read_only`, `state_changing` or `unknown`. `unknown` is treated as state-changing. An operation is read-only only if a person has declared and approved it; a safe HTTP method alone makes it `unknown`.
- **HTTP method is a minimum constraint.** A request with an unsafe method on either side is state-changing whatever is declared. A read-only declaration on such a route is rejected when made and when the adapter is compiled.
- **Side-effecting `GET`s** are covered by declaring the operation state-changing, or simply by leaving it undeclared.
- **Sequences are blocked as a whole** if any step is state-changing or unknown and the permission is absent.
- **Checked twice.** The planner never schedules an unauthorised request. The dispatcher re-derives the decision from the pinned adapter version, the request's actual methods, the traffic source and the permissions snapshotted at run start, immediately before each send.
- **Disclosed and audited.** Every run report states whether state-changing traffic was enabled, what was sent by source and effect, what was blocked, and which operations are unknown. Permission changes and effect declarations are audit records.
- **Isolated, resettable state** is supported through optional sandbox headers and a reset request on the target, applied to verification traffic only.
- Verification traffic goes through the same outbound guard as all other outbound connections.

### Management API
- `Authorization: Bearer <REVENANT_ADMIN_TOKEN>`, compared in constant time.
- CORS: explicit origin allowlist from `REVENANT_CORS_ORIGINS`; no wildcard with credentials.
- The frontend asks the operator for the token and keeps it in `sessionStorage`; it is not built into the bundle.
- Pydantic validation on every body, with `extra='forbid'`.

### Runtime
- Inbound limits: body size, header count and size, path length.
- Legacy credential verified in constant time when the adapter declares inbound auth.
- Timeouts: connect 5 s, read 15 s, total 30 s (configurable per project within bounds).
- Per-project concurrency cap with `503` and `Retry-After` when exceeded.

### Adapter safety
- Adapter Documents are data, validated on compile and on every load (see `transformation-language.md` §8).
- The engine has no access to the filesystem, environment, network or clock other than what the runtime passes in.
- Nothing generated by API Revenant is ever passed to a shell or interpreter.

## Security tests (Stage 2 onward)

| Area | Test |
|---|---|
| SSRF — address rules | Table-driven rejection of loopback, private, link-local, metadata, CGNAT, multicast, reserved, IPv6 unique-local and link-local, IPv4-mapped, IPv4-compatible and NAT64 forms, decimal/octal/hex IP encodings, IP-literal hosts, URLs with credentials |
| SSRF — DNS rebinding | Resolver stub returns a public address at configuration time and a private one at connection time → connection refused; resolver stub returns public first and private on a second call → the resolver is called exactly once per connection and the connection goes to the first, validated address |
| SSRF — mixed answers | Answer containing one public and one private address (A only, AAAA only, and A public + AAAA private) → refused; all-public answer → connects to an address from that answer; peer address equals a validated address |
| SSRF — TLS | Local TLS server with a test CA: certificate for the hostname verifies while the socket is connected by IP; certificate for a different name fails; the server observes the original hostname as SNI; `Host` header is the hostname |
| SSRF — connection reuse | A new connection after pool expiry is resolved and validated again; a changed DNS answer pointing to a private address is refused on the next new connection |
| SSRF — other | Redirect not followed; `HTTP_PROXY`/`HTTPS_PROXY` in the environment ignored; allowlist exceptions do not disable resolve-at-connect or TLS verification |
| Uploads | Oversize, wrong type, YAML with tags/anchors bombs, deep nesting, cyclic `$ref`, remote `$ref` not fetched |
| Credentials | API never returns values; inbound keys stored only as keyed hashes; outbound ciphertext differs from plaintext; both credential values absent from logs, traces, events and adapter documents (asserted by searching captured output); legacy key never present in any upstream request; leak check aborts a request that would forward it; rotation and rollback; query-parameter credential absent from upstream URLs, traces, logs, error bodies and stored events while other parameters are preserved byte-for-byte |
| Auth | Missing/incorrect admin token → 401; runtime rejects a wrong legacy key with no upstream call; upstream `401` surfaces as `502` |
| Host routing | Strict normalisation table and fuzzing; exact matching (no suffix or subdomain match); malformed hosts `400`; unexpected hosts `421`; management endpoints never served on a legacy hostname; forwarded-host spoofing from untrusted and trusted peers; host collisions including case, trailing-dot, port and A-label variants and concurrent registration |
| Trace pipeline | Bounded memory under sustained persistence failure; drops counted; no dropped trace reported as stored |
| Process model | Start-up refused with more than one worker; lease acquisition, renewal, expiry, graceful release, handover, stale recovery and database-outage fencing; request lifetime bound and in-flight behaviour at lease expiry and ownership loss; randomised mutual-exclusion test across two instances under the modelled assumptions; a test showing overlap becomes possible when clock divergence exceeds the margin |
| Result integrity | Five behaviour states sum to the inventory size; unresolved and unassessed behaviour cannot be removed from the denominator; metrics never returned without denominators |
| Verification traffic | Exhaustive permission matrix (three permissions × two live sources × three effects) asserted on what the target received; nothing sent with all permissions off or with only the state-changing permission; side-effecting `GET` blocked when declared state-changing or left unknown; read-only declaration on an unsafe method rejected at declaration, at compilation and at send time; whole scripted sequence blocked if any step is state-changing or unknown; dispatcher refuses an unauthorised request even when the plan is wrong; generated state-changing requests blocked without the permission; every report discloses state-changing traffic; reset and sandbox headers applied to verification traffic only |
| Adapter | Unknown `op`, extra fields, bad paths, collisions, missing `mapping_id` all rejected; path-parameter injection (`../`, `?`, `#`) is encoded, not interpreted |
| Config | Production refuses to start with example secrets or `ALLOW_PRIVATE_UPSTREAMS` |

## Known limitations (accepted for the first release)

- Single shared admin token; no per-user accounts or roles.
- No rate limiting on the management API beyond the host platform's.
- Evidence content is stored unencrypted in the database; do not import evidence containing live credentials. Fixture import redacts known credential headers.
- Infrastructure in front of the backend (platform routers, load balancers, proxies) may log full request URLs, including a query-parameter credential. The application cannot prevent this; header carriers are recommended.
- The instance lease prevents overlapping serving only under its stated assumptions, and there is no upstream fencing token.
- Credentials embedded in a URL *path* are not a supported carrier and are not stripped.
- A read-only classification is a human declaration. The product enforces that it cannot be applied to an unsafe HTTP method and records who made it, but it cannot prove an operation has no side effects. Request volume and cost on a real sandbox are the operator's responsibility.
- The outbound transport has been demonstrated by a spike on Windows only; it must be run on Linux in CI before it is relied on in production. It depends on `httpcore`'s network-backend interface, so the dependency range is pinned and the spike tests stay in the suite.
