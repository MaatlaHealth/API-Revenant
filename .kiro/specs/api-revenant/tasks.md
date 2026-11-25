# Implementation Plan — API Revenant

Revision 6. The plan is organised as **vertical slices**. Each slice ends with something that runs and is tested, and a slice is not started until the previous one is green in CI. The 166 acceptance criteria are not built at once; each task lists the criteria it delivers.

Stage 1 (specifications, scaffold, the outbound-transport spike and the CI workflow definition) is complete and not listed.

**Entry gate for Stage 2 — met.** The CI workflow ran on commit `cf9841f` on 2026-11-24; the complete outbound-transport spike suite and its mutation check passed on `ubuntu-latest` and `windows-latest` with the pinned dependencies ([run](https://github.com/MaatlaHealth/API-Revenant/actions/runs/45490838142)).

Rules for every slice:

- Keep `main` runnable; every merged change has tests.
- Do not weaken a test or an outbound control to obtain a pass. Do not report a test as passed unless it was executed.
- Do not extend the lease design unless a concrete defect is demonstrated. Do not add infrastructure or dependencies that the slice does not need.

---

## Slice 1 — Linux CI and transport integration

*Done when:* CI is green on Linux for the spike suite, the mutation check and the first backend tests; the backend starts and answers `/healthz`; the outbound guard lives in the application with the spike's tests passing against it.

- [x] 1.1 **Gate:** push the repository, run `.github/workflows/ci.yml`, and record the Linux result of the complete outbound-transport spike suite and mutation check with `requirements.lock.txt`. Fix defects found; never by relaxing a test or an SSRF control
  - _Requirements: 12.5, 12.6_
- [x] 1.2 Create the `revenant` package, settings (`pydantic-settings`), structured logging with query-free request paths and no raw exception messages, app factory, `/healthz` and `/readyz`; production start-up checks for example secrets and unsafe flags
  - _Requirements: 11.5, 12.2, 6.18_
- [x] 1.3 Outbound guard in `revenant.security.outbound`, promoted from `spikes/outbound_transport` (httpx on an httpcore pool with a custom network backend): URL rules, resolve immediately before each new connection, validate every IPv4 and IPv6 address, refuse mixed answers, connect to the validated address with no second resolution, hostname-based SNI and certificate verification, bounded connection reuse, environment proxies ignored, redirects refused
  - _Requirements: 11.1, 11.2, 11.11, 11.12, 11.13_
- [x] 1.4 Carry the spike's tests and mutation check over to the application module; add backend jobs to CI: `ruff`, `mypy`, `pytest`, the mutation check and a server smoke test, on Linux and Windows. *The PostgreSQL service job moves to task 2.1: slice 1 has no database code to run against it.*
  - _Requirements: 11.1, 11.2, 11.11, 11.12, 11.13, 12.5_

## Slice 2 — Backend foundation and database migrations

*Done when:* migrations apply on SQLite and PostgreSQL in CI; the management API rejects unauthenticated calls; credentials can be stored and are never returned.

- [x] 2.1 SQLAlchemy models and the initial Alembic migration (projects, both credential tables, audit records; further tables arrive with the slice that uses them); upgrade verified on SQLite and PostgreSQL; add the PostgreSQL service job to CI
  - _Requirements: 12.1, 12.5_
- [x] 2.2 Admin-token dependency, CORS allowlist and the common error envelope
  - _Requirements: 11.4_
- [x] 2.3 Single-worker start-up check (`WEB_CONCURRENCY`, `REVENANT_WORKERS`, supervisor detection) that refuses to start with `unsupported_multi_worker`
  - _Requirements: 7.18_
- [x] 2.4 Inbound key store: keyed-hash verifiers, multiple active keys, constant-time verification, add/revoke API. *Refusing to revoke the last active key of a running runtime belongs to task 6.3, where runtimes exist.*
  - _Requirements: 11.3, 11.7, 6.10_
- [x] 2.5 Outbound credential store: Fernet encryption, versions, rotate/rollback/retire API, master-key rotation with `REVENANT_SECRET_KEY_PREVIOUS`. *Reloading a running adapter on rotation belongs to task 6.3.*
  - _Requirements: 11.3, 11.8, 5.6_
- [x] 2.6 Redactor for logs and traces, with tests that search captured output for both credential values
  - _Requirements: 11.3_

## Slice 3 — Project creation and evidence import

*Done when:* through the API, a project can be created and the GravePay and NovaPay specifications and fixtures imported, with provenance and a missing-evidence list returned.

- [x] 3.1 Projects API: CRUD, slug uniqueness, workflow status, activity. *The workflow reports reconstruction and later steps as not yet available until their slices land.*
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5_
- [x] 3.2 Upload validation, storage and parse reports
  - _Requirements: 2.4, 2.5, 11.6_
- [x] 3.3 OpenAPI 3.x and Swagger 2.0 parser with local `$ref` resolution, cycle detection, and fact extraction with locators. *Markdown, text and standalone JSON Schema files are stored and reported as not analysed.*
  - _Requirements: 2.1, 2.6, 2.9_
- [x] 3.4 Fixture parser for legacy fixtures and paired replacement recordings (`pair_with`), credential redaction, endpoint association, fixture-only endpoints, content hashing
  - _Requirements: 2.2, 2.3, 2.7_
- [x] 3.5 Missing-evidence computation and evidence API routes
  - _Requirements: 2.8_
- [x] 3.6 Evidence versioning: `evidence_version` and fingerprint maintained on import and removal; soft delete that retains removed files
  - _Requirements: 2.10_
- [x] 3.7 Demonstration evidence: GravePay OpenAPI specification, documentation fragment and fixtures including the four unresolved gaps; NovaPay OpenAPI specification and paired recordings, including the side-effecting `GET` receipt operation and the events operation
  - _Requirements: 9.1, 9.5, 9.6, 9.8_

## Slice 4 — Reconstruction and mapping approval

*Done when:* reconstruction of the demonstration evidence is deterministic and produces mappings, gaps, invariants, an inventory and operation-effect proposals; mappings and effects can be approved or rejected through the API.

- [x] 4.1 Normalised contract model and endpoint pairing
  - _Requirements: 3.1_
- [x] 4.2 Proposer rules (identity, normalised name, structural, fixture correlation, enum, auth, pagination, error shape) with confidence classification and evidence references
  - _Requirements: 3.2, 3.3, 3.4_
- [x] 4.3 Gap detection; reconstruction records its evidence version and fingerprint; staleness by fingerprint comparison; determinism test
  - _Requirements: 3.5, 3.6, 3.7_
- [x] 4.4 Mapping review API: approve, reject, edit, bulk approve, audit records. *The rule models of the transformation language (`revenant/transform/model.py`) were written here, ahead of task 5.1, because an edited rule has to be validated. Review decisions are not carried over to a later reconstruction.*
  - _Requirements: 4.1, 4.2, 4.3_
- [x] 4.5 Operation effect: engine proposals (`state_changing` when a method is unsafe, otherwise `unknown`, never `read_only`); declaration API requiring a note; rejection of read-only on an unsafe method or against an explicit evidence marker; audit records
  - _Requirements: 4.5, 8.35_
- [x] 4.6 Legacy-only invariant derivation (echo, schema, pagination, error) with confidence and evidence; invariant review API
  - _Requirements: 8.4, 8.11_
- [x] 4.7 Behaviour inventory: deterministic enumeration (responses, auth, pagination, gaps, unsupported features, project-level behaviours), immutable per reconstruction, inventory difference against the previous reconstruction, scope marking API
  - _Requirements: 8.14, 8.20, 3.8, 3.9_

## Slice 5 — Adapter compilation

*Done when:* approved mappings for the demonstration compile into a validated Adapter Document, and a dry run shows the transformed NovaPay request for each GravePay fixture.

- [x] 5.1 Pydantic models for the Adapter Document and all rule types, including `route.effect`
  - _Requirements: 5.1_
- [x] 5.2 Path parser and interpreter for `move`, `const`, `default`, `convert`, `enum_map`, `template`, `each`, `drop`, with conditions and rule traces
  - _Requirements: 6.1, 6.7, 6.8_
- [x] 5.3 Pagination: `computed_cursor` and response envelope conversion
  - _Requirements: 6.3_
- [x] 5.4 Error rules and envelope
  - _Requirements: 6.5, 6.6_
- [x] 5.5 Static validator: credential carriers (header or query parameter) unreachable by rules; a route declared read-only may not use a state-changing method on either side. *Not implemented: the type-compatibility check between source and target fields. A missing replacement credential is a compile warning; it is enforced when the runtime loads the adapter (slice 6).*
  - _Requirements: 5.3, 5.4, 5.7, 6.12_
- [x] 5.6 Engine property tests (operation semantics, safety, cursor round-trip, no credential carrier reachable)
  - _Requirements: 6.1, 6.3, 6.12_
- [x] 5.7 Adapter compiler: compile approved mappings and effects, required-field coverage check, versioning, dry run. *A route that fails the coverage check is left out of the document and listed with the reason; the other routes still compile.*
  - _Requirements: 4.4, 5.1, 5.2, 5.5, 5.7_
- [ ]* 5.8 Pagination `cursor_cache` strategy with hop limit
  - _Requirements: 6.4_

## Slice 6 — Runtime and unchanged-client demonstration

*Done when:* the unchanged GravePay client fails against the NovaPay simulator directly and succeeds through the runtime, by host entry and by path entry, with traces visible through the API; restart recovery works.

- [ ] 6.1 Instance lease: single-row table, atomic acquisition, renewal thread, `admit_until` and `stop_by` deadlines on a suspend-aware clock, sibling-worker refusal, timing validation (`TTL ≥ grace + request lifetime + clock margin`), stale-lease expiry, development force-acquire flag, readiness integration
  - _Requirements: 7.19, 7.26, 7.27, 7.30, 7.31, 7.39_
- [ ] 6.2 Lease handover protocol: handover request, standby wait, holder stop-accepting → drain → trace drain → release, renewal continues during drain, handover deadline enforced by the renewal thread, exit rules for waiting processes
  - _Requirements: 7.28, 7.29, 7.36, 7.37, 7.38_
- [ ] 6.3 Registry, activation/deactivation transaction, credential resolution at load, readiness preconditions
  - _Requirements: 7.1, 7.2, 7.3, 11.9_
- [ ] 6.4 `HostDispatcher`: shared `normalise_host`, exact-match host table, three-way dispatch (legacy / management / `421`), `400` for malformed hosts, unmodified scope pass-through, management isolation, forwarded-header policy with trusted proxies, registration validation and collision handling
  - _Requirements: 7.6, 7.12, 7.13, 7.20, 7.21, 7.22, 7.23, 7.24, 7.25_
- [ ] 6.5 `RuntimeProxy`: path entry mount, route matching, forwarding through the outbound guard, upstream error mapping
  - _Requirements: 7.9, 7.11_
- [ ] 6.6 Request lifetime and lease checks: one total deadline covering body read, upstream call and response; `408` / `504` outcomes; admission check and second check before the upstream call; watchdog at `stop_by`; cancellation on ownership loss; graceful draining
  - _Requirements: 7.32, 7.33, 7.34, 7.35_
- [ ] 6.7 Authentication separation: carrier extraction before any other processing, inbound verification, outbound attachment after snapshot, pre-send leak check in raw and decoded form, failure behaviours
  - _Requirements: 6.2, 6.9, 6.10, 6.11, 11.10_
- [ ] 6.8 Query-parameter credentials: exact extraction procedure, sanitised query preserving other parameters and order, duplicate rejection, no recording of query or headers before an adapter is resolved, query-free upstream URLs in logs and traces, server access log disabled
  - _Requirements: 6.13, 6.14, 6.15, 6.16, 6.17, 6.18_
- [ ] 6.9 Trace pipeline: bounded queue, size cap, batched writer thread with bounded retry, counters, `trace_loss` events, supervised writer, graceful shutdown drain, retention
  - _Requirements: 7.7, 7.14, 7.15, 7.16, 7.17_
- [ ] 6.10 Startup recovery, database-outage behaviour, runtime health endpoint with trace, lease, in-flight and upstream state; runtime events API (persisted events only) and response headers
  - _Requirements: 7.4, 7.5, 7.8, 7.10, 7.16, 7.27_
- [ ] 6.11 Demonstration services: deterministic NovaPay simulator that imports no `revenant` code, with isolated resettable sandboxes, the side-effecting receipt `GET` and its side-effect counter; legacy client and checksum file; demo seed endpoint and `demo/run_demo.py`
  - _Requirements: 9.1, 9.2, 9.3, 9.6, 9.7, 9.8_
- [ ] 6.12 Dockerfiles and Compose profiles with hostname alias routing to `HostDispatcher`
  - _Requirements: 9.4, 12.3_
- [ ] 6.13 End-to-end acceptance test (before fails, after succeeds, checksum equal, host entry exercised with an explicit `Host` header)
  - _Requirements: 9.2, 9.3, 9.4_
- [ ] 6.14 Runtime test tables from `docs/testing-strategy.md`: hostname routing and host security; authentication; query-parameter credentials; trace pipeline; worker constraint and instance lease including handover and stale-process cases; request lifetime and in-flight requests; randomised mutual-exclusion test within the assumptions and the tests that demonstrate the documented limits; resilience
  - _Requirements: 7.4, 7.5, 7.6, 7.9, 7.10, 7.12, 7.14, 7.15, 7.16, 7.17, 7.18, 7.19, 7.20, 7.21, 7.22, 7.23, 7.24, 7.25, 7.26, 7.27, 7.28, 7.29, 7.30, 7.31, 7.32, 7.33, 7.34, 7.35, 7.36, 7.37, 7.38, 7.39, 6.9, 6.10, 6.11, 6.13, 6.14, 6.15, 6.16, 6.17, 6.18_

## Slice 7 — Independent verification

*Done when:* a verification run on the demonstration produces both metrics with all denominators, lists unresolved, unknown and partially verified items, and the permission matrix is proven by tests that assert on what the simulator received.

- [ ] 7.1 Planner: deterministic enumeration of checks from the behaviour inventory, fixtures and invariants; every check linked to one behaviour and one group; refusal on a stale reconstruction; snapshot of evidence version, inventory, scopes, configuration and permissions at run start
  - _Requirements: 8.5, 8.14, 8.24, 8.25_
- [ ] 7.2 Independent oracles package (schema validation with `jsonschema`, diff with declared normalisation, invariant checker, value lookup) and the architecture test forbidding imports of `transform` and `adapter`
  - _Requirements: 8.2, 8.3, 8.12_
- [ ] 7.3 Layer A: offline fixture replay (request-side schema validation, response-side replay of paired recordings), evidence-overlap flag; sends nothing and needs no permission
  - _Requirements: 8.1, 8.13, 8.33_
- [ ] 7.4 Traffic permissions: `allow_replay_traffic`, `allow_generated_traffic`, `allow_state_changing_traffic`, all disabled by default; the permission matrix as one pure function; audit record on change; specific skip reasons
  - _Requirements: 8.10, 8.23, 8.30, 8.31, 8.32, 8.33_
- [ ] 7.5 Operation classification: `effective_effect` (declared effect from the pinned adapter version, unknown treated as state-changing, HTTP method as a floor, method alone never read-only); whole-sequence classification
  - _Requirements: 8.34, 8.35, 8.36_
- [ ] 7.6 Enforcement: planner-level skipping with reason and effect; dispatcher that independently re-derives the decision immediately before each send; target sandbox headers and reset request
  - _Requirements: 8.37, 8.39_
- [ ] 7.7 Layer B: contract checks; Hypothesis property tests subject to the generated-traffic and state-changing permissions
  - _Requirements: 8.3, 8.4, 8.10, 8.32_
- [ ] 7.8 Layer C: live recorded and scripted traffic through the runtime, read-only with replay permission alone
  - _Requirements: 8.9, 8.10, 8.23, 8.31_
- [ ] 7.9 Scoring: five behaviour states with the fully/partially verified split; behavioural compatibility over endpoint groups and the project-level group (all and overlap-excluded); tested share; checks executed of planned executable; evidence coverage; null handling; incomplete flag; stored weights and formula version; summary schema that requires both metrics with all denominators
  - _Requirements: 8.6, 8.7, 8.15, 8.16, 8.17, 8.18, 8.19, 8.20, 8.21, 8.22, 8.27, 8.28, 8.29_
- [ ] 7.10 Run lifecycle: interrupted-run handling, evidence fingerprint before/after, immutability of completed runs, computed "evidence has changed" label, disclosure of state-changing traffic by source and effect and of unknown operations, verification API
  - _Requirements: 8.8, 8.11, 8.25, 8.26, 8.38_
- [ ] 7.11 Verification tests from `docs/testing-strategy.md`: exhaustive permission matrix; side-effecting `GET`; unknown operations; conflicting declarations; generated state-changing requests; whole-sequence blocking; dispatcher refusal when the plan is wrong; mutation tests of the verifier and of the classifier; evidence-version, state, formula and display-denominator tests
  - _Requirements: 8.5, 8.6, 8.15, 8.17, 8.18, 8.21, 8.22, 8.23, 8.24, 8.26, 8.27, 8.28, 8.29, 8.30, 8.31, 8.32, 8.33, 8.34, 8.35, 8.36, 8.37, 8.38, 8.39, 3.8, 3.9, 4.5, 5.7_

## Slice 8 — Frontend and final polish

*Done when:* the whole workflow can be run in the browser against the real backend, the application is deployed to Render and Netlify, and the deployment record is filled in from an actual deployment.

- [ ] 8.1 Frontend foundation: Next.js app, design tokens, typed API client, token gate, error and empty states
  - _Requirements: 10.2, 10.4, 10.6_
- [ ] 8.2 Landing page
  - _Requirements: 10.1_
- [ ] 8.3 Projects list and creation
  - _Requirements: 1.1, 1.3, 10.1, 10.5_
- [ ] 8.4 Workspace: Evidence Vault (upload, parsed endpoints, provenance, missing evidence, evidence version and stale-reconstruction notice)
  - _Requirements: 2.8, 2.10, 3.7, 10.1, 10.3_
- [ ] 8.5 Workspace: comparison and mapping review (side-by-side contracts, approvals, operation effects with Unknown shown as needing a decision, invariants, gaps, behaviour inventory and inventory difference)
  - _Requirements: 4.1, 4.2, 4.5, 3.9, 10.1_
- [ ] 8.6 Workspace: verification (three layers, traffic permissions with the permission matrix, planned requests by source and effect before a run, disclosure of state-changing traffic on each run, both metrics with all denominators, five-state breakdown with partial verification, unresolved, unknown and not-assessed lists, diffs, overlap disclosure, evidence version and "evidence has changed" label)
  - _Requirements: 8.7, 8.13, 8.23, 8.26, 8.27, 8.29, 8.30, 8.38, 10.1, 10.2, 10.7_
- [ ] 8.7 Workspace: runtime (controls, health including trace counters, lease state and in-flight requests, persisted requests, traces with sanitised queries)
  - _Requirements: 7.8, 7.16, 10.1_
- [ ] 8.8 Settings (project configuration, inbound keys, outbound credentials with rotation and rollback, legacy hosts, runtime configuration)
  - _Requirements: 10.1, 11.3, 11.7, 11.8_
- [ ]* 8.9 Playwright journey over the real backend
  - _Requirements: 10.2, 10.3_
- [ ] 8.10 Full test suite, type checks and lint green; manual walkthrough in the browser; accessibility and responsive checks; secret scan of the repository
  - _Requirements: 10.6, 12.4_
- [ ] 8.11 Finalise `render.yaml`, `netlify.toml`, backend Dockerfile, migration-on-start, `--workers 1`, `--no-access-log` and `WEB_CONCURRENCY=1` everywhere, `REVENANT_PUBLIC_HOSTS` set; confirm no configuration for any other backend target exists
  - _Requirements: 12.3, 7.18, 7.22, 6.18_
- [ ] 8.12 PostgreSQL verification of lease behaviour between two real processes (acquire race, handover, stale recovery); hosted demo simulator mount; production configuration checklist
  - _Requirements: 12.1, 7.19, 7.26, 7.29, 7.30, 7.36_
- [ ] 8.13 Deploy to Render and Netlify, verify (including one deployment handover and one restart), and fill in the deployment record in `docs/deployment.md`
- [ ] 8.14 Record real Kiro sessions in `docs/kiro-usage.md`

Tasks marked `*` are optional for the first release.
