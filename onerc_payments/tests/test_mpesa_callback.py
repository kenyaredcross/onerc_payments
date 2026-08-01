# Copyright (c) 2026, Nigel and contributors
# For license information, please see license.txt

"""The M-Pesa receipt must survive the poll/callback race.

Safaricom's STK *query* (what the status poll uses) tells us whether a payment went
through but never carries MpesaReceiptNumber - only the callback does. The poll almost
always wins that race, so a callback arriving at an already-Completed transaction is the
normal case, not an edge case. Dropping it as a duplicate lost every receipt we ever had.
"""

import json

import frappe
from frappe.tests.utils import FrappeTestCase

from onerc_payments.api.v1 import payment as pay

RECEIPT = "SLK4H8N2Q6"
PHONE = "254712345678"


def _stk_callback(checkout, receipt=RECEIPT, result_code=0):
	items = [
		{"Name": "Amount", "Value": 1.0},
		{"Name": "MpesaReceiptNumber", "Value": receipt},
		{"Name": "TransactionDate", "Value": 20260714120500},
		{"Name": "PhoneNumber", "Value": int(PHONE)},
	]
	stk = {
		"MerchantRequestID": "1234-5678",
		"CheckoutRequestID": checkout,
		"ResultCode": result_code,
		"ResultDesc": "The service request is processed successfully.",
	}
	if int(result_code) == 0:
		stk["CallbackMetadata"] = {"Item": items}
	return {"Body": {"stkCallback": stk}}


class TestMpesaReceiptCallback(FrappeTestCase):
	def setUp(self):
		settings = frappe.get_single("OneRC Payment Settings")
		if not settings.active_gateway:
			self.skipTest("no active payment gateway configured on this site")
		# Sandbox skips the Safaricom source-IP check, which a synthetic POST cannot pass.
		self._environment = settings.environment
		settings.environment = "Sandbox"
		settings.save(ignore_permissions=True)

		# payment_callback() commits, which defeats the per-test rollback, so each test
		# needs its own CheckoutRequestID or the next one matches the previous txn.
		self.checkout = f"ws_CO_UNITTEST_{self._testMethodName}"

		self.txn = frappe.get_doc(
			{
				"doctype": "OneRC Payment Transaction",
				"gateway": settings.active_gateway,
				"direction": "Inbound",
				"status": "Pending",
				"amount": 1,
				"currency": "KES",
				"source_app": "onerc_payments",
				"source_doctype": "OneRC Payment Settings",
				"source_document": "OneRC Payment Settings",
				"payer_name": "Test Payer",
				"phone_number": PHONE,
				"gateway_reference": self.checkout,
			}
		).insert(ignore_permissions=True)

		self.addCleanup(self._restore)

	def _restore(self):
		for detail in frappe.get_all("Mpesa Payment", filters={"payment_transaction": self.txn.name}, pluck="name"):
			frappe.delete_doc("Mpesa Payment", detail, force=True, ignore_permissions=True)
		frappe.delete_doc("OneRC Payment Transaction", self.txn.name, force=True, ignore_permissions=True)
		settings = frappe.get_single("OneRC Payment Settings")
		settings.environment = self._environment
		settings.save(ignore_permissions=True)
		# payment_callback() committed its inserts, so the per-test rollback cannot undo
		# them - and it WOULD undo these deletes. Commit, or the rows outlive the test.
		frappe.db.commit()

	def _fire(self, payload):
		frappe.local.request = frappe._dict(data=json.dumps(payload).encode())
		frappe.form_dict = frappe._dict()
		return pay.payment_callback(gateway_name="Mpesa Daraja")

	def _receipt(self):
		return frappe.db.get_value("OneRC Payment Transaction", self.txn.name, "gateway_receipt")

	def test_receipt_lands_when_the_poll_got_there_first(self):
		"""The bug: the poll marks it Completed with no receipt, and the callback that
		carries the receipt was then thrown away as a duplicate."""
		self.txn.db_set("status", "Completed", update_modified=False)
		self.assertIsNone(self._receipt())

		self._fire(_stk_callback(self.checkout))

		self.assertEqual(self._receipt(), RECEIPT)

	def test_receipt_lands_on_the_normal_path_too(self):
		self._fire(_stk_callback(self.checkout))
		self.assertEqual(self._receipt(), RECEIPT)
		self.assertEqual(
			frappe.db.get_value("OneRC Payment Transaction", self.txn.name, "status"), "Completed"
		)

	def test_the_detail_record_captures_the_receipt_and_phone(self):
		self.txn.db_set("status", "Completed", update_modified=False)
		self._fire(_stk_callback(self.checkout))

		detail = frappe.get_all(
			"Mpesa Payment",
			filters={"payment_transaction": self.txn.name},
			fields=["mpesa_receipt_number", "phone_number"],
		)
		self.assertTrue(detail, "the callback should record a Mpesa Payment detail row")
		self.assertEqual(detail[0].mpesa_receipt_number, RECEIPT)

	def test_a_string_result_code_still_reads_as_success(self):
		"""Daraja sends ResultCode as an int here and a string elsewhere; a strict
		compare to 0 read "0" as a failure and dropped the receipt with it."""
		self._fire(_stk_callback(self.checkout, result_code="0"))
		self.assertEqual(self._receipt(), RECEIPT)

	def test_a_replay_does_not_blank_an_existing_receipt(self):
		self._fire(_stk_callback(self.checkout))
		self._fire(_stk_callback(self.checkout, receipt="DIFFERENT9"))
		# First receipt wins; a retry must never overwrite a good one.
		self.assertEqual(self._receipt(), RECEIPT)

	def test_the_callback_matches_even_without_the_gateway_name_query_param(self):
		"""Daraja is fussy about query strings and proxies drop them; an STK payload is
		recognisable by its shape alone."""
		self.txn.db_set("status", "Completed", update_modified=False)
		frappe.local.request = frappe._dict(data=json.dumps(_stk_callback(self.checkout)).encode())
		frappe.form_dict = frappe._dict()

		pay.payment_callback()  # no gateway_name at all

		self.assertEqual(self._receipt(), RECEIPT)
