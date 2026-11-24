---
inclusion: always
---

# Project Structure

```
.kiro/                 specs, steering, agents, hooks
backend/
  revenant/
    api/               FastAPI routers only; no business logic
    evidence/          upload validation, parsers, provenance
    reconstruction/    contract model, diff, proposers, gaps
    transform/         transformation language: models, validator, engine (pure, no I/O)
    adapter/           compile approved mappings into Adapter Documents
    runtime/           ASGI proxy, registry, tracing, health
    verification/      replay, contract, property tests, scoring
    security/          outbound guard, secrets, redaction, auth
    db/                models, session, migrations support
  alembic/
  tests/{unit,property,integration,security,resilience,e2e}/
frontend/              Next.js application
demo/
  gravepay/            legacy spec and fixtures
  novapay/             replacement spec and simulator
  legacy-client/       original client — never edit after creation
docs/                  authoritative design documents
spikes/                feasibility spikes with their own tests; not application code, never imported by backend/
```

## Rules

- Dependency direction: `api → services → transform`. `transform` imports nothing from the rest of the package.
- `runtime` may read the database only through the registry and the trace writer.
- A new module needs tests in the matching `tests/` layer in the same change.
- Design documents in `docs/` are authoritative. If code must diverge, update the document in the same change and say why.
- `demo/legacy-client/` is frozen once written; a checksum test enforces this.
- File naming: Python `snake_case.py`; React components `PascalCase.tsx`; routes follow Next.js App Router conventions.
