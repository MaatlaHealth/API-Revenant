# Testing Strategy

Revision 12 (slices 1 to 6 implemented).

This document covers two different things that must not be confused:

- **Part 1 — Product verification:** what API Revenant runs for a user's project to measure compatibility. Its design is in `architecture.md` §5–§6.
- **Part 2 — The project's own test suite:** how we test API Revenant itself.

Tools: Pytest, pytest-asyncio, Hypothesis, HTTPX `ASGITransport`, a third-party JSON Schema validator (`jsonschema`), Vitest and Playwright for the frontend. Tests are deterministic: fixed Hypothesis seed in CI (`derandomize=True`), injected clock, no real network.

## Part 1 — Product verification layers

| Layer | Category | Network | What is exercised | Oracle |
|---|---|---|---|---|
| A. Offline historical fixture replay | `replay_offline` | None | Transformation engine + compiled adapter | Recorded legacy responses; replacement request schema |
| B. Independent contract and property tests | `contract`, `property` | Contract none; property needs a test target with `allow_generated_traffic` | Legacy-facing outputs; generated requests through the runtime | Legacy contract as JSON Schema; legacy-only invariants |
| C. Live runtime integration | `integration` | A test target with `allow_replay_traffic` | Full ASGI runtime over real HTTP | Recorded legacy responses; legacy-only invariants |

### Layer A — offline historical fixture replay

1. Transform each legacy fixture request; validate the produced upstream request against the replacement specification's request schema with `jsonschema`; compare with the paired replacement request recording if one exists.
2. Transform each paired replacement response recording; diff the result against the legacy fixture's recorded response after declared normalisation.
3. No paired recording → response-side result `unresolved`. No route for the fixture's endpoint → `failed`.

### Layer B — independent contract and property tests

- **Contract:** validate every legacy-shaped response from layers A and C and from property cases against the legacy contract: required fields, structure, types, enums, status code within the evidence-established set, required headers.
- **Property:** Hypothesis generates legacy requests from legacy request schemas and sends them through the runtime to a declared test target. Invariants are legacy-only (echo, schema, pagination, error) and derived from evidence. One invariant is one test. Requires `allow_generated_traffic`; without it → `skipped` with the reason.

### Layer C — live runtime integration

Real HTTP through the mounted runtime to the NovaPay simulator: live replay of historical requests, stateful sequences, error triggers, pagination walks, authentication negatives, upstream timeout and refusal. Requires `allow_replay_traffic`; it sends recorded and scripted requests, never generated ones. With that permission alone it exercises only operations classified read-only; operations classified state-changing or unknown additionally require `allow_state_changing_traffic`.

### Permission matrix

All permissions are disabled by default. The full definition is in `architecture.md` §5.4.

| Traffic | Operation effect | Required |
|---|---|---|
| Offline fixture replay (layer A) | any | No traffic permission; nothing is sent |
| Live recorded / scripted (layer C) | read-only | `allow_replay_traffic` |
| Live recorded / scripted (layer C) | state-changing or unknown | `allow_replay_traffic` and `allow_state_changing_traffic` |
| Hypothesis-generated (layer B) | read-only | `allow_generated_traffic` |
| Hypothesis-generated (layer B) | state-changing or unknown | `allow_generated_traffic` and `allow_state_changing_traffic` |

### Test-target permission tests

| Scenario | Assertion |
|---|---|
| No target declared | Layer A and contract checks run; layer C and property checks `skipped: no_test_target` |
| `allow_replay_traffic = true`, `allow_generated_traffic = false` | Layer C runs; property checks `skipped: generated_traffic_not_authorised`; the target receives only requests that appear in fixtures or scripted sequences (asserted by comparing received requests with the planned set) |
| `allow_generated_traffic = true`, `allow_replay_traffic = false` | Property checks run; layer C `skipped: replay_traffic_not_authorised` |
| Both false with a target URL set | Nothing is sent to the target (request counter zero) |
| Permissions default | Both `false` on creation |
| Permission changed during a run | The run uses the permissions snapshotted at start |
| All three permissions default | `false` on creation |
| Permission matrix, exhaustive | For each of the 8 combinations of the three permissions × traffic source (recorded, generated) × effect (read-only, state-changing, unknown): the request is sent if and only if the matrix in `architecture.md` §5.4 allows it. Asserted on what the target actually received, not on what the plan said |
| `allow_replay_traffic` only | Recorded read-only operations are sent; recorded state-changing and unknown operations are `skipped: state_changing_traffic_not_authorised`; nothing generated is sent |
| `allow_generated_traffic` only | Generated read-only operations are sent; generated state-changing and unknown cases are skipped; nothing recorded is sent |
| `allow_state_changing_traffic` only | Nothing is sent to the target (request counter zero) |
| `allow_replay_traffic` + `allow_state_changing_traffic` | All recorded operations are sent; nothing generated is sent |
| `allow_generated_traffic` + `allow_state_changing_traffic` | Generated state-changing requests are sent; nothing recorded is sent |
| Generated state-changing requests without `allow_state_changing_traffic` | Hypothesis generates `POST` cases; none reaches the target; the property check is `skipped` with `effect: state_changing` |
| Report disclosure | The run summary always contains `state_changing_traffic` with counts by source and effect and the list of unknown operations; a summary without it fails schema validation; when `enabled` is false every state-changing and unknown sent-count is zero |
| Audit | Changing any permission, and declaring or changing an operation's effect, each write an audit record |
| Reset declared, state-changing traffic enabled | Reset is called once before the run and recorded; if it fails, checks that are not read-only are `skipped: target_reset_failed` and read-only checks still run |
| Sandbox headers | Present on every verification request and on no runtime request made for the legacy client |
| Property test (Hypothesis) over arbitrary plans, effects, sources and permission combinations | The target never receives a request whose effective effect is state-changing or unknown unless `allow_state_changing_traffic` was true in the run snapshot, and never receives anything from a source whose permission was false |

### Operation classification tests

