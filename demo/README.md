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

## Not built yet

The NovaPay simulator, the original GravePay client and the Docker demonstration arrive
with slice 6.
