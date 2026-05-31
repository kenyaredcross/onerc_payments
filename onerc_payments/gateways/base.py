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

	@abstractmethod
	def initiate(self, transaction):
		"""
		Start a payment. For inbound: prompt the payer.
		For outbound: send money to the recipient.

		transaction: Payment Transaction doc (already saved, status=Initiated)

		Must return a dict:
		{
		    "success": True/False,
		    "gateway_reference": "...",   # e.g. CheckoutRequestID
		    "message": "...",             # shown to the user
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
		}
		"""

	@abstractmethod
	def generate_receipt(self, transaction):
		"""
		Generate a human-readable receipt string for this transaction.

		transaction: Payment Transaction doc

		Must return a string (plain text or HTML).
		"""