The classifier is a pure function `effective_effect(legacy_method, replacement_method, declared_effect)`; it is tested exhaustively and through the planner and dispatcher.

| Scenario | Assertion |
|---|---|
| Truth table | All combinations of legacy method × replacement method (`GET`, `HEAD`, `OPTIONS`, `POST`, `PUT`, `PATCH`, `DELETE`, an unrecognised method) × declared effect (`read_only`, `state_changing`, `unknown`, none): the result equals the definition in `architecture.md` §5.4 |
| Method is a floor | Any unsafe method on either side yields `state_changing` for every declared value, including `read_only` |
| Method alone never yields read-only | `GET` → `GET` with no declaration is `unknown`, not `read_only` |
| **Side-effecting `GET`** | The demo's receipt operation (`GET` on both sides, declared `state_changing`) is blocked with `allow_replay_traffic` alone and sent only with `allow_state_changing_traffic`; the simulator's side-effect counter is unchanged in the first case |
| Side-effecting `GET` left undeclared | Classified `unknown`, therefore blocked without `allow_state_changing_traffic`, and listed under `unknown_operations` in the report |
| **Unknown operations** | A route with safe methods and no approved declaration is `unknown`; a fixture matching no route is `unknown`; both need `allow_state_changing_traffic`; both are reported as unknown, not as state-changing |
| Unknown defaults to state-changing | For every permission decision `unknown` behaves exactly as `state_changing` (property test: swapping one for the other never changes whether a request is sent) |
| **Conflicting declarations: read-only on an unsafe method** | Declaring `read_only` on a `POST` route is rejected with `400`; an Adapter Document containing it fails compilation with `adapter_invalid`; a hand-built document that bypasses both is still classified `state_changing` at send time |
| Conflicting declarations: evidence marker | Declaring `read_only` where either specification carries `x-revenant-effect: state_changing` is rejected |
| Conflicting declarations: two sources disagree | The stricter value is used |
| Stricter is always accepted | Declaring `state_changing` on a `GET` route succeeds |
| Read-only requires a person | The engine never proposes `read_only`; a `read_only` effect without an approval record cannot be compiled into an adapter |
| Declaration is versioned | Changing a declaration does not affect a run pinned to an earlier adapter version |
| **Scripted sequence with one state-changing step** | Without `allow_state_changing_traffic` the entire sequence is blocked; the target receives zero requests, including the sequence's read-only steps |
| Scripted sequence with one unknown step | Blocked in full in the same way |
| Scripted sequence, all steps read-only | Sent with `allow_replay_traffic` alone |
| Classification before execution | The verification-plan endpoint lists every check's source and effect before a run; the plan and the run agree |
| **Dispatcher checks the declared classification** | Plan altered in a test so a state-changing check is marked read-only → the dispatcher recomputes from the pinned adapter version and the request, refuses to send, and records `error: blocked_unauthorised_request`; the target receives nothing |
| Dispatcher uses the snapshot | Permissions changed after the run started do not change what the dispatcher allows |
| Mutation checks | Weakening the classifier (method-only classification; `unknown` treated as read-only; floor removed; sequence checked step by step) each causes at least one test above to fail |

### Demonstration simulator state tests

| Scenario | Assertion |
|---|---|
| Two sandboxes, a write in one | The other sandbox is unchanged |
| Verification writes in its sandbox | The data seen by the legacy-client demonstration is unchanged |
| Reset a sandbox | Its state equals the deterministic seed exactly; other sandboxes are untouched |
| Reset twice, replay the same state-changing sequence | Identical responses both times apart from declared variable fields |
| Unknown sandbox name | Created from the seed on first use |
| Side-effecting `GET` (receipt) | First call records `receipt_issued_at` in that sandbox and increments its side-effect counter; the counter is readable by tests; reset clears it |

### How expected outputs are established

Only from imported, immutable evidence: recorded legacy responses, the legacy contract, and invariants derived deterministically from those two. Anything else is `unresolved`. Normalisation is declared before the run and listed in the report.

### Circularity controls and the tests that enforce them

| Control | Enforced by |
|---|---|
| The adapter never supplies an expectation; no snapshot-update feature exists | Test: verification leaves fixtures, contracts and invariants byte-identical (hash before/after) |
| Oracles do not import `revenant.transform` or `revenant.adapter` and do not read the Adapter Document | Architecture test that walks the import graph of `revenant.verification.oracles` |
| Invariants are stated in legacy terms only | Unit test: no generated invariant references a replacement path or a mapping id |
| Evidence overlap is disclosed | Test: replaying a fixture that is evidence for an exercised mapping sets `evidence_overlap = true`; a held-out fixture does not |
| Normalisation cannot erase a test | Test: a fixture whose compared fields are all normalised yields `unresolved` |
| The simulator is independent | Architecture test: `demo/novapay/simulator` imports nothing from `revenant` |
| The verifier can fail | Mutation tests below |
| Unresolved and unassessed behaviour stays counted | Test: a documented status code without a fixture and an open gap are each an Unresolved behaviour in the inventory; accepting a gap's risk does not change its state; an unsupported endpoint is Not assessed, not omitted |

**Mutation tests of the verifier.** Verification is run against deliberately broken adapters and must report failures in the stated layer:

| Mutation | Must be caught by |
|---|---|
| Wrong field mapping (`customer` → wrong target) | A (schema or diff), C |
| Required response field dropped | B contract |
| Enum mapped to the wrong legacy value | A diff, C |
| Off-by-one cursor | C, B pagination invariant |
| Error mapped to the wrong legacy code | A diff, B error invariant |
| Amount scaled silently | B echo invariant, A diff |
| Outbound auth removed | C only (demonstrates why layer C exists) |

### Evidence version and inventory tests

