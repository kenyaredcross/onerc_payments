# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Jenga card payments: the checkout, the payer's return, and the notification that decides."""

import base64
import json
from html.parser import HTMLParser
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from onerc_payments.api.v1 import card
from onerc_payments.api.v1 import payment as pay
from onerc_payments.gateways.base import MANUAL_CHECK
from onerc_payments.gateways.jenga import ORDER_REFERENCE_LENGTH
from onerc_payments.tests.account_utils import (
	BASE,
	CARD_ACCOUNT,
	CARD_ACCOUNT_B,
	IPN_PASSWORD,
	IPN_PASSWORD_B,
	IPN_USER,
	IPN_USER_B,
	MERCHANT,
	TRANSACTION,
	AccountTestCase,
	basic,
	jenga_ipn,
)


class _Form(HTMLParser):
	def __init__(self):
		super().__init__()
		self.action = None
		self.fields = {}

	def handle_starttag(self, tag, attrs):
		attrs = dict(attrs)
		if tag == "form":
			self.action = attrs.get("action")
		if tag == "input":
			self.fields[attrs.get("name")] = attrs.get("value")


def form_of(response):
	parser = _Form()
	parser.feed(response.get_data(as_text=True))
	return parser


class TestCardAccount(AccountTestCase):
	def test_it_cannot_be_switched_on_without_notification_credentials(self):
		self.make_card(activate=False, ipn_username=None, ipn_password=None)

		with self.assertRaises(frappe.ValidationError) as caught:
			self.activate(CARD_ACCOUNT)
		self.assertIn("jenga_notification", str(caught.exception))

	def test_saving_shows_the_address_to_register_with_jenga(self):
		self.make_card(activate=False)

		self.assertEqual(
			frappe.db.get_value("Jenga Account", CARD_ACCOUNT, "notification_url"),
			f"{BASE}/api/method/onerc_payments.api.v1.card.jenga_notification",
		)

	def test_a_failed_sign_in_or_bad_key_leaves_no_secret_in_a_logged_traceback(self):
		import requests

		from onerc_payments.gateways import get_gateway

		self.make_card(
			activate=False,
			private_key="-----BEGIN PRIVATE KEY-----\nZZKEYTEXTNOTAKEY\n-----END PRIVATE KEY-----",
		)
		driver = get_gateway(CARD_ACCOUNT, allow_inactive=True)
		tracebacks = []
		with patch(
			"onerc_payments.gateways.jenga.requests.post",
			side_effect=requests.exceptions.ConnectionError("no route to host"),
		):
			for step in (lambda: driver.get_token(refresh=True), driver._private_key):
				try:
					step()
				except frappe.ValidationError:
					tracebacks.append(frappe.get_traceback(with_context=True))
		frappe.clear_messages()

		self.assertEqual(len(tracebacks), 2)
		for text in tracebacks:
			for secret in ("test-api-key", "test-consumer-secret", "ZZKEYTEXTNOTAKEY"):
				self.assertNotIn(secret, text)
		self.assertIn("no route to host", tracebacks[0])

	def test_two_accounts_cannot_share_a_notification_username(self):
		self.make_card(activate=False)

		with self.assertRaises(frappe.ValidationError):
			self.make_card(activate=False, name=CARD_ACCOUNT_B, ipn_username=IPN_USER)


class TestCardStart(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_card()

	def test_it_returns_a_checkout_page_on_this_site(self):
		result = self.start_card(return_url="/donate/thank-you")

		self.assertEqual(result["status"], "Pending")
		reference = result["gateway_reference"]
		self.assertEqual(len(reference), ORDER_REFERENCE_LENGTH)
		self.assertTrue(reference.isalnum())
		self.assertEqual(
			result["checkout_url"], f"{BASE}/api/method/onerc_payments.api.v1.card.checkout?ref={reference}"
		)
		detail = frappe.get_doc("Jenga Payment", {"payment_transaction": result["transaction_id"]})
		self.assertEqual(
			(detail.order_reference, detail.return_url, detail.status),
			(reference, "/donate/thank-you", "Pending"),
		)

	def test_a_return_address_off_this_site_is_refused_before_anything_is_written(self):
		for value in ("https://elsewhere.example/thanks", "//elsewhere.example/x", "/\\elsewhere.example"):
			with self.subTest(value), self.assertRaises(frappe.ValidationError):
				self.start_card(return_url=value)
		self.assertFalse(frappe.db.exists(TRANSACTION, {"source_document": self.document}))

	def test_a_currency_the_merchant_does_not_take_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.start_card(currency="EUR")

	def test_a_double_click_gets_the_same_checkout(self):
		first = self.start_card()
		second = self.start_card()

		self.assertEqual(second["transaction_id"], first["transaction_id"])
		self.assertEqual(second["checkout_url"], first["checkout_url"])

	def test_wrong_credentials_fail_before_the_payer_sees_a_page(self):
		from onerc_payments.tests.utils import FakeResponse

		with patch("onerc_payments.gateways.jenga.requests.post", return_value=FakeResponse({}, 401)):
			result = self.initiate(CARD_ACCOUNT, 25, "USD", None)

		self.assertEqual(result["status"], "Failed")
		self.assertNotIn("checkout_url", result)


class TestCheckoutPage(AccountTestCase):
	def setUp(self):
		super().setUp()
		from cryptography.hazmat.primitives import serialization
		from cryptography.hazmat.primitives.asymmetric import rsa

		self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
		pem = self.key.private_bytes(
			serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
		).decode()
		self.make_card(private_key=pem)
		self.result = self.start_card(amount=25)
		self.reference = self.result["gateway_reference"]
		frappe.local.request = None

	def open(self, ref=None):
		with self.jenga_http():
			return card.checkout(ref=ref or self.reference)

	def test_it_posts_the_payer_to_jenga_with_a_signed_order(self):
		from cryptography.hazmat.primitives import hashes
		from cryptography.hazmat.primitives.asymmetric import padding

		response = self.open()

		self.assertEqual(response.status_code, 200)
		self.assertIn("form-action https:", response.headers["Content-Security-Policy"])
		form = form_of(response)
		self.assertEqual(form.action, "https://v3-uat.jengapgw.io/processPayment")
		fields = form.fields
		callback = f"{BASE}/api/method/onerc_payments.api.v1.card.jenga_return"
		self.assertEqual(
			(fields["merchantCode"], fields["orderReference"], fields["currency"], fields["orderAmount"]),
			(MERCHANT, self.reference, "USD", "25.00"),
		)
		self.assertEqual(fields["callbackUrl"], callback)
		self.assertEqual(fields["token"], "jenga-token")
		self.key.public_key().verify(
			base64.b64decode(fields["signature"]),
			f"{MERCHANT}{self.reference}USD25.00{callback}".encode(),
			padding.PKCS1v15(),
			hashes.SHA256(),
		)
		self.assertEqual(
			frappe.db.get_value("Jenga Payment", {"order_reference": self.reference}, "checkout_opens"), 1
		)

	def test_an_unknown_or_malformed_reference_gets_no_form(self):
		for ref in ("NOTAREALREFERENCE000", "<script>", ""):
			with self.subTest(ref):
				response = self.open(ref=ref) if ref else card.checkout(ref=None)
				self.assertEqual(response.status_code, 404)
				self.assertIsNone(form_of(response).action)
				self.assertNotIn("<script>alert", response.get_data(as_text=True))

	def test_a_paid_or_closed_payment_gets_no_form(self):
		for status in ("Completed", "Cancelled"):
			with self.subTest(status):
				frappe.db.set_value(TRANSACTION, self.result["transaction_id"], "status", status)
				self.assertIsNone(form_of(self.open()).action)

	def test_an_old_link_gets_no_form(self):
		frappe.db.set_value(
			TRANSACTION, self.result["transaction_id"], "creation", add_to_date(now_datetime(), minutes=-31)
		)

		self.assertIsNone(form_of(self.open()).action)

	def test_switching_card_payments_off_stops_new_checkouts(self):
		self.activate(CARD_ACCOUNT, on=False)

		response = self.open()

		self.assertEqual(response.status_code, 503)
		self.assertIsNone(form_of(response).action)


class TestCardNotification(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_card()
		self.result = self.start_card(amount=25)
		self.name = self.result["transaction_id"]
		self.reference = self.result["gateway_reference"]

	def test_an_authenticated_notification_completes_the_payment_once(self):
		payload = jenga_ipn(self.reference, reference="JGREF0001")

		with patch.object(pay, "_notify_source_app") as notify:
			_reply, status = self.fire_ipn(payload)
			self.fire_ipn(payload)  # Jenga retries

		self.assertEqual(status, 200)
		txn = self.doc(self.name)
		self.assertEqual((txn.status, txn.gateway_receipt), ("Completed", "JGREF0001"))
		self.assertFalse(txn.needs_review)
		self.assertEqual(notify.call_count, 1)
		log = self.last_log("Card notification")
		self.assertEqual(
			(log.status, log.gateway, log.payment_transaction), ("Processed", CARD_ACCOUNT, self.name)
		)
		detail = frappe.get_doc("Jenga Payment", {"payment_transaction": self.name})
		self.assertEqual((detail.status, detail.jenga_reference), ("Completed", "JGREF0001"))

	def test_the_order_reference_is_found_in_any_likely_field(self):
		for field in ("billNumber", "orderReference", "customer.reference"):
			with self.subTest(field):
				self.assertEqual(
					pay._callback_reference(
						"Card notification", None, jenga_ipn(self.reference, field=field)
					),
					self.reference,
				)

	def test_a_notification_without_the_password_changes_nothing(self):
		for credentials in (None, (IPN_USER, "wrong-password"), ("someone", IPN_PASSWORD)):
			for state in ("SUCCESS", "FAILED"):
				with self.subTest(credentials=credentials, state=state):
					_reply, status = self.fire_ipn(jenga_ipn(self.reference, status=state), credentials)

					self.assertEqual(status, 401)
					txn = self.doc(self.name)
					self.assertEqual(txn.status, "Pending")
					self.assertFalse(txn.needs_review)
					self.assertEqual(self.last_log("Card notification").status, "Ignored")

	def test_another_merchants_password_cannot_settle_it_even_when_processed_again(self):
		self.make_card(name=CARD_ACCOUNT_B)
		self.fire_ipn(jenga_ipn(self.reference), (IPN_USER_B, IPN_PASSWORD_B))
		log = self.last_log("Card notification")
		self.assertEqual(log.status, "Ignored")

		with self.jenga_http():
			pay.reprocess_saved_callback(log.name)

		self.assertEqual(self.doc(self.name).status, "Pending")
		self.assertEqual(frappe.db.get_value("OneRC Payment Callback", log.name, "status"), "Ignored")

	def test_a_saved_authenticated_notification_processed_again_still_counts(self):
		frappe.db.set_value("OneRC Payment Gateway", CARD_ACCOUNT, "is_active", 0)
		with patch.object(pay, "_process_payment_callback", side_effect=RuntimeError("worker died")):
			self.fire_ipn(jenga_ipn(self.reference))
		log = self.last_log("Card notification")
		self.assertEqual(log.status, "Error")

		with self.jenga_http():
			pay.reprocess_saved_callback(log.name)

		self.assertEqual(self.doc(self.name).status, "Completed")

	def test_an_authenticated_failure_fails_the_payment(self):
		self.fire_ipn(jenga_ipn(self.reference, status="FAILED"))

		self.assertEqual(self.doc(self.name).status, "Failed")

	def test_a_different_amount_or_currency_goes_to_a_person(self):
		for case, payload in (
			("amount", jenga_ipn(self.reference, amount=1)),
			("currency", jenga_ipn(self.reference, currency="KES")),
		):
			with self.subTest(case), patch.object(pay, "_notify_source_app") as notify:
				self.fire_ipn(payload)

				txn = self.doc(self.name)
				self.assertEqual(txn.status, "Pending")
				self.assertTrue(txn.needs_review)
				self.assertFalse(notify.called)
				log = self.last_log("Card notification")
				self.assertEqual((log.status, log.gateway_confirmation), ("Needs review", MANUAL_CHECK))

	def test_a_card_charged_in_another_currency_completes_on_the_order(self):
		self.fire_ipn(
			jenga_ipn(self.reference, amount=25, currency="USD", charged=3250, charged_currency="KES")
		)

		self.assertEqual(self.doc(self.name).status, "Completed")

	def test_the_daraja_endpoint_cannot_settle_a_card_payment(self):
		self.fire({"reference": self.reference, "status": "Completed"})
		self.fire(jenga_ipn(self.reference))

		self.assertEqual(self.doc(self.name).status, "Pending")


class TestPayerReturn(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_card()
		self.result = self.start_card(amount=25, return_url="/donate/thank-you")
		self.name = self.result["transaction_id"]
		self.reference = self.result["gateway_reference"]

	def come_back(self, **data):
		frappe.local.request = None
		frappe.local.form_dict = frappe._dict(data)
		with self.jenga_http() as http:
			response = card.jenga_return(**data)
		return response, http

	def test_the_payers_return_completes_nothing(self):
		response, http = self.come_back(
			orderReference=self.reference, status="paid", transactionId="FAKE1", amount="25.00"
		)

		self.assertEqual(self.doc(self.name).status, "Pending")
		self.assertFalse(http.queried.called)
		page = response.get_data(as_text=True)
		self.assertIn("Confirming your payment", page)
		self.assertIn("/donate/thank-you?payment=", page)
		log = self.last_log("Card return")
		self.assertEqual((log.status, log.payment_transaction), ("Processed", self.name))

	def test_it_shows_the_result_once_jenga_has_confirmed(self):
		self.fire_ipn(jenga_ipn(self.reference))

		response, _ = self.come_back(orderReference=self.reference)

		self.assertIn("Payment received", response.get_data(as_text=True))

	def test_an_unknown_reference_is_noted_and_shown_as_not_found(self):
		response, _ = self.come_back(orderReference="NOTAREALREFERENCE000")

		self.assertIn("Payment not found", response.get_data(as_text=True))
		self.assertEqual(self.last_log("Card return").status, "Unmatched")
		self.references.append("NOTAREALREFERENCE000")

	def test_with_the_status_query_on_the_return_asks_jenga(self):
		frappe.db.set_value("Jenga Account", CARD_ACCOUNT, "query_status", 1)
		answer = {
			"status": True,
			"data": {"state": "COMPLETED", "amount": 25, "currency": "USD", "transactionReference": "JGQ1"},
		}
		frappe.local.request = None
		frappe.local.form_dict = frappe._dict(orderReference=self.reference)

		with self.jenga_http(query=answer) as http, patch.object(pay, "_notify_source_app") as notify:
			card.jenga_return(orderReference=self.reference)

		self.assertTrue(http.queried.called)
		self.assertEqual(self.doc(self.name).status, "Completed")
		self.assertEqual(notify.call_count, 1)


class TestStatusQuery(AccountTestCase):
	"""Jenga's Transaction Details query, for an account that switched it on."""

	def setUp(self):
		super().setUp()
		self.make_card(query_status=1)
		self.result = self.start_card(amount=25)
		self.name = self.result["transaction_id"]
		self.reference = self.result["gateway_reference"]

	def ask(self, data):
		with (
			self.jenga_http(query={"status": True, "data": data}),
			patch.object(pay, "_notify_source_app") as notify,
		):
			pay.refresh_status(self.doc(self.name))
		return notify

	def test_a_matching_answer_completes_it(self):
		notify = self.ask(
			{"state": "COMPLETED", "amount": 25, "currency": "USD", "transactionReference": "JGQ2"}
		)

		txn = self.doc(self.name)
		self.assertEqual((txn.status, txn.gateway_receipt), ("Completed", "JGQ2"))
		self.assertEqual(notify.call_count, 1)

	def test_an_answer_for_another_amount_or_currency_or_none_completes_nothing(self):
		for case, data in (
			("amount", {"state": "COMPLETED", "amount": 1, "currency": "USD"}),
			("currency", {"state": "COMPLETED", "amount": 25, "currency": "KES"}),
			("no amount", {"state": "COMPLETED", "currency": "USD"}),
			("no currency", {"state": "COMPLETED", "amount": 25}),
		):
			with self.subTest(case):
				notify = self.ask(data)

				txn = self.doc(self.name)
				self.assertEqual(txn.status, "Pending")
				self.assertTrue(txn.needs_review)
				self.assertFalse(notify.called)

	def test_a_notification_the_query_disagrees_with_goes_to_a_person(self):
		disagrees = {"status": True, "data": {"state": "COMPLETED", "amount": 1, "currency": "USD"}}
		frappe.local.request = frappe._dict(
			data=json.dumps(jenga_ipn(self.reference)).encode(),
			headers={"Authorization": basic(IPN_USER, IPN_PASSWORD)},
		)
		frappe.local.form_dict = frappe._dict()
		frappe.local.response = frappe._dict()

		with self.jenga_http(query=disagrees), patch.object(pay, "_notify_source_app") as notify:
			card.jenga_notification()

		txn = self.doc(self.name)
		self.assertEqual(txn.status, "Pending")
		self.assertTrue(txn.needs_review)
		self.assertFalse(notify.called)
		self.assertEqual(self.last_log("Card notification").gateway_confirmation, MANUAL_CHECK)


class TestDetailRecord(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_card()

	def test_a_checkout_whose_record_cannot_be_written_is_not_offered(self):
		from onerc_payments.gateways.jenga import JengaGateway

		with patch.object(JengaGateway, "record_initiation_details", side_effect=RuntimeError("disk full")):
			result = self.start_card()

		self.assertEqual(result["status"], "Failed")
		self.assertNotIn("checkout_url", result)
		self.assertFalse(frappe.db.get_value(TRANSACTION, result["transaction_id"], "initiation_unconfirmed"))

	def test_a_long_return_address_is_kept_whole(self):
		path = "/donate/thank-you?" + "&".join(f"field{i}=value{i}" for i in range(30))
		result = self.start_card(return_url=path)

		self.assertGreater(len(path), 140)
		self.assertEqual(
			frappe.db.get_value(
				"Jenga Payment", {"payment_transaction": result["transaction_id"]}, "return_url"
			),
			path,
		)


class TestAbandonedCheckout(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_card()
		self.result = self.start_card(amount=25)
		self.name = self.result["transaction_id"]
		self.reference = self.result["gateway_reference"]

	def age(self, minutes):
		frappe.db.set_value(TRANSACTION, self.name, "creation", add_to_date(now_datetime(), minutes=-minutes))

	def test_a_fresh_checkout_is_left_alone_without_asking_jenga(self):
		with self.jenga_http() as http:
			pay.refresh_status(self.doc(self.name))

		self.assertEqual(self.doc(self.name).status, "Pending")
		self.assertFalse(http.queried.called)

	def test_an_unanswered_checkout_is_cancelled_after_an_hour(self):
		self.age(61)

		with self.jenga_http():
			pay.refresh_status(self.doc(self.name))

		self.assertEqual(self.doc(self.name).status, "Cancelled")

	def test_a_notification_after_it_was_cancelled_recovers_it(self):
		self.age(61)
		with self.jenga_http():
			pay.refresh_status(self.doc(self.name))

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire_ipn(jenga_ipn(self.reference))

		self.assertEqual(self.doc(self.name).status, "Completed")
		self.assertEqual(notify.call_count, 1)

	def test_the_hourly_job_looks_after_card_payments_even_when_switched_off(self):
		# The job itself polls every Pending payment on the site (refresh_status, tested
		# above), so it is not run here: it would ask Safaricom about this site's others.
		from onerc_payments import tasks

		self.activate(CARD_ACCOUNT, on=False)

		self.assertIn(CARD_ACCOUNT, tasks._automatic_gateways())
