# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""The three web addresses of a Jenga card payment. See ``gateways/jenga.py`` for the flow.

- ``checkout``: the payer's browser comes here from the app (``checkout_url``); we sign in
  to Jenga, sign the order and post the browser on to Jenga's card page.
- ``jenga_return``: Jenga sends the payer's browser back here. Anyone can call this with
  anything, so it settles nothing: it is logged, and at most makes us ask Jenga.
- ``jenga_notification``: Jenga's server posts the result here with the HTTP Basic
  credentials set on the Jenga Account (and registered in the JengaHQ portal). Only this
  - or Jenga's status query, where switched on - completes or fails a card payment.
"""

import html
import json
import secrets

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import cint, get_datetime, now_datetime, time_diff_in_seconds
from werkzeug.wrappers import Response

from onerc_payments.api.v1 import payment as pay
from onerc_payments.gateways import get_gateway

NOTIFICATION = "Card notification"
RETURN = "Card return"
DETAIL = "Jenga Payment"
TRANSACTION = "OneRC Payment Transaction"

# Jenga's page gives the payer 15 minutes (``paymentTimeLimit``). A checkout opened later
# than this after the payment started could outlive CHECKOUT_EXPIRY_MINUTES, when an
# unanswered payment is Cancelled - so it is not opened; the payer starts again.
CHECKOUT_OPENS_FOR_MINUTES = 30


# ── pages ───────────────────────────────────────────────────────────────────


def _page(title, body, *, status=200, script=None, form_action=None):
	nonce = secrets.token_urlsafe(16)
	csp = "; ".join(
		[
			"default-src 'none'",
			"style-src 'unsafe-inline'",
			f"script-src 'nonce-{nonce}'",
			f"form-action {form_action}" if form_action else "form-action 'none'",
			"base-uri 'none'",
			"frame-ancestors 'none'",
		]
	)
	script_tag = f'<script nonce="{nonce}">{script}</script>' if script else ""
	document = (
		"<!doctype html><html lang='en'><head><meta charset='utf-8'>"
		"<meta name='viewport' content='width=device-width,initial-scale=1'>"
		f"<title>{html.escape(title)}</title>"
		"<style>body{font-family:system-ui,-apple-system,'Segoe UI',Roboto,sans-serif;margin:0;"
		"background:#f6f7f9;color:#14171a}main{max-width:440px;margin:12vh auto;padding:24px;"
		"text-align:center}h1{font-size:1.25rem;margin:0 0 8px}p{color:#4a5560;line-height:1.5;margin:0 0 12px}"
		"button,a.button{display:inline-block;margin-top:12px;padding:10px 18px;border-radius:8px;"
		"border:0;background:#1f2933;color:#fff;font:inherit;text-decoration:none;cursor:pointer}"
		".sandbox{background:#fff4e5;border:1px solid #f5c27a;color:#7a4a00;padding:8px 12px;"
		"border-radius:8px;font-size:.875rem;margin-bottom:16px}</style></head>"
		f"<body><main>{body}</main>{script_tag}</body></html>"
	)
	return Response(
		document,
		status=status,
		content_type="text/html; charset=utf-8",
		headers={
			"Content-Security-Policy": csp,
			"Cache-Control": "no-store",
			# The origin only: our address may be checked by Jenga, the order reference not leaked.
			"Referrer-Policy": "strict-origin",
			"X-Content-Type-Options": "nosniff",
		},
	)


def _message(title, text, status=200):
	return _page(title, f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>", status=status)


def _script_value(value):
	return json.dumps(value).replace("<", "\\u003c").replace(">", "\\u003e")


def _detail_by_order(reference):
	reference = str(reference or "").strip()
	if not reference or not reference.isalnum() or len(reference) > 40:
		return None
	return frappe.db.get_value(
		DETAIL,
		{"order_reference": reference},
		["name", "payment_transaction", "gateway", "return_url", "checkout_opens"],
		as_dict=True,
	)


# ── 1. checkout ────────────────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=120, seconds=600)
def checkout(ref=None):
	from onerc_payments.gateways.jenga import JengaGateway

	detail = _detail_by_order(ref)
	if not detail:
		return _message("Payment not found", "This payment link is not valid. Start the payment again.", 404)

	transaction = frappe.get_doc(TRANSACTION, detail.payment_transaction)
	if transaction.status == "Completed":
		return _message("Already paid", "This payment has already been received. Thank you.")
	if transaction.status != "Pending":
		return _message("Payment closed", "This payment can no longer be paid. Start the payment again.")
	if (
		time_diff_in_seconds(now_datetime(), get_datetime(transaction.creation))
		> CHECKOUT_OPENS_FOR_MINUTES * 60
	):
		return _message("Payment link expired", "This payment link has expired. Start the payment again.")

	try:
		# Active only: switching card payments off stops new checkouts at once.
		driver = get_gateway(transaction.gateway)
		if not isinstance(driver, JengaGateway):
			return _message("Payment not found", "This payment is not a card payment.", 404)
		action, fields = driver.checkout_form(transaction)
	except Exception:
		# Deferred: this is a GET request, and Frappe rolls those back at the end.
		frappe.log_error(title=f"Card checkout could not be opened for {transaction.name}", defer_insert=True)
		return _message(
			"Card payment unavailable",
			"We could not open the card payment page. Close this window and try again, or pay another way.",
			503,
		)

	frappe.db.set_value(
		DETAIL,
		detail.name,
		{"checkout_opened_on": now_datetime(), "checkout_opens": cint(detail.checkout_opens) + 1},
		update_modified=False,
	)
	frappe.db.commit()

	inputs = "".join(
		f'<input type="hidden" name="{html.escape(name)}" value="{html.escape(str(value))}">'
		for name, value in fields.items()
	)
	sandbox = (
		"<div class='sandbox'>Test mode: no real card will be charged.</div>"
		if (driver.account.environment or "Sandbox") != "Production"
		else ""
	)
	body = (
		f"{sandbox}<h1>Taking you to the secure card page</h1>"
		"<p>If nothing happens in a few seconds, press Continue.</p>"
		f'<form id="pay" method="post" action="{html.escape(action)}">{inputs}'
		"<button type='submit'>Continue</button></form>"
	)
	return _page(
		"Card payment",
		body,
		script="document.getElementById('pay').submit();",
		# Any https address: browsers apply form-action to the redirects that follow the
		# post too, and Jenga's page may move the payer on (3-D Secure, the card's bank).
		form_action="https:",
	)


# ── 2. the payer's browser comes back ──────────────────────────────────────


def _order_reference_in(data):
	for key in ("orderReference", "orderRef", "order_reference"):
		value = str((data or {}).get(key) or "").strip()
		if value:
			return value
	return None


@frappe.whitelist(allow_guest=True, methods=["GET", "POST"])
@rate_limit(limit=120, seconds=600)
def jenga_return(**kwargs):
	raw, data = pay._read_body()
	reference = _order_reference_in(data)
	log = pay._open_log(RETURN, raw, None, reference, False)
	pay._run_callback(RETURN, data, None, log)
	frappe.db.commit()

	detail = _detail_by_order(reference)
	status = frappe.db.get_value(TRANSACTION, detail.payment_transaction, "status") if detail else None
	return _result_page(status, detail)


def process_return(data, log):
	"""Note the payer's return. It decides nothing: if the account's status query is on and
	the payment is still open, Jenga is asked - that answer is Jenga's own."""
	detail = _detail_by_order(_order_reference_in(data))
	if not detail:
		pay._update_log(
			log, status="Unmatched", note="The browser came back with an order reference we do not have."
		)
		return

	pay._update_log(log, payment_transaction=detail.payment_transaction, gateway=detail.gateway)
	frappe.db.set_value(DETAIL, detail.name, "raw_return", frappe.as_json(data), update_modified=False)
	note = (
		"The payer's browser came back from Jenga's page. That decides nothing by itself; only "
		"Jenga's notification or its status query does."
	)

	transaction = frappe.get_doc(TRANSACTION, detail.payment_transaction)
	if transaction.status == "Pending":
		driver = get_gateway(transaction.gateway, allow_inactive=True)
		if cint((driver.account or {}).get("query_status")) and pay._due_for_gateway_query(transaction):
			pay.refresh_status(transaction)
			note += " Jenga was asked for the payment's status."
	pay._update_log(log, status="Processed", note=note)


def _result_page(status, detail):
	if status == "Completed":
		title, text = "Payment received", "Thank you. Your card payment went through."
	elif status == "Failed":
		title, text = "Payment not completed", "The card payment did not go through."
	elif status == "Cancelled":
		title, text = (
			"Payment not completed",
			"This payment was not completed in time. If your card was charged, keep the bank's "
			"reference and contact us: it will be matched to your payment.",
		)
	elif status:
		title, text = (
			"Confirming your payment",
			"We are waiting for the bank to confirm your payment. You can close this window.",
		)
	else:
		title, text = "Payment not found", "We could not find this payment."

	transaction = detail.payment_transaction if detail else None
	return_url = (detail.return_url if detail else None) or None
	if return_url:
		separator = "&" if "?" in return_url else "?"
		return_url = f"{return_url}{separator}payment={transaction}"

	body = f"<h1>{html.escape(title)}</h1><p>{html.escape(text)}</p>"
	if return_url:
		body += f'<a class="button" href="{html.escape(return_url)}">Continue</a>'
	else:
		body += "<button type='button' id='close'>Close this window</button>"

	script = (
		"try{if(window.opener){window.opener.postMessage("
		f"{{source:'onerc_payments',transaction:{_script_value(transaction)},status:{_script_value(status)}}},"
		"window.location.origin);}}catch(e){}"
	)
	if return_url:
		script += f"setTimeout(function(){{window.location.replace({_script_value(return_url)});}},1500);"
	else:
		script += "var c=document.getElementById('close');if(c){c.onclick=function(){window.close();};}"
	return _page(title, body, script=script)


# ── 3. Jenga's notification ────────────────────────────────────────────────


@frappe.whitelist(allow_guest=True, methods=["POST"])
def jenga_notification(**kwargs):
	"""Saved first, like every callback. A sender without the right credentials gets 401,
	so a misconfigured password shows in the JengaHQ portal and Jenga retries; what it sent
	is kept but changes nothing."""
	from onerc_payments.gateways.jenga import JengaGateway

	raw, data = pay._read_body()
	account = JengaGateway.authenticated_account()
	log = pay._open_log(
		NOTIFICATION, raw, account, pay._reference_or_none(account, data, NOTIFICATION), bool(account)
	)
	pay._run_callback(NOTIFICATION, data, account, log)
	if not account:
		frappe.local.response["http_status_code"] = 401
		return {"status": "Unauthorised"}
	return {"status": "Received"}