| Scenario | Assertion |
|---|---|
| Import or remove an evidence file | `evidence_version` increments and the fingerprint changes |
| Evidence imported after a reconstruction | That reconstruction reports `stale`; its inventory rows are unchanged (row hashes equal before and after) |
| Start verification on a stale reconstruction | Refused with `409 stale_reconstruction` |
| New reconstruction after new evidence | A new inventory is created; the old one is untouched; the inventory difference lists added and removed behaviours and both N values |
| New reconstruction with a smaller inventory | Every removed behaviour is listed |
| Completed run, then: import evidence, reconstruct again, change a behaviour's scope, change the formula version | The stored run row, its behaviour-state rows and its check rows are byte-identical before and after; its summary API response is identical apart from the computed `evidence_changed_since_run` label |
| Attempt to update or delete a completed run or its rows through any service function | Rejected |
| Delete an evidence file that a run depended on | File is soft-deleted; the run's evidence remains retrievable; the reconstruction it used is retained |
| Run summary | Returns the evidence version, evidence fingerprint, reconstruction id, inventory size and inventory fingerprint recorded at start |

### Scoring and coverage tests

The definitions are in `architecture.md` §6. Tests:

**Behaviour states**

- Every behaviour in the inventory is assigned exactly one of the five states, and `P + F + X + U + A = N` for arbitrary generated runs (property test).
- A behaviour with one failed check and any number of passed checks is Failed.
- A behaviour whose checks all ended `skipped` or `error` is Not executed, split correctly into skipped and error.
- A behaviour with no check, or only `unresolved` checks, is Unresolved; marking its gap `accepted_risk` leaves it Unresolved.
- A behaviour the product cannot analyse and one marked out of scope are each Not assessed with the right reason. Before any run, all N are Not assessed.
- A behaviour with a passed check and a skipped or errored check is Verified and flagged *partially verified*; with every planned executable check passed it is *fully verified*; `P = P_full + P_partial`.
- The inventory is deterministic for a given evidence set, and its size does not change during a run.
- Marking a behaviour out of scope does not reduce N.
- An inventory is immutable; a behaviour disappears only in a new reconstruction, where the inventory difference reports it.

**Formulas**

- The worked example in §6.9 reproduces exactly: BC 0.906, TS 0.65, EC 0.75, state counts 12 (11 fully, 1 partially) · 1 · 2 · 3 · 2, checks executed 13 of 16.
- Checks of project-level behaviours form one group that enters the BC mean like an endpoint; with no executed check the group is left out; project-level behaviours always count in N, the states, TS and EC.
- Adding Unresolved or Not assessed behaviours never increases BC, EC or TS, and strictly decreases EC and TS (property test).
- Adding a Not executed behaviour never increases BC or TS.
- Adding a failed check never increases BC and never decreases EC.
- EC is unchanged by turning a Failed behaviour into a Verified one (it does not depend on the adapter).
- BC is `null` when nothing executed; EC is `null` when N = 0; neither is ever reported as a number in those cases.
- A category with 1 000 checks does not outweigh another category beyond its weight.
- Any `error` check sets the run's `incomplete` flag.
- The overlap-excluded BC is computed only from checks with `evidence_overlap = false`.
- Stored `weights` and `formula_version` reproduce a historical run's numbers.

**Display denominators**

- The run summary API always returns, together: BC with `behaviours_tested` / `behaviours_total` **and** `checks_executed` / `checks_planned_executable`; EC with its numerator and denominator; the five state counts with the fully/partially split; the Unresolved, Not assessed and partially verified lists. A response missing any of them fails schema validation.
- `checks_planned_executable = passed + failed + skipped + error` and excludes `unresolved` checks, which are reported beside it.
- Frontend component test: the compatibility figure cannot render without both denominators ("of N behaviours" and "of K planned checks executed"), the evidence coverage figure and the five-state breakdown with partial verification shown; "low tested share" appears when TS < 0.5; an undefined metric renders as "—".

## Part 2 — The project's own test suite

### Layers

| Layer | Location | Scope |
|---|---|---|
| Unit | `backend/tests/unit` | Parsers, contract normalisation, each proposer rule, each language operation, validator, planner, scoring, redactor, credential store |
| Property | `backend/tests/property` | Transformation engine semantics, pagination, scoring monotonicity |
| Integration | `backend/tests/integration` | API routes with a real database (SQLite file per test; PostgreSQL in CI when `TEST_DATABASE_URL` is set), runtime through `ASGITransport` against the in-process NovaPay simulator |
| Security | `backend/tests/security` | See `security.md` |
| Resilience | `backend/tests/resilience` | Restart recovery, trace pipeline, database loss, concurrency, worker constraint, instance lease, request lifetime |
| Architecture | `backend/tests/architecture` | Import-graph rules |
| End-to-end | `backend/tests/e2e` | The full workflow on the demonstration data |
| Frontend | `frontend/` | Component tests; one Playwright journey over the real backend |

### Engine property tests (Hypothesis)

These test the engine against the language specification. They are not part of product verification and do not contribute to any compatibility score.

1. `move` copies the value unchanged for arbitrary JSON values and valid paths.
2. Nest-then-flatten with inverse rule sets is the identity.
3. `convert` round-trips where the pair of conversions is lossless, and raises where precision would be lost.
4. `enum_map` is total over declared values and obeys `on_unmapped`.
5. `computed_cursor`: `decode(encode(page)) == page`; page 2 encodes to the documented example.
6. For any dataset size and limit, walking legacy pages yields every record exactly once, in order.
7. Engine safety: for arbitrary JSON within limits the engine terminates, raises no unhandled exception, and emits no value from a scope a rule did not reference.
8. Validator soundness: any randomly generated document accepted by the validator executes without internal errors.
9. No rule set accepted by the validator can read or emit a credential carrier.

### Hostname routing and host security tests

`normalise_host` is tested table-driven and with Hypothesis (arbitrary byte strings never raise; output is either a rejection or a string that re-normalises to itself).

