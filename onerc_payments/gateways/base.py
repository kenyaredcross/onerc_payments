# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""
Base gateway class. Every payment gateway driver must inherit
from this class and implement all four methods.

Adding a new gateway:
1. Create a new file in onerc_payments/gateways/
2. Inherit from BaseGateway
3. Implement initiate(), check_status(), handle_callback(), generate_receipt()
4. Create a Payment Gateway fixture record with the driver_class path
5. Add credential fields to Payment Settings if needed
"""

from abc import ABC, abstractmethod


class BaseGateway(ABC):

	def __init__(self, settings):
		"""
		settings is the Payment Settings single doc.
		Each driver reads the credentials it needs from settings.
		"""
		self.settings = settings

	def verify_callback_source(self):
		"""
		Return True if the current inbound request is from a trusted source.

		Called by the callback endpoint BEFORE any processing. Drivers that
		can authenticate callbacks (e.g. by source-IP allowlist) should
		override this and return False for untrusted callers.

		Default: trust (no verification). Overriding is optional so existing
		drivers keep working unchanged.
		"""
		return True

	def record_payment_details(self, data, transaction):
		"""Persist a gateway-specific detail record from a verified callback and
		return its docname (or None).

		Called by the callback endpoint AFTER verify_callback_source() has passed
		and the callback has been matched to an initiated transaction, so the driver
		may trust `data` exactly as much as the transaction it already matched. Use
		it to capture the full gateway payload (e.g. the M-Pesa receipt + metadata)
		into a dedicated doctype and link it to `transaction`.

		Default: no detail record, so existing drivers are unaffected.
		"""
		return None

	def record_status_update(self, transaction, result):
		"""Sync the per-gateway detail record when a status *query* (not a callback)
		resolves a transaction, so the detail record doesn't lag behind.

		Called by check_payment_status() after the transaction's status changes.
		`result` is the dict returned by check_status(). Default: no-op, so gateways
		without a detail record are unaffected.
		"""
		return None

	def record_initiation_details(self, transaction, result):
		"""Persist gateway-specific initiation data into the per-gateway detail
		record and link it back, returning its docname (or None).

		Called by initiate_payment() after the gateway responds, so raw request/
		response payloads and any gateway ids from initiation live on the detail
		doctype instead of the generic transaction. Set
		``transaction.gateway_detail_doctype`` / ``transaction.gateway_detail`` here;
		the caller saves the transaction afterwards.

		Default: no detail record, so existing drivers are unaffected.
		"""
		return None

	@abstractmethod
	def initiate(self, transaction):
		"""
		Start a payment. For inbound: prompt the payer.
		For outbound: send money to the recipient.

		transaction: Payment Transaction doc (already saved, status=Initiated)

		Must return a dict:
		{
		    "success": True/False,
		    "gateway_reference": "...",     # e.g. CheckoutRequestID
		    "merchant_request_id": "...",   # optional, e.g. MerchantRequestID
		    "raw_request": "...",           # optional, JSON of the sent payload
		    "raw_response": "...",          # optional, JSON of the gateway reply
		    "message": "...",               # shown to the user
		}
		"""

	@abstractmethod
	def check_status(self, transaction):
		"""
		Query the gateway for the current status of a transaction.
		Called by the hourly scheduler for Pending transactions.

		transaction: Payment Transaction doc

		Must return a dict:
		{
		    "status": "Completed" / "Failed" / "Pending",
		    "gateway_receipt": "...",     # final receipt number if completed
		    "failure_reason": "...",      # if failed
		}
		"""

	@abstractmethod
	def handle_callback(self, data, transaction):
		"""
		Process an incoming callback/webhook from the gateway.

		data: raw callback payload (dict)
		transaction: Payment Transaction doc

		Must return a dict:
		{
		    "status": "Completed" / "Failed",
		    "gateway_receipt": "...",
		    "failure_reason": "...",
		    "result_code": "...",          # optional, raw gateway result code
		    "result_description": "...",   # optional, human-readable result
		    "transaction_date": "...",     # optional, gateway-reported datetime
		}
		"""

	@abstractmethod
	def generate_receipt(self, transaction):
		"""
		Generate a human-readable receipt string for this transaction.

		transaction: Payment Transaction doc

		Must return a string (plain text or HTML).
		"""