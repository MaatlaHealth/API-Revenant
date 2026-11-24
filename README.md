# API Revenant

**The dead shouldn't break the living.**

API Revenant is a design for reconstructing discontinued or breaking APIs from incomplete historical evidence, generating executable compatibility adapters for replacement services, and verifying whether existing applications can keep operating without rewriting their client code.

It reconstructs *observable interface behaviour*. It does not recover hidden source code or undocumented business logic. Every reconstructed behaviour is classified as **observed**, **inferred** or **unknown**, and linked to the evidence behind it.

## Status: design complete, application not yet built

**This repository does not contain a working application yet.** It contains the reviewed architecture and specifications, one feasibility spike with tests, and a CI workflow. Nothing has been deployed.

| Area | State |
|---|---|
| Architecture, requirements, data model, API contracts, security and testing design | **Written and reviewed** (`docs/`, `.kiro/specs/`) |
| Outbound transport feasibility spike | **Implemented and tested** (`spikes/outbound_transport`) — 78 tests passing on Windows; see [CI](#continuous-integration) for Linux |
| Specification consistency checker | **Implemented** (`tools/check_spec.py`) |
| CI workflow | **Defined** (`.github/workflows/ci.yml`) |
| Backend (evidence import, reconstruction, adapter, runtime, verification) | **Not implemented.** `backend/` holds a dependency manifest only |
| Demonstration services (GravePay, NovaPay simulator, legacy client) | **Not implemented.** `demo/` is a placeholder |
| Frontend | **Not implemented.** `frontend/` is a placeholder |
| Docker Compose demonstration | **Planned topology only**; it references images that do not exist yet and has never been run |
| Deployment (Render, Netlify) | **Configuration drafted, never deployed** |
| Kiro project files (`.kiro/`) | **Authored outside Kiro and not yet loaded in it** |

Everything below the next heading describes the **planned** product.

## How it is designed to work (planned)

1. **Evidence Vault** — import the legacy specification, the replacement specification and historical request/response fixtures. Every extracted fact records its source.
2. **Reconstruction** — deterministic comparison of the two contracts produces proposed mappings with evidence and a confidence class, plus a list of gaps.
3. **Review** — a human approves, rejects or corrects each mapping.
4. **Adapter** — approved mappings compile into a declarative, versioned Adapter Document. No generated code is executed.
5. **Runtime** — the adapter runs as a proxy mounted inside the backend, exposing the legacy interface in front of the replacement API.
6. **Verification** — three layers (offline fixture replay, independent contract and property tests, live runtime integration) produce two metrics computed from executed checks: tested behavioural compatibility and evidence coverage. Everything that evidence does not establish stays listed as unresolved.

## Planned demonstration

A fictional discontinued payment API (**GravePay**) and a fictional replacement (**NovaPay**). An original GravePay client is intended to fail against NovaPay directly and succeed through the API Revenant runtime, with identical source code. These are demonstration services, not real payment providers. None of this exists yet.

## What can be run today

The outbound transport spike, which proves that the SSRF-safe connection design works on `httpx` and `httpcore`:

```
python -m venv .venv
.venv\Scripts\python -m pip install -r spikes/outbound_transport/requirements.lock.txt
cd spikes/outbound_transport
..\..\.venv\Scripts\python -m pytest -v
```

On Linux or macOS use `.venv/bin/python`. See [`spikes/outbound_transport/README.md`](spikes/outbound_transport/README.md).

The specification consistency check:

```
python -m pip install pyyaml
python tools/check_spec.py
```

## Continuous integration

`.github/workflows/ci.yml` runs on every push and pull request:

- the complete outbound-transport spike suite and its mutation check, on Linux and Windows, from pinned dependency versions;
- the specification and configuration consistency check.

The Linux run is the gate for starting application development: the transport may not be used by application code until it passes there. Results are on the repository's Actions tab; this file does not assert a result.

## Documentation

| Document | Contents |
|---|---|
| [docs/architecture.md](docs/architecture.md) | Components, runtime design, recovery, concurrency, verification, scoring, decisions |
| [docs/transformation-language.md](docs/transformation-language.md) | The constrained adapter language |
| [docs/data-model.md](docs/data-model.md) | Tables, relationships, constraints |
| [docs/api-contracts.md](docs/api-contracts.md) | Management API and runtime data plane |
| [docs/security.md](docs/security.md) | Threat model and controls |
| [docs/testing-strategy.md](docs/testing-strategy.md) | Verification layers and the project's own test plan |
| [docs/demo-environment.md](docs/demo-environment.md) | GravePay → NovaPay demonstration design |
| [docs/local-development.md](docs/local-development.md) | Intended local workflow (becomes runnable as slices land) |
| [docs/deployment.md](docs/deployment.md) | Render and Netlify configuration, deployment record (empty) |
| [docs/kiro-usage.md](docs/kiro-usage.md) | What was generated for Kiro vs. what was done in Kiro |

These documents describe the intended design. Where they use the present tense, read it as specification, not as a description of running software.

## Kiro

The `.kiro/` directory contains the project spec (`specs/api-revenant`), steering documents, three custom agents and a hooks file. They were written to Kiro's documented formats but **authored outside Kiro and not yet loaded in it**; `docs/kiro-usage.md` records that distinction and is where real Kiro sessions are to be logged. No Kiro usage is claimed.

## Planned stack

FastAPI · Pydantic · SQLAlchemy · Alembic · HTTPX on httpcore · Hypothesis · Next.js · TypeScript · Tailwind CSS · SQLite (local) · PostgreSQL (production) · Render · Netlify

## Implementation plan

Development proceeds in vertical slices, each ending in something that runs and is tested (`.kiro/specs/api-revenant/tasks.md`).

| Slice | Scope | Status |
|---|---|---|
| — | Architecture, specifications, transport spike, CI definition | Done |
| 1 | Linux CI and transport integration | Not started; gated on the Linux CI result |
| 2 | Backend foundation and database migrations | Not started |
| 3 | Project creation and evidence import | Not started |
| 4 | Reconstruction and mapping approval | Not started |
| 5 | Adapter compilation | Not started |
| 6 | Runtime and unchanged-client demonstration | Not started |
| 7 | Independent verification | Not started |
| 8 | Frontend and final polish | Not started |

## Security

The design treats imported specifications and fixtures as untrusted data, never follows redirects on outbound calls, and keeps credentials out of logs and traces; see [docs/security.md](docs/security.md). The fictional demonstration credentials in `.env.example` (`legacy-demo-key`, `replacement-demo-token`) are placeholders for a simulator that does not exist yet. No real credentials are stored in this repository.

## Licence

[MIT](LICENSE) © 2026 Maatla Health
