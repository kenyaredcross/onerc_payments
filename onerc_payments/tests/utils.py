# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Shared set-up for the M-Pesa payment tests.

Nothing here talks to Safaricom: every test that reaches a Daraja call patches it. The
callback endpoint commits, which a test's rollback cannot undo, so every row a test
creates is deleted again in its cleanup - transactions, their Mpesa Payment details,
saved callbacks and comments - and the settings it changes are put back.
"""

import json
from unittest.mock import patch

import frappe
import requests
from frappe.tests.utils import FrappeTestCase

from onerc_payments.api.v1 import payment as pay
from onerc_payments.gateways.base import CONFIRMED
from onerc_payments.gateways.mpesa_daraja import MpesaDarajaGateway

# The real method, taken before any test patches it on the class.
REAL_CONFIRM_CALLBACK = MpesaDarajaGateway.confirm_callback

GATEWAY = "Mpesa Daraja"
PHONE = "254712345678"
RECEIPT = "SLK4H8N2Q6"
SETTINGS = "OneRC Payment Settings"
SETTINGS_FIELDS = (
	"environment",
	"active_gateway",
	"callback_confirmation",
	"recover_failed_payments",
	"default_reference_prefix",
)


class FakeResponse:
	def __init__(self, payload, status_code=200):
		self._payload = payload
		self.status_code = status_code
		self.text = json.dumps(payload)

	def json(self):
		return self._payload

	def raise_for_status(self):
		if self.status_code >= 400:
			raise requests.HTTPError(f"HTTP {self.status_code}", response=self)


def stk_callback(checkout, receipt=RECEIPT, result_code=0, amount=10, phone=PHONE, desc=None):
	"""An STK callback as Daraja posts it."""
	stk = {
		"MerchantRequestID": f"MR-{checkout}",
		"CheckoutRequestID": checkout,
		"ResultCode": result_code,
		"ResultDesc": desc
		or (
			"The service request is processed successfully."
			if str(result_code) == "0"
			else "Request cancelled by user"
		),
	}
	if str(result_code) == "0":
		stk["CallbackMetadata"] = {
			"Item": [
				{"Name": "Amount", "Value": float(amount)},
				{"Name": "MpesaReceiptNumber", "Value": receipt},
				{"Name": "TransactionDate", "Value": 20261117120500},
				{"Name": "PhoneNumber", "Value": int(phone) if str(phone).isdigit() else phone},
			]
		}
	return {"Body": {"stkCallback": stk}}


class PaymentTestCase(FrappeTestCase):
	"""A site with the M-Pesa gateway active in Sandbox, and Safaricom's answer to "was this
	callback really paid?" patched to CONFIRMED unless a test says otherwise."""

	confirmation = CONFIRMED

	def setUp(self):
		if not frappe.db.exists("OneRC Payment Gateway", GATEWAY):
			self.skipTest("the Mpesa Daraja gateway fixture is not on this site")

		frappe.set_user("Administrator")
		self._gateway_active = frappe.db.get_value("OneRC Payment Gateway", GATEWAY, "is_active")
		frappe.db.set_value("OneRC Payment Gateway", GATEWAY, "is_active", 1)
		self._settings = {f: frappe.db.get_single_value(SETTINGS, f) for f in SETTINGS_FIELDS}
		self._prefix_rows = frappe.get_all(
			"OneRC Payment Reference Prefix",
			filters={"parent": SETTINGS},
			fields=["source_app", "prefix", "description"],
			order_by="idx asc",
		)
		frappe.db.set_single_value(
			SETTINGS,
			{"environment": "Sandbox", "callback_confirmation": "Watch only", "recover_failed_payments": 1},
		)

		self.created = []
		self.references = []
		self.addCleanup(self._restore)

		confirm = patch.object(MpesaDarajaGateway, "confirm_callback", return_value=self.confirmation)
		self.confirm = confirm.start()
		self.addCleanup(confirm.stop)

	# ── building blocks ───────────────────────────────────────────

	def transaction(self, checkout=None, status="Pending", amount=10, receipt=None, source=None, **values):
		"""A transaction as initiate_payment leaves it, with its Mpesa Payment detail."""
		checkout = (
			checkout if checkout is not None else f"ws_CO_TEST_{self._testMethodName}_{len(self.created)}"
		)
		source_doctype, source_document = source or (SETTINGS, SETTINGS)
		txn = frappe.get_doc(
			{
				"doctype": "OneRC Payment Transaction",
				"gateway": GATEWAY,
				"direction": "Inbound",
				"status": status,
				"amount": amount,
				"currency": "KES",
				"source_app": "onerc_payments",
				"source_doctype": source_doctype,
				"source_document": source_document,
				"payer_name": "Test Payer",
				"phone_number": PHONE,
				"gateway_reference": checkout or None,
				"gateway_receipt": receipt,
				**values,
			}
		).insert(ignore_permissions=True, set_name=f"PAY-TEST-{frappe.generate_hash(length=10).upper()}")
		self.created.append(txn.name)
		if checkout:
			self.references.append(checkout)
			detail = frappe.get_doc(
				{
					"doctype": "Mpesa Payment",
					"payment_transaction": txn.name,
					"checkout_request_id": checkout,
					"merchant_request_id": f"MR-{checkout}",
					"status": status if status in ("Pending", "Completed", "Failed") else "Pending",
					"expected_amount": amount,
				}
			).insert(ignore_permissions=True)
			txn.db_set(
				{"gateway_detail_doctype": "Mpesa Payment", "gateway_detail": detail.name},
				update_modified=False,
			)
		return txn

	def fire(self, payload, gateway_name=GATEWAY, headers=None):
		"""Post a callback to the endpoint the way Daraja does."""
		for ref in [((payload.get("Body") or {}).get("stkCallback") or {}).get("CheckoutRequestID")]:
			if ref and ref not in self.references:
				self.references.append(ref)
		frappe.local.request = frappe._dict(data=json.dumps(payload).encode(), headers=headers or {})
		frappe.form_dict = frappe._dict()
		return pay.payment_callback(gateway_name=gateway_name)

	def value(self, name, field):
		return frappe.db.get_value("OneRC Payment Transaction", name, field)

	def log_for(self, reference):
		rows = frappe.get_all(
			"OneRC Payment Callback",
			filters={"gateway_reference": reference},
			fields=["*"],
			order_by="creation desc",
			limit=1,
		)
		return rows[0] if rows else None

	def settings(self, **values):
		frappe.db.set_single_value(SETTINGS, values)

	# ── cleanup ───────────────────────────────────────────────────

	def _restore(self):
		frappe.set_user("Administrator")
		frappe.local.request = None
		for name in self.created:
			for detail in frappe.get_all(
				"Mpesa Payment", filters={"payment_transaction": name}, pluck="name"
			):
				frappe.delete_doc("Mpesa Payment", detail, force=True, ignore_permissions=True)
			frappe.db.delete("OneRC Payment Callback", {"payment_transaction": name})
			frappe.db.delete(
				"Comment", {"reference_doctype": "OneRC Payment Transaction", "reference_name": name}
			)
			frappe.db.delete("Version", {"ref_doctype": "OneRC Payment Transaction", "docname": name})
			frappe.delete_doc("OneRC Payment Transaction", name, force=True, ignore_permissions=True)
		for ref in self.references:
			frappe.db.delete("OneRC Payment Callback", {"gateway_reference": ref})
			for detail in frappe.get_all("Mpesa Payment", filters={"checkout_request_id": ref}, pluck="name"):
				frappe.delete_doc("Mpesa Payment", detail, force=True, ignore_permissions=True)

		frappe.db.set_value("OneRC Payment Gateway", GATEWAY, "is_active", self._gateway_active)
		frappe.db.set_single_value(SETTINGS, {f: v for f, v in self._settings.items()})
		frappe.db.delete("OneRC Payment Reference Prefix", {"parent": SETTINGS})
		for idx, row in enumerate(self._prefix_rows, start=1):
			frappe.get_doc(
				{
					"doctype": "OneRC Payment Reference Prefix",
					"parent": SETTINGS,
					"parenttype": SETTINGS,
					"parentfield": "reference_prefixes",
					"idx": idx,
					**row,
				}
			).db_insert()
		frappe.clear_document_cache(SETTINGS, SETTINGS)
		# The callback endpoint committed its writes, so the per-test rollback cannot undo
		# them - and it WOULD undo these deletes. Commit, or the rows outlive the test.
		frappe.db.commit()