| Area | Scenario | Assertion |
|---|---|---|
| Normalisation | `API.GravePay.Demo`, `api.gravepay.demo.`, `api.gravepay.demo:8000` | All normalise to `api.gravepay.demo` and route to the GravePay runtime |
| Normalisation | Same function used for registration and dispatch | A host accepted at registration always matches at dispatch (property test) |
| Exact matching | `evil.api.gravepay.demo`, `api.gravepay.demo.evil.com`, `xapi.gravepay.demo`, `gravepay.demo` | None match; `421` |
| Exact matching | Wildcard or IP literal offered at registration | Rejected |
| Malformed hosts | Missing `Host`; two `Host` headers; empty value; `a b`; control characters; `user@api.gravepay.demo`; `api.gravepay.demo/path`; `api.gravepay.demo:99999`; `api.gravepay.demo:abc`; `-bad.demo`; `a..b`; underscore; label of 64 characters; name of 254 characters; raw non-ASCII bytes; percent-encoded host | `400`; neither the runtime nor the management API is invoked (asserted with call counters) |
| Malformed target | Absolute-form target naming a different host than `Host` | `400` |
| Unexpected hosts | Well-formed host that is neither registered nor a management host | `421`; `/healthz` and `/readyz` still answer; `/api/v1/*` does not |
| Management isolation | `/api/v1/projects`, `/docs`, `/openapi.json`, `/healthz`, `/readyz`, `/runtime/other-slug/x` requested on a legacy host, with and without a valid admin token | Never served by FastAPI; adapter `404`; FastAPI call counter stays zero |
| Management isolation | Import graph | `RuntimeProxy` holds no reference to the FastAPI application (architecture test) |
| Spoofing | `Host: <management host>` plus `X-Forwarded-Host: api.gravepay.demo`, trust flag off | Dispatched by `Host` only; forwarded header ignored |
| Spoofing | `X-Forwarded-Host`, `Forwarded`, `X-Original-Host`, `X-Host`, `X-Forwarded-Server` each naming a legacy host or a management host | No effect on dispatch |
| Spoofing | Trust flag on, peer **outside** `REVENANT_TRUSTED_PROXIES` | Forwarded header ignored |
| Spoofing | Trust flag on, trusted peer, two `X-Forwarded-Host` values or a malformed value | `400` |
| Spoofing | Trust flag on with empty `REVENANT_TRUSTED_PROXIES` | Application refuses to start |
| Spoofing | Legacy host with a valid admin token but no legacy key; management host with a legacy key but no admin token | `401` in both cases — choosing a host never grants access |
| Spoofing | Request on legacy host A carrying project B's legacy key | `401` |
| Collisions | Register the same host for two projects | Second is rejected with `409` |
| Collisions | Register case, trailing-dot, port and Unicode/A-label variants of an existing host | Each rejected as a duplicate |
| Collisions | Two concurrent registrations of one host | Exactly one succeeds (primary-key constraint) |
| Collisions | Register a management host or `localhost` | Rejected |
| Collisions | `REVENANT_PUBLIC_HOSTS` later changed to include a registered legacy host | Management wins; host absent from the host table; error event; health reports the conflict |
| Collisions | Production start with empty `REVENANT_PUBLIC_HOSTS` | Application refuses to start |
| Preservation | Path with percent-encoding, `//`, repeated and ordered query parameters, duplicate headers | Runtime receives identical bytes and order (asserted by an echoing test adapter) |
| Preservation | Same request by host entry and by path entry | Identical upstream request and legacy response |
| State | Registered host, runtime stopped | `503`, no fall-through |
| State | Host table updated while requests are in flight | No request observes a partially updated table |
| Protocol | WebSocket upgrade on a legacy host | Rejected |

### Authentication tests

| Scenario | Assertion |
|---|---|
| Valid legacy key | Upstream receives the replacement credential; the legacy key value appears nowhere in the upstream URL, headers or body |
| Missing or wrong legacy key | `401`; the simulator records zero calls; outcome `auth_rejected` |
| Rule attempts to read or write a credential carrier | Rejected by the validator |
| Legacy key value placed by the client in a body field that is mapped upstream | Request aborted, outcome `credential_leak_blocked`, zero upstream calls |
| Two active inbound keys; revoke one | The other keeps working; the revoked one fails on the next request |
| Rotate the outbound credential under load | No failed requests; later requests carry the new value; rollback restores the previous one |
| Outbound credential missing or undecryptable at load | Runtime `failed: credential_unavailable`; does not serve |
| Upstream answers `401` | Client receives `502`, outcome `upstream_auth_failed`, health `degraded` |
| Activation with declared inbound auth and no active key | `409 runtime_not_ready` |
| Stored inbound key | Database holds a keyed hash; the plaintext is not present anywhere in the database |
| Captured logs, traces, events, adapter documents, API responses | Contain neither credential (searched by value) |
| Encryption-key rotation with `REVENANT_SECRET_KEY_PREVIOUS` | Outbound values decrypt; inbound keys verify and are upgraded on use |

### Query-parameter credential tests

The adapter under test declares `api_key_query` with parameter `api_key`. Every assertion that something is absent is made by searching for the credential value in raw and percent-encoded form.

| Scenario | Assertion |
|---|---|
| `?page=2&api_key=K&limit=20` | Credential verified; the sanitised query is exactly `page=2&limit=20`; upstream query is produced only from rules over the sanitised query |
| Carrier first, last, or the only parameter | Sanitised query is the remaining segments with no leading, trailing or doubled `&` introduced |
| Other parameters with percent-encoding, `+`, repeated keys, empty values, empty segments | Preserved byte-for-byte and in order |
| Carrier key percent-encoded (`api%5Fkey=K`) | Recognised and removed |
| Carrier present twice | `401`; zero upstream calls |
| Key differs only in case (`API_KEY`) | Not treated as the carrier; request is unauthenticated → `401` |
| Valid request | Value absent from: the upstream URL and query, upstream headers and body, the stored event (`legacy_query`, snapshots, rule trace), captured application logs, the response headers and body |
| Wrong credential (`401`) | The rejected value is absent from logs, the stored event and the response body |
| Request that times out during body upload, or fails in transformation | The stored event and logs contain only the sanitised query |
| Request for an unknown project or a stopped runtime | No query string and no headers are recorded at all |
| Rule that names the carrier parameter | Rejected by the validator |
| Client also repeats the credential value in a mapped body field | Request aborted by the leak check; outcome `credential_leak_blocked`; zero upstream calls |
| Server access log | Disabled; captured server output contains no request target with a query string |
| Upstream error whose body is passed back | Response trace and client response do not contain the value |
| Outbound credential type `api_key_query` | Replacement credential absent from logs, traces and stored events; upstream connection errors are logged with a query-free URL |
| Property test (Hypothesis) over arbitrary query strings containing one carrier | The sanitised query equals the original with exactly that segment removed, and never contains the credential value |

### Trace pipeline tests

| Scenario | Assertion |
|---|---|
| Queue full (tiny queue, blocked writer) | Every proxy request still succeeds with unchanged latency bound; `dropped_queue_full` increments; response header `X-Revenant-Trace: dropped` |
| Persistence fails, then recovers | Batch retried 3 times then dropped; `dropped_persist_failed` equals batch size; a `trace_loss` event is written on recovery |
| Sustained persistence failure under load | Process memory for traces stays within queue size × trace cap + one batch |
| Oversized snapshot | Truncated to the cap and marked `truncated` |
| Counter identities | `generated = accepted + dropped_queue_full` and `accepted = persisted + dropped_persist_failed + dropped_shutdown + queued + in_flight` hold after each scenario |
| Dropped trace | `GET events/{id}` returns `404`; the product's request count equals persisted rows |
| Writer task crashes | Supervisor restarts it; health shows `writer: stopped` then `running` |
| Graceful shutdown with a non-empty queue | Drains within the deadline; remainder counted as `dropped_shutdown` and logged; writer thread and connections closed |
| Shutdown while the database is down | Exits within the deadline; does not hang |
| Health | Reports queue depth, capacity, all counters and writer state; `degraded` when the writer is not running or drops occurred in the last 60 s |

### Worker-constraint and instance-lease tests

Lease tests use an injected monotonic clock and a controllable database clock, so timing is deterministic. Two application instances are built in one test process against one database file; the PostgreSQL CI job repeats the core cases with two real processes.

| Area | Scenario | Assertion |
|---|---|---|
| Configuration | `WEB_CONCURRENCY=2` or `REVENANT_WORKERS=2` | Application refuses to start with `unsupported_multi_worker` |
| Configuration | `WEB_CONCURRENCY=1` or unset | Starts |
| Configuration | `TTL < grace + request lifetime + clock margin` | Application refuses to start |
| Configuration | `REVENANT_LEASE_WAIT_SECONDS < TTL + 10` | Application refuses to start |
| Acquisition | Empty or released lease | Acquired; `epoch` increases by one; process becomes ready |
| Acquisition | Two instances race to acquire | Exactly one succeeds (single-statement compare-and-set); the other is `standby` |
| Acquisition | Live lease held by a process with the same host and parent PID | Second process exits with `unsupported_multi_worker` |
| Renewal | Normal operation | `heartbeat_at` advances each interval; `serve_until` extends |
| Renewal | Renewal affects zero rows (ownership lost) | Process fences immediately; runtime `503`, `X-Revenant-Runtime: superseded`; `/readyz` fails |
| Expiry | Holder stops renewing; clock advanced to just before TTL | Another instance cannot acquire |
| Expiry | Clock advanced past TTL | Another instance acquires; `epoch` increases |
| Fencing | Holder's monotonic clock passes `serve_until` without renewal | Next runtime request gets `503`, `X-Revenant-Runtime: lease_expired`, with no database access on the request path |
| Fencing | Simulated process pause longer than TTL, successor acquired meanwhile | On resume the old process serves **zero** runtime requests |
| Graceful shutdown | `SIGTERM` with in-flight requests and queued traces | Order observed: stop accepting → in-flight complete → traces drained → `released = true`; lease is never released while a request is in flight |
| Graceful shutdown | Successor waiting in standby | Acquires within one retry interval of the release, without waiting for TTL |
| Handover | New instance S starts while holder H is live (different host) | S does not take the lease; it writes `handover_requested_at` and `handover_requested_by`; the observed order on H is: stop accepting (`503 handover`) → in-flight complete → traces drained → `released = true`; H renews throughout the drain; S acquires within one retry interval and becomes ready |
| Handover | In-flight request on H longer than the drain wait | Cancelled at the lifetime deadline; lease released afterwards, never before |
| Handover | H completely hung (no renewals) | S acquires only after TTL |
| Handover | H's event loop hung, renewal thread alive | Renewal thread sets the fenced flag and releases at the handover deadline (L + trace drain + 5 s); S acquires; H serves nothing afterwards |
| Handover | S crashes while waiting, or right after H releases | Lease is free or expires; the next process acquires; H does not resume |
| Handover | Two successors start together | Exactly one acquires; the other exits with `lease_unavailable` without issuing a second handover request |
| Handover | S cannot acquire within `REVENANT_LEASE_WAIT_SECONDS` | S exits non-zero with `lease_unavailable` |
| Handover | Superseded instance | Never requests the lease back; the instances do not oscillate |
| Stale lease | Holder hard-killed without release | Successor acquires after TTL with no manual step |
| Stale lease | `REVENANT_LEASE_FORCE_ACQUIRE=true` in development | Acquires immediately; in production the application refuses to start with this flag |
| Database outage | Outage shorter than grace | Proxy traffic uninterrupted; health `degraded`; `lease.valid_for_seconds` decreases |
| Database outage | Outage longer than grace | Runtime self-fences: `503`, `lease_expired`; adapters remain loaded |
| Database outage | Database returns, lease row unchanged | Renewal succeeds; serving resumes without reloading adapters |
| Database outage | Database returns, another instance acquired meanwhile | Stays not serving as `superseded` |
| Database outage | Start-up with the database down | Not ready; never serves; exits after bounded retry |
| Mutual exclusion (within assumptions) | Randomised schedule of start, stop, kill, detectable pause, database outage and clock advance across two instances, with clock divergence kept inside the margin (Hypothesis stateful test) | At no step are requests in progress on both instances; an upstream call counter tagged by instance never shows a call from the old instance after the new one acquired |
| Limits of the guarantee | Same model with clock-rate divergence set larger than the margin | The test demonstrates that overlap becomes possible, confirming the documented assumption instead of hiding it |
| Limits of the guarantee | Upstream call already transmitted when ownership is lost | The upstream still receives and may process it; the client connection is aborted; the event is recorded as `lease_lost` (documents the absence of an upstream fencing token) |

### Request lifetime and in-flight request tests

All timing uses the injected clock. The simulator can hold a response open and the test client can send a body slowly.

| Scenario | Assertion |
|---|---|
| Slow request-body upload exceeding the lifetime | `408`, outcome `request_timeout`; no upstream call; handler task ended by the deadline |
| Slow body upload that finishes inside the lifetime | Upstream timeout equals the time remaining, not the full configured timeout |
| Upstream holds the response past the lifetime | `504`, outcome `request_deadline_exceeded`; upstream connection closed, not returned to the pool |
| Total lifetime across body read + upstream + response write | Never exceeds L for any generated combination of delays (property test) |
| Lease passes `admit_until` while a request is in progress | The request may finish, and ends no later than `stop_by`; new requests get `503 lease_expired` |
| Request still running at `stop_by` | Cancelled by the watchdog; nothing from the old process is in progress after `stop_by` |
| Request paused during upstream execution (process suspended), resumed after a successor acquired | On resume the request is cancelled; the old process sends no further upstream call and completes no response |
| Ownership lost (renewal changes zero rows) with requests in progress | All cancelled immediately; `503` where no response byte was sent, otherwise connection aborted; upstream connections closed |
| Ownership lost between transformation and the upstream call | Second lease check stops the request; zero upstream calls |
| Successor attempts acquisition while the old process still has requests in progress | Acquisition fails until the lease is released or older than TTL; with `TTL ≥ G + L + M` no old request is still in progress when it first succeeds |
| Graceful drain (handover or `SIGTERM`) | New requests `503 handover` with `Retry-After`; in-flight requests complete normally; lease released only after the last one ends |
| Drain with a request that would exceed the lifetime | Cancelled at its deadline; drain completes within L |
| Cancellation | Cancelled requests release their concurrency slot, close their upstream connection and still offer a trace with the cancellation outcome |
| Watchdog tick late by more than the renew interval | Admission stops until the next successful renewal; requests that have not sent their upstream call are aborted |

### Resilience tests

| Scenario | Assertion |
|---|---|
| Backend restart with a running runtime | Serves immediately on the **same pinned adapter version**; host table restored; a `lifecycle` event records the recovery |
| Restart with a corrupted adapter row | That project is `failed` with `last_error`; other projects recover; process stays up |
| Restart during a verification run | Run becomes `errored: interrupted` |
| Activation atomicity (failure injected between the DB write and registry load) | State `failed`, desired state retained; retry succeeds; no half-loaded adapter serves |
| 200 concurrent requests, mixed routes, jittered upstream latency | Every response matches its own request; traces accounted for by the counter identities |
| Adapter swap under load | In-flight requests complete on v1; later ones use v2; no errors |
| Concurrent start/stop calls | Final state equals the last committed desired state; `generation` strictly increases |
| Database unavailable, within lease grace | Proxy keeps serving; health `degraded`; management API `503 database_unavailable` |
| Database unavailable, beyond lease grace | Proxy self-fences with `503`; adapters stay loaded; resumes on recovery if the lease is still held |
| Database recovery | Writer resumes; `trace_loss` event if anything was dropped; health `ok` |
| Upstream timeout / refusal | `504` / `502`; outcome `upstream_error` |

PostgreSQL-specific behaviour (pool reconnect after a real connection drop, lease contention and handover between real processes) is exercised in CI against a PostgreSQL service container. The CI workflow (`.github/workflows/ci.yml`) runs on Linux and Windows, and runs the unit, integration and credential tests a second time against a PostgreSQL 16 service container, where every test gets an emptied and freshly migrated database. Lease tests between real processes arrive with slice 6. PostgreSQL cannot be run on the current development machine, so PostgreSQL results come only from CI.

### Outbound connection tests

Listed in `security.md` (SSRF rows): address rules, DNS rebinding with a resolver stub, mixed public/private answers, TLS hostname verification and SNI while connected by IP, revalidation of each new connection, redirect refusal and ignored proxy environment variables. They use a stub resolver and a local TLS server with a test certificate authority; no external network is contacted.

**Status.** The spike in `spikes/outbound_transport` passed its complete suite (78 tests) and mutation check on Linux and Windows in CI on 2026-11-24 ([run](https://github.com/MaatlaHealth/API-Revenant/actions/runs/45490838142)). The guard has since been promoted to `revenant.security.outbound`. `backend/tests/security/test_outbound_transport.py` is generated from the spike's test file by `tools/sync_outbound_tests.py`, and CI fails if the two drift apart. `backend/tests/security/test_outbound_rules.py` adds cases for the URL rules, the settings-driven policy, the explicit exceptions and error mapping. `backend/tests/mutation/outbound_mutation_check.py` weakens the application module twenty ways and requires every non-equivalent weakening to be detected.

### Implemented so far (slice 2)

| Area | Tests |
|---|---|
| Migrations | Upgrade creates the tables; models and migrations describe the same schema (autogenerate comparison is empty); downgrade and re-upgrade; foreign keys, check constraints and the one-active-version partial unique index are enforced; failed unit of work rolls back |
| Admin authentication | Ten malformed or wrong token forms rejected with the same envelope; every method of every management route requires the token; an unset token rejects everything; authentication runs before the project lookup; rejected tokens are not logged |
| Error envelope | Not found, wrong method, validation (field named, submitted value never echoed), malformed JSON, unexpected error (message withheld), database unavailable (`503`) |
| CORS | Listed origins allowed without credentials; lookalike origins get no headers; wildcard refused at start-up |
| Single worker | `WEB_CONCURRENCY` or `REVENANT_WORKERS` other than 1, and a multi-process supervisor outside development, refuse start-up |
| Credentials | Inbound keys stored only as keyed hashes (database dump searched for the plaintext and its hex form); multiple active keys; immediate revocation; project scoping; outbound values encrypted, versioned, rotated, rolled back and retired with never more than one active version; master-key rotation including upgrade-on-use; write-only API; audit records without values; no value in captured or rendered logs |
| Redaction | Headers by name, sensitive paths, known secret values in any position including percent-encoded and as dictionary keys; the log processor scrubs registered secrets from any log line |

`backend/tests/mutation/foundation_mutation_check.py` weakens twenty of these controls one at a time (for example: accept any token, store keys in plaintext, log the query string, return internal error messages) and requires each to be detected.

### Implemented so far (slice 3)

| Area | Tests |
|---|---|
| Upload validation | Disallowed types, oversized, empty, binary and non-UTF-8 files rejected with specific errors; filenames reduced to a base name; per-project file limit |
| Safe parsing | YAML that tries to construct Python objects is refused; nesting depth and document size are bounded (including YAML alias expansion); malformed JSON and YAML become a stored report, not a crash |
| OpenAPI 3.x and Swagger 2.0 | Endpoints, methods, parameters, request and response fields with types and required flags, nested and array fields, status codes, error shapes, pagination parameters, security schemes; `allOf` merged; `oneOf` reported; non-JSON bodies recorded as unsupported |
| References | Local `$ref` resolved with the locator pointing at the definition; remote and file references never fetched and reported; cyclic references terminate |
| Provenance | Every fact has a locator that resolves inside the source document; the same fact from two sources has two rows |
| Fixtures | Demonstration files import completely; invalid fixtures rejected one by one with reasons; credential headers and query parameters redacted and absent from every stored column; recordings paired with legacy fixtures; import order does not matter |
| Nothing discarded | Six kinds of unparseable file are stored with a report and listed as missing evidence |
| Missing evidence | The demonstration produces exactly the expected list: one endpoint without a fixture and seven documented status codes without an example |
| Versioning and removal | Version and fingerprint change on import and removal; the fingerprint depends only on the current file set; removed files leave the current set but their content and facts are retained |
| Projects | Slug generation and uniqueness, validation, pagination, workflow status, activity, deletion refused while running; eight unsafe replacement URLs rejected before saving |

`backend/tests/mutation/evidence_mutation_check.py` weakens twenty-seven of these controls one at a time (for example: load YAML unsafely, keep credentials in stored fixtures, silently drop unparseable files, skip the outbound guard on the replacement URL).

### Implemented so far (slice 4)

| Area | Tests |
|---|---|
| Demonstration result | Exact counts on the demonstration evidence (55 mappings, 47 observed and 8 inferred; 11 gaps; 28 invariants; 27 behaviours) and the exact transformation of representative mappings: nested request fields, epoch-to-ISO conversion, array items, enumerations, route with path parameter, authentication, pagination, error rule and envelope |
| Evidence behind proposals | Every mapping records references, rule, confidence and approval flag; every evidence reference of every mapping resolves to an imported file, fixture or fact; no mapping touches a credential carrier; every engine proposal passes the same validator as a human edit |
| Confidence | Name matches are inferred; identity in both specifications and fixture correlation are observed; a correlation must hold in every recording; booleans, nulls and single small numbers do not count; ambiguity yields a gap, not a choice; fixtures that contradict a name match win |
| Pairing | Recordings pair endpoints; similarity gives an inferred pair; below-threshold, tied and contested endpoints are left unpaired and reported |
| Determinism | Two runs give identical output; a second project with the files imported in reverse order gives identical output; the pure engine is order-independent |
| Staleness and inventories | Stale after import, current again when the evidence set is restored; no stale column exists; a completed inventory is unchanged by new evidence; added and removed behaviours and sizes reported; earlier reconstructions return `409 immutable` for every review action |
| Review | Decisions append-only with actor, note and time; edits validated (eight invalid rules, credential headers, legacy route, unknown upstream, leaked credential value not echoed) and stored as superseding human mappings; bulk approval by filter takes observed mappings only and cannot be asked for inferred ones |
| Operation effect | The engine never proposes read-only; unsafe method and specification marker give state-changing; safe methods give unknown; read-only needs a note and is refused on unsafe methods and against a marker; the method floor table; declarations audited |
| Invariants | Stated in legacy terms only (no replacement field name appears in any definition; derivation refuses replacement contracts and gives the same result without them); two fixtures with different values for observed; inferred ones wait for approval; a contradicted pagination invariant is not emitted |
| Inventory | Documented-without-evidence, gaps, unsupported bodies and unreadable legacy files are all counted; each gap is represented by exactly one behaviour; scope changes need a note, are audited and never remove a behaviour |
| Access | All twenty routes require the admin token before the project is looked up; nothing of one project is reachable through another |

`backend/tests/mutation/reconstruction_mutation_check.py` weakens forty-eight of these rules one at a time (for example: propose read-only for safe methods, drop the method floor, bulk-approve inferred mappings, store an edit without validating it, never report staleness, classify name matches as observed, break pairing ties by order).

### Implemented so far (slice 5)

| Area | Tests |
|---|---|
| Replay of the demonstration | Each of the ten recorded GravePay requests, dry-run through the compiled adapter, equals the paired recorded NovaPay request (method, path, query, body); each of the ten recorded NovaPay responses transforms into exactly the recorded GravePay response, errors included |
| Compilation | Refused without a reconstruction, with a stale one, without an approved authentication mapping or base URL; only approved mappings appear in the document and every rule names its mapping; a rejected mapping leaves the next version; earlier versions never change; the same mappings give the same document hash |
| Coverage | A route missing a required replacement request field, a required legacy response field or its pagination mapping is left out and reported with the reason |
| Effects | Compiled from approved declarations only; an unapproved proposal is not a declaration; the method floor and the specification marker apply |
| Credentials | No credential value in the document, the database column or any API response; carriers stripped before rules run; a legacy key that would travel upstream in a body is blocked |
| Engine operations | Every operation, conditions, required and missing sources, nineteen conversions and nineteen refusals, conflicts, limits, case-insensitive headers, traces without values |
| Validator | A valid document and twenty-four broken ones, each rejected for the stated reason; query-parameter carriers; all failures listed |
| Path safety | Path values are percent-encoded as one segment and cannot add a segment or a query string |

Engine property tests (Hypothesis, derandomised): `move` copies any JSON value; nesting then flattening is the identity; time and integer conversions round-trip; scaling is exact or refused; fractions never silently become integers; `enum_map` is total and obeys `on_unmapped`; the cursor decodes to its page; walking legacy pages over a separately written cursor-paginated list yields every record once, in order; arbitrary rule sets terminate with a result or a transformation error; nothing is emitted from a place no rule referenced; no document the validator accepts can read or emit a credential carrier; accepted documents execute without internal errors. These tests found and fixed one defect: a whole float above 2^53 was converted to a different integer.

`backend/tests/mutation/adapter_mutation_check.py` weakens forty-four rules of the engine, validator and compiler one at a time.

### Implemented so far (slice 6)

Tests in this slice that say "real servers" start the NovaPay simulator and the backend on local TCP ports and talk to them over HTTP; the legacy client runs as its own process.

| Area | Tests |
|---|---|
| Demonstration (real servers) | The unchanged client fails against NovaPay directly and succeeds through the runtime; the five recorded requests are stored in order; `demo/run_demo.py` is run as a script; the client file matches its recorded checksum; the simulator reproduces every recording and imports no API Revenant code |
| Host dispatch | Normalisation of twelve well-formed and thirty-eight malformed hosts; three-way dispatch by exact match with the scope passed through untouched; `421` for unknown, suffix, prefix and sibling hosts; probes only on their exact paths; `400` for missing, duplicated and malformed hosts and absolute-form targets; five forwarded-host headers ignored by default; forwarded host used only from a trusted proxy, once, well formed; a property test that any host accepted at registration matches at dispatch |
| Management isolation (real servers) | The management API, its documentation and the probes are not served on a legacy hostname, with or without the admin token; the admin token is not a legacy key and the reverse |
| Credential separation (real servers) | Six kinds of missing or wrong legacy credential get `401` and nothing reaches the replacement; a rejected replacement credential is `502`; keys added and revoked take effect without a restart; the last key of a running runtime cannot be revoked; rotation and rollback reload the adapter; neither credential appears in any stored event |
| Query-parameter credentials | Thirteen extraction cases and a property test (the sanitised query is the original minus exactly one segment); stored queries are sanitised; a duplicated carrier is `401`; other parameters keep their bytes and order; a key that would travel upstream in a body is blocked with `500` |
| Request lifetime (real servers) | A slow upstream ends at the lifetime with `504`; a body that never arrives ends with `408` and nothing is sent; connection failure `502`, upstream timeout `504`; exception text is never stored |
| Lease on the request path (real servers) | Nothing is admitted in standby, handover, superseded or fenced state; the second check stops a request before anything is sent; requests in progress are cancelled at once when ownership is lost; forty concurrent requests keep their own data; the concurrency cap refuses the excess |
| Lease (real database row, injected clock) | One row, created released; only one of two processes acquires, including two that read the same row; a renewal defeats a concurrent take; a live lease is never taken and a stale one is; deadlines run from the last confirmed renewal and are not extended by failed renewals; serving through a database outage until grace ends and resuming afterwards; the request-path checks use no database; pause detection; sibling refusal; handover with renewal continuing during the drain; giving up without taking a live lease; not asking a newer holder; the handover deadline enforced by the renewal pass; force-acquire only when asked; the timing inequality |
| Handover (two live instances) | A second instance on the same database acquires only after the first has stopped accepting and released; the first then serves nothing and is not ready; the second recovers the running runtime by itself |
| Recovery (real servers) | After a restart the runtime serves on its pinned adapter version, not the latest, with no management call; a corrupted adapter row and an unusable credential each leave their runtime failed while the others recover and the process stays up |
| Trace pipeline | Batched persistence; a thousand offers against a stuck writer never wait and memory stays at the queue bound; failures retried three times, then counted and recorded once as a loss event; shutdown drains within its deadline and counts the rest; a stopped writer is reported and restarted; the size cap; retention. Both counter identities are asserted after every scenario |
| Control | Start refused with the specific reason until an adapter, the credential and a key exist, with nothing half-started; stop and start; host registration refuses nine kinds of unacceptable host and six spellings of a taken one; the seed is idempotent and does not take over an ordinary project |

`backend/tests/mutation/runtime_mutation_check.py` weakens fifty-nine rules of the runtime one at a time.

### End-to-end acceptance test

One test drives the whole workflow on the demonstration data: seed → import evidence → reconstruct → approve mappings → compile → set credentials → verify (all three layers) → start runtime → run the legacy client as a subprocess against the runtime → assert its output. A companion test runs the same client against the NovaPay simulator directly and asserts that it **fails**. A checksum assertion proves the client source is identical in both.

### Gates

- `pytest` green; coverage ≥ 85 % for `transform`, `runtime`, `security`, `verification`.
- `ruff`; `mypy --strict` on `revenant.transform`, `revenant.security` and `revenant.verification.oracles`; `mypy` standard elsewhere.
- Frontend: `tsc --noEmit`, `eslint`, `vitest`, production build.
