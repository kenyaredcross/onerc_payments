# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Check a KCB Buni or Jenga account against the provider's SANDBOX, step by step.

The drivers were built to KCB's and Jenga's documents; these checks are how a real
round-trip is verified once sandbox credentials are in. Each step prints what happened and
what to look at. They refuse a Production account.

    # 1. credentials only (nothing is charged, no one is prompted)
    bench --site SITE execute onerc_payments.sandbox_check.sign_in --kwargs '{"account": "KCB Test Till"}'

    # 2a. KCB: a real sandbox prompt to a phone, then (after answering it) its outcome
    bench --site SITE execute onerc_payments.sandbox_check.kcb_prompt \\
        --kwargs '{"account": "KCB Test Till", "phone": "0712345678", "amount": 1}'
    bench --site SITE execute onerc_payments.sandbox_check.outcome --kwargs '{"transaction": "PAY-2026-00001"}'

    # 2b. Jenga: a sandbox card checkout to open in a browser (Jenga's test cards), then its outcome
    bench --site SITE execute onerc_payments.sandbox_check.card_checkout \\
        --kwargs '{"account": "Jenga Test", "amount": 10, "currency": "KES"}'
    bench --site SITE execute onerc_payments.sandbox_check.outcome --kwargs '{"transaction": "PAY-2026-00002"}'

    # 3. Jenga: what the status query answers for that order - decides whether
    #    "Ask Jenga for Payment Status" can be switched on for the account
    bench --site SITE execute onerc_payments.sandbox_check.card_query --kwargs '{"transaction": "PAY-2026-00002"}'

The site must be reachable from the internet at the account's Public Site Address for the
callback / notification to arrive (e.g. a tunnel to a dev machine).

LIVE (KCB only, real money): for an account set to Production, when there are no sandbox
credentials. Every prompt is KES 1, needs the confirmation phrase, and at most five are sent
per account per day. Pay from your own phone into a till you control.

    bench --site SITE execute onerc_payments.sandbox_check.live_sign_in --kwargs '{"account": "Nairobi Vooma Till"}'
    bench --site SITE execute onerc_payments.sandbox_check.live_kcb_prompt \\
        --kwargs '{"account": "Nairobi Vooma Till", "phone": "0712345678", "confirm": "KES 1 from my own phone"}'
    bench --site SITE execute onerc_payments.sandbox_check.outcome --kwargs '{"transaction": "PAY-2026-00003"}'
"""

import json

import frappe

GATEWAY = "OneRC Payment Gateway"
TRANSACTION = "OneRC Payment Transaction"

LIVE_CONFIRMATION = "KES 1 from my own phone"
LIVE_AMOUNT = 1
LIVE_DAILY_LIMIT = 5


def _say(title, value=None):
	print(f"\n== {title}")
	if value is not None:
		print(value if isinstance(value, str) else json.dumps(value, indent=2, default=str))


def _driver(account, live=False):
	from onerc_payments.gateways import get_gateway
	from onerc_payments.gateways.kcb_buni import KcbBuniGateway

	if not frappe.db.exists(GATEWAY, account):
		frappe.throw(f"No payment gateway called {account}. Save the KCB Buni / Jenga Account first.")
	driver = get_gateway(account, allow_inactive=True)
	if not driver.account:
		frappe.throw(f"{account} is not a KCB Buni or Jenga account.")
	production = (driver.account.environment or "Sandbox") != "Sandbox"
	if production and not live:
		frappe.throw(f"{account} is set to Production. Use the live_* steps for it.")
	if live and not production:
		frappe.throw(f"{account} is set to Sandbox. Use the sandbox steps for it.")
	if live and not isinstance(driver, KcbBuniGateway):
		frappe.throw("Live checks are for KCB Buni accounts only.")
	return driver


def sign_in(account):
	"""Step 1: the account's checks, then a sign-in. Nothing is charged."""
	driver = _driver(account)
	driver.validate_account()
	_say("Account checks", "passed")
	driver.test_connection()
	_say("Sign-in", "accepted: the credentials work")
	if hasattr(driver, "callback_url"):
		_say("KCB will post results to", driver.callback_url())
	if hasattr(driver, "notification_url"):
		_say("Register this IPN address in the JengaHQ portal (with the account's username/password)")
		print(driver.notification_url())


def _start(account, amount, currency, phone=None, live=False, **extra):
	from onerc_payments.api.v1.payment import initiate_payment

	driver = _driver(account, live=live)
	if not frappe.db.get_value(GATEWAY, account, "is_active"):
		frappe.throw(f"Switch the gateway {account} on first (it runs the account checks).")
	result = initiate_payment(
		amount=amount,
		currency=currency,
		direction="Inbound",
		source_app="onerc_payments",
		source_doctype=GATEWAY,
		source_document=account,
		payer_name="Sandbox Check",
		payer_phone=phone,
		gateway=account,
		**extra,
	)
	frappe.db.commit()
	return driver, result


