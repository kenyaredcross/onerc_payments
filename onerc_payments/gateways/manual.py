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
	Admin calls this to manually confirm a payment.
	Can be wired to a custom button on the
	OneRC Payment Transaction form.
	"""
	transaction = frappe.get_doc(
		"OneRC Payment Transaction",
		transaction_name,
	)

	if transaction.status != "Pending":
		frappe.throw(
			f"Cannot confirm a transaction with status {transaction.status}."
		)

	transaction.status = "Completed"
	transaction.gateway_receipt = receipt_number
	transaction.save(ignore_permissions=True)

	from onerc_payments.api.v1.payment import _notify_source_app
	_notify_source_app(transaction)

	return {"success": True, "message": f"Payment {transaction_name} confirmed."}