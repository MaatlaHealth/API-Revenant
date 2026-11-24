# Requirements — API Revenant

## Introduction

API Revenant reconstructs the observable behaviour of a discontinued or breaking API from historical evidence, generates a declarative compatibility adapter for a replacement service, executes that adapter as a proxy runtime, and verifies its behaviour against the evidence. It distinguishes observed facts from inferences and reports behaviour that cannot be established.

Out of scope for this release: LLM-assisted proposals, multi-tenancy and user accounts, non-JSON payloads, streaming and WebSocket APIs, Postman collection import, and automatic translation of arbitrary pagination schemes.

## Requirement 1 — Projects

**User story:** As an engineer, I want to create a resurrection project for one legacy/replacement API pair, so that all evidence, mappings and results are kept together.

### Acceptance criteria

1. WHEN a user submits a project name, legacy provider name and replacement provider name THE SYSTEM SHALL create a project with a unique URL-safe slug.
2. WHEN a submitted slug already exists THE SYSTEM SHALL reject the request with a conflict error naming the field.
3. WHEN a user opens a project THE SYSTEM SHALL show which workflow steps are complete and what blocks the next step.
4. WHEN a user sets a replacement base URL THE SYSTEM SHALL validate it with the outbound guard before saving.
5. WHEN a user deletes a project whose runtime is running THE SYSTEM SHALL refuse and explain that the runtime must be stopped first.

## Requirement 2 — Evidence Vault

**User story:** As an engineer, I want to import specifications and historical fixtures, so that reconstruction is based on recorded evidence.

### Acceptance criteria

1. WHEN a user uploads an OpenAPI 3.x or Swagger 2.0 document in JSON or YAML THE SYSTEM SHALL parse endpoints, methods, parameters, headers, request and response schemas, status codes and security schemes.
2. WHEN a user uploads a fixture file in the documented fixture format THE SYSTEM SHALL store each request/response pair and associate it with the matching endpoint where one exists.
3. WHEN a fixture matches no known endpoint THE SYSTEM SHALL keep it and create an endpoint whose only basis is that fixture.
4. WHEN an uploaded file exceeds the size limit or is not an allowed type THE SYSTEM SHALL reject it with a specific error.
5. WHEN an uploaded file is an allowed type but cannot be parsed THE SYSTEM SHALL store it with status `failed` and a report listing each problem, and SHALL NOT discard it silently.
6. WHEN a fact is extracted THE SYSTEM SHALL record the evidence file and the location within it that supports the fact.
7. WHEN a fixture contains credential headers THE SYSTEM SHALL redact their values at import.
8. WHEN a user views the vault THE SYSTEM SHALL list evidence that is missing, including endpoints without fixtures and documented status codes without examples.
9. WHEN a specification contains a remote or file `$ref` THE SYSTEM SHALL NOT fetch it and SHALL report it as unresolved.
10. WHEN an evidence file is imported or removed THE SYSTEM SHALL increment the project's evidence version, and WHEN a file is removed THE SYSTEM SHALL take it out of the current evidence set while retaining it for the historical reconstructions and runs that used it.

## Requirement 3 — Behavioural reconstruction

**User story:** As an engineer, I want the differences between the two contracts analysed and mappings proposed, so that I can review a concrete compatibility plan.

### Acceptance criteria

1. WHEN a user runs reconstruction THE SYSTEM SHALL compare paths, methods, headers, authentication, query parameters, request and response fields, nesting, types, required/optional status, defaults, status codes, error formats and pagination.
2. WHEN a mapping is proposed THE SYSTEM SHALL record the legacy reference, the replacement reference, the transformation, the supporting evidence, the proposing rule, a confidence classification and whether approval is required.
3. WHEN a mapping is supported directly by imported evidence THE SYSTEM SHALL classify it `observed`.
4. WHEN a mapping is produced by a heuristic without direct confirmation THE SYSTEM SHALL classify it `inferred` and require human approval.
5. WHEN evidence is insufficient to establish a behaviour THE SYSTEM SHALL record a gap and SHALL NOT propose a mapping for it.
6. WHEN reconstruction is run twice on the same evidence THE SYSTEM SHALL produce the same proposals.
7. WHEN an evidence file is imported or removed after a reconstruction THE SYSTEM SHALL report that reconstruction as stale, determined by comparing its recorded evidence fingerprint with the project's current one.
8. THE SYSTEM SHALL NOT change the behaviour inventory of a completed reconstruction; evidence imported afterwards SHALL enter an inventory only through a new reconstruction.
9. WHEN a new reconstruction replaces a previous one THE SYSTEM SHALL report the behaviours added and removed and the inventory size before and after.

## Requirement 4 — Mapping review

**User story:** As an engineer, I want to approve, reject or correct each proposed mapping, so that only reviewed transformations are executed.

### Acceptance criteria

1. WHEN a user approves or rejects a mapping THE SYSTEM SHALL record the decision, actor, note and time in an append-only record.
2. WHEN a user edits a mapping THE SYSTEM SHALL validate the edited rule and store it as a new human-originated mapping that supersedes the proposal.
3. WHEN a user bulk-approves by filter THE SYSTEM SHALL include only `observed` mappings.
4. WHEN a mapping is not approved THE SYSTEM SHALL exclude it from every compiled adapter.
5. WHEN an operation is declared read-only THE SYSTEM SHALL require an explicit human decision recorded with actor and note, and SHALL reject the declaration when either the legacy or the replacement method of the route is not `GET`, `HEAD` or `OPTIONS`, or when evidence explicitly marks the operation as state-changing.

## Requirement 5 — Adapter generation

**User story:** As an engineer, I want an executable adapter generated from approved mappings, so that legacy requests can be served by the replacement API.

### Acceptance criteria

1. WHEN a user compiles an adapter THE SYSTEM SHALL produce an immutable, versioned Adapter Document in the Revenant Transformation Language.
2. WHEN a required replacement request field is not produced by any approved rule THE SYSTEM SHALL fail compilation for that route and list the missing fields.
3. WHEN an Adapter Document fails static validation THE SYSTEM SHALL reject it and list every failure.
4. THE SYSTEM SHALL NOT execute generated source code, shell commands or expressions as part of an adapter.
5. WHEN a user requests a dry run THE SYSTEM SHALL return the transformed upstream request and rule trace without sending it.
6. WHEN an adapter references a replacement-provider credential THE SYSTEM SHALL store only a reference, never the value, and SHALL NOT reference any individual legacy-client key.
7. WHEN an adapter is compiled THE SYSTEM SHALL record each route's declared effect — read-only, state-changing or unknown — in the Adapter Document, and SHALL reject a document in which a route declared read-only uses a state-changing HTTP method on either side.

## Requirement 6 — Transformations

**User story:** As an engineer, I want the adapter to translate requests and responses deterministically, so that behaviour is predictable and testable.

### Acceptance criteria

1. WHEN a legacy request arrives THE SYSTEM SHALL apply approved rules for path, query, headers and body, supporting rename, nesting, flattening, constants, defaults, type conversion, enum mapping and array iteration.
2. WHEN the adapter declares authentication translation THE SYSTEM SHALL verify the legacy-client credential against the project's active inbound keys and, separately, attach the active replacement-provider credential from encrypted storage.
3. WHEN a legacy paginated request arrives on a route using `computed_cursor` THE SYSTEM SHALL compute the replacement cursor from the page number and convert the response envelope to the legacy shape.
4. WHEN a route uses `cursor_cache` and the requested page cannot be reached within the hop limit THE SYSTEM SHALL return a legacy-shaped error instead of guessing.
5. WHEN the replacement returns an error matching an approved error rule THE SYSTEM SHALL return the mapped legacy status and error body.
6. WHEN the replacement returns an error matching no rule THE SYSTEM SHALL preserve the status, flag the response as unmapped and SHALL NOT invent a specific legacy error code.
7. WHEN a conversion would lose precision or an enum value is unmapped with `on_unmapped: error` THE SYSTEM SHALL fail the transformation explicitly.
8. WHEN source fields are not consumed by any rule in build mode THE SYSTEM SHALL list them in the trace.
9. THE SYSTEM SHALL NOT forward a legacy-client credential to the replacement API, and SHALL abort a request before sending if the presented legacy credential value occurs anywhere in the upstream request.
10. WHEN a legacy-client credential is missing or invalid THE SYSTEM SHALL respond `401` without calling the replacement API and without revealing whether any key exists.
11. WHEN the replacement API rejects the replacement-provider credential THE SYSTEM SHALL respond `502`, not `401`, and record the outcome as an upstream authentication failure.
12. WHEN a rule reads or writes a credential carrier THE SYSTEM SHALL reject the Adapter Document at validation.
13. WHEN the legacy-client credential is carried in a query parameter THE SYSTEM SHALL extract it before any other processing of the request, remove that parameter from the query string, and preserve every other parameter byte-for-byte and in its original order.
14. WHEN the legacy-client credential parameter appears more than once THE SYSTEM SHALL respond `401` without calling the replacement API.
15. THE SYSTEM SHALL NOT include a legacy-client credential, however it was carried, in any upstream URL or query string, request or response trace, application log, error response or persisted record, and SHALL NOT store or log the unsanitised query string.
16. WHEN a runtime request fails before an adapter is resolved THE SYSTEM SHALL NOT record its query string or headers.
17. WHEN the replacement-provider credential is carried in a query parameter THE SYSTEM SHALL record and log upstream URLs without their query string.
18. THE SYSTEM SHALL disable the HTTP server's built-in access log and SHALL log request paths without query strings.

