# GravePay integration notes (fictional)

These notes are part of the demonstration evidence. GravePay is not a real
payment provider. They are stored by API Revenant as documentation; free text
is not analysed in this release.

- Every request carries the header `X-API-Key`.
- Amounts are whole units of the currency; `500` with `ZAR` means R500.
- `reference` is the caller's own identifier. The original documentation said
  a repeated `reference` would not create a second charge, but no recorded
  exchange shows what was actually returned in that case.
- Lists start at `page=1`. Nobody recorded what a page beyond the last one
  returned.
- Requests were rate limited. A `429` was returned when the limit was hit; its
  body and headers were never captured.
- A charge that had already been fully refunded could not be refunded again.
  The error code used is not known.
