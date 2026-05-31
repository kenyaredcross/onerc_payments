# ADR-001: Payment Gateway Abstraction via Driver Pattern

**Date:** 2026-05-30
**Status:** Accepted
**Deciders:** Kelvin Njenga, OneRC 

---

## Context

Multiple OneRC apps require payment functionality:
- onerc_donations — inbound donations via mobile money
- onerc_prequalification — inbound application fees
- onerc_vmms — outbound volunteer payments, inbound membership subscriptions and events payments
- onerc_cva — outbound cash disbursements to beneficiaries
- onerc_fundraising — inbound fundraising collections

OneRC is designed to be installed by any Organization or National Society globally.
Payment gateways vary by country:
- Kenya: M-Pesa (Safaricom Daraja)
- Uganda, Rwanda, Ghana, Cameroon: MTN Mobile Money
- Senegal, Mali, Côte d'Ivoire: Orange Money / Wave
- International: Stripe, PayPal, bank transfer

Each app independently implementing its own gateway integration
would result in duplicated code, inconsistent payment records,
country-specific logic embedded in programme apps, and bugs
that must be fixed in six places instead of one.

## Decision

Create a dedicated `onerc_payments` Frappe app that:

1. Owns all payment logic — no other OneRC app implements
   gateway calls directly
2. Implements a gateway driver pattern — each payment method
   is a Python class implementing a common BaseGateway interface
3. Is configured entirely via settings — the admin selects
   the active gateway and enters credentials; no code changes
   are required to switch gateways
4. Handles both directions — inbound (collecting money) and
   outbound (disbursing money), enabling CVA and volunteer
   payments through the same abstraction
5. Is optionally integrated with ERPNext — if ERPNext is
   installed and the setting is enabled, successful payments
   create a Payment Entry in the GL; this is never required

All OneRC apps that need payments declare `onerc_payments`
as a dependency and call its API exclusively.

## Options Considered

### Option A — Per-app implementation
Each app implements its own M-Pesa or gateway code.

Rejected: duplicated logic, country-specific code in programme
apps, bugs fixed in one place but not others, impossible to
add a new gateway retroactively across all apps.

### Option B — Navari frappe-mpesa-payments
Use the existing community library for M-Pesa.

Rejected: requires ERPNext as a hard dependency, Kenya-only,
no outbound payment support, tightly coupled to ERPNext's
accounting layer which many deployments will not use.

### Option C — Frappe native payments app
Use frappe/payments from the Frappe ecosystem.

Rejected: insufficient African mobile money gateway support,
not designed for outbound humanitarian disbursements, adds
complexity for organisations that do not need card payments.

### Option D — onerc_payments with driver pattern ✅ Chosen
Dedicated app, abstract interface, pluggable drivers,
settings-driven configuration.

Chosen because: works without ERPNext, supports any gateway
via a driver, handles both directions, single place to fix
bugs, zero code changes to switch gateways, consistent
payment records across all apps.

## Consequences

**Positive:**
- Any National Society can install OneRC apps and configure
  their local payment gateway in under five minutes
- A bug fix in the M-Pesa driver fixes it for all apps
- Adding MTN MoMo support automatically gives it to every
  app that uses onerc_payments
- CVA disbursements and fee collection share the same
  infrastructure and records
- Optional ERPNext integration satisfies organisations that
  need accounting without forcing it on those that do not

**Negative:**
- onerc_payments must be installed before any app that
  depends on it — adds one step to deployment
- Existing onerc_donations M-Pesa code must be migrated
  to use onerc_payments (planned, not immediate)

**Risks:**
- Gateway driver quality depends on contributor knowledge
  of each gateway's API — mitigated by requiring tests
  and a manual driver that works everywhere as fallback
- ERPNext optional integration may drift as ERPNext
  updates — mitigated by keeping the integration thin
  and version-pinned

## Review Date

Revisit if a National Society requires a gateway that
does not fit the driver pattern (e.g. USSD-only gateway
with no callback URL support).