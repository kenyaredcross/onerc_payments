# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""
The single entry point for all OneRC payment operations.

Every OneRC app that needs payments calls initiate_payment().
Nothing else. The gateway, credentials, and flow are handled here.
"""

import frappe
from frappe.utils import now_datetime
from onerc_payments.gateways import get_gateway


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
		"payer_phone": payer_phone,
		"payer_email": payer_email,
		"recipient_name": recipient_name,
		"recipient_phone": recipient_phone,
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

	transaction.save(ignore_permissions=True)

	return {
		"transaction_id": transaction.name,
		"status": transaction.status,
		"gateway_reference": transaction.gateway_reference,
		"message": result.get("message"),
	}


@frappe.whitelist()
def check_payment_status(transaction_id):
	"""
	Check the current status of a payment.
	Called by frontend polling loops.
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
		transaction.save(ignore_permissions=True)

		if result["status"] == "Completed":
			_notify_source_app(transaction)

	return {
		"transaction_id": transaction.name,
		"status": transaction.status,
		"gateway_receipt": transaction.gateway_receipt,
		"failure_reason": transaction.failure_reason,
	}


@frappe.whitelist(allow_guest=True)
def payment_callback(gateway_name=None, **kwargs):
    import json

    try:
        raw = frappe.request.data
        data = json.loads(raw) if raw else {}
    except Exception:
        data = {}

    if not data:
        data = {k: v for k, v in frappe.form_dict.items()
                if k != "cmd"}

    if not gateway_name:
        gateway_name = data.get("gateway_name", "")

    gateway_reference = _extract_gateway_reference(gateway_name, data)

    if not gateway_reference:
        frappe.logger().warning(
            f"onerc_payments: callback received but no reference found. "
            f"gateway={gateway_name} data={data}"
        )
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    transaction_name = frappe.db.get_value(
        "OneRC Payment Transaction",
        {"gateway_reference": gateway_reference},
        "name",
    )

    if not transaction_name:
        frappe.logger().warning(
            f"onerc_payments: no transaction found for reference "
            f"{gateway_reference}"
        )
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    transaction_doc = frappe.get_doc(
        "OneRC Payment Transaction", transaction_name
    )
    gateway = get_gateway()
    result = gateway.handle_callback(data, transaction_doc)

    transaction_doc.status = result["status"]
    transaction_doc.raw_response = str(data)

    if result.get("gateway_receipt"):
        transaction_doc.gateway_receipt = result["gateway_receipt"]
        transaction_doc.transaction_date = now_datetime()

    if result.get("failure_reason"):
        transaction_doc.failure_reason = result["failure_reason"]

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