## Requirement 7 — Compatibility runtime

**User story:** As an operator, I want to run the adapter as a proxy, so that existing clients keep working without code changes.

### Acceptance criteria

1. THE SYSTEM SHALL serve the runtime as an ASGI application mounted inside the backend and SHALL NOT open new listening sockets in response to API requests.
2. WHEN a user starts a runtime THE SYSTEM SHALL persist the desired state and pinned adapter version atomically and begin serving the legacy interface.
3. WHEN a user stops a runtime THE SYSTEM SHALL stop serving and respond `503` to subsequent runtime requests.
4. WHEN the backend restarts THE SYSTEM SHALL restore every runtime whose desired state is running, on its pinned adapter version, without any frontend interaction.
5. WHEN recovery of one runtime fails THE SYSTEM SHALL mark it failed with the reason and continue recovering the others.
6. WHEN a request arrives whose `Host` header is a registered legacy host THE SYSTEM SHALL dispatch it inside the backend to that project's runtime, delivering the original method, raw path, raw query string, headers and body unchanged.
7. WHEN a request is processed THE SYSTEM SHALL offer a redacted transformation trace with outcome, statuses and timings to a bounded trace queue without waiting.
8. WHEN the runtime health endpoint is called THE SYSTEM SHALL report adapter load state, version, database state, lease state, last upstream outcome, and trace writer state, queue depth and dropped-trace counters, all computed at call time.
9. WHEN requests are handled concurrently THE SYSTEM SHALL keep each request's data isolated and SHALL complete in-flight requests on the adapter version they started with.
10. WHEN the database is unavailable THE SYSTEM SHALL keep serving loaded runtimes for as long as its instance lease remains valid, report degraded health, stop serving runtime traffic once the lease grace period has elapsed without a confirmed renewal, and resume serving and trace persistence when the database returns and the lease is still held.
11. WHEN the replacement times out or refuses the connection THE SYSTEM SHALL return `504` or `502` respectively and record the failure.
12. WHEN a request arrives on a registered legacy host THE SYSTEM SHALL NOT serve the management API or documentation on that host, and SHALL respond `503` if that project's runtime is not running.
13. WHEN a legacy host is registered THE SYSTEM SHALL reject hosts that are malformed, already registered to another project, or the backend's own hostnames.
14. WHEN the trace queue is full or persistence fails THE SYSTEM SHALL drop the trace, increment the corresponding counter, and SHALL NOT delay, fail or retry the proxy request.
15. THE SYSTEM SHALL bound trace memory by a fixed queue size and a per-trace size cap.
16. THE SYSTEM SHALL NOT report a trace as stored unless it has been persisted, and SHALL record a trace-loss event when persistence resumes after drops.
17. WHEN the backend shuts down THE SYSTEM SHALL drain the trace queue within a fixed deadline, count and log any remainder as dropped, and stop the writer.
18. WHEN the backend is started with a configuration of more than one worker THE SYSTEM SHALL refuse to start with an explicit error.
19. THE SYSTEM SHALL be designed so that, under its documented assumptions, at most one process serves runtime traffic at a time, using a lease held in its own database and no additional infrastructure.
20. WHEN a request arrives THE SYSTEM SHALL normalise its host strictly (exactly one `Host` header, origin-form target, restricted hostname grammar, ASCII only; lowercase, port and one trailing dot removed) and SHALL reject a malformed host with `400` without dispatching it to the runtime or the management API.
21. THE SYSTEM SHALL match hosts by exact equality with a registered legacy host after normalisation, and SHALL NOT use wildcard, suffix, subdomain, prefix or default-host matching.
22. WHEN a well-formed host is neither a registered legacy host nor a configured management host THE SYSTEM SHALL respond `421`, except for the liveness and readiness endpoints.
23. THE SYSTEM SHALL NOT use any forwarded-host header for dispatch unless forwarding trust is explicitly enabled and the directly connected peer is within the configured trusted proxy ranges.
24. THE SYSTEM SHALL require the legacy-client credential on the runtime and the admin token on the management API regardless of the host presented.
25. WHEN a legacy host is registered THE SYSTEM SHALL normalise it with the same function used for dispatch and SHALL reject wildcards, IP literals and any value that collides with an existing registration after normalisation.
26. WHEN a process starts THE SYSTEM SHALL acquire the instance lease with a single atomic compare-and-set and SHALL NOT serve runtime traffic before acquisition succeeds.
27. WHEN the lease holder cannot confirm a renewal within the grace period, or a renewal shows the lease is no longer its own, THE SYSTEM SHALL stop admitting runtime requests, enforcing this on the request path without database access.
28. WHEN the lease holder shuts down gracefully THE SYSTEM SHALL stop accepting runtime requests and let in-flight requests finish before releasing the lease.
29. WHEN a starting process finds a live lease held by another process THE SYSTEM SHALL NOT take it, SHALL request a handover and wait in standby, and SHALL exit with an explicit error if it cannot acquire the lease within the configured wait.
30. WHEN a lease holder stops renewing without releasing THE SYSTEM SHALL allow another process to acquire the lease after the lease time-to-live, without manual intervention.
31. WHEN the configured lease time-to-live is less than the grace period plus the total request lifetime plus the clock margin THE SYSTEM SHALL refuse to start.
32. THE SYSTEM SHALL bound the total lifetime of every runtime request — reading the request body, the upstream call and writing the response — by one configured limit, and SHALL end a request that exceeds it with `408` when the body was not received in time and `504` otherwise.
33. THE SYSTEM SHALL check the lease in memory when a runtime request is admitted and again immediately before its upstream call is sent, and SHALL send nothing upstream if the second check fails.
34. WHEN the lease grace period elapses without a confirmed renewal THE SYSTEM SHALL ensure every request already in progress has ended no later than one request lifetime after admission stopped.
35. WHEN a renewal shows that lease ownership has been lost THE SYSTEM SHALL cancel every runtime request in progress immediately and close its upstream connection.
36. WHEN the lease holder receives a handover request THE SYSTEM SHALL stop accepting runtime requests, keep renewing the lease, let requests in progress finish within the request lifetime, drain the trace queue, and only then release the lease.
37. WHEN the lease holder has not completed a handover by the handover deadline THE SYSTEM SHALL, from the renewal thread and independently of the request-handling loop, fence the process and release the lease.
38. WHEN a waiting process finds the lease acquired by a holder newer than the one it asked THE SYSTEM SHALL exit with an explicit error and SHALL NOT issue another handover request.
39. THE SYSTEM SHALL document the assumptions on which the single-server property depends — clock-rate divergence within the configured margin, detectable pauses, a consistent lease store, and the absence of an upstream fencing token — and SHALL NOT describe that property as holding outside them.

