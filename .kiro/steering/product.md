---
inclusion: always
---

# Product — API Revenant

**Tagline:** The dead shouldn't break the living.

API Revenant reconstructs the observable behaviour of a discontinued or breaking API from historical evidence, generates a declarative compatibility adapter for a replacement service, runs it as a proxy, and verifies the result against the evidence.

## Scope

In: evidence import (OpenAPI/Swagger, JSON fixtures), contract comparison, evidence-backed mapping proposals with human review, adapter compilation, an ASGI-mounted proxy runtime, fixture replay, contract and property tests, a GravePay → NovaPay demonstration.

Out (this release): LLM proposals, user accounts and multi-tenancy, non-JSON payloads, streaming, Postman import, automatic translation of arbitrary pagination schemes.

## Principles that override convenience

1. **Evidence first.** Every fact and mapping links to the evidence that supports it.
2. **Three confidence classes, never blurred.** `observed` (directly supported), `inferred` (heuristic; needs approval), `unknown` (reported as a gap, never mapped).
3. **No invented behaviour.** If evidence does not establish an expected result, the test is `unresolved`. Do not add defaults that make scores look better.
4. **Deterministic execution.** Transformations are declarative data run by a fixed engine. No generated code is executed.
5. **Computed results only.** Metrics, health and activity come from stored results and live state. No hardcoded success values anywhere, including the UI. Two metrics are published together — behavioural compatibility and evidence coverage — and unresolved behaviour always stays visible.
6. **Honest wording.** Say "tested compatibility", never "equivalent" or "guaranteed".
7. **The legacy client is never modified.** Its checksum is asserted by tests.

## Demonstration services

GravePay (legacy) and NovaPay (replacement) are fictional and must always be labelled as demonstration services. No real payments or credentials.

## Workflow

Create project → import legacy evidence → import replacement spec → import fixtures → reconstruct → review mappings → compile adapter → verify → start runtime → inspect traces.
