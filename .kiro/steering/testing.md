---
inclusion: fileMatch
fileMatchPattern: ["backend/**/*.py", "frontend/**/*.test.*", "demo/**/*.py"]
---

# Testing Standards

Full strategy: #[[file:docs/testing-strategy.md]]

- Write tests with the component, not afterwards.
- Tests are deterministic: no real network, no wall-clock dependence, Hypothesis derandomised in CI.
- Integration tests use the real app through `httpx.ASGITransport` and a real database. Do not mock the transformation engine or the runtime in integration tests.
- Property tests state the invariant in the test name and reference the mapping or requirement it comes from.
- Never assert a hardcoded pass. A verifier test must include a deliberately broken adapter that fails.
- An expected value that is not established by evidence is `unresolved`, not a pass and not a guess.
- Product verification has three layers: offline historical replay, independent contract and property tests, live runtime integration. Keep them separate; do not let one layer's code supply another's expectations.
- `revenant.verification.oracles` must not import `revenant.transform` or `revenant.adapter`, or read the Adapter Document. Never add a way to record adapter output as an expected result.
- Engine property tests belong to this project's test suite and never contribute to a project's compatibility metrics.
- Scoring changes require updating `docs/architecture.md` §6, `formula_version`, and the scoring tests together.
- Every behaviour in the inventory gets exactly one of five states and the counts must sum to the inventory size. Never drop a behaviour from the inventory to improve a metric.
- Inventories and completed verification runs are immutable. Never write code or a test helper that updates a completed run, its states or its results.
- Three traffic sources: offline fixture replay (sends nothing, needs no permission), live recorded or scripted traffic (`allow_replay_traffic`), and Hypothesis-generated traffic (`allow_generated_traffic`). Operations that are not read-only additionally need `allow_state_changing_traffic`, from either live source. Layer C must run read-only with replay permission alone, and tests must assert on what the target actually received. `allow_state_changing_traffic` applies to recorded and generated traffic alike.
- Operation effect is `read_only`, `state_changing` or `unknown`; `unknown` behaves as state-changing. Tests must cover side-effecting `GET` operations, unknown operations, read-only declared on an unsafe method, generated state-changing requests, whole-sequence blocking and every permission combination. Never write a test fixture that classifies by HTTP method only.
- The outbound transport tests and their mutation check must pass on Linux in CI before application code uses the transport. Never relax a test or an outbound control to get a pass, and never report a test as passed unless it was executed.
- The demonstration simulator keeps state in isolated, resettable sandboxes; tests that write to it reset their sandbox first and never share one with the legacy-client demonstration.
- Lease and host-security tests use injected clocks and explicit `Host` headers; they must not depend on real time or real DNS.
- Resilience behaviour (restart recovery, concurrency, database loss) has dedicated tests in `tests/resilience`; changes to `runtime/` must keep them green.
- A defect fix starts with a failing test that reproduces it.
- Do not weaken or delete a failing test to make a build pass; fix the cause or report it.