## Requirement 8 — Behavioural verification

**User story:** As an engineer, I want the adapter tested against historical evidence, so that I know what is and is not established.

### Acceptance criteria

1. WHEN offline replay runs THE SYSTEM SHALL, without network access, transform each historical legacy request and validate the result against the replacement request schema, and transform each paired replacement recording and compare the result with the recorded legacy response.
2. WHEN a comparison is made THE SYSTEM SHALL apply only normalisation declared before the run, report field-level differences and the number of ignored fields, and report `unresolved` if no field remains to compare.
3. WHEN contract tests run THE SYSTEM SHALL validate legacy-shaped responses against the legacy contract for required fields, structure, types, evidence-established status codes and required headers, using a validator independent of the transformation engine.
4. WHEN property tests run THE SYSTEM SHALL execute Hypothesis tests of invariants that are stated only in legacy terms and derived from the legacy contract and fixtures, and report any minimal failing example.
5. WHEN an expected result is not established by evidence THE SYSTEM SHALL report the check as `unresolved` with the reason and SHALL NOT count it as passed.
6. WHEN a run completes THE SYSTEM SHALL compute two separate metrics from stored results using the published formulas, weights and formula version: behavioural compatibility over executed checks, and evidence coverage over the behaviour inventory.
7. THE SYSTEM SHALL present behavioural compatibility as tested compatibility, always together with evidence coverage, the numerator and denominator of each, the five behaviour-state counts, the lists of unresolved, not-assessed and partially verified behaviour, and the evidence version the run used, and state that it is not a guarantee of equivalence.
8. WHEN a run is interrupted by a restart THE SYSTEM SHALL mark it errored.
9. WHEN live integration tests run THE SYSTEM SHALL send requests over HTTP through the mounted runtime to the declared replacement test target and compare results with recorded legacy responses and legacy-only invariants.
10. WHEN the declared test target does not permit replay traffic THE SYSTEM SHALL mark live integration checks `skipped`, and WHEN it does not permit generated traffic THE SYSTEM SHALL mark property checks `skipped`, each with the specific reason.
11. THE SYSTEM SHALL establish expected results only from imported evidence and SHALL NOT provide any means of recording adapter output as an expected result.
12. THE SYSTEM SHALL implement verification oracles without importing the transformation engine or reading the Adapter Document.
13. WHEN a replayed fixture is also evidence for a mapping it exercises THE SYSTEM SHALL flag the result as evidence-overlapping and SHALL also publish behavioural compatibility computed without such results.
14. THE SYSTEM SHALL enumerate a behaviour inventory deterministically from evidence before any check runs, including every documented behaviour without evidence, every open gap, and every legacy endpoint or feature the product cannot analyse, and SHALL fix the inventory size for the duration of a run.
15. THE SYSTEM SHALL exclude unresolved, skipped and errored checks from behavioural compatibility, SHALL use the full behaviour inventory as the denominator of evidence coverage and of the tested share, and SHALL report a metric as undefined rather than as a number when its denominator is zero.
16. WHEN any check ends in error THE SYSTEM SHALL flag the run as incomplete.
17. WHEN a run completes THE SYSTEM SHALL assign every behaviour in the inventory exactly one of five states — verified, failed, not executed, unresolved, not assessed — such that the five counts sum to the inventory size.
18. WHEN any check of a behaviour fails THE SYSTEM SHALL report that behaviour as failed regardless of other checks that passed.
19. WHEN a behaviour has evidence-established checks and none of them executed THE SYSTEM SHALL report it as not executed, distinguishing skipped from errored.
20. WHEN a behaviour cannot be analysed by the product or is marked out of scope THE SYSTEM SHALL keep it in the inventory and report it as not assessed with the reason.
21. WHEN a gap is marked as accepted risk THE SYSTEM SHALL continue to report its behaviour as unresolved.
22. THE SYSTEM SHALL NOT return a verification summary that omits either metric, a denominator, the state counts, or the unresolved and not-assessed lists.
23. THE SYSTEM SHALL treat permission to send recorded and scripted requests to a test target and permission to send generated requests as two separate settings, both off by default, neither implying the other, so that live integration tests can run without authorising generated traffic.
24. WHEN verification is requested for a stale reconstruction THE SYSTEM SHALL refuse to start the run.
25. WHEN a verification run starts THE SYSTEM SHALL record the reconstruction, evidence version and fingerprint, inventory size and fingerprint, behaviour scopes, configuration, target permissions, formula version and weights it uses.
26. THE SYSTEM SHALL NOT modify a completed verification run, its inventory, its denominators or its scores, and SHALL indicate that evidence has changed since a run only through a label computed when the run is displayed.
27. THE SYSTEM SHALL display behavioural compatibility with both the behaviours tested out of the inventory size and the checks executed out of the planned executable checks.
28. THE SYSTEM SHALL place the checks of behaviours that belong to no endpoint in a single project-level group that enters behavioural compatibility like an endpoint, and SHALL count those behaviours in the inventory, the states and both coverage figures.
29. WHEN a verified behaviour has at least one skipped or errored check THE SYSTEM SHALL report it as partially verified and list the checks that did not run.
30. THE SYSTEM SHALL provide a test-target permission `allow_state_changing_traffic`, disabled by default, that applies to every traffic source and grants nothing on its own.
31. THE SYSTEM SHALL send live recorded or scripted traffic for a read-only operation only when `allow_replay_traffic` is enabled, and for a state-changing operation only when both `allow_replay_traffic` and `allow_state_changing_traffic` are enabled.
32. THE SYSTEM SHALL send Hypothesis-generated traffic for a read-only operation only when `allow_generated_traffic` is enabled, and for a state-changing operation only when both `allow_generated_traffic` and `allow_state_changing_traffic` are enabled.
33. THE SYSTEM SHALL require no traffic permission for offline fixture replay, which SHALL send nothing to any target, and SHALL create every traffic permission disabled.
34. THE SYSTEM SHALL classify every operation as read-only, state-changing or unknown from the declared effect of its route, SHALL treat unknown as state-changing for every permission decision, and SHALL classify as state-changing any request whose legacy or replacement method is not `GET`, `HEAD` or `OPTIONS` regardless of what is declared.
35. THE SYSTEM SHALL NOT classify an operation as read-only on the basis of its HTTP method alone; an operation with safe methods and no approved read-only declaration SHALL be classified unknown.
36. WHEN any step of a scripted sequence is state-changing or unknown and `allow_state_changing_traffic` is not enabled THE SYSTEM SHALL block the entire sequence and send none of its steps.
37. THE SYSTEM SHALL classify every planned request before any request is sent and record each unauthorised check as `skipped` with the reason and the effect, and SHALL, immediately before sending each request, independently check the declared effect of its route in the run's pinned adapter version, its actual methods, its traffic source and the run's snapshotted permissions, and refuse to send it if it is not authorised.
38. THE SYSTEM SHALL record in every verification run, and disclose in its report, whether state-changing traffic was enabled, the requests sent by traffic source and effect, the checks blocked by effect, and the operations whose effect is unknown, and SHALL write an audit record whenever a traffic permission changes or an operation's effect is declared.
39. WHEN a test target declares a reset request and state-changing traffic is enabled THE SYSTEM SHALL call the reset before the run and record its outcome, SHALL skip checks that are not read-only if it fails, and SHALL add the target's declared sandbox headers to verification traffic only.

## Requirement 9 — Demonstration

**User story:** As an evaluator, I want a repeatable end-to-end demonstration, so that the value is evident.

### Acceptance criteria

1. THE SYSTEM SHALL include a fictional legacy provider (GravePay), a fictional replacement (NovaPay) with a simulator, historical fixtures and an original legacy client, all identified as demonstration artefacts.
2. WHEN the legacy client is run against NovaPay directly THE SYSTEM SHALL demonstrate that it fails.
3. WHEN the legacy client is run through the compatibility runtime THE SYSTEM SHALL demonstrate that it succeeds with identical client source, proven by checksum.
4. WHEN the Docker demonstration runs THE SYSTEM SHALL route the client's original hostname to the runtime without using a base-URL override.
5. THE SYSTEM SHALL include at least one unresolved behavioural gap in the demonstration evidence and report it as unresolved.
6. THE SYSTEM SHALL include paired NovaPay recordings as replacement-side evidence, and a NovaPay simulator that imports no API Revenant code.
7. THE SYSTEM SHALL provide a demonstration simulator whose test state is held in named sandboxes isolated from each other and from the state used by the legacy-client demonstration, each resettable to the same deterministic seed data.
8. THE SYSTEM SHALL include in the demonstration a `GET` operation that has a side effect on the replacement and is declared state-changing, and an operation whose effect is left unknown, so that classification by declared effect is exercised.

## Requirement 10 — Application interface

**User story:** As an engineer, I want a clear product interface over real data, so that I can run the workflow without the command line.

### Acceptance criteria

1. THE SYSTEM SHALL provide a landing page, project list, project workspace (evidence, comparison, mappings, verification, runtime) and settings.
2. WHEN a screen displays results, metrics or activity THE SYSTEM SHALL load them from the backend and SHALL NOT display hardcoded values.
3. WHEN a control is visible THE SYSTEM SHALL perform its stated action or be disabled with the reason shown.
4. WHEN the backend is unreachable or returns an error THE SYSTEM SHALL show the error and a recovery action.
5. WHEN data shown belongs to the demonstration project THE SYSTEM SHALL label it as demo data.
6. THE SYSTEM SHALL be usable by keyboard, meet WCAG AA contrast, and remain usable at 375 px width.
7. THE SYSTEM SHALL NOT display behavioural compatibility without its "of N behaviours" denominator, its "of K planned checks executed" denominator, evidence coverage with its denominator, and the five-state breakdown including partial verification, and SHALL display an undefined metric as "—".

## Requirement 11 — Security

**User story:** As an operator, I want untrusted input and credentials handled safely.

### Acceptance criteria

1. WHEN an outbound destination resolves to a loopback, private, link-local or metadata address THE SYSTEM SHALL refuse the connection unless explicitly allowlisted.
2. THE SYSTEM SHALL validate outbound destinations at configuration time and again for every new connection, and SHALL NOT follow redirects.
3. THE SYSTEM SHALL store replacement-provider credentials encrypted and legacy-client keys as non-reversible keyed hashes, and SHALL NOT return or record either value in any API response, log, trace, event or adapter document.
4. WHEN a management request lacks a valid admin token THE SYSTEM SHALL respond `401`.
5. WHEN running in production with example secrets or with private upstreams allowed THE SYSTEM SHALL refuse to start.
6. THE SYSTEM SHALL treat imported documents as data and SHALL NOT execute or follow instructions contained in them.
7. WHEN a legacy-client key is added or revoked THE SYSTEM SHALL apply the change without restarting the runtime, and SHALL allow several keys to be active at once.
8. WHEN a replacement-provider credential is rotated THE SYSTEM SHALL activate the new version atomically, retain the previous version for rollback, and reload the running adapter.
9. WHEN a runtime's replacement-provider credential is missing or cannot be decrypted, or inbound authentication is declared with no active key, THE SYSTEM SHALL NOT serve that runtime.
10. THE SYSTEM SHALL take trace snapshots after removing the legacy credential and before attaching the replacement credential.
11. WHEN opening a new outbound connection THE SYSTEM SHALL resolve the hostname immediately before connecting, validate every resolved IPv4 and IPv6 address, and refuse the connection if any resolved address is disallowed.
12. THE SYSTEM SHALL connect to an address it has validated without a second name resolution, while sending the original hostname as TLS SNI and verifying the certificate against that hostname.
13. THE SYSTEM SHALL ignore proxy settings taken from the environment for outbound connections.

## Requirement 12 — Operations

**User story:** As a developer, I want to run and deploy the system from documentation.

### Acceptance criteria

1. THE SYSTEM SHALL run locally with SQLite using documented commands and in production with PostgreSQL using the same migrations.
2. THE SYSTEM SHALL provide liveness and readiness endpoints.
3. THE SYSTEM SHALL provide deployment configuration for Render (backend) and Netlify (frontend) and for no other hosting target, an `.env.example` without secrets, and Docker support for the demonstration.
4. THE SYSTEM SHALL NOT contain committed credentials.
5. THE SYSTEM SHALL provide a continuous-integration workflow that runs on Linux, runs the outbound transport tests and their mutation check with pinned dependency versions, and runs the backend test suite against SQLite and against PostgreSQL.
6. THE SYSTEM SHALL NOT use the outbound transport from application code until its complete test suite has passed on Linux in continuous integration, and no test or outbound control SHALL be weakened to obtain that result.
