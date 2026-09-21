# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""An STK status query must resolve a finished push, not park it on Pending.

``check_status`` used to treat only 1032 (cancelled) and 1037 (timed out) as
terminal. Every other failure - most commonly 2001, a wrong M-Pesa PIN - fell
through to "Pending", so the hourly poller re-queried a long-dead push for ever
and the payment screen showed it as still in flight.

Codes we do NOT recognise deliberately stay Pending: ``payment_callback`` short-
circuits on a Failed transaction, so resolving a code wrongly would make us throw
away a genuine callback that arrived afterwards.
"""

from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase

from onerc_payments.gateways.mpesa_daraja import MpesaDarajaGateway


class _FakeResponse:
	def __init__(self, payload):
		self._payload = payload

	def json(self):
		return self._payload


def _query(payload):
	"""Run check_status against a canned Daraja STK-query response."""
	settings = frappe._dict(environment="Sandbox", mpesa_shortcode="174379")
	gateway = MpesaDarajaGateway(settings)
	transaction = frappe._dict(name="PAY-TEST-0001", gateway_reference="ws_CO_TEST_1")

	with (
		patch.object(MpesaDarajaGateway, "_generate_password", return_value="pw"),
		patch.object(MpesaDarajaGateway, "_get_token", return_value="tok"),
		patch("onerc_payments.gateways.mpesa_daraja.requests.post", return_value=_FakeResponse(payload)),
	):
		return gateway.check_status(transaction)


class TestStkStatusMapping(FrappeTestCase):
	def test_a_wrong_pin_is_a_failure_not_a_pending_payment(self):
		result = _query({"ResultCode": "2001", "ResultDesc": "The initiator information is invalid."})

		self.assertEqual(result["status"], "Failed")
		self.assertEqual(result["result_code"], "2001")
		self.assertEqual(result["failure_reason"], "The initiator information is invalid.")

	def test_the_previously_handled_codes_still_fail(self):
		for code in ("1032", "1037"):
			with self.subTest(code=code):
				self.assertEqual(_query({"ResultCode": code, "ResultDesc": "x"})["status"], "Failed")

	def test_the_other_terminal_codes_fail(self):
		for code in ("1", "1001", "1019", "1025", "9999"):
			with self.subTest(code=code):
				self.assertEqual(_query({"ResultCode": code, "ResultDesc": "x"})["status"], "Failed")

	def test_success_still_completes(self):
		result = _query({"ResultCode": "0", "ResultDesc": "The service request is processed successfully."})

		self.assertEqual(result["status"], "Completed")
		# The query API never carries the receipt - only the callback does. This is
		# exactly why a poll-resolved payment has no M-Pesa code to reconcile with.
		self.assertIsNone(result["gateway_receipt"])

	def test_a_push_still_in_flight_stays_pending(self):
		# While the customer is still deciding, Daraja answers with an errorCode
		# (500.001.1001 "The transaction is being processed") and no ResultCode.
		result = _query({"errorCode": "500.001.1001", "errorMessage": "The transaction is being processed"})

		self.assertEqual(result["status"], "Pending")
		self.assertIsNone(result["result_code"])

	def test_an_unrecognised_code_stays_pending(self):
		# Conservative on purpose: a premature Failed would make payment_callback
		# discard the real callback if one turned up later.
		self.assertEqual(_query({"ResultCode": "7777", "ResultDesc": "?"})["status"], "Pending")
