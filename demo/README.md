# Demonstration

**GravePay** (a discontinued legacy payment API) and **NovaPay** (its replacement) are
fictional services created for this project. They are not real payment providers. All
records are deterministic sample data; no real payments, customers or credentials are
involved. See `docs/demo-environment.md` for the design.

## What exists

| Path | Contents |
|---|---|
| `gravepay/openapi.yaml` | The legacy contract: six operations, `X-API-Key` authentication, page/limit pagination |
| `gravepay/fixtures.json` | Ten recorded GravePay exchanges, with credential headers already redacted |
| `gravepay/notes.md` | A fragment of legacy documentation (stored by the product; free text is not analysed) |
| `novapay/openapi.yaml` | The replacement contract: bearer authentication, cursor pagination, nested bodies, problem-style errors |
| `novapay/recordings.json` | Ten NovaPay recordings, each paired with the GravePay fixture for the same operation |

These files import cleanly through the Evidence Vault API and are used by the test suite.

## Deliberately left open

The evidence is incomplete on purpose, so the product has something honest to report:

- `GET /v1/charges/{id}/events` has no recorded exchange at all.
- The `429` response of `POST /v1/charges` is documented without a body.
- The `409` of the refund operation names no error code.
- Nothing shows what a page beyond the last one returned, or what a repeated `reference` did.

## Running it

Without Docker, from the repository root, in the environment where the backend is installed:

```
python demo/run_demo.py
```

It starts the NovaPay simulator and the backend as separate processes on free local ports,
runs the unchanged client against NovaPay directly (it fails), drives the whole workflow
through the management API, and runs the same client through the runtime (it succeeds).
The test suite runs this script.

| Path | Contents |
|---|---|
| `novapay/simulator/app.py` | The NovaPay simulator. Imports no API Revenant code. Isolated, resettable sandboxes; rejects any request carrying a GravePay key; the receipt `GET` has a counted side effect |
| `legacy-client/gravepay_client.py` | The GravePay client, written once and not edited. `legacy-client/SHA256` records its checksum and a test compares them |
| `run_demo.py` | The runner described above |

The Docker Compose demonstration (`docker-compose.yml`, three Dockerfiles) is written but has
never been built or run: Docker is not installed on the development machine.
