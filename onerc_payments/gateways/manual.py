# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""
Manual payment gateway.

Used when:
- The organisation collects payment via bank transfer
- An admin manually confirms a cash payment
- Testing without live gateway credentials

Flow:
  Inbound:  transaction created → status stays Pending →
            admin reviews proof of payment →
            admin calls confirm_payment() → status = Completed

  Outbound: admin records that payment was made manually →
            marks Completed with a reference number
"""

import frappe
from frappe.utils import now_datetime

from .base import BaseGateway


class ManualGateway(BaseGateway):

	def initiate(self, transaction):
		bank_name = getattr(self.settings, "manual_bank_name", None)
		account = getattr(self.settings, "manual_account_number", None)
		custom_instructions = getattr(self.settings, "manual_instructions", None)

		if custom_instructions:
			instructions = custom_instructions
		elif bank_name and account:
			instructions = (
				f"Please pay {transaction.currency} {transaction.amount} "
				f"to {bank_name} account {account}. "
				f"Use reference: {transaction.name}"
			)
		else:
			instructions = (
				f"Please pay {transaction.currency} {transaction.amount}. "
				f"Use reference: {transaction.name} and contact us to confirm."
			)

		return {
			"success": True,
			"gateway_reference": transaction.name,
			"message": instructions,
		}

	def check_status(self, transaction):
		# Manual payments do not auto-resolve.
		# An admin must confirm them explicitly via confirm_payment().
		return {
			"status": transaction.status,
			"gateway_receipt": transaction.gateway_receipt,
			"failure_reason": None,
		}

	def handle_callback(self, data, transaction):
		# Manual gateway has no callback URL.
		# Confirmation happens via confirm_payment() below.
		return {
			"status": "Pending",
			"gateway_receipt": None,
			"failure_reason": "Manual gateway does not use callbacks.",
		}

	def generate_receipt(self, transaction):
		bank_name = getattr(self.settings, "manual_bank_name", "—")
		return (
			f"PAYMENT RECEIPT\n"
			f"Reference:  {transaction.name}\n"
			f"Amount:     {transaction.currency} {transaction.amount}\n"
			f"Receipt No: {transaction.gateway_receipt or '—'}\n"
			f"Date:       {transaction.transaction_date or '—'}\n"
			f"Bank:       {bank_name}\n"
			f"Status:     {transaction.status}\n"
		)


@frappe.whitelist()
def confirm_payment(transaction_name, receipt_number):
	"""
	Confirm a manually collected payment — cash at a branch, or a bank transfer.

	Wired to the Confirm Payment button on the OneRC Payment Transaction form.

	**Write permission on the transaction is the gate, and it is not optional.**
	This method is whitelisted, so without a check any authenticated user could
	call it against any docname and mark it Completed — and because completion
	notifies the source app through `_notify_source_app`, that is not merely a
	wrong status: it is the applicant activating their own membership without
	paying for it. The check is `has_permission`, so who may confirm stays a
	role question the site answers, exactly like every other write here.

	The save below keeps `ignore_permissions=True` deliberately: authority has
	been established at the boundary, and the fields being written — status,
	receipt, the detail link — are read-only on the form precisely so that they
	move through this path rather than by hand.
	"""
	transaction = frappe.get_doc(
		"OneRC Payment Transaction",
		transaction_name,
	)

	frappe.has_permission(
		"OneRC Payment Transaction", ptype="write", doc=transaction, throw=True
	)

	if transaction.status != "Pending":
		frappe.throw(
			f"Cannot confirm a transaction with status {transaction.status}."
		)

	transaction.status = "Completed"
	transaction.gateway_receipt = receipt_number

	# Record the confirmation on the per-gateway detail doctype and link it back.
	# Best-effort: a detail failure must not block confirming the payment.
	try:
		detail = _record_manual_detail(transaction, receipt_number)
		transaction.gateway_detail_doctype = "Manual Payment"
		transaction.gateway_detail = detail.name
	except Exception as e:
		frappe.logger().error(
			f"onerc_payments: failed to record manual payment detail for {transaction.name}: {e}"
		)

	transaction.save(ignore_permissions=True)

	from onerc_payments.api.v1.payment import _notify_source_app
	_notify_source_app(transaction)

	return {"success": True, "message": f"Payment {transaction_name} confirmed."}


def _record_manual_detail(transaction, receipt_number):
	"""Upsert the Manual Payment detail record for a manually confirmed transaction."""
	values = {
		"payment_transaction": transaction.name,
		"status": "Completed",
		"receipt_number": receipt_number,
		"amount": transaction.amount,
		"confirmed_by": frappe.session.user,
		"confirmed_on": now_datetime(),
	}
	existing = frappe.db.get_value("Manual Payment", {"payment_transaction": transaction.name}, "name")
	if existing:
		doc = frappe.get_doc("Manual Payment", existing)
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": "Manual Payment", **values})
		doc.insert(ignore_permissions=True)
	return doc