# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""
The single entry point for all OneRC payment operations.

Every OneRC app that needs payments calls initiate_payment().
Nothing else. The gateway, credentials, and flow are handled here.
"""

import frappe
from frappe.rate_limiter import rate_limit
from frappe.utils import now_datetime
from onerc_payments.gateways import get_gateway


def normalize_phone(phone):
	"""Normalize a Kenyan mobile number to E.164 (254XXXXXXXXX, 12 chars).

	Accepts '0712345678', '+254712345678', '254712345678', '712345678'.
	Returns the canonical form, or None for empty input. Other formats are
	returned digits-only so callers still get something to store.
	"""
	if not phone:
		return None
	digits = "".join(ch for ch in str(phone) if ch.isdigit())
	if not digits:
		return None
	if digits.startswith("254") and len(digits) == 12:
		return digits
	if digits.startswith("0") and len(digits) == 10:
		return "254" + digits[1:]
	if len(digits) == 9:
		return "254" + digits
	return digits


def _client_ip():
	"""Originating IP of the current request, for callback audit logging."""
	return getattr(frappe.local, "request_ip", None) or ""


@frappe.whitelist()
def initiate_payment(
	amount,
	currency,
	direction,
	source_app,
	source_doctype,
	source_document,
	payer_name=None,
	payer_phone=None,
	payer_email=None,
	recipient_name=None,
	recipient_phone=None,
	recipient_account=None,
	metadata=None,
	gateway=None,
):
	"""
	Initiate a payment. Called by any OneRC app.

	`gateway` names which gateway to collect through. Omitted, the one set as
	active in OneRC Payment Settings is used, so every existing caller behaves
	exactly as it did. It exists because a site may keep several gateways live
	at once and let the payer pick: an organisation taking both M-Pesa and bank
	transfer offers both, and a single site-wide setting can only answer for one
	of them — which meant a payer with no mobile money account had no way to say
	so. A named gateway that is not active is refused; see `get_gateway`.

	Returns:
	{
	    "transaction_id": "PAY-2026-00001",
	    "status": "Pending",
	    "gateway_reference": "...",
	    "message": "Check your phone and enter your M-Pesa PIN",
	}
	"""
	settings = frappe.get_single("OneRC Payment Settings")

	# Resolved before the transaction is written, so a bad gateway name is
	# refused without leaving an Initiated row nobody will ever resolve.
	driver = get_gateway(gateway)

	transaction = frappe.get_doc({
		"doctype": "OneRC Payment Transaction",
		"gateway": (gateway or "").strip() or settings.active_gateway,
		"direction": direction,
		"status": "Initiated",
		"amount": float(amount),
		"currency": currency or settings.default_currency or "KES",
		"source_app": source_app,
		"source_doctype": source_doctype,
		"source_document": source_document,
		"payer_name": payer_name,
		"phone_number": normalize_phone(payer_phone),
		"email": payer_email,
		"recipient_name": recipient_name,
		"recipient_phone": normalize_phone(recipient_phone),
		"recipient_account": recipient_account,
		"metadata": metadata,
	})
	transaction.insert(ignore_permissions=True)

	result = driver.initiate(transaction)

	if result.get("success"):
		transaction.status = "Pending"
		transaction.gateway_reference = result.get("gateway_reference")
	else:
		transaction.status = "Failed"
		transaction.failure_reason = result.get("message")

	# Gateway-specific initiation data (merchant ids, raw request/response) lives on
	# the per-gateway detail doctype, not the generic transaction. Best-effort: a
	# detail-record failure must never break initiating the payment.
	try:
		driver.record_initiation_details(transaction, result)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: failed to record initiation details for {transaction.name}: {e}"
		)

	transaction.save(ignore_permissions=True)

	return {
		"transaction_id": transaction.name,
		"status": transaction.status,
		"gateway_reference": transaction.gateway_reference,
		"message": result.get("message"),
	}


@frappe.whitelist()
@rate_limit(limit=60, seconds=60)
def check_payment_status(transaction_id):
	"""
	Check the current status of a payment.
	Called by frontend polling loops (rate-limited per IP). When invoked
	internally by the scheduler there is no request, so the limit is skipped.
	"""
	transaction = frappe.get_doc("OneRC Payment Transaction", transaction_id)
	# Snapshot the doc before the gateway sees it. check_status is free to enrich the
	# transaction in place (gateway reference, receipt, failure detail) even when the
	# status itself has not moved, and those writes must not be dropped.
	#
	# Compared by value rather than through a framework dirty-check: this line used to
	# call Document.is_dirty(), which upstream removed, and every poll then died with
	# AttributeError - a 500 on the endpoint the vendor portal polls to learn that its
	# payment cleared. A local snapshot cannot be taken away from us.
	before = transaction.as_dict(no_default_fields=True)
	# The gateway that *collected* this, not whichever one the site has active
	# now. Those were the same thing until a caller could name one, and they stop
	# being the same the moment an organisation takes both M-Pesa and bank
	# transfer: polling an M-Pesa payment with the manual driver would report it
	# as unresolved for ever. Falls back to the active gateway for a transaction
	# written before the column carried anything.
	gateway = get_gateway(transaction.gateway)
	result = gateway.check_status(transaction)

	if result["status"] != transaction.status:
		transaction.status = result["status"]
		if result.get("gateway_receipt"):
			transaction.gateway_receipt = result["gateway_receipt"]
			transaction.transaction_date = now_datetime()
		if result.get("failure_reason"):
			transaction.failure_reason = result["failure_reason"]

		# Keep the per-gateway detail record (e.g. Mpesa Payment) in step with the
		# transaction. Without this, a payment resolved by polling rather than by the
		# callback would leave its detail record stuck on "Initiated". Best-effort:
		# a detail-sync failure must never block resolving the payment itself.
		try:
			gateway.record_status_update(transaction, result)
		except Exception as e:
			frappe.logger().error(
				f"onerc_payments: failed to sync gateway detail for "
				f"{transaction.name}: {e}"
			)

		transaction.save(ignore_permissions=True)

		if result["status"] == "Completed":
			_notify_source_app(transaction)
	elif transaction.as_dict(no_default_fields=True) != before:
		transaction.save(ignore_permissions=True)

	return {
		"transaction_id": transaction.name,
		"status": transaction.status,
		"gateway_receipt": transaction.gateway_receipt,
		"failure_reason": transaction.failure_reason,
	}


@frappe.whitelist()
def reconcile_mpesa_payment(mpesa_payment):
	"""Desk action: re-query the gateway for a single Mpesa Payment and sync it.

	Lets an admin resolve a record that is still showing "Initiated"/"Pending"
	(e.g. because the STK callback never reached us) straight from the form.
	Note: the STK status query returns the payment's status but not the M-Pesa
	receipt number - only the callback carries that - so this confirms whether a
	payment went through, but the receipt itself still depends on the callback.
	"""
	frappe.has_permission("Mpesa Payment", "read", doc=mpesa_payment, throw=True)
	transaction_id = frappe.db.get_value(
		"Mpesa Payment", mpesa_payment, "payment_transaction"
	)
	if not transaction_id:
		frappe.throw("This Mpesa Payment is not linked to a payment transaction.")
	return check_payment_status(transaction_id)


@frappe.whitelist(allow_guest=True)
def payment_callback(gateway_name=None, **kwargs):
	import json

	gateway = get_gateway()

	# Reject callbacks that don't originate from the gateway's trusted source
	# (e.g. Safaricom's published IPs in production). Drivers that can't verify
	# the source return True by default, so other gateways are unaffected.
	if not gateway.verify_callback_source():
		frappe.log_error(
			title="M-Pesa callback rejected (untrusted source)",
			message=(
				f"A payment callback was rejected because its source could not be "
				f"trusted, so its receipt was not stored.\n\n"
				f"request_ip={_client_ip()}\n"
				f"X-Forwarded-For={frappe.get_request_header('X-Forwarded-For')}\n"
				f"CF-Connecting-IP={frappe.get_request_header('CF-Connecting-IP')}\n"
				f"X-Real-IP={frappe.get_request_header('X-Real-IP')}\n"
				f"gateway={gateway_name}\n\n"
				f"If this was a genuine Safaricom callback, add its real source IP to "
				f"the site_config key mpesa_extra_allowed_ips (comma-separated, CIDR "
				f"supported). If your proxy chain can't expose the origin IP at all, "
				f"set mpesa_verify_callback_ip = 0 to rely on the CheckoutRequestID "
				f"match instead."
			),
		)
		frappe.local.response["http_status_code"] = 403
		return {"ResultCode": 1, "ResultDesc": "Forbidden"}

	try:
		raw = frappe.request.data
		data = json.loads(raw) if raw else {}
	except Exception:
		data = {}

	if not data:
		data = {k: v for k, v in frappe.form_dict.items() if k != "cmd"}

	if not gateway_name:
		gateway_name = data.get("gateway_name", "")

	gateway_reference = _extract_gateway_reference(gateway_name, data)

	if not gateway_reference:
		frappe.log_error(
			title="M-Pesa callback: no reference found",
			message=(
				f"A callback was received but no gateway reference could be "
				f"extracted, so it could not be matched to a payment.\n"
				f"gateway={gateway_name}\ndata={data}"
			),
		)
		return {"ResultCode": 0, "ResultDesc": "Accepted"}

	transaction_name = frappe.db.get_value(
		"OneRC Payment Transaction",
		{"gateway_reference": gateway_reference},
		"name",
	)

	if not transaction_name:
		frappe.log_error(
			title="M-Pesa callback: no matching transaction",
			message=(
				f"A callback with reference {gateway_reference} could not be linked "
				f"to any initiated payment, so its receipt was not stored."
			),
		)
		return {"ResultCode": 0, "ResultDesc": "Accepted"}

	# Idempotency: Safaricom retries callbacks until it gets a 200, and a retry
	# may race the original. Lock the row (SELECT ... FOR UPDATE) so concurrent
	# duplicates serialize; if the transaction is already resolved we must not run
	# the status transition again, or the source-app hook (receipt / GL posting)
	# fires twice.
	#
	# But we cannot simply drop the callback either. The STK *query* used by the
	# status poll resolves a payment to Completed while carrying no receipt - only
	# this callback ever carries MpesaReceiptNumber. The poll (browser, every few
	# seconds; scheduler, every few minutes) usually wins that race, so returning
	# here threw the receipt away for good, and every payment showed a blank M-Pesa
	# code. Absorb what only the callback knows, then stop.
	current_status = frappe.db.get_value(
		"OneRC Payment Transaction",
		transaction_name,
		"status",
		for_update=True,
	)
	already_resolved = current_status in ("Completed", "Failed", "Cancelled", "Refunded")

	transaction_doc = frappe.get_doc(
		"OneRC Payment Transaction", transaction_name
	)

	# Re-resolved to the gateway that collected this payment, now that we know
	# which payment it is. The driver above was only ever for
	# `verify_callback_source`, which has to run before the transaction is known
	# and must therefore not be chosen by anything in the request — `gateway_name`
	# arrives from the caller, and a callback that could pick its own driver could
	# pick one whose source check passes.
	gateway = get_gateway(transaction_doc.gateway)

	result = gateway.handle_callback(data, transaction_doc)

	if already_resolved:
		_absorb_late_receipt(gateway, data, transaction_doc, result)
		return {"ResultCode": 0, "ResultDesc": "Accepted"}

	# Neutral fields stay on the transaction; the raw gateway payload, result codes
	# and callback IP are captured on the per-gateway detail record below.
	transaction_doc.status = result["status"]

	if result.get("gateway_receipt"):
		transaction_doc.gateway_receipt = result["gateway_receipt"]
		transaction_doc.transaction_date = (
			result.get("transaction_date") or now_datetime()
		)

	if result.get("failure_reason"):
		transaction_doc.failure_reason = result["failure_reason"]

	# Capture the full gateway payload (e.g. the M-Pesa receipt + metadata) into a
	# dedicated detail record and link it here. Best-effort: a failure to record the
	# detail must never block confirming the payment itself.
	try:
		gateway.record_payment_details(data, transaction_doc)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: failed to record gateway payment details for "
			f"{transaction_doc.name}: {e}"
		)

	transaction_doc.save(ignore_permissions=True)
	frappe.db.commit()

	if result["status"] == "Completed":
		_notify_source_app(transaction_doc)

	return {"ResultCode": 0, "ResultDesc": "Accepted"}


def _absorb_late_receipt(gateway, data, transaction, result):
	"""Take the receipt off a callback for a payment the poll already resolved.

	The status transition and ``on_payment_confirmed`` have already happened, so we
	deliberately do NOT re-run them - that would post a receipt or a GL entry twice.
	What we do take is the one thing the callback alone can tell us: the M-Pesa code
	(and the full payload, onto the gateway detail record). The source app is then
	given the receipt it never got, via the optional ``on_payment_receipt`` hook.
	"""
	receipt = result.get("gateway_receipt")

	if receipt and not transaction.gateway_receipt:
		_store_receipt(transaction, receipt, result.get("transaction_date") or now_datetime())

	# Best-effort: the raw payload / result codes belong on the per-gateway detail
	# record whether or not a receipt came with them (a failed callback is evidence too).
	try:
		gateway.record_payment_details(data, transaction)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: failed to record late gateway payment details for "
			f"{transaction.name}: {e}"
		)

	frappe.db.commit()

	if receipt:
		_notify_source_receipt(transaction, receipt)


def _store_receipt(transaction, receipt, transaction_date=None):
	"""Write a receipt onto a transaction that is already resolved.

	``db_set`` without touching ``modified``: the status transition happened earlier
	and the record should still say when. ``transaction_date`` is set only when the
	source knows when the money moved; a code typed in by staff does not, and the
	source app then falls back to the date it recorded the payment.
	"""
	transaction.db_set("gateway_receipt", receipt, update_modified=False)
	if transaction_date:
		transaction.db_set("transaction_date", transaction_date, update_modified=False)
	transaction.gateway_receipt = receipt


# ── RECOVERING A RECEIPT THE CALLBACK NEVER DELIVERED ──────────────────────
#
# A payment confirmed by the status poll has no receipt until the callback comes.
# If the callback is lost, staff can type the code from the payer's message, or
# ask the gateway to look it up. Either way the receipt only ever fills a blank:
# a receipt already on a transaction is never replaced, and the source app hears
# about it through ``on_payment_receipt`` exactly as for a late callback.


def _assert_receipt_missing(transaction):
	if transaction.status != "Completed":
		frappe.throw(
			f"{transaction.name} is {transaction.status}, not Completed. Only a "
			"completed payment can be given its receipt."
		)
	if transaction.gateway_receipt:
		frappe.throw(f"{transaction.name} already has receipt {transaction.gateway_receipt}.")


def _receipt_used_by(receipt, transaction_name):
	"""The other transaction that already carries this receipt, if any."""
	return frappe.db.get_value(
		"OneRC Payment Transaction",
		{"gateway_receipt": receipt, "name": ("!=", transaction_name)},
		"name",
	)


@frappe.whitelist()
def receipt_options(transaction_name):
	"""What the desk may offer for a transaction with no receipt."""
	transaction = frappe.get_doc("OneRC Payment Transaction", transaction_name)
	frappe.has_permission("OneRC Payment Transaction", ptype="read", doc=transaction, throw=True)

	missing = transaction.status == "Completed" and not transaction.gateway_receipt
	if not missing or not transaction.gateway:
		return {"missing": False, "can_enter": False, "can_request": False}

	try:
		gateway = get_gateway(transaction.gateway)
	except Exception:
		return {"missing": True, "can_enter": False, "can_request": False}

	return {
		"missing": True,
		"can_enter": bool(getattr(gateway, "accepts_entered_receipt", False)),
		"can_request": bool(gateway.can_request_receipt()),
		"last_requested_on": frappe.db.get_value(
			"Mpesa Payment",
			{"payment_transaction": transaction.name},
			"status_query_requested_on",
		) if transaction.gateway_detail_doctype == "Mpesa Payment" else None,
	}


@frappe.whitelist(methods=["POST"])
def record_gateway_receipt(transaction_name, receipt):
	"""Staff enter the receipt from the payer's message, e.g. the M-Pesa SMS.

	Write permission on the transaction is the gate, as for ``confirm_payment``.
	The code is checked for shape by the gateway and refused if another
	transaction already carries it, which is the one mistake that matters: the
	same SMS used to receipt two payments.
	"""
	transaction = frappe.get_doc("OneRC Payment Transaction", transaction_name)
	frappe.has_permission("OneRC Payment Transaction", ptype="write", doc=transaction, throw=True)

	# Serialise against a late callback or status result landing at the same time.
	frappe.db.get_value("OneRC Payment Transaction", transaction.name, "status", for_update=True)
	transaction.reload()
	_assert_receipt_missing(transaction)

	gateway = get_gateway(transaction.gateway)
	code = gateway.normalise_receipt(receipt)

	other = _receipt_used_by(code, transaction.name)
	if other:
		frappe.throw(f"Receipt {code} is already recorded on {other}. Check the code with the payer.")

	_store_receipt(transaction, code)

	try:
		gateway.record_entered_receipt(transaction, code)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: failed to note entered receipt on the detail record of {transaction.name}: {e}"
		)

	transaction.add_comment(
		"Info",
		f"Receipt {code} entered by {frappe.utils.get_fullname(frappe.session.user)} "
		"from the payer's message.",
	)

	_notify_source_receipt(transaction, code)
	return {"receipt": code}


@frappe.whitelist(methods=["POST"])
def request_gateway_receipt(transaction_name):
	"""Ask the gateway to look up the receipt. The answer arrives later, on
	``receipt_query_result``; the form is reloaded by the person to see it."""
	transaction = frappe.get_doc("OneRC Payment Transaction", transaction_name)
	frappe.has_permission("OneRC Payment Transaction", ptype="write", doc=transaction, throw=True)
	_assert_receipt_missing(transaction)

	gateway = get_gateway(transaction.gateway)
	if not gateway.can_request_receipt():
		frappe.throw("This payment method is not set up to look up receipts.")

	gateway.request_receipt(transaction)
	return {"requested": True}


def _request_json():
	import json

	try:
		raw = frappe.request.data
		return json.loads(raw) if raw else {}
	except Exception:
		return {}


@frappe.whitelist(allow_guest=True, methods=["POST"])
def receipt_query_result(**kwargs):
	"""Daraja posts the answer to a Transaction Status query here (also its queue
	timeout).

	The result is matched by its ConversationID, which Safaricom issued to us when
	we asked and which nobody else has seen, and then the source IP is checked
	exactly as for the STK callback. A receipt is taken only if the lookup is
	unambiguous: the amount matches what we asked for and no other transaction
	already carries the code. Anything else is logged for a person to look at,
	because a wrong code on a receipt is worse than a blank one.
	"""
	accepted = {"ResultCode": 0, "ResultDesc": "Accepted"}
	data = _request_json()
	conversation_id = ((data.get("Result") or {}).get("ConversationID")) if isinstance(data, dict) else None

	detail = frappe.db.get_value(
		"Mpesa Payment",
		{"status_query_conversation_id": conversation_id},
		["name", "payment_transaction"],
		as_dict=True,
	) if conversation_id else None

	if not detail or not detail.payment_transaction:
		frappe.log_error(
			title="M-Pesa receipt lookup: no matching request",
			message=f"A Transaction Status result for ConversationID {conversation_id} matched no lookup we sent.",
		)
		return accepted

	transaction = frappe.get_doc("OneRC Payment Transaction", detail.payment_transaction)
	gateway = get_gateway(transaction.gateway)

	if not gateway.verify_callback_source():
		frappe.log_error(
			title="M-Pesa receipt lookup rejected (untrusted source)",
			message=(
				f"A Transaction Status result for {transaction.name} came from a source that "
				f"could not be trusted, so it was ignored.\nrequest_ip={_client_ip()}\n"
				f"X-Forwarded-For={frappe.get_request_header('X-Forwarded-For')}"
			),
		)
		frappe.local.response["http_status_code"] = 403
		return {"ResultCode": 1, "ResultDesc": "Forbidden"}

	parser = getattr(gateway, "parse_receipt_result", None)
	parsed = parser(data) if parser else {}

	frappe.db.set_value(
		"Mpesa Payment", detail.name, "raw_status_result", frappe.as_json(data), update_modified=False
	)

	status = frappe.db.get_value("OneRC Payment Transaction", transaction.name, "status", for_update=True)
	transaction.reload()
	receipt = parsed.get("receipt")

	if receipt and transaction.gateway_receipt == receipt:
		# The callback or a person got there first, with the same code.
		frappe.db.commit()
		return accepted

	problem = None
	if not receipt:
		problem = (
			f"Safaricom did not return a completed transaction for {transaction.name}: "
			f"{parsed.get('result_description') or 'no detail'}."
		)
	elif status != "Completed" or transaction.gateway_receipt:
		problem = (
			f"{transaction.name} is {status} with receipt {transaction.gateway_receipt or 'none'}; "
			f"the lookup returned {receipt}, which was not applied."
		)
	elif parsed.get("amount") is not None and _amount(parsed["amount"]) != _amount(transaction.amount):
		problem = (
			f"The lookup for {transaction.name} returned {receipt} for {parsed['amount']}, but the "
			f"payment was for {transaction.amount}. The receipt was not applied."
		)
	elif _receipt_used_by(receipt, transaction.name):
		problem = f"The lookup for {transaction.name} returned {receipt}, which another transaction already carries."

	if problem:
		frappe.log_error(title="M-Pesa receipt lookup not applied", message=problem)
		frappe.db.commit()
		return accepted

	_store_receipt(transaction, receipt, parsed.get("transaction_date"))
	frappe.db.set_value(
		"Mpesa Payment",
		detail.name,
		{
			"mpesa_receipt_number": receipt,
			"receipt_source": "Status Query",
			"transaction_date": parsed.get("transaction_date"),
		},
		update_modified=False,
	)
	frappe.db.commit()

	_notify_source_receipt(transaction, receipt)
	return accepted


def _amount(value):
	try:
		return round(float(value), 2)
	except (TypeError, ValueError):
		return None


def _notify_source_receipt(transaction, receipt):
	"""Hand a late-arriving receipt to the source app, if it wants one.

	Separate from ``on_payment_confirmed``: the payment was already confirmed, so
	only the receipt is new. A source doc that does not implement the hook is fine -
	it just keeps whatever reference it recorded at confirmation.
	"""
	try:
		doc = frappe.get_doc(transaction.source_doctype, transaction.source_document)
		if hasattr(doc, "on_payment_receipt"):
			doc.on_payment_receipt(receipt=receipt, transaction_id=transaction.name)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: could not hand receipt {receipt} to "
			f"{transaction.source_doctype} {transaction.source_document}: {e}"
		)


def _notify_source_app(transaction):
	"""
	Tell the source app the payment is complete.
	Calls on_payment_confirmed() on the source document if it exists.
	"""
	try:
		doc = frappe.get_doc(transaction.source_doctype, transaction.source_document)
		if hasattr(doc, "on_payment_confirmed"):
			doc.on_payment_confirmed(
				amount=transaction.amount,
				receipt=transaction.gateway_receipt,
				transaction_id=transaction.name,
			)
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: could not notify {transaction.source_doctype} "
			f"{transaction.source_document}: {e}"
		)


def _extract_gateway_reference(gateway_name, data):
	"""
	Extract the gateway reference from a callback payload.
	Each gateway puts it in a different place.

	``gateway_name`` reaches us as a query parameter on the CallBackURL, which is the
	one part of the URL we cannot count on: Daraja is fussy about query strings, and a
	proxy or rewrite in front of the site can drop them. So we also recognise a payload
	by its shape - an M-Pesa STK callback is the only one carrying Body.stkCallback -
	rather than silently failing to match the transaction and binning the receipt.
	"""
	gateway_name = (gateway_name or "").lower()

	stk = (data.get("Body") or {}).get("stkCallback") or {}
	if stk or "mpesa" in gateway_name:
		return stk.get("CheckoutRequestID")
	if "mtn" in gateway_name:
		return data.get("financialTransactionId") or data.get("externalId")
	if "stripe" in gateway_name:
		return data.get("id")
	return data.get("reference") or data.get("transaction_id")