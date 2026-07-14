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
):
	"""
	Initiate a payment. Called by any OneRC app.

	Returns:
	{
	    "transaction_id": "PAY-2026-00001",
	    "status": "Pending",
	    "gateway_reference": "...",
	    "message": "Check your phone and enter your M-Pesa PIN",
	}
	"""
	settings = frappe.get_single("OneRC Payment Settings")

	transaction = frappe.get_doc({
		"doctype": "OneRC Payment Transaction",
		"gateway": settings.active_gateway,
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

	gateway = get_gateway()
	result = gateway.initiate(transaction)

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
		gateway.record_initiation_details(transaction, result)
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
	gateway = get_gateway()
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
	elif transaction.is_dirty():
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
	# duplicates serialize; if the transaction is already resolved, ack without
	# re-processing so the source-app hook (receipt / GL posting) never fires
	# twice.
	current_status = frappe.db.get_value(
		"OneRC Payment Transaction",
		transaction_name,
		"status",
		for_update=True,
	)
	if current_status in ("Completed", "Failed", "Cancelled", "Refunded"):
		return {"ResultCode": 0, "ResultDesc": "Accepted"}

	transaction_doc = frappe.get_doc(
		"OneRC Payment Transaction", transaction_name
	)
	result = gateway.handle_callback(data, transaction_doc)

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
	"""
	if "mpesa" in gateway_name.lower():
		return (
			data.get("Body", {})
			    .get("stkCallback", {})
			    .get("CheckoutRequestID")
		)
	if "mtn" in gateway_name.lower():
		return data.get("financialTransactionId") or data.get("externalId")
	if "stripe" in gateway_name.lower():
		return data.get("id")
	return data.get("reference") or data.get("transaction_id")