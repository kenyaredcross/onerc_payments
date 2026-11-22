# Copyright (c) 2026, Nigel and contributors
# For license information, please see license.txt

"""The M-Pesa receipt must survive the poll/callback race.

Safaricom's STK *query* (what the status poll uses) tells us whether a payment went
through but never carries MpesaReceiptNumber - only the callback does. The poll almost
always wins that race, so a callback arriving at an already-Completed transaction is the
normal case, not an edge case. Dropping it as a duplicate lost every receipt we ever had.
"""

from unittest.mock import patch

import frappe

from onerc_payments.api.v1 import payment as pay
from onerc_payments.tests.utils import PHONE, RECEIPT, PaymentTestCase, stk_callback


class TestMpesaReceiptCallback(PaymentTestCase):
	def setUp(self):
		super().setUp()
		self.txn = self.transaction(amount=1)
		self.checkout = self.txn.gateway_reference

	def _receipt(self):
		return self.value(self.txn.name, "gateway_receipt")

	def test_receipt_lands_when_the_poll_got_there_first(self):
		"""The bug: the poll marks it Completed with no receipt, and the callback that
		carries the receipt was then thrown away as a duplicate."""
		self.txn.db_set("status", "Completed", update_modified=False)
		self.assertIsNone(self._receipt())

		self.fire(stk_callback(self.checkout, amount=1))

		self.assertEqual(self._receipt(), RECEIPT)

	def test_receipt_lands_on_the_normal_path_too(self):
		self.fire(stk_callback(self.checkout, amount=1))
		self.assertEqual(self._receipt(), RECEIPT)
		self.assertEqual(self.value(self.txn.name, "status"), "Completed")

	def test_the_detail_record_captures_the_receipt_and_phone(self):
		self.txn.db_set("status", "Completed", update_modified=False)
		self.fire(stk_callback(self.checkout, amount=1))

		detail = frappe.get_all(
			"Mpesa Payment",
			filters={"payment_transaction": self.txn.name},
			fields=["mpesa_receipt_number", "phone_number"],
		)
		self.assertTrue(detail, "the callback should record a Mpesa Payment detail row")
		self.assertEqual(detail[0].mpesa_receipt_number, RECEIPT)
		self.assertEqual(detail[0].phone_number, PHONE)

	def test_a_string_result_code_still_reads_as_success(self):
		"""Daraja sends ResultCode as an int here and a string elsewhere; a strict
		compare to 0 read "0" as a failure and dropped the receipt with it."""
		self.fire(stk_callback(self.checkout, result_code="0", amount=1))
		self.assertEqual(self._receipt(), RECEIPT)
		# The detail record made the same mistake on its own, filing "0" as Failed.
		self.assertEqual(
			frappe.db.get_value("Mpesa Payment", {"payment_transaction": self.txn.name}, "status"),
			"Completed",
		)

	def test_a_replay_does_not_blank_an_existing_receipt(self):
		self.fire(stk_callback(self.checkout, amount=1))
		self.fire(stk_callback(self.checkout, receipt="DIFFERENT9", amount=1))
		# First receipt wins; a retry must never overwrite a good one.
		self.assertEqual(self._receipt(), RECEIPT)

	def test_a_replay_with_a_different_receipt_changes_nothing_anywhere(self):
		"""A Safaricom retry repeats the receipt. A second "paid" with another code is not
		one: it must reach neither the app nor the detail record."""
		self.fire(stk_callback(self.checkout, amount=1))

		with patch.object(pay, "_notify_source_receipt") as notify:
			self.fire(stk_callback(self.checkout, receipt="DIFFERENT9", amount=1))

		notify.assert_not_called()
		self.assertEqual(
			frappe.db.get_value(
				"Mpesa Payment", {"payment_transaction": self.txn.name}, "mpesa_receipt_number"
			),
			RECEIPT,
		)

	def test_the_receipt_is_kept_exactly_as_safaricom_sent_it(self):
		self.fire(stk_callback(self.checkout, receipt="MPESA4ca2ce05ae", amount=1))
		self.assertEqual(self._receipt(), "MPESA4ca2ce05ae")

	def test_the_callback_matches_even_without_the_gateway_name_query_param(self):
		"""Daraja is fussy about query strings and proxies drop them; an STK payload is
		recognisable by its shape alone."""
		self.txn.db_set("status", "Completed", update_modified=False)
		self.fire(stk_callback(self.checkout, amount=1), gateway_name=None)

		self.assertEqual(self._receipt(), RECEIPT)
