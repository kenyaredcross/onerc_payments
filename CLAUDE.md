# onerc_payments

A Frappe v16 app that owns **all payment logic for the OneRC platform**. Other OneRC
apps (prequalification, donations, VMMS, CVA, fundraising) never talk to a payment
gateway directly — they call this app's API and implement a callback hook on their
own document. See `docs/adr/001-payment-gateway-abstraction.md` for the reasoning.

Two design commitments drive everything:

1. **Driver pattern.** Each payment method is a Python class implementing
   `BaseGateway`. Switching gateways (M-Pesa → MTN MoMo → Stripe) is a settings
   change, not a code change. OneRC is deployed by National Societies worldwide, so
   country-specific gateway logic must never leak into a programme app.
2. **ERPNext is optional.** The app works standalone; GL posting is a setting, never
   a dependency.

Currently shipped drivers: **M-Pesa Daraja** (Kenya; STK Push inbound, B2C outbound)
and **Manual** (bank transfer / cash, admin-confirmed — also the fallback for testing
without live credentials).

## Layout

```
onerc_payments/
  api/v1/payment.py       # THE public API — initiate / check status / callback
  gateways/
    base.py               # BaseGateway ABC — the driver contract
    __init__.py           # get_gateway() — loads the active driver by dotted path
    mpesa_daraja.py       # Safaricom Daraja driver
    manual.py             # Manual driver + confirm_payment()
  onerc_payments/doctype/ # the doctypes (note the doubled directory — Frappe module)
  tasks.py                # hourly scheduler: poll Pending transactions
  patches/                # migration patches
  fixtures/               # the two OneRC Payment Gateway records
  tests/test_mpesa_callback.py
```

## Doctypes

| DocType | Role |
|---|---|
| **OneRC Payment Settings** (single) | Active gateway, environment (Sandbox/Production), currency, encrypted M-Pesa credentials, callback base URL, manual-bank details. |
| **OneRC Payment Gateway** | One record per driver. Holds `driver_class` (dotted path) and capability flags. Shipped as a fixture. |
| **OneRC Payment Transaction** | `PAY-{YYYY}-{#####}`. The **gateway-neutral** record: amount, direction, status, payer/recipient, source app/doctype/document, receipt, and a dynamic link (`gateway_detail_doctype` + `gateway_detail`) to the per-gateway detail record. |
| **Mpesa Payment** | Per-gateway detail, keyed by `checkout_request_id`. Full STK payload, result codes, amount cross-check, callback IP, raw request/response/callback. All fields read-only, desk-only. |
| **Manual Payment** | Per-gateway detail, keyed by `payment_transaction`. Who confirmed it and when. |

Gateway-specific data belongs on the detail doctype; the transaction stays generic.
This split is deliberate — adding a gateway must not add fields to the transaction.

## The contract with consumer apps

```python
from onerc_payments.api.v1.payment import initiate_payment

initiate_payment(
    amount, currency, direction,          # "Inbound" | "Outbound"
    source_app, source_doctype, source_document,
    payer_name=..., payer_phone=..., metadata=...,
)
```

On completion this app calls back **into the source document**:

- `on_payment_confirmed(amount, receipt, transaction_id)` — fired once, when the
  payment resolves to Completed.
- `on_payment_receipt(receipt, transaction_id)` — optional, fired only when the
  M-Pesa receipt arrives *after* confirmation (see the race below). Both hooks are
  optional; a missing method is not an error.

`onerc_prequalification` is the live consumer (`required_apps = ["onerc_payments"]`)
— read its `api/payments.py` for a worked example of both hooks.

## Non-obvious behaviour — read before touching the callback path

These are hard-won; the last three commits are all about them.

- **Only the callback carries the M-Pesa receipt.** Safaricom's STK *query* (used by
  the browser poll and the hourly scheduler) tells you whether a payment went
  through, but never returns `MpesaReceiptNumber`. The poll normally wins the race,
  so a callback arriving at an already-Completed transaction is the **normal** case.
  Dropping it as a duplicate lost every receipt — `_absorb_late_receipt()` exists to
  take the receipt without re-firing `on_payment_confirmed` (which would double-post
  a receipt or a GL entry).
- **Idempotency uses a row lock.** `payment_callback()` does
  `get_value(..., for_update=True)` on the transaction status so Safaricom retries
  serialize instead of racing. A retry must never overwrite a good receipt.
- **`ResultCode` is an int on the callback and a string on the query.** Coerce
  (`_as_int`) before comparing — a strict `== 0` read `"0"` as a failure.
- **The callback is matched by payload shape, not just the query param.**
  `gateway_name` rides on the `CallBackURL` query string, which Daraja and reverse
  proxies both mangle; `Body.stkCallback` identifies an STK callback on its own.
- **Callback source verification** (`verify_callback_source`) checks
  `request_ip`, the whole `X-Forwarded-For` chain, and CDN headers against
  Safaricom's published IPs — production only; Sandbox trusts everything so the
  simulator works. Escape hatches live in `site_config.json`:
  `mpesa_extra_allowed_ips` (comma-separated, CIDR ok) and
  `mpesa_verify_callback_ip = 0` (falls back to the unguessable CheckoutRequestID
  match, which is the real security gate).
- **STK push refuses a non-public callback URL.** `_callback_url()` throws rather
  than accept a payment whose receipt can never be delivered — the failure would
  otherwise be silent (payment succeeds, receipt never arrives).
- **Detail-record writes are always best-effort.** Every `record_*` call is wrapped
  in try/except and logged; a detail failure must never block confirming a payment.
  `_upsert_mpesa_payment()` drops `None` values so a later stage can't blank a field
  an earlier stage set.

## Adding a gateway

1. New file in `gateways/`, subclass `BaseGateway`.
2. Implement the four abstract methods: `initiate`, `check_status`,
   `handle_callback`, `generate_receipt`.
3. Optionally override the hooks that default to no-ops:
   `verify_callback_source`, `record_initiation_details`, `record_payment_details`,
   `record_status_update`.
4. Add an `OneRC Payment Gateway` fixture record with the dotted `driver_class`.
5. Add credential fields to `OneRC Payment Settings` if needed.
6. Teach `_extract_gateway_reference()` in `api/v1/payment.py` how to find your
   reference in a callback payload.

## Commands

```bash
cd /home/nigel/frappe/main-bench
bench --site <site> run-tests --app onerc_payments   # needs allow_tests=true
bench --site <site> migrate                          # after doctype/patch changes
bench --site <site> export-fixtures --app onerc_payments
```

Sites in this bench: `mysite.localhost`, `donations.localhost`, `vmms.localhost`,
`localisation.localhost`. CI (`.github/workflows/ci.yml`) installs the app on a
fresh site and runs the same test command.

Formatting is enforced by pre-commit: **ruff** (tabs, double quotes, line length
110), eslint, prettier. Target Python 3.14.

## Known gaps

- `post_to_erpnext_gl` and `erpnext_payment_entry` exist as fields but **nothing
  reads them** — the ERPNext GL integration from ADR-001 is not implemented yet.
- `generate_receipt()` is implemented by both drivers but never called anywhere.
- Outbound B2C reuses `mpesa_passkey` as the `SecurityCredential`; Daraja treats
  these as different credentials, so B2C is unverified against a live shortcode.
- The doctype controllers are all empty `pass` classes; validation lives in the API
  and driver layers, not on the documents.