def kcb_prompt(account, phone, amount=1):
	"""Step 2a: send a real sandbox prompt. Answer it on the phone, then run ``outcome``."""
	_gateway, result = _start(account, amount, "KES", phone=phone)
	_show_kcb_start(result)


def live_sign_in(account):
	"""LIVE step 1: the account's checks and a sign-in with the live credentials. No money
	moves and no one is prompted."""
	driver = _driver(account, live=True)
	driver.validate_account()
	_say("Account checks", "passed")
	driver.test_connection()
	_say("Live sign-in", "accepted: the live credentials work from this machine")
	_say("KCB will post results to", driver.callback_url())


def live_kcb_prompt(account, phone, confirm=None):
	"""LIVE step 2: a real KES 1 M-Pesa prompt to ``phone``, paid into the account's till.
	Answer it on the phone (cancel once, pay once), then run ``outcome``."""
	if confirm != LIVE_CONFIRMATION:
		frappe.throw(
			"This sends a real M-Pesa prompt and moves real money. To go ahead, pass "
			f'"confirm": "{LIVE_CONFIRMATION}".'
		)
	_driver(account, live=True)
	sent_today = frappe.db.count(
		TRANSACTION,
		{
			"gateway": account,
			"source_doctype": GATEWAY,
			"source_document": account,
			"creation": [">=", frappe.utils.today()],
		},
	)
	if sent_today >= LIVE_DAILY_LIMIT:
		frappe.throw(f"{sent_today} live checks were already sent from {account} today; that is the limit.")
	_gateway, result = _start(account, LIVE_AMOUNT, "KES", phone=phone, live=True)
	_show_kcb_start(result)


def _show_kcb_start(result):
	_say("Started", result)
	detail = frappe.db.get_value(
		"KCB Buni Payment",
		{"payment_transaction": result["transaction_id"]},
		["invoice_number", "merchant_request_id", "checkout_request_id", "raw_request", "raw_response"],
		as_dict=True,
	)
	_say("What KCB was sent and answered", detail)
	print(
		"\nLook at: MerchantRequestID - random-looking (good) or a sequence like 17684-56147665-1 "
		"(tell the developer); CheckoutRequestID - a ws_CO_... string or false."
	)


def card_checkout(account, amount=10, currency="KES", return_url=None):
	"""Step 2b: start a sandbox card payment and print the page to open in a browser."""
	_gateway, result = _start(account, amount, currency, return_url=return_url)
	_say("Started", result)
	if result.get("checkout_url"):
		_say("Open this in a browser and pay with one of Jenga's sandbox test cards", result["checkout_url"])


def outcome(transaction):
	"""What became of a sandbox payment: the transaction, its detail record, every message."""
	txn = frappe.get_doc(TRANSACTION, transaction)
	_say(
		"Transaction",
		{
			f: txn.get(f)
			for f in (
				"status",
				"gateway",
				"amount",
				"currency",
				"gateway_reference",
				"gateway_receipt",
				"failure_reason",
				"needs_review",
				"review_reason",
			)
		},
	)
	if txn.gateway_detail_doctype and txn.gateway_detail:
		detail = frappe.get_doc(txn.gateway_detail_doctype, txn.gateway_detail).as_dict()
		_say(txn.gateway_detail_doctype, {k: v for k, v in detail.items() if v and not k.startswith("_")})
	logs = frappe.get_all(
		"OneRC Payment Callback",
		filters={"payment_transaction": txn.name},
		fields=["name", "kind", "status", "gateway_confirmation", "note", "payload", "error"],
		order_by="creation asc",
	)
	_say(f"Messages received for it ({len(logs)})", logs or "none yet")
	if not logs:
		print(
			"\nNo message yet. If the payment was made, check that the provider can reach this site "
			"(Public Site Address), and look for Unmatched / Ignored rows in OneRC Payment Callback."
		)


def card_query(transaction):
	"""Step 3: Jenga's status query for a card order, raw, to decide whether to rely on it."""
	txn = frappe.get_doc(TRANSACTION, transaction)
	driver = _driver(txn.gateway)
	answer = driver.query(txn, timeout=20)
	_say("Jenga's status query answered", answer)
	print(
		"\nSwitch on 'Ask Jenga for Payment Status' only if this names the payment's state, "
		"amount AND currency correctly for a paid and for an unpaid card order."
	)
