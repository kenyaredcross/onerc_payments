# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""The KCB and Jenga endpoints through Frappe's whole web stack, as the providers reach them.

Calling an endpoint function directly skips what Frappe does before it: routing, the
``kcb`` alias, authentication (which once refused every Jenga notification, because Frappe
reads HTTP Basic credentials as its own API-key login), the allowed HTTP methods, and how
the response is sent. These tests go through ``frappe.app.application`` instead.
"""

from unittest.mock import patch

import frappe

from onerc_payments.api.v1 import payment as pay
from onerc_payments.tests.account_utils import (
	CARD_ACCOUNT,
	IPN_PASSWORD,
	IPN_USER,
	TRANSACTION,
	AccountTestCase,
	basic,
	jenga_ipn,
	kcb_callback,
	new_receipt,
)

NOTIFICATION = "/api/method/onerc_payments.api.v1.card.jenga_notification"
CHECKOUT = "/api/method/onerc_payments.api.v1.card.checkout"
CALLBACK_LOG = "OneRC Payment Callback"


class TestJengaThroughTheWebServer(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_card()
		self.result = self.start_card(amount=25)
		self.name = self.result["transaction_id"]
		self.reference = self.result["gateway_reference"]

	def notify(self, credentials, payload=None):
		with self.jenga_http(), patch.object(pay, "_notify_source_app") as notified:
			response = self.through_the_web_server(
				"post",
				NOTIFICATION,
				json=payload or jenga_ipn(self.reference),
				headers={"Authorization": basic(*credentials)},
			)
		return response, notified

	def test_jengas_notification_with_basic_auth_reaches_the_endpoint_and_completes(self):
		response, notified = self.notify((IPN_USER, IPN_PASSWORD))

		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(response.json["message"], {"status": "Received"})
		self.assertEqual(self.doc(self.name).status, "Completed")
		self.assertEqual(notified.call_count, 1)

	def test_a_wrong_password_is_answered_and_kept_by_the_endpoint_itself(self):
		response, notified = self.notify((IPN_USER, "not-the-password"))

		self.assertEqual(response.status_code, 401)
		self.assertEqual(response.json["message"], {"status": "Unauthorised"})
		self.assertEqual(self.doc(self.name).status, "Pending")
		self.assertFalse(notified.called)
		# Kept, unlike a refusal by Frappe before the endpoint: the message is on the log.
		self.assertEqual(
			frappe.db.get_value(CALLBACK_LOG, {"payment_transaction": self.name}, "status"), "Ignored"
		)

	def test_basic_credentials_anywhere_else_are_still_frappes_to_judge(self):
		for path in ("/api/method/frappe.auth.get_logged_user", "/api/method/ping"):
			with self.subTest(path):
				response = self.through_the_web_server(
					"post", path, headers={"Authorization": basic(IPN_USER, IPN_PASSWORD)}
				)
				self.assertEqual(response.status_code, 401)

	def test_the_checkout_page_is_served_as_html(self):
		with self.jenga_http():
			response = self.through_the_web_server("get", f"{CHECKOUT}?ref={self.reference}")

		self.assertEqual(response.status_code, 200)
		self.assertTrue(response.headers["Content-Type"].startswith("text/html"))
		self.assertIn("form-action https:", response.headers["Content-Security-Policy"])
		self.assertEqual(response.headers["Cache-Control"], "no-store")
		self.assertIn('action="https://v3-uat.jengapgw.io/processPayment"', response.get_data(as_text=True))

	def test_the_checkout_page_reflects_no_markup(self):
		response = self.through_the_web_server("get", f'{CHECKOUT}?ref="><script>alert(1)</script>')

		self.assertEqual(response.status_code, 404)
		self.assertNotIn("<script>alert", response.get_data(as_text=True))

	def test_starting_or_reading_a_payment_is_not_open_over_http(self):
		before = frappe.db.count(TRANSACTION)
		for path, body in (
			(
				"/api/method/onerc_payments.api.v1.payment.initiate_payment",
				{
					"amount": 1,
					"currency": "USD",
					"direction": "Inbound",
					"source_app": "x",
					"source_doctype": "User",
					"source_document": "Guest",
					"gateway": CARD_ACCOUNT,
				},
			),
			("/api/method/onerc_payments.api.v1.payment.check_payment_status", {"transaction_id": self.name}),
		):
			with self.subTest(path):
				response = self.through_the_web_server("post", path, json=body)
				self.assertIn(response.status_code, (401, 403, 404))
				self.assertNotIn(self.reference, response.get_data(as_text=True))
		self.assertEqual(frappe.db.count(TRANSACTION), before)


class TestKcbThroughTheWebServer(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_kcb()
		self.result, _ = self.start_kcb()
		self.name = self.result["transaction_id"]
		self.request_ids = self.ids(self.name)

	def test_kcb_reaches_the_callback_at_the_short_address(self):
		payload = kcb_callback(
			self.request_ids.merchant_request_id,
			self.request_ids.checkout_request_id,
			receipt=new_receipt(),
			amount=10,
		)
		self.references.append(self.request_ids.merchant_request_id)

		with patch.object(pay, "_notify_source_app") as notified:
			response = self.through_the_web_server("post", "/api/method/kcb", json=payload)

		self.assertEqual(response.status_code, 200, response.get_data(as_text=True))
		self.assertEqual(response.json["message"], pay.ACCEPTED)
		self.assertEqual(self.doc(self.name).status, "Completed")
		self.assertEqual(notified.call_count, 1)

	def test_the_callback_takes_posts_only(self):
		response = self.through_the_web_server("get", "/api/method/kcb")

		self.assertNotEqual(response.status_code, 200)
		self.assertEqual(self.doc(self.name).status, "Pending")
