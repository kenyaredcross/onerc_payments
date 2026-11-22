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

A gateway that can have several accounts (KCB tills, card merchants) keeps each
account's credentials on its own account doctype instead, and each account is a
gateway record of its own (see ``onerc_payments.gateways.accounts``).
"""

from abc import ABC, abstractmethod

# What ``confirm_callback`` can answer about a callback that claims a payment succeeded.
CONFIRMED = "Confirmed"  # the gateway, asked directly, says it was paid
CONTRADICTED = "Contradicted"  # the gateway says it was NOT paid
UNAVAILABLE = "Unavailable"  # no definite answer right now (unreachable, still processing)
# The gateway cannot be asked, and what the message carries is not enough to take it on
# trust: a person checks the statement. ``manual_check_reason`` on the driver says why.
MANUAL_CHECK = "Needs a person"


class BaseGateway(ABC):
	# What a person calls this payment method, for messages shown to them.
	label = "This payment method"

	# Currencies this gateway can collect, e.g. ("KES",). None means any.
	supported_currencies = None

	# True when starting a payment sends the payer a prompt (an STK push). A second request
	# for the same thing a moment later then reuses the first instead of prompting again.
	prompts_payer = False

	# True when the payer pays on the gateway's own page (a card checkout): ``initiate``
	# returns a ``checkout_url`` the app sends the payer to. A second request a moment later
	# reuses the first, as for a prompt.
	opens_checkout = False

	# True when the gateway resolves a payment by itself (status query, callback), rather
	# than a person confirming it. Only these are flagged when they hang in Pending.
	resolves_automatically = False

	# Which callback endpoint this driver's messages arrive at (OneRC Payment Callback
	# "kind"). A message that arrives at one endpoint can never settle a payment taken
	# through a driver that reports at another: every M-Pesa-shaped message looks alike.
	callback_kind = "Payment"

	# True when a message from an authenticated sender is itself the gateway's word (Jenga
	# posts its notification with a password only it and we know). Such a message from a
	# sender that is NOT authenticated is ignored entirely - paid or failed.
	sender_proves_payment = False

	# Set by ``confirm_callback`` when it answers MANUAL_CHECK: why a person must look.
	manual_check_reason = None

	# True when the per-gateway detail record is the only place a callback can be matched
	# from (KCB keeps its private request IDs there, Jenga its order reference). A payment
	# whose detail record could not be written is then not left waiting for a callback
	# that can never find it.
	detail_is_required = False

	def __init__(self, settings, gateway=None):
		"""
		settings is the Payment Settings single doc.
		gateway is the OneRC Payment Gateway record this driver was loaded for. A driver
		with several accounts reads its credentials from ``self.account``; the others read
		them from settings.
		"""
		self.settings = settings
		self.gateway = gateway
		self._account = None

	@property
	def gateway_name(self):
		return self.gateway.name if self.gateway else None

	@property
	def account(self):
		"""The account record behind this gateway (e.g. a KCB Buni Account), or None."""
		import frappe

		if self._account is None and self.gateway and self.gateway.get("account"):
			self._account = frappe.get_doc(self.gateway.account_doctype, self.gateway.account)
		return self._account

	def validate_account(self):
		"""Refuse to be switched on (or to take a payment) while set up in a way that cannot
		work: missing credentials, a callback address the gateway cannot reach. Default: no
		checks."""
		return None

	@staticmethod
	def find_transaction(reference):
		"""The transaction a callback ``reference`` belongs to. Default: the transaction
		whose gateway_reference it is. A driver whose reference must stay private (it is
		what proves a callback genuine) keeps it on its detail record instead."""
		import frappe

		if not reference:
			return None
		return frappe.db.get_value("OneRC Payment Transaction", {"gateway_reference": reference}, "name")

	@staticmethod
	def callback_reference(data):
		"""The reference a message at this driver's own endpoint carries. Only drivers with
		their own ``callback_kind`` need it; "Payment" messages use the shared extractor."""
		return None

	def check_currency(self, currency):
		"""Refuse a currency this gateway cannot collect, before anything is written."""
		import frappe

		allowed = self.supported_currencies
		if allowed and (currency or "").strip().upper() not in allowed:
			frappe.throw(
				f"{self.label} only accepts payments in {', '.join(allowed)}. "
				f"Ask for the amount in {allowed[0]}, or choose another way to pay."
			)

	def verify_callback_source(self):
		"""
		Return True if the current inbound request comes from an address the
		gateway is known to send from.

		For information only: the answer is saved on the callback log. It does not
		decide whether a callback is processed - a source address can be faked
		through proxy headers, so the real check is ``confirm_callback``.

		Default: True.
		"""
		return True

	def confirm_callback(self, transaction, result, data):
		"""Ask the gateway directly whether a callback's "paid" is real.

		Called before a callback that claims success is allowed to complete a payment
		or recover one marked Failed. Return CONFIRMED, CONTRADICTED or UNAVAILABLE.

		Default: None, meaning this gateway cannot be asked; the callback is trusted as
		it always was.
		"""
		return None

	def failure_code(self, transaction):
		"""The provider's result code for a payment that ended Failed or Cancelled (M-Pesa
		"1032": cancelled on the phone), as text, or None when it is not known.

		Called only for Failed/Cancelled payments, to add ``failure_code`` to the status reply.
		Read it from what is already stored (the detail record); never ask the gateway.

		Default: None.
		"""
		return None

	def match_unconfirmed(self, data, gateway_names):
		"""Find the payment a callback belongs to when its reference matches nothing.

		That happens when the request to start the payment timed out: the gateway
		created the payment, but its reply - carrying the reference - never reached us.
		Return the transaction name only when exactly one payment fits; otherwise None.
		``gateway_names`` are the gateway records that use this driver.

		Default: None (no matching).
		"""
		return None

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

	# ── RECEIPT RECOVERY ─────────────────────────────────────────────
	# For a payment that was confirmed but whose receipt never arrived (a lost
	# callback). Both paths are optional; a driver that has no use for them keeps
	# the defaults and the desk offers nothing.

	# True when staff may type this gateway's receipt in by hand.
	accepts_entered_receipt = False

	def normalise_receipt(self, value):
		"""Return a receipt typed in by staff in the gateway's canonical form, or
		raise ``frappe.ValidationError`` if it cannot be one. Default: refuse, so a
		driver must opt in before staff can type receipts against it."""
		import frappe

		frappe.throw("This payment method does not take a receipt typed in by staff.")

	def record_entered_receipt(self, transaction, receipt):
		"""Note on the per-gateway detail record that staff entered ``receipt``.
		Default: nothing to note."""
		return None

	def can_request_receipt(self):
		"""True when ``request_receipt`` is configured and may be called."""
		return False

	def request_receipt(self, transaction):
		"""Ask the gateway for the receipt of a completed payment. The answer may
		arrive later, on a result URL. Default: unsupported."""
		import frappe

		frappe.throw("This payment method cannot look up a receipt.")

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
