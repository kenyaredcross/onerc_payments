# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Set-up for the tests of gateways with accounts (KCB Buni tills, Jenga card merchants).

Nothing here talks to KCB or Jenga: every HTTP call the drivers make is patched. Each test
creates its own accounts (named "ZZ Test ..."), switches their gateways on, and deletes
them again - with every payment, detail record and saved callback they gathered.
"""

import base64
import json
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from threading import Thread
from unittest.mock import patch

import frappe
from frappe.utils import get_test_client

from onerc_payments.api.v1 import card, kcb
from onerc_payments.api.v1 import payment as pay
from onerc_payments.tests.utils import PHONE, SETTINGS, FakeResponse, PaymentTestCase, stk_callback

GATEWAY_DOCTYPE = "OneRC Payment Gateway"
TRANSACTION = "OneRC Payment Transaction"
CALLBACK_LOG = "OneRC Payment Callback"
BASE = "https://pay.example.org"
TEST_PREFIX = "ZACCT"

KCB_ACCOUNT = "ZZ Test KCB Till"
TILL = "1234567"

CARD_ACCOUNT = "ZZ Test Card"
CARD_ACCOUNT_B = "ZZ Test Card B"
MERCHANT = "TESTMERCH01"
IPN_USER = "zz-test-ipn"
IPN_PASSWORD = "zz-test-ipn-password-1"
IPN_USER_B = "zz-test-ipn-b"
IPN_PASSWORD_B = "zz-test-ipn-password-2"


def new_receipt():
	return ("TK" + frappe.generate_hash(length=8)).upper()


def kcb_callback(merchant, checkout, receipt=None, result_code=0, amount=10, phone=PHONE):
	"""A KCB callback: Daraja's shape, with the request IDs KCB gave the prompt."""
	payload = stk_callback(
		checkout,
		receipt=new_receipt() if receipt is None else receipt,
		result_code=result_code,
		amount=amount,
		phone=phone,
	)
	payload["Body"]["stkCallback"]["MerchantRequestID"] = merchant
	return payload


def jenga_ipn(
	order_reference,
	status="SUCCESS",
	amount=25,
	currency="USD",
	reference=None,
	field="billNumber",
	charged=None,
	charged_currency=None,
):
	"""A Jenga Instant Payment Notification (as in Jenga's IPN docs), the order reference in
	``field``. ``amount``/``currency`` are the order; ``charged*`` what the card paid."""
	payload = {
		"callbackType": "IPN",
		"customer": {"name": "Test Payer", "mobileNumber": "", "reference": ""},
		"transaction": {
			"date": "2026-11-22T10:15:00.000",
			"reference": reference or ("JG" + frappe.generate_hash(length=10)).upper(),
			"paymentMode": "CARD",
			"amount": charged if charged is not None else amount,
			"currency": charged_currency or currency,
			"orderAmount": amount,
			"orderCurrency": currency,
			"billNumber": "",
			"serviceCharge": 0,
			"status": status,
			"remarks": "Approved" if status == "SUCCESS" else "Declined",
		},
		"bank": {"reference": "BK" + frappe.generate_hash(length=8).upper(), "transactionType": "C"},
	}
	if field == "customer.reference":
		payload["customer"]["reference"] = order_reference
	else:
		payload["transaction"][field] = order_reference
	return payload


def basic(username, password):
	return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


class AccountTestCase(PaymentTestCase):
	"""PaymentTestCase, plus accounts made and removed by the test."""

	def setUp(self):
		super().setUp()
		self.settings(default_reference_prefix=TEST_PREFIX)
		self.document = f"TEST-{self._testMethodName}"[:140]
		self.accounts = []
		self.fired_logs = []
		for doctype, name in (
			("KCB Buni Account", KCB_ACCOUNT),
			("Jenga Account", CARD_ACCOUNT),
			("Jenga Account", CARD_ACCOUNT_B),
		):
			self._drop_account(doctype, name)
		frappe.db.commit()
		from onerc_payments.gateways import jenga, kcb_buni

		kcb_buni.clear_token_cache()
		jenga.clear_token_cache()
		# The guessing alarm counts every unmatched KCB callback on the site; a test says
		# how many there are rather than inherit whatever else is on it.
		unmatched = patch("onerc_payments.gateways.kcb_buni._recent_unmatched_callbacks", return_value=0)
		self.unmatched = unmatched.start()
		self.addCleanup(unmatched.stop)

	# ── accounts ────────────────────────────────────────────────────

	def make_account(self, doctype, values, activate=True):
		doc = frappe.get_doc({"doctype": doctype, **values}).insert(ignore_permissions=True)
		self.accounts.append((doctype, doc.name))
		if activate:
			self.activate(doc.name)
		return doc

	def activate(self, name, on=True):
		gateway = frappe.get_doc(GATEWAY_DOCTYPE, name)
		gateway.is_active = 1 if on else 0
		gateway.save(ignore_permissions=True)
		return gateway

	def make_kcb(self, activate=True, **values):
		return self.make_account(
			"KCB Buni Account",
			{
				"account_name": KCB_ACCOUNT,
				"till_number": TILL,
				"environment": "Sandbox",
				"consumer_key": "test-key",
				"consumer_secret": "test-secret",
				"callback_base_url": BASE,
				"complete_on_callback": 1,
				**values,
			},
			activate,
		)

	def make_card(self, activate=True, name=CARD_ACCOUNT, **values):
		return self.make_account(
			"Jenga Account",
			{
				"account_name": name,
				"merchant_code": MERCHANT,
				"environment": "Sandbox",
				"currencies": "KES, USD",
				"api_key": "test-api-key",
				"consumer_secret": "test-consumer-secret",
				"ipn_username": IPN_USER if name == CARD_ACCOUNT else IPN_USER_B,
				"ipn_password": IPN_PASSWORD if name == CARD_ACCOUNT else IPN_PASSWORD_B,
				"callback_base_url": BASE,
				"default_email": "payer@example.org",
				**values,
			},
			activate,
		)

	# ── KCB ─────────────────────────────────────────────────────────

	def kcb_reply(self):
		token = frappe.generate_hash(length=10)
		return {
			"header": {"statusCode": "0", "statusDescription": "Success. Request accepted for processing"},
			"response": {
				"MerchantRequestID": f"KCBMR-{token}",
				"CheckoutRequestID": f"ws_CO_KCB_{token}",
				"ResponseCode": 0,
				"ResponseDescription": "Success. Request accepted for processing",
				"CustomerMessage": "Success. Request accepted for processing",
			},
		}

	def start_kcb(self, amount=10, phone="0712345678", reply=None, error=None, status=200, **kwargs):
		"""initiate_payment through the test till, KCB's reply canned. Returns (result, sent)."""
		reply = reply if reply is not None else self.kcb_reply()
		sent = []

		def post(url, **kw):
			if url.endswith("/token"):
				return FakeResponse({"access_token": "kcb-token", "expires_in": 3599})
			sent.append({"url": url, **kw})
			if error:
				raise error
			return FakeResponse(reply, status)

		with patch("onerc_payments.gateways.kcb_buni.requests.post", side_effect=post):
			result = self.initiate(kwargs.pop("gateway", KCB_ACCOUNT), amount, "KES", phone, **kwargs)
		response = reply.get("response") if isinstance(reply, dict) else None
		for value in ((response or {}).get("MerchantRequestID"), (response or {}).get("CheckoutRequestID")):
			if isinstance(value, str):
				self.references.append(value)
		return result, sent

	def ids(self, transaction_name):
		return frappe.db.get_value(
			"KCB Buni Payment",
			{"payment_transaction": transaction_name},
			["merchant_request_id", "checkout_request_id", "invoice_number", "mpesa_receipt_number"],
			as_dict=True,
		)

	def fire_kcb(self, payload):
		from onerc_payments.gateways.kcb_buni import _stk

		merchant = _stk(payload).get("MerchantRequestID")
		if isinstance(merchant, str) and merchant:
			self.references.append(merchant)
		frappe.local.request = frappe._dict(data=json.dumps(payload).encode(), headers={})
		frappe.local.form_dict = frappe._dict()
		return kcb.callback()

	# ── Jenga ───────────────────────────────────────────────────────

	def start_card(self, amount=25, currency="USD", **kwargs):
		with self.jenga_http():
			return self.initiate(kwargs.pop("gateway", CARD_ACCOUNT), amount, currency, None, **kwargs)

	@contextmanager
	def jenga_http(self, query=None):
		"""Jenga's sign-in canned, and its status query answering ``query``. Without ``query``
		the query raises - but the driver catches that, so a test checks ``queried.called``."""
		expires = (datetime.now(UTC) + timedelta(minutes=15)).isoformat().replace("+00:00", "Z")
		answer = (lambda *a, **k: FakeResponse(query)) if query is not None else AssertionError("no query")
		with (
			patch(
				"onerc_payments.gateways.jenga.requests.post",
				return_value=FakeResponse({"accessToken": "jenga-token", "expiresIn": expires}),
			) as posted,
			patch("onerc_payments.gateways.jenga.requests.get", side_effect=answer) as queried,
		):
			yield frappe._dict(posted=posted, queried=queried)

	def fire_ipn(self, payload, credentials=(IPN_USER, IPN_PASSWORD)):
		"""Post a notification the way Jenga's server does. Returns (reply, HTTP status)."""
		headers = {"Content-Type": "application/json"}
		if credentials:
			headers["Authorization"] = basic(*credentials)
		frappe.local.request = frappe._dict(data=json.dumps(payload).encode(), headers=headers)
		frappe.local.form_dict = frappe._dict()
		frappe.local.response = frappe._dict()
		with self.jenga_http():
			reply = card.jenga_notification()
		return reply, frappe.local.response.get("http_status_code") or 200

	# ── shared ──────────────────────────────────────────────────────

	def initiate(self, gateway, amount, currency, phone, **kwargs):
		result = pay.initiate_payment(
			amount=amount,
			currency=currency,
			direction=kwargs.pop("direction", "Inbound"),
			source_app="onerc_payments",
			source_doctype=SETTINGS,
			source_document=kwargs.pop("source_document", self.document),
			payer_name="Test Payer",
			payer_phone=phone,
			gateway=gateway,
			**kwargs,
		)
		if result.get("transaction_id") and result["transaction_id"] not in self.created:
			self.created.append(result["transaction_id"])
		if result.get("gateway_reference"):
			self.references.append(result["gateway_reference"])
		return result

	def through_the_web_server(self, method, path, **kwargs):
		"""A request through Frappe's whole web stack (frappe.app.application): routing,
		authentication, method checks, the response. Runs in its own thread, as Frappe's own
		API tests do, so it sees only what is committed."""
		frappe.db.commit()
		headers = {"X-Frappe-Site-Name": frappe.local.site, **(kwargs.pop("headers", None) or {})}
		out = {}

		def run():
			out["response"] = getattr(get_test_client(use_cookies=False), method)(
				path, headers=headers, **kwargs
			)

		thread = Thread(target=run)
		thread.start()
		thread.join()
		return out["response"]

	def fire(self, payload, *args, **kwargs):
		"""Daraja's endpoint, keeping the log it writes: a message naming no payment (as these
		tests send) is linked to nothing the cleanup could find it by."""
		try:
			return super().fire(payload, *args, **kwargs)
		finally:
			log = self.last_log("Payment")
			if log:
				self.fired_logs.append(log.name)

	def doc(self, name):
		return frappe.get_doc(TRANSACTION, name)

	def last_log(self, kind):
		rows = frappe.get_all(
			CALLBACK_LOG, filters={"kind": kind}, fields=["*"], order_by="creation desc", limit=1
		)
		return rows[0] if rows else None

	# ── cleanup ─────────────────────────────────────────────────────

	def _drop_account(self, doctype, name):
		"""Remove an account with everything it gathered (also what a crashed run left)."""
		if frappe.db.exists(doctype, name) or frappe.db.exists(GATEWAY_DOCTYPE, name):
			for txn in frappe.get_all(TRANSACTION, filters={"gateway": name}, pluck="name"):
				for detail_doctype in ("KCB Buni Payment", "Jenga Payment"):
					frappe.db.delete(detail_doctype, {"payment_transaction": txn})
				frappe.db.delete(CALLBACK_LOG, {"payment_transaction": txn})
				frappe.db.delete("Comment", {"reference_doctype": TRANSACTION, "reference_name": txn})
				frappe.db.delete(TRANSACTION, {"name": txn})
			if frappe.db.exists(doctype, name):
				frappe.delete_doc(doctype, name, force=True, ignore_permissions=True)
		frappe.db.delete(CALLBACK_LOG, {"gateway": name})
		if frappe.db.exists(GATEWAY_DOCTYPE, name):
			frappe.delete_doc(GATEWAY_DOCTYPE, name, force=True, ignore_permissions=True)

	def _restore(self):
		frappe.local.response = frappe._dict()
		for _doctype, name in getattr(self, "accounts", []):
			for txn in frappe.get_all(TRANSACTION, filters={"gateway": name}, pluck="name"):
				if txn not in self.created:
					self.created.append(txn)
		super()._restore()
		for doctype, name in getattr(self, "accounts", []):
			self._drop_account(doctype, name)
		if getattr(self, "fired_logs", None):
			frappe.db.delete(CALLBACK_LOG, {"name": ["in", self.fired_logs]})
		frappe.db.delete("Series", {"name": ["like", f"onerc_payments.reference.{TEST_PREFIX}%"]})
		frappe.db.commit()
