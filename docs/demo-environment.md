# Demonstration Environment — GravePay → NovaPay

GravePay and NovaPay are **fictional services created for this demonstration**. They are not real payment providers. No real payments, customer data or financial credentials are involved. All records are deterministic sample data and are labelled as demo data in the product.

## The two contracts

| Aspect | GravePay (legacy, discontinued) | NovaPay (replacement) |
|---|---|---|
| Authentication | `X-API-Key: <key>` | `Authorization: Bearer <token>` |
| Create | `POST /v1/charges` | `POST /payments` |
| Retrieve | `GET /v1/charges/{id}` | `GET /payments/{payment_id}` |
| List | `GET /v1/charges?page=2&limit=20` | `GET /payments?cursor=eyJwYWdlIjoyfQ==&page_size=20` |
| Refund | `POST /v1/charges/{id}/refund` | `POST /payments/{payment_id}/refunds` |
| Receipt | `GET /v1/charges/{id}/receipt` | `GET /payments/{payment_id}/receipt` — **has a side effect**: the first call records `receipt_issued_at` |
| Events | `GET /v1/charges/{id}/events` | `GET /payments/{payment_id}/events` |
| Create body | `{ customer, amount, currency, reference? }` | `{ customer_id, transaction: { value, currency_code }, external_ref? }` |
| Resource | `{ id, customer, amount, currency, status, created }` | `{ payment_id, customer_id, transaction: {…}, state, created_at }` |
| Status enum | `succeeded`, `failed`, `pending` | `COMPLETED`, `DECLINED`, `PROCESSING` |
| Timestamp | `created`: ISO 8601 string | `created_at`: epoch seconds |
| List envelope | `{ data, page, limit, has_more }` | `{ items, next_cursor }` |
| Error format | `{ "error": { "code": "invalid_currency", "message": "…" } }`, status 400/401/404 | `{ "type", "title", "status", "detail", "code": "CURRENCY_NOT_SUPPORTED" }`, status 422/401/404 |

NovaPay's cursor is documented in its specification as base64 of `{"page": N}`, which is what makes the stateless `computed_cursor` strategy legitimate for this demonstration.

## Transformations exercised

Nesting (`amount` → `transaction.value`), flattening (the reverse on responses), rename, enum mapping, type conversion (epoch → ISO 8601), header-based auth translation, query and pagination translation, list envelope conversion, error normalisation (422 `CURRENCY_NOT_SUPPORTED` → 400 `invalid_currency`).

## Deliberately unresolved gaps

These are present in the evidence set on purpose, and the product must report them as unresolved rather than assume an answer:

1. **Rate limiting.** The GravePay specification lists a `429` response but no fixture shows its body or headers. No expected result is established.
2. **Pagination past the end.** No fixture shows what GravePay returned for a page beyond the last one (empty list or `404`).
3. **Refund of an already-refunded charge.** The specification names an error but gives no code; no fixture exists.
4. **`reference` uniqueness.** Documentation text mentions idempotency by `reference`, but no fixture demonstrates a repeated request.

## Files

```
demo/gravepay/openapi.yaml          legacy contract
demo/gravepay/fixtures.json         historical request/response pairs (credential headers redacted)
demo/gravepay/notes.md              legacy documentation fragment
demo/novapay/openapi.yaml           replacement contract
demo/novapay/recordings.json        paired replacement recordings (pair_with → GravePay fixture); the response-side input for offline replay
demo/novapay/simulator/             FastAPI simulator with deterministic seeded data; imports no API Revenant code
demo/legacy-client/gravepay_client.py   the original client, written once against GravePay
demo/legacy-client/SHA256           checksum checked by tests and printed by the demo
demo/run_demo.py                    non-Docker runner
```

## How `api.gravepay.demo` reaches the runtime

Docker DNS is only the first hop. The full path in the *after* phase:

1. Docker's embedded DNS resolves `api.gravepay.demo` to the API Revenant backend container (network alias).
2. The client connects to port 8000 and sends its normal GravePay request with `Host: api.gravepay.demo:8000`.
3. Inside the backend, `HostDispatcher` — the outermost ASGI application — strictly normalises the host to `api.gravepay.demo`, finds an exact match registered to the GravePay project in the host table, and hands the request to the runtime proxy with method, path, query string, headers and body exactly as sent. The management API is not reachable on this hostname; it answers only on the management hosts (`revenant`, `localhost`).
4. The runtime verifies the GravePay key, discards it, transforms the request, and calls NovaPay with the NovaPay credential.

The demo set-up step registers `api.gravepay.demo` as a legacy host through `PATCH /projects/{id}/runtime`. No reverse proxy is involved and the client's base-URL override is not set. Details: `architecture.md` §4.2.

## Credentials in the demonstration

| Credential | Held by | Sent to |
|---|---|---|
| `legacy-demo-key` (fictional) | The legacy client; registered in API Revenant as an inbound key and stored only as a hash | API Revenant runtime only. Never forwarded to NovaPay. |
| `replacement-demo-token` (fictional) | API Revenant, encrypted | NovaPay simulator only |

The simulator rejects any request that carries an `X-API-Key` header, which makes accidental forwarding of the legacy key a visible failure.

## Verification target in the demonstration

The demo set-up declares the NovaPay simulator as the verification target with all three permissions enabled — `allow_replay_traffic`, `allow_state_changing_traffic` and `allow_generated_traffic` — because it is a disposable local simulator with resettable state. The product's default for each is `false`; a real sandbox would normally be granted `allow_replay_traffic` alone first. The demonstration report therefore discloses that state-changing traffic was enabled.

## Operation effects in the demonstration

The demonstration is built to show that HTTP method does not decide classification.

| Operation | Methods | Declared effect in the demo | Why |
|---|---|---|---|
| Retrieve, List | `GET` → `GET` | `read_only` (approved in the demo set-up) | Genuinely read-only on the simulator |
| Create, Refund | `POST` → `POST` | `state_changing` | Unsafe method; cannot be declared otherwise |
| Receipt | `GET` → `GET` | `state_changing` | A `GET` with a side effect: issuing a receipt is recorded on the payment |
| Events | `GET` → `GET` | left `unknown` | Deliberately undeclared, so the report shows an unknown operation and treats it as state-changing |

With only `allow_replay_traffic` enabled, Retrieve and List are exercised live and the other four are blocked. The simulator exposes a per-sandbox side-effect counter so tests can prove the Receipt operation was not called.

## Isolated, resettable simulator state

The simulator keeps its data in named **sandboxes**, each created from the same deterministic seed on first use and held separately in memory.

| Sandbox | Selected by | Used for |
|---|---|---|
| `default` | No `X-NovaPay-Sandbox` header | The legacy-client demonstration (before and after phases) |
| `verification` | `X-NovaPay-Sandbox: verification`, declared as the target's `sandbox_headers` | All verification traffic |
| any other name | The same header with another value | Automated tests that need a private data set |

- `POST /__sandbox/{name}/reset` restores that sandbox to the seed and leaves the others untouched. The demo declares `/__sandbox/verification/reset` as the target's `reset_request`, so every state-changing run starts from the same state.
- Writes made by verification therefore never appear in the data the legacy client sees, and a run that sends state-changing traffic is repeatable.
- The sandbox header is added by verification only. The runtime does not send it for legacy-client requests, and adapter rules cannot set it.
- These endpoints exist only on the fictional simulator; they are not part of the NovaPay contract and are excluded from reconstruction evidence.

## Legacy client rules

- Written once against the GravePay contract, then never edited. A test asserts its checksum.
- Default base URL in source: `http://api.gravepay.demo:8000`. `GRAVEPAY_BASE_URL` may override it for the non-Docker runner. The Docker demonstration does not set the override.
- It performs: create a charge, retrieve it, list page 2 with limit 2, trigger an invalid-currency error and handle it by its legacy error code, refund part of the charge it created. It exits non-zero on any unexpected response.

## Phases

| Phase | Command (Docker) | Routing of `api.gravepay.demo` | Outcome |
|---|---|---|---|
| Before | `docker compose --profile before run --rm legacy-client-before` | NovaPay simulator | Client fails |
| After | `docker compose --profile after run --rm legacy-client-after` | API Revenant runtime (host-routed) | Client succeeds |

Without Docker: `python demo/run_demo.py` runs both phases on localhost ports using the environment override.
