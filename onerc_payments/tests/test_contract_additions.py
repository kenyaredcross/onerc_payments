# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""What this release adds for the apps that take payments through onerc_payments - and that
the callers already live see no change:

- the install stops on a site where another app owns our settings' name in another case;
- check_payment_status no longer counts against the calling endpoint's own rate limit;
- an app may give the account reference the payer sees;
- the status reply says which result code a failed payment ended with.

No provider is called: every request is patched.
"""

import inspect
from unittest.mock import patch

import frappe
from frappe.rate_limiter import rate_limit
from frappe.tests.utils import FrappeTestCase
from frappe.utils import cint

from onerc_payments import install, references
from onerc_payments.api.v1 import payment as pay
from onerc_payments.gateways import get_gateway
from onerc_payments.gateways.mpesa_daraja import MpesaDarajaGateway
from onerc_payments.tests.account_utils import CARD_ACCOUNT, TILL, AccountTestCase, kcb_callback
from onerc_payments.tests.test_payment_flow import TEST_PREFIX, InitiateTestCase
from onerc_payments.tests.utils import GATEWAY, RECEIPT, FakeResponse, PaymentTestCase, stk_callback

TRANSACTION = "OneRC Payment Transaction"


# ── THE INSTALL GUARD ──────────────────────────────────────────────────────

# A name no real doctype has; the guard is asked about it as it is asked about ours.
FAKE = "ZZ OneRC Clash Settings"


class TestInstallGuard(FrappeTestCase):
	"""onerc_fundraising's "ONERC Payment Settings" is our "OneRC Payment Settings" to MariaDB.
	The rows are written raw inside the test and removed again; none are left behind."""

	def setUp(self):
		self.addCleanup(self._remove_fakes)

	def _remove_fakes(self):
		frappe.db.rollback()
		frappe.clear_messages()
		frappe.db.sql("delete from `tabDocType` where lower(name) = lower(%s)", FAKE)
		frappe.db.sql("delete from `tabSingles` where lower(doctype) = lower(%s)", FAKE)
		frappe.db.commit()

	def fake_doctype(self, name, module):
		frappe.db.sql(
			"""insert into `tabDocType` (name, module, issingle, creation, modified, owner, modified_by)
			values (%s, %s, 1, now(), now(), 'Administrator', 'Administrator')""",
			(name, module),
		)

	def fake_singles(self, doctype):
		frappe.db.sql(
			"insert into `tabSingles` (doctype, field, value) values (%s, 'consumer_key', 'not-a-secret')",
			doctype,
		)

	def test_the_hook_is_registered(self):
		self.assertIn(
			"onerc_payments.install.before_install",
			frappe.get_hooks("before_install", app_name="onerc_payments"),
		)

	def test_a_site_with_our_own_settings_passes(self):
		# This site has onerc_payments installed: our doctype, in our module, with values.
		install.before_install()
		install.check_settings_clash(FAKE)  # and a name nobody uses

	def test_another_apps_doctype_differing_only_in_case_stops_the_install(self):
		self.fake_doctype(FAKE.upper(), "OneRC Fundraising")

		with self.assertRaises(frappe.ValidationError) as caught:
			install.check_settings_clash(FAKE)

		message = str(caught.exception)
		self.assertIn(FAKE.upper(), message)
		self.assertIn("OneRC Fundraising", message)
		self.assertIn("Rename the other app", message)

	def test_settings_stored_without_our_doctype_stop_the_install(self):
		self.fake_singles(FAKE.lower())

		with self.assertRaises(frappe.ValidationError) as caught:
			install.check_settings_clash(FAKE)

		self.assertIn(FAKE.lower(), str(caught.exception))

	def test_our_own_doctype_and_values_do_not_stop_a_reinstall(self):
		self.fake_doctype(FAKE, "Onerc Payments")
		self.fake_singles(FAKE)

		install.check_settings_clash(FAKE)

	def test_nothing_is_left_behind(self):
		self.fake_doctype(FAKE, "OneRC Fundraising")
		self.fake_singles(FAKE)
		self._remove_fakes()

		self.assertFalse(frappe.db.sql("select 1 from `tabDocType` where lower(name) = lower(%s)", FAKE))
		self.assertFalse(frappe.db.sql("select 1 from `tabSingles` where lower(doctype) = lower(%s)", FAKE))


# ── THE RATE LIMIT ─────────────────────────────────────────────────────────


class TestPollRateLimit(PaymentTestCase):
	"""Frappe keys a rate limit by the request's method name and IP, not by the function it
	decorates. A Frappe limit on check_payment_status shared the counter of the app's own
	endpoint (same request, same window), so every poll counted twice there. Ours counts under
	a key of its own."""

	IP = "203.0.113.7"
	OTHER_IP = "198.51.100.9"

	def setUp(self):
		super().setUp()
		self.cmd = f"onerc_payments.tests.consumer_poll_{frappe.generate_hash(length=8)}"
		previous_ip = getattr(frappe.local, "request_ip", None)
		# The key rate_limit(..., seconds=60) counts under for this request, and ours per IP.
		self.consumer_key = frappe.cache.make_key(f"rl:{self.cmd}:{self.IP}") + b":60"
		self.poll_keys = [self.poll_key(self.cmd, ip) for ip in (self.IP, self.OTHER_IP)]
		self._forget_counters()
		self.addCleanup(self._leave_request, previous_ip)
		self.enter_request(self.IP)
		self.txn = self.transaction(status="Completed", receipt=RECEIPT)

	@staticmethod
	def poll_key(cmd, ip):
		return frappe.cache.make_key(f"onerc_payments:status_poll:{cmd}:{ip}")

	def enter_request(self, ip, cmd=None):
		frappe.local.request = frappe._dict(method="GET", headers={})
		frappe.local.request_ip = ip
		frappe.local.form_dict = frappe._dict(cmd=cmd or self.cmd)

	def _forget_counters(self):
		frappe.cache.delete(self.consumer_key, *self.poll_keys)

	def _leave_request(self, previous_ip):
		self._forget_counters()
		frappe.clear_messages()
		frappe.local.request = None
		frappe.local.request_ip = previous_ip
		frappe.local.form_dict = frappe._dict()

	def test_a_consumer_endpoint_counts_each_poll_once(self):
		limit = 3

		@rate_limit(limit=limit, seconds=60)
		def consumer_poll(transaction_id):
			return pay.check_payment_status(transaction_id)

		for _ in range(limit):
			self.assertEqual(consumer_poll(self.txn.name)["status"], "Completed")
		self.assertEqual(cint(frappe.cache.get(self.consumer_key)), limit)
		self.assertEqual(cint(frappe.cache.get(self.poll_keys[0])), limit)  # counted apart

		with self.assertRaises(frappe.RateLimitExceededError):
			consumer_poll(self.txn.name)

	def test_one_ip_gets_sixty_a_minute_and_another_ip_its_own(self):
		self.assertFalse(hasattr(pay.check_payment_status, "__wrapped__"))
		for _ in range(pay.STATUS_POLL_LIMIT):
			pay.check_payment_status(self.txn.name)

		with self.assertRaises(frappe.RateLimitExceededError) as caught:
			pay.check_payment_status(self.txn.name)
		self.assertIn("rate limit", str(caught.exception))
		ttl = frappe.cache.ttl(self.poll_keys[0])
		self.assertTrue(0 < ttl <= pay.STATUS_POLL_WINDOW_SECONDS)

		self.enter_request(self.OTHER_IP)
		self.assertEqual(pay.check_payment_status(self.txn.name)["status"], "Completed")

	def test_each_endpoint_keeps_its_own_budget(self):
		# Two endpoints of one app (vmmsx: membership_payment, pay_membership_again) each had
		# their own 60 under Frappe's decorator; one IP using both must not share one 60.
		other_cmd = f"{self.cmd}_again"
		other_key = self.poll_key(other_cmd, self.IP)
		self.addCleanup(frappe.cache.delete, other_key)
		for _ in range(pay.STATUS_POLL_LIMIT):
			pay.check_payment_status(self.txn.name)
		with self.assertRaises(frappe.RateLimitExceededError):
			pay.check_payment_status(self.txn.name)

		self.enter_request(self.IP, cmd=other_cmd)
		self.assertEqual(pay.check_payment_status(self.txn.name)["status"], "Completed")
		self.assertEqual(cint(frappe.cache.get(other_key)), 1)

	def test_outside_a_request_or_without_an_ip_nothing_is_limited(self):
		for request, ip in ((frappe._dict(method="GET"), None), (None, self.IP)):
			with self.subTest(request=bool(request), ip=ip):
				frappe.local.request = request
				frappe.local.request_ip = ip
				for _ in range(pay.STATUS_POLL_LIMIT + 1):
					pay.check_payment_status(self.txn.name)
				self.assertIsNone(frappe.cache.get(self.poll_keys[0]))


# ── THE CALLER'S OWN ACCOUNT REFERENCE ─────────────────────────────────────


class TestCallerReference(InitiateTestCase):
	def series(self):
		rows = frappe.db.sql(
			"select current from `tabSeries` where name = %s", f"onerc_payments.reference.{TEST_PREFIX}"
		)
		return rows[0][0] if rows else None

	def test_existing_positional_callers_are_unaffected(self):
		params = list(inspect.signature(pay.initiate_payment).parameters.values())
		self.assertEqual(
			[p.name for p in params],
			[
				"amount",
				"currency",
				"direction",
				"source_app",
				"source_doctype",
				"source_document",
				"payer_name",
				"payer_phone",
				"payer_email",
				"recipient_name",
				"recipient_phone",
				"recipient_account",
				"metadata",
				"gateway",
				"return_url",
				"account_reference",
			],
		)
		self.assertIsNone(params[-1].default)

	def test_a_given_reference_replaces_the_running_number(self):
		self.start(source_document=f"{self.document}-first")  # the series exists from here on
		before = self.series()

		result, sent = self.start(account_reference="GEN-FUND")

		self.assertEqual(self.value(result["transaction_id"], "account_reference"), "GENFUND")
		self.assertEqual(sent[0]["AccountReference"], "GENFUND")
		self.assertEqual(sent[0]["TransactionDesc"], "Payment")  # the description is unchanged
		self.assertEqual(self.series(), before)  # no number was used up

	def test_it_is_kept_to_ascii_letters_and_digits_and_twelve_characters(self):
		cases = {
			"GEN-FUND": "GENFUND",
			" Gen Fund 2026! ": "GenFund2026",
			"ABCDEFGHIJKLMNOPQ": "ABCDEFGHIJKL",
			"Fünd-١٢": "Fnd",
			12345: "12345",
		}
		for given, expected in cases.items():
			with self.subTest(given=given):
				self.assertEqual(references.clean_account_reference(given), expected)

	def test_safaricom_is_sent_it_in_capitals_within_twelve(self):
		result, sent = self.start(account_reference="Gen Fund 2026 Appeal")

		self.assertEqual(self.value(result["transaction_id"], "account_reference"), "GenFund2026A")
		self.assertEqual(sent[0]["AccountReference"], "GENFUND2026A")

	def test_none_or_nothing_left_falls_back_to_the_running_number(self):
		for i, given in enumerate((None, "", "  ", "---", "€£")):
			with self.subTest(given=given):
				result, sent = self.start(account_reference=given, source_document=f"{self.document}-{i}")
				reference = self.value(result["transaction_id"], "account_reference")
				self.assertTrue(reference.startswith(TEST_PREFIX))
				self.assertEqual(sent[0]["AccountReference"], reference)

	def test_an_outbound_payment_ignores_it(self):
		with patch.object(
			MpesaDarajaGateway,
			"initiate",
			return_value={"success": True, "gateway_reference": f"AG_OUT_{frappe.generate_hash(length=8)}"},
		):
			result = pay.initiate_payment(
				amount=10,
				currency="KES",
				direction="Outbound",
				source_app="onerc_payments",
				source_doctype="OneRC Payment Settings",
				source_document=self.document,
				recipient_name="Test Recipient",
				recipient_phone="0712345678",
				gateway=GATEWAY,
				account_reference="GENFUND",
			)
		self.created.append(result["transaction_id"])
		self.references.append(result["gateway_reference"])

		self.assertFalse(self.value(result["transaction_id"], "account_reference"))


class TestCallerReferenceOnAccounts(AccountTestCase):
	def test_a_kcb_till_shows_till_and_reference_within_twenty(self):
		self.make_kcb()

		result, sent = self.start_kcb(account_reference="ABCDEFGHIJKLMNOP")

		invoice = sent[0]["json"]["invoiceNumber"]
		self.assertEqual(invoice, f"{TILL}-ABCDEFGHIJKL")
		self.assertLessEqual(len(invoice), 20)
		self.assertEqual(result["status"], "Pending")

	def test_a_card_checkout_carries_it(self):
		self.make_card()

		result = self.start_card(account_reference="GEN-FUND")
		with self.jenga_http():
			_action, fields = get_gateway(CARD_ACCOUNT).checkout_form(self.doc(result["transaction_id"]))

		self.assertEqual(fields["secondaryReference"], "GENFUND")
		self.assertTrue(fields["productDescription"].endswith("GENFUND"))
		# The order reference Jenga matches on is still our own random one.
		self.assertEqual(fields["orderReference"], result["gateway_reference"])
		self.assertNotEqual(result["gateway_reference"], "GENFUND")


# ── THE FAILURE CODE IN THE STATUS REPLY ───────────────────────────────────


class TestFailureCode(PaymentTestCase):
	def setUp(self):
		super().setUp()
		frappe.local.request = None

	def detail_code(self, txn, code):
		frappe.db.set_value("Mpesa Payment", {"payment_transaction": txn.name}, "result_code", code)

	def test_the_reply_keeps_every_key_and_value_it_had(self):
		txn = self.transaction(status="Completed", receipt=RECEIPT)

		self.assertEqual(
			pay.check_payment_status(txn.name),
			{
				"transaction_id": txn.name,
				"status": "Completed",
				"gateway_receipt": RECEIPT,
				"failure_reason": None,
				"failure_code": None,
			},
		)

	def test_a_payment_cancelled_on_the_phone_reports_1032(self):
		txn = self.transaction()
		self.fire(stk_callback(txn.gateway_reference, result_code=1032))  # an int, as Daraja posts it

		reply = pay.check_payment_status(txn.name)

		self.assertEqual(reply["status"], "Failed")
		self.assertEqual(reply["failure_reason"], "Request cancelled by user")
		self.assertEqual(reply["failure_code"], "1032")

	def test_a_failure_found_by_the_poll_reports_its_code(self):
		txn = self.transaction()
		self.addCleanup(frappe.cache().delete_value, pay._status_query_key(txn))

		with (
			patch.object(MpesaDarajaGateway, "_generate_password", return_value="pw"),
			patch.object(
				MpesaDarajaGateway,
				"_post",
				return_value=FakeResponse(
					{"ResultCode": "2001", "ResultDesc": "The initiator information is invalid."}
				),
			),
		):
			reply = pay.check_payment_status(txn.name)  # the query answers a string

		self.assertEqual(reply["status"], "Failed")
		self.assertEqual(reply["failure_code"], "2001")

	def test_a_payment_not_failed_costs_no_lookup(self):
		for status in ("Initiated", "Pending", "Completed", "Refunded"):
			with self.subTest(status=status):
				txn = self.transaction(status=status)
				self.detail_code(txn, "1032")
				with patch.object(pay, "get_gateway") as loaded:
					reply = pay._status_reply(txn)
				loaded.assert_not_called()
				self.assertIsNone(reply["failure_code"])

	def test_it_asks_the_gateway_that_collected_the_payment_even_switched_off(self):
		txn = self.transaction(status="Failed")
		self.detail_code(txn, "1037")
		frappe.db.set_value("OneRC Payment Gateway", GATEWAY, "is_active", 0)

		with patch.object(pay, "get_gateway", wraps=get_gateway) as loaded:
			reply = pay.check_payment_status(txn.name)

		loaded.assert_called_once_with(GATEWAY, allow_inactive=True)
		self.assertEqual(reply["failure_code"], "1037")

	def test_no_code_recorded_is_none(self):
		txn = self.transaction(status="Failed")  # e.g. the push itself was refused

		self.assertIsNone(pay.check_payment_status(txn.name)["failure_code"])

	def test_a_success_code_on_a_failed_payment_is_not_a_failure_code(self):
		# A later "paid" message overwrote the record while the payment stayed Failed
		# (recovery switched off): the failure's own code is gone, and "0" is not one.
		txn = self.transaction(status="Failed")
		self.detail_code(txn, "0")

		self.assertIsNone(pay.check_payment_status(txn.name)["failure_code"])

	def test_a_gateway_with_no_codes_reports_none(self):
		txn = self.transaction(checkout="", gateway="Manual", status="Failed")

		self.assertIsNone(pay.check_payment_status(txn.name)["failure_code"])

	def test_a_driver_that_raises_gives_none_and_the_reply_still_comes(self):
		txn = self.transaction(status="Failed", failure_reason="Request cancelled by user")
		self.detail_code(txn, "1032")

		with patch.object(MpesaDarajaGateway, "failure_code", side_effect=RuntimeError("boom")):
			reply = pay.check_payment_status(txn.name)

		self.assertEqual(
			(reply["status"], reply["failure_reason"], reply["failure_code"]),
			("Failed", "Request cancelled by user", None),
		)

	def test_a_gateway_that_cannot_be_loaded_leaves_no_error_for_the_page(self):
		txn = self.transaction(status="Failed")
		frappe.clear_messages()

		with patch.object(pay, "get_gateway", side_effect=lambda *a, **k: frappe.throw("no such gateway")):
			reply = pay.check_payment_status(txn.name)

		self.assertIsNone(reply["failure_code"])
		self.assertEqual(frappe.local.message_log, [])


class TestKcbFailureCode(AccountTestCase):
	def test_a_kcb_failure_reports_its_code(self):
		self.make_kcb()
		result, _ = self.start_kcb()
		ids = self.ids(result["transaction_id"])

		self.fire_kcb(kcb_callback(ids.merchant_request_id, ids.checkout_request_id, result_code=1032))
		reply = pay.check_payment_status(result["transaction_id"])

		self.assertEqual((reply["status"], reply["failure_code"]), ("Failed", "1032"))
