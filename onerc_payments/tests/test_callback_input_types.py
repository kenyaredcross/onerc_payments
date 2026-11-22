"""A callback's values come from whoever posts to the public endpoint, as JSON: a list or an
object can stand where Daraja sends a string. A value that reaches a query as a filter must
be a plain string - ["like", "ws_CO_%"] would otherwise match whichever payment came first."""

import json

import frappe

from onerc_payments.api.v1 import payment as pay
from onerc_payments.tests.utils import GATEWAY, PaymentTestCase, stk_callback

LOG = "OneRC Payment Callback"


class TestCallbackValuesMustBePlainStrings(PaymentTestCase):
	def post(self, payload):
		"""Post straight to the endpoint. Not through fire(): its cleanup deletes callback logs
		by each posted CheckoutRequestID, and a list there would be a filter too."""
		frappe.local.request = frappe._dict(data=json.dumps(payload).encode(), headers={})
		frappe.local.form_dict = frappe._dict()
		pay.payment_callback(gateway_name=GATEWAY)
		marker = payload["Body"]["stkCallback"]["MerchantRequestID"]
		logs = frappe.get_all(LOG, filters={"payload": ["like", f"%{marker}%"]}, pluck="name")
		for name in logs:
			self.addCleanup(self._delete_log, name)
		return frappe.get_doc(LOG, logs[0]) if logs else None

	@staticmethod
	def _delete_log(name):
		frappe.db.delete(LOG, {"name": name})
		frappe.db.commit()

	def test_a_list_as_checkout_request_id_matches_no_payment(self):
		tag = frappe.generate_hash(length=8).upper()
		txn = self.transaction(checkout=f"ws_CO_LISTREF_{tag}")
		for value in (["like", f"ws_CO_LISTREF_{tag}%"], ["is", "set"]):
			payload = stk_callback("placeholder", receipt=f"LST{tag}{len(value[1])}")
			payload["Body"]["stkCallback"]["CheckoutRequestID"] = value
			payload["Body"]["stkCallback"]["MerchantRequestID"] = f"MR-LIST-{tag}-{value[0]}"

			log = self.post(payload)

			self.assertEqual(self.value(txn.name, "status"), "Pending", value)
			self.assertFalse(self.value(txn.name, "gateway_receipt"), value)
			self.assertIsNotNone(log, value)
			self.assertFalse(log.payment_transaction, value)
			self.assertNotIn(log.status, ("Processed", "Awaiting confirmation"), value)

	def test_a_receipt_that_is_not_a_string_is_not_taken(self):
		txn = self.transaction()
		payload = stk_callback(txn.gateway_reference)
		for item in payload["Body"]["stkCallback"]["CallbackMetadata"]["Item"]:
			if item["Name"] == "MpesaReceiptNumber":
				item["Value"] = ["like", "%"]

		self.fire(payload)

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertFalse(self.value(txn.name, "gateway_receipt"))
		self.assertFalse(self.value(txn.name, "needs_review"))
		detail = frappe.db.get_value(
			"Mpesa Payment", {"payment_transaction": txn.name}, "mpesa_receipt_number"
		)
		self.assertFalse(detail)
