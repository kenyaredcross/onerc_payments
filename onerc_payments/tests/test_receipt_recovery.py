# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""A completed payment whose callback was lost can still get its M-Pesa code.

The STK query that confirms a payment never carries the receipt; only the callback
does. When the callback is lost the payment is Completed with a blank code. Two ways
back: staff type the code from the payer's SMS, or Daraja's Transaction Status API
looks it up and posts the answer to ``receipt_query_result``.
"""

import json
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from frappe.utils import add_to_date, now_datetime

from onerc_payments import tasks
from onerc_payments.api.v1 import payment as pay
from onerc_payments.gateways.mpesa_daraja import MpesaDarajaGateway

GATEWAY = "Mpesa Daraja"
PHONE = "254712345678"


class _FakeResponse:
	def __init__(self, payload):
		self._payload = payload

	def json(self):
		return self._payload


def _status_result(conversation_id, receipt="TKB1A2C3D4", amount="10", result_code=0, status="Completed"):
	params = [
		{"Key": "DebitPartyName", "Value": f"{PHONE} - Test Payer"},
		{"Key": "TransactionStatus", "Value": status},
		{"Key": "FinalisedTime", "Value": "20261109084700"},
		{"Key": "Amount", "Value": amount},
		{"Key": "ReceiptNo", "Value": receipt},
	]
	return {
		"Result": {
			"ConversationID": conversation_id,
			"OriginatorConversationID": "1234-5678-1",
			"ResultCode": result_code,
			"ResultDesc": "The service request is processed successfully.",
			"ResultParameters": {"ResultParameter": params},
			"ResultType": 0,
			"TransactionID": "TKB0000000",
		}
	}


class TestReceiptRecovery(FrappeTestCase):
	def setUp(self):
		if not frappe.db.exists("OneRC Payment Gateway", GATEWAY):
			self.skipTest("the Mpesa Daraja gateway fixture is not on this site")

		self._was_active = frappe.db.get_value("OneRC Payment Gateway", GATEWAY, "is_active")
		frappe.db.set_value("OneRC Payment Gateway", GATEWAY, "is_active", 1)

		# Sandbox skips the Safaricom source-IP check, which a synthetic POST cannot pass.
		settings = frappe.get_single("OneRC Payment Settings")
		self._environment = settings.environment
		frappe.db.set_single_value("OneRC Payment Settings", "environment", "Sandbox")

		self.created = []
		self.txn = self._transaction("ws_CO_RECOVERY_" + self._testMethodName)
		self.addCleanup(self._restore)

	def _transaction(self, checkout, status="Completed", receipt=None):
		txn = frappe.get_doc(
			{
				"doctype": "OneRC Payment Transaction",
				"gateway": GATEWAY,
				"direction": "Inbound",
				"status": status,
				"amount": 10,
				"currency": "KES",
				"source_app": "onerc_payments",
				"source_doctype": "OneRC Payment Settings",
				"source_document": "OneRC Payment Settings",
				"payer_name": "Test Payer",
				"phone_number": PHONE,
				"gateway_reference": checkout,
				"gateway_receipt": receipt,
				"gateway_detail_doctype": "Mpesa Payment",
			}
		).insert(ignore_permissions=True)
		detail = frappe.get_doc(
			{
				"doctype": "Mpesa Payment",
				"payment_transaction": txn.name,
				"checkout_request_id": checkout,
				"merchant_request_id": f"MR-{checkout}",
				"status": status,
				"expected_amount": 10,
			}
		).insert(ignore_permissions=True)
		txn.db_set("gateway_detail", detail.name, update_modified=False)
		self.created.append(txn.name)
		return txn

	def _restore(self):
		frappe.set_user("Administrator")
		for name in self.created:
			for detail in frappe.get_all("Mpesa Payment", filters={"payment_transaction": name}, pluck="name"):
				frappe.delete_doc("Mpesa Payment", detail, force=True, ignore_permissions=True)
			frappe.db.delete("Comment", {"reference_doctype": "OneRC Payment Transaction", "reference_name": name})
			frappe.delete_doc("OneRC Payment Transaction", name, force=True, ignore_permissions=True)
		frappe.db.set_value("OneRC Payment Gateway", GATEWAY, "is_active", self._was_active)
		frappe.db.set_single_value("OneRC Payment Settings", "environment", self._environment)
		# The result endpoint commits, which the per-test rollback cannot undo.
		frappe.db.commit()

	def _receipt(self, name=None):
		return frappe.db.get_value("OneRC Payment Transaction", name or self.txn.name, "gateway_receipt")

	def _detail(self, name=None):
		return frappe.get_all(
			"Mpesa Payment",
			filters={"payment_transaction": name or self.txn.name},
			fields=["name", "mpesa_receipt_number", "receipt_source", "transaction_date", "status_query_conversation_id"],
		)[0]

	# ── The code's shape ───────────────────────────────────────────

	def test_a_code_copied_from_an_sms_is_tidied(self):
		gateway = MpesaDarajaGateway(frappe._dict())
		self.assertEqual(gateway.normalise_receipt(" tkb1 a2c3d4 "), "TKB1A2C3D4")

	def test_anything_but_ten_letters_and_digits_is_refused(self):
		gateway = MpesaDarajaGateway(frappe._dict())
		for value in ("", "TKB1A2C3D", "TKB1A2C3D4X", "TKB1-2C3D4"):
			with self.subTest(value=value), self.assertRaises(frappe.ValidationError):
				gateway.normalise_receipt(value)

	# ── Staff type it in ───────────────────────────────────────────

	def test_staff_enter_the_code_and_the_source_app_hears_of_it(self):
		with patch.object(pay, "_notify_source_receipt") as notify:
			result = pay.record_gateway_receipt(self.txn.name, "tkb1a2c3d4")

		self.assertEqual(result["receipt"], "TKB1A2C3D4")
		self.assertEqual(self._receipt(), "TKB1A2C3D4")
		detail = self._detail()
		self.assertEqual(detail.mpesa_receipt_number, "TKB1A2C3D4")
		self.assertEqual(detail.receipt_source, "Entered by Staff")
		notify.assert_called_once()
		self.assertEqual(notify.call_args.args[1], "TKB1A2C3D4")
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{"reference_doctype": "OneRC Payment Transaction", "reference_name": self.txn.name, "comment_type": "Info"},
			)
		)

	def test_an_existing_receipt_is_never_replaced(self):
		self.txn.db_set("gateway_receipt", "TKB9Z9Z9Z9", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			pay.record_gateway_receipt(self.txn.name, "TKB1A2C3D4")
		self.assertEqual(self._receipt(), "TKB9Z9Z9Z9")

	def test_a_payment_not_yet_completed_cannot_take_a_receipt(self):
		self.txn.db_set("status", "Pending", update_modified=False)
		with self.assertRaises(frappe.ValidationError):
			pay.record_gateway_receipt(self.txn.name, "TKB1A2C3D4")

	def test_one_sms_cannot_receipt_two_payments(self):
		self._transaction("ws_CO_RECOVERY_other", receipt="TKB1A2C3D4")
		with self.assertRaises(frappe.ValidationError):
			pay.record_gateway_receipt(self.txn.name, "tkb1a2c3d4")
		self.assertIsNone(self._receipt())

	def test_entering_a_code_needs_write_on_the_transaction(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			pay.record_gateway_receipt(self.txn.name, "TKB1A2C3D4")

	# ── Daraja looks it up ─────────────────────────────────────────

	def _gateway(self, **settings):
		return MpesaDarajaGateway(
			frappe._dict(
				environment="Sandbox",
				mpesa_shortcode="174379",
				mpesa_callback_base_url="https://payments.example.org",
				mpesa_initiator_name="testapi",
				**settings,
			)
		)

	def test_the_lookup_asks_by_the_stk_request_id_and_remembers_the_answer_id(self):
		gateway = self._gateway()
		response = {
			"OriginatorConversationID": "1236-7134259-1",
			"ConversationID": "AG_RECOVERY_1",
			"ResponseCode": "0",
			"ResponseDescription": "Accept the service request successfully.",
		}
		with (
			patch.object(MpesaDarajaGateway, "_initiator", return_value=("testapi", "CRED")),
			patch.object(MpesaDarajaGateway, "_get_token", return_value="tok"),
			patch("onerc_payments.gateways.mpesa_daraja.requests.post", return_value=_FakeResponse(response)) as post,
		):
			gateway.request_receipt(self.txn)

		sent = post.call_args.kwargs["json"]
		self.assertTrue(post.call_args.args[0].endswith("/mpesa/transactionstatus/v1/query"))
		self.assertEqual(sent["CommandID"], "TransactionStatusQuery")
		self.assertEqual(sent["OriginalConversationID"], f"MR-{self.txn.gateway_reference}")
		self.assertEqual(sent["SecurityCredential"], "CRED")
		self.assertEqual(
			sent["ResultURL"],
			"https://payments.example.org/api/method/onerc_payments.api.v1.payment.receipt_query_result",
		)
		self.assertEqual(self._detail().status_query_conversation_id, "AG_RECOVERY_1")

	def test_a_refused_lookup_says_why(self):
		with (
			patch.object(MpesaDarajaGateway, "_initiator", return_value=("testapi", "CRED")),
			patch.object(MpesaDarajaGateway, "_get_token", return_value="tok"),
			patch(
				"onerc_payments.gateways.mpesa_daraja.requests.post",
				return_value=_FakeResponse({"errorCode": "401.002.01", "errorMessage": "Invalid Access Token"}),
			),
			self.assertRaises(frappe.ValidationError),
		):
			self._gateway().request_receipt(self.txn)

	def test_no_lookup_without_an_initiator(self):
		with patch.object(MpesaDarajaGateway, "_initiator", return_value=("", None)):
			self.assertFalse(self._gateway().can_request_receipt())
			with self.assertRaises(frappe.ValidationError):
				self._gateway().request_receipt(self.txn)

	def _fire(self, payload):
		frappe.local.request = frappe._dict(data=json.dumps(payload).encode())
		frappe.form_dict = frappe._dict()
		with patch.object(pay, "_notify_source_receipt") as notify:
			response = pay.receipt_query_result()
		return response, notify

	def _await_answer(self, conversation_id="AG_RECOVERY_2"):
		frappe.db.set_value(
			"Mpesa Payment", self._detail().name, "status_query_conversation_id", conversation_id
		)
		return conversation_id

	def test_the_answer_fills_the_receipt(self):
		conversation = self._await_answer()

		response, notify = self._fire(_status_result(conversation))

		self.assertEqual(response["ResultCode"], 0)
		self.assertEqual(self._receipt(), "TKB1A2C3D4")
		detail = self._detail()
		self.assertEqual(detail.receipt_source, "Status Query")
		self.assertEqual(str(detail.transaction_date), "2026-11-09 08:47:00")
		notify.assert_called_once()

	def test_an_answer_for_a_different_amount_is_not_applied(self):
		conversation = self._await_answer()

		_, notify = self._fire(_status_result(conversation, amount="500"))

		self.assertIsNone(self._receipt())
		notify.assert_not_called()

	def test_an_answer_nobody_asked_for_changes_nothing(self):
		self._await_answer()

		_, notify = self._fire(_status_result("AG_SOMEBODY_ELSE"))

		self.assertIsNone(self._receipt())
		notify.assert_not_called()

	def test_a_failed_lookup_changes_nothing(self):
		conversation = self._await_answer()

		_, notify = self._fire(_status_result(conversation, result_code=2001, status="Failed"))

		self.assertIsNone(self._receipt())
		notify.assert_not_called()

	def test_a_code_already_on_another_payment_is_not_applied(self):
		self._transaction("ws_CO_RECOVERY_dupe", receipt="TKB1A2C3D4")
		conversation = self._await_answer()

		self._fire(_status_result(conversation))

		self.assertIsNone(self._receipt())

	def test_a_single_result_parameter_is_read_too(self):
		payload = _status_result("AG_X")
		payload["Result"]["ResultParameters"]["ResultParameter"] = {"Key": "ReceiptNo", "Value": "TKB1A2C3D4"}
		# No TransactionStatus means it cannot be read as completed.
		self.assertIsNone(MpesaDarajaGateway.parse_receipt_result(payload)["receipt"])

	# ── The hourly sweep ───────────────────────────────────────────

	def _age(self, minutes):
		frappe.db.set_value(
			"OneRC Payment Transaction",
			self.txn.name,
			"modified",
			add_to_date(now_datetime(), minutes=-minutes),
			update_modified=False,
		)

	def _sweep(self):
		with (
			patch.object(MpesaDarajaGateway, "can_request_receipt", return_value=True),
			patch.object(MpesaDarajaGateway, "request_receipt") as ask,
		):
			tasks.request_missing_receipts()
		return [call.args[0].name for call in ask.call_args_list]

	def test_the_sweep_asks_for_a_receipt_the_callback_never_brought(self):
		self._age(60)
		self.assertIn(self.txn.name, self._sweep())

	def test_the_sweep_gives_the_callback_its_chance_first(self):
		self._age(2)
		self.assertNotIn(self.txn.name, self._sweep())

	def test_the_sweep_does_not_ask_again_straight_away(self):
		self._age(60)
		frappe.db.set_value("Mpesa Payment", self._detail().name, "status_query_requested_on", now_datetime())
		self.assertNotIn(self.txn.name, self._sweep())
