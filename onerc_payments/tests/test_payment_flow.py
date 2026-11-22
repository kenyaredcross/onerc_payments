# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Everything around a payment other than the callback itself.

- a payment marked Failed that Safaricom confirms was paid is recovered (once, safely);
- a push whose reply was lost can still be matched to its callback;
- the browser poll and the hourly job ask Safaricom only when it can help;
- a double click does not send a second prompt;
- the account reference and description fit Safaricom's limits;
- M-Pesa refuses other currencies, and never collects less than was asked;
- the access token is cached per credential, dropped on save, and refreshed when refused;
- the review list's actions, and the reconciliation report.

Safaricom is never called: every Daraja request is patched.
"""

import json
from unittest.mock import patch

import frappe
import requests

from onerc_payments import references, tasks
from onerc_payments.api.v1 import payment as pay
from onerc_payments.gateways.base import CONFIRMED, CONTRADICTED, UNAVAILABLE, BaseGateway
from onerc_payments.gateways.mpesa_daraja import (
	TOKEN_CACHE_PREFIX,
	MpesaDarajaGateway,
	charged_amount,
	clear_token_cache,
)
from onerc_payments.onerc_payments.doctype.onerc_payment_settings.onerc_payment_settings import (
	OneRCPaymentSettings,
)
from onerc_payments.tests.utils import (
	GATEWAY,
	PHONE,
	RECEIPT,
	SETTINGS,
	FakeResponse,
	PaymentTestCase,
	stk_callback,
)

TRANSACTION = "OneRC Payment Transaction"
TEST_PREFIX = "ZTEST"


def _ago(**kwargs):
	return frappe.utils.add_to_date(frappe.utils.now_datetime(), **{k: -v for k, v in kwargs.items()})


# ── RECOVERING A PAYMENT MARKED FAILED ─────────────────────────────────────


class TestRecovery(PaymentTestCase):
	"""The payer paid after we called it a failure (slow PIN, a lost reply). The money is
	in the account, so the record must say so - but the app must not activate twice."""

	def test_a_confirmed_late_payment_is_recovered_and_the_app_told_once(self):
		txn = self.transaction(status="Failed", failure_reason="Request cancelled by user")

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertEqual(self.value(txn.name, "gateway_receipt"), RECEIPT)
		self.assertIsNone(self.value(txn.name, "failure_reason"))
		self.assertFalse(self.value(txn.name, "needs_review"))
		notify.assert_called_once()
		self.assertTrue(
			frappe.db.exists(
				"Comment",
				{"reference_name": txn.name, "content": ["like", "%after it was recorded as Failed%"]},
			)
		)

	def test_a_contradicted_late_payment_changes_nothing(self):
		self.confirm.return_value = CONTRADICTED
		txn = self.transaction(status="Failed")

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Failed")
		self.assertIsNone(self.value(txn.name, "gateway_receipt"))
		self.assertTrue(self.value(txn.name, "needs_review"))
		notify.assert_not_called()

	def test_an_unconfirmable_late_payment_waits(self):
		self.confirm.return_value = UNAVAILABLE
		txn = self.transaction(status="Cancelled")

		self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Cancelled")
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Awaiting confirmation")

	def test_with_recovery_switched_off_it_is_flagged_not_changed(self):
		self.settings(recover_failed_payments=0)
		txn = self.transaction(status="Failed")

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Failed")
		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertIn("switched off", self.value(txn.name, "review_reason"))
		notify.assert_not_called()

	def test_a_payer_who_paid_again_is_flagged_and_the_app_not_told_twice(self):
		self.transaction(status="Completed", receipt="TKB0000001")  # the retry that succeeded
		txn = self.transaction(status="Failed")  # the "failed" first attempt, paid after all

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertIn("paid twice", self.value(txn.name, "review_reason"))
		notify.assert_not_called()

	def test_a_payer_who_paid_again_on_a_new_record_is_flagged_but_both_count(self):
		"""Apps like asset disposal start a retry on a new order; the same-record check
		cannot see that, so the same phone, app and amount within a day is flagged."""
		self.transaction(status="Completed", receipt="TKB0000009", source=("User", "Administrator"))
		txn = self.transaction(status="Failed")

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertIn("the same phone also paid", self.value(txn.name, "review_reason"))
		notify.assert_called_once()

	def test_the_desk_requery_recovers_a_failed_payment_safaricom_says_was_paid(self):
		txn = self.transaction(status="Failed")
		detail = frappe.db.get_value("Mpesa Payment", {"payment_transaction": txn.name}, "name")

		with (
			patch.object(MpesaDarajaGateway, "check_status", return_value={"status": "Completed"}),
			patch.object(pay, "_notify_source_app") as notify,
		):
			pay.reconcile_mpesa_payment(detail)

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		notify.assert_called_once()

	def test_the_poll_never_reopens_a_failed_payment_by_itself(self):
		txn = self.transaction(status="Failed")

		with patch.object(MpesaDarajaGateway, "check_status") as query:
			pay.refresh_status(frappe.get_doc(TRANSACTION, txn.name))

		query.assert_not_called()
		self.assertEqual(self.value(txn.name, "status"), "Failed")

	def test_an_app_that_fails_to_record_a_payment_puts_it_on_the_review_list(self):
		txn = self.transaction()

		with patch.object(
			OneRCPaymentSettings,
			"on_payment_confirmed",
			create=True,
			side_effect=RuntimeError("ledger closed"),
		):
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertIn("ledger closed", self.value(txn.name, "review_reason"))


# ── A PUSH WHOSE REPLY WAS LOST ────────────────────────────────────────────


class TestUnansweredStart(PaymentTestCase):
	def _unanswered(self, amount=10, **values):
		return self.transaction(
			checkout="", status="Failed", amount=amount, initiation_unconfirmed=1, **values
		)

	def test_a_callback_for_it_is_matched_by_phone_and_amount(self):
		txn = self._unanswered()

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback("ws_CO_LOST_REPLY_1"))

		self.assertEqual(self.value(txn.name, "gateway_reference"), "ws_CO_LOST_REPLY_1")
		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertEqual(self.value(txn.name, "gateway_receipt"), RECEIPT)
		self.assertEqual(self.log_for("ws_CO_LOST_REPLY_1").payment_transaction, txn.name)
		notify.assert_called_once()

	def test_only_a_confirmed_callback_may_claim_it(self):
		"""Claiming writes the reference for good; an unproven message must not, or the
		real callback would find the payment taken."""
		txn = self._unanswered()
		self.confirm.return_value = CONTRADICTED

		self.fire(stk_callback("ws_CO_LOST_REPLY_5"))

		self.assertIsNone(self.value(txn.name, "gateway_reference"))
		self.assertEqual(self.value(txn.name, "status"), "Failed")
		self.assertEqual(self.log_for("ws_CO_LOST_REPLY_5").status, "Unmatched")

	def test_a_claim_waits_for_safaricom_and_then_completes(self):
		txn = self._unanswered()
		self.confirm.return_value = UNAVAILABLE

		self.fire(stk_callback("ws_CO_LOST_REPLY_6"))
		self.assertIsNone(self.value(txn.name, "gateway_reference"))
		self.assertEqual(self.log_for("ws_CO_LOST_REPLY_6").status, "Awaiting confirmation")

		self.confirm.return_value = CONFIRMED
		tasks.retry_unconfirmed_callbacks()

		self.assertEqual(self.value(txn.name, "gateway_reference"), "ws_CO_LOST_REPLY_6")
		self.assertEqual(self.value(txn.name, "status"), "Completed")

	def test_two_that_fit_are_not_guessed_between(self):
		first, second = self._unanswered(), self._unanswered()

		self.fire(stk_callback("ws_CO_LOST_REPLY_2"))

		self.assertEqual(self.log_for("ws_CO_LOST_REPLY_2").status, "Unmatched")
		for txn in (first, second):
			self.assertEqual(self.value(txn.name, "status"), "Failed")

	def test_a_different_amount_or_an_old_push_does_not_match(self):
		self._unanswered(amount=50)
		old = self._unanswered()
		frappe.db.set_value(TRANSACTION, old.name, "creation", _ago(minutes=30), update_modified=False)

		self.fire(stk_callback("ws_CO_LOST_REPLY_3", amount=10))

		self.assertEqual(self.log_for("ws_CO_LOST_REPLY_3").status, "Unmatched")

	def test_a_masked_phone_does_not_match(self):
		self._unanswered()
		self.fire(stk_callback("ws_CO_LOST_REPLY_4", phone="2547 ***** 678"))
		self.assertEqual(self.log_for("ws_CO_LOST_REPLY_4").status, "Unmatched")


# ── STARTING A PAYMENT ─────────────────────────────────────────────────────


class InitiateTestCase(PaymentTestCase):
	def setUp(self):
		super().setUp()
		self.settings(default_reference_prefix=TEST_PREFIX)
		self.addCleanup(self._drop_test_series)
		self.document = f"TEST-{self._testMethodName}"[:140]
		for target, value in (
			("_get_token", "tok"),
			("_generate_password", "pw"),
			("_callback_url", "https://pay.example.org/callback"),
		):
			patcher = patch.object(MpesaDarajaGateway, target, return_value=value)
			patcher.start()
			self.addCleanup(patcher.stop)

	def _drop_test_series(self):
		frappe.db.delete("Series", {"name": ["like", "onerc_payments.reference.Z%"]})
		frappe.db.commit()

	def start(self, amount=10, currency="KES", phone="0712345678", reply=None, error=None, **kwargs):
		"""initiate_payment with Safaricom's reply (or failure) canned. Returns (result, sent)."""
		reply = reply or {
			"ResponseCode": "0",
			"CheckoutRequestID": f"ws_CO_INIT_{frappe.generate_hash(length=8)}",
			"MerchantRequestID": "MR-1",
			"CustomerMessage": "Success. Request accepted for processing",
		}
		sent = []

		def post(url, json=None, headers=None, timeout=None):
			sent.append(json)
			if error:
				raise error
			return FakeResponse(reply)

		with patch("onerc_payments.gateways.mpesa_daraja.requests.post", side_effect=post):
			result = pay.initiate_payment(
				amount=amount,
				currency=currency,
				direction="Inbound",
				source_app=kwargs.pop("source_app", "onerc_payments"),
				source_doctype="OneRC Payment Settings",
				source_document=kwargs.pop("source_document", self.document),
				payer_name="Test Payer",
				payer_phone=phone,
				gateway=GATEWAY,
				**kwargs,
			)
		if result.get("transaction_id") and result["transaction_id"] not in self.created:
			self.created.append(result["transaction_id"])
		if result.get("gateway_reference"):
			self.references.append(result["gateway_reference"])
		return result, sent


class TestInitiate(InitiateTestCase):
	def test_what_safaricom_is_sent_fits_its_limits(self):
		result, sent = self.start(amount=1500.5)

		payload = sent[0]
		txn = frappe.get_doc(TRANSACTION, result["transaction_id"])
		self.assertEqual(payload["Amount"], 1501)
		self.assertEqual(payload["AccountReference"], txn.account_reference)
		self.assertTrue(txn.account_reference.startswith(TEST_PREFIX))
		self.assertLessEqual(len(payload["AccountReference"]), 12)
		self.assertTrue(payload["AccountReference"].isalnum())
		self.assertLessEqual(len(payload["TransactionDesc"]), 13)
		self.assertEqual(payload["PhoneNumber"], PHONE)
		self.assertEqual(txn.status, "Pending")

	def test_each_payment_gets_its_own_reference(self):
		first, _ = self.start(source_document=f"{self.document}-1")
		second, _ = self.start(source_document=f"{self.document}-2")

		a = self.value(first["transaction_id"], "account_reference")
		b = self.value(second["transaction_id"], "account_reference")
		self.assertNotEqual(a, b)
		self.assertEqual(int(b[len(TEST_PREFIX) :]), int(a[len(TEST_PREFIX) :]) + 1)

	def test_a_double_click_gets_the_first_prompt_back(self):
		first, sent_first = self.start()
		second, sent_second = self.start()

		self.assertEqual(second["transaction_id"], first["transaction_id"])
		self.assertIn("less than two minutes ago", second["message"])
		self.assertEqual(len(sent_first) + len(sent_second), 1)

	def test_a_different_amount_is_a_new_prompt(self):
		first, _ = self.start(amount=10)
		second, sent = self.start(amount=20)

		self.assertNotEqual(second["transaction_id"], first["transaction_id"])
		self.assertEqual(len(sent), 1)

	def test_a_lost_reply_is_marked_so_its_callback_can_still_match(self):
		result, _ = self.start(error=requests.exceptions.ReadTimeout("read timed out"))

		self.assertEqual(result["status"], "Failed")
		self.assertTrue(self.value(result["transaction_id"], "initiation_unconfirmed"))

	def test_a_push_that_never_left_is_a_plain_failure(self):
		result, _ = self.start(error=requests.exceptions.ConnectTimeout("connect timed out"))

		self.assertEqual(result["status"], "Failed")
		self.assertFalse(self.value(result["transaction_id"], "initiation_unconfirmed"))

	def test_another_currency_is_refused_before_anything_is_written(self):
		before = frappe.db.count(TRANSACTION, {"source_document": self.document})

		with self.assertRaises(frappe.ValidationError) as caught:
			self.start(currency="USD")

		self.assertIn("KES", str(caught.exception))
		self.assertEqual(frappe.db.count(TRANSACTION, {"source_document": self.document}), before)

	def test_currency_case_does_not_matter(self):
		result, _ = self.start(currency="kes")
		self.assertEqual(self.value(result["transaction_id"], "currency"), "KES")


class TestCurrencyAndAmount(PaymentTestCase):
	def test_mpesa_never_collects_less_than_was_asked(self):
		for asked, charged in ((10, 10), (1500.5, 1501), (100.00000001, 100), (0.4, 1), ("250", 250)):
			with self.subTest(asked=asked):
				self.assertEqual(charged_amount(asked), charged)

	def test_a_page_can_ask_what_a_gateway_takes(self):
		self.assertEqual(
			pay.get_gateway_capabilities(GATEWAY),
			{"label": "M-Pesa", "currencies": ["KES"], "prompts_payer": True},
		)

	def test_a_gateway_that_names_no_currencies_takes_any(self):
		BaseGateway.check_currency(frappe._dict(supported_currencies=None, label="x"), "USD")


# ── THE POLL ───────────────────────────────────────────────────────────────


class TestPolling(PaymentTestCase):
	def setUp(self):
		super().setUp()
		frappe.local.request = None  # no rate limiting outside a request

	def _forget_throttle(self, txn):
		self.addCleanup(frappe.cache().delete_value, pay._status_query_key(txn))

	def test_a_decided_payment_is_answered_from_our_record(self):
		txn = self.transaction(status="Completed", receipt=RECEIPT)
		with patch.object(MpesaDarajaGateway, "check_status") as query:
			reply = pay.check_payment_status(txn.name)
		query.assert_not_called()
		self.assertEqual(reply["gateway_receipt"], RECEIPT)

	def test_safaricom_is_asked_at_most_once_per_interval(self):
		txn = self.transaction()
		self._forget_throttle(txn)

		with patch.object(MpesaDarajaGateway, "check_status", return_value={"status": "Pending"}) as query:
			pay.check_payment_status(txn.name)  # the first poll always asks
			pay.check_payment_status(txn.name)
			pay.check_payment_status(txn.name)
			self.assertEqual(query.call_count, 1)

			frappe.cache().delete_value(pay._status_query_key(txn))  # the interval has passed
			pay.check_payment_status(txn.name)
			self.assertEqual(query.call_count, 2)

	def test_the_first_poll_reports_a_failure_at_once(self):
		"""Consumers poll straight after starting a payment and show what Safaricom says."""
		txn = self.transaction()
		self._forget_throttle(txn)

		with patch.object(
			MpesaDarajaGateway,
			"check_status",
			return_value={"status": "Failed", "failure_reason": "Timed out"},
		):
			reply = pay.check_payment_status(txn.name)

		self.assertEqual(reply["status"], "Failed")
		self.assertEqual(reply["failure_reason"], "Timed out")

	def test_the_hourly_job_stops_asking_after_a_day(self):
		recent = self.transaction()
		old = self.transaction()
		frappe.db.set_value(TRANSACTION, old.name, "creation", _ago(hours=25), update_modified=False)
		asked = []

		def query(self_, transaction, timeout=30):
			asked.append(transaction.name)
			return {"status": "Pending"}

		with (
			patch.object(MpesaDarajaGateway, "check_status", autospec=True, side_effect=query),
			patch.object(tasks, "flag_stale_pending"),
		):
			tasks.poll_pending_transactions()

		self.assertIn(recent.name, asked)
		self.assertNotIn(old.name, asked)

	def test_a_payment_with_no_answer_after_a_day_goes_on_the_review_list_once(self):
		stale = self.transaction()
		manual = self.transaction(checkout="", gateway="Manual")
		reviewed = self.transaction(reviewed_by="Administrator")
		fresh = self.transaction()
		for txn in (stale, manual, reviewed):
			frappe.db.set_value(TRANSACTION, txn.name, "creation", _ago(hours=25), update_modified=False)

		try:
			tasks.flag_stale_pending()
			tasks.flag_stale_pending()

			self.assertTrue(self.value(stale.name, "needs_review"))
			self.assertEqual(self.value(stale.name, "review_reason").count("No final answer"), 1)
			self.assertFalse(self.value(manual.name, "needs_review"))  # bank/cash wait for a person
			self.assertFalse(self.value(reviewed.name, "needs_review"))  # a person closed it already
			self.assertFalse(self.value(fresh.name, "needs_review"))
		finally:
			# The job flags every stale payment on the site, not only these; keep none of it.
			frappe.db.rollback()


# ── REFERENCES ─────────────────────────────────────────────────────────────


def _settings(rows=(), default=None):
	return frappe._dict(
		reference_prefixes=[frappe._dict(r) for r in rows],
		default_reference_prefix=default,
	)


class TestReferences(PaymentTestCase):
	def setUp(self):
		super().setUp()
		self.addCleanup(self._drop_series)

	def _drop_series(self):
		frappe.db.delete("Series", {"name": ["like", "onerc_payments.reference.Z%"]})
		frappe.db.commit()

	def test_nothing_set_falls_back_to_pay(self):
		self.assertEqual(
			references.reference_settings("some_app", _settings()),
			("PAY", "Payment"),
		)

	def test_the_order_is_settings_row_then_app_hook_then_default(self):
		hooks = {
			"onerc_payments_reference_prefix": "HOOK",
			"onerc_payments_reference_description": "From the app",
		}
		with patch.object(references, "_from_hooks", side_effect=lambda hook, app: hooks.get(hook)):
			self.assertEqual(
				references.reference_settings(
					"prequal",
					_settings([{"source_app": "prequal", "prefix": "preq", "description": "Prequal"}]),
				),
				("PREQ", "Prequal"),
			)
			self.assertEqual(
				references.reference_settings("prequal", _settings(default="KRCS")),
				("HOOK", "From the app"),
			)
		self.assertEqual(references.reference_settings("prequal", _settings(default="krcs"))[0], "KRCS")

	def test_a_bad_prefix_from_an_app_does_not_stop_a_payment(self):
		with patch.object(references, "_from_hooks", return_value="NOT-VALID!"):
			self.assertEqual(references.reference_settings("x", _settings(default="KRCS"))[0], "KRCS")

	def test_an_app_that_is_not_installed_declares_nothing(self):
		self.assertIsNone(references._from_hooks("onerc_payments_reference_prefix", "no_such_app"))

	def test_references_count_up_per_prefix_and_fit_twelve_characters(self):
		settings = _settings([{"source_app": "a", "prefix": "ZQA"}, {"source_app": "b", "prefix": "ZQB"}])
		a1, _ = references.next_reference("a", settings)
		a2, _ = references.next_reference("a", settings)
		b1, _ = references.next_reference("b", settings)

		self.assertEqual(a1, "ZQA000001")
		self.assertEqual(a2, "ZQA000002")
		self.assertEqual(b1, "ZQB000001")

	def test_a_very_long_count_keeps_the_prefix_and_twelve_characters(self):
		with patch.object(references, "getseries", return_value="12345678"):
			reference, _ = references.next_reference("a", _settings(default="ZQCDE"))
		self.assertEqual(reference, "ZQCDE2345678")

	def test_prefix_and_description_are_cleaned_to_safaricoms_rules(self):
		self.assertEqual(references.clean_prefix(" krcs "), "KRCS")
		for bad in ("KR-CS", "TOOLONG", "K RC"):
			with self.subTest(bad=bad), self.assertRaises(frappe.ValidationError):
				references.clean_prefix(bad)
		self.assertEqual(references.clean_description("KRCS — Donation (2026)!"), "KRCS Donation")
		self.assertLessEqual(len(references.clean_description("x" * 40)), 13)

	def test_settings_refuse_two_rows_for_one_app(self):
		settings = frappe.get_single(SETTINGS)
		settings.default_reference_prefix = "krcs"
		settings.set("reference_prefixes", [])
		settings.append("reference_prefixes", {"source_app": "prequal", "prefix": "preq"})
		settings.run_method("validate")
		self.assertEqual(settings.default_reference_prefix, "KRCS")
		self.assertEqual(settings.reference_prefixes[0].prefix, "PREQ")

		settings.append("reference_prefixes", {"source_app": "prequal", "prefix": "pq"})
		with self.assertRaises(frappe.ValidationError):
			settings.run_method("validate")


# ── THE ACCESS TOKEN ───────────────────────────────────────────────────────


class FakeCache:
	def __init__(self):
		self.values, self.lifetimes = {}, {}

	def get_value(self, key, **kwargs):
		return self.values.get(key)

	def set_value(self, key, value, expires_in_sec=None, **kwargs):
		self.values[key] = value
		self.lifetimes[key] = expires_in_sec


class TestAccessToken(PaymentTestCase):
	def gateway(self, environment="Sandbox", shortcode="174379"):
		return MpesaDarajaGateway(frappe._dict(environment=environment, mpesa_shortcode=shortcode))

	def test_the_token_is_cached_for_its_stated_life_less_two_minutes(self):
		cache = FakeCache()
		gateway = self.gateway()
		reply = FakeResponse({"access_token": "T1", "expires_in": "3599"})
		with (
			patch.object(MpesaDarajaGateway, "_credentials", return_value=("ck", "cs")),
			patch("onerc_payments.gateways.mpesa_daraja.requests.get", return_value=reply) as fetch,
			patch.object(frappe, "cache", return_value=cache),
		):
			self.assertEqual(gateway._get_token(), "T1")
			self.assertEqual(gateway._get_token(), "T1")
			self.assertEqual(fetch.call_count, 1)
			gateway._get_token(refresh=True)
			self.assertEqual(fetch.call_count, 2)

		self.assertEqual(list(cache.lifetimes.values()), [3479])

	def test_a_short_lived_token_is_still_kept_a_minute(self):
		cache = FakeCache()
		with (
			patch.object(MpesaDarajaGateway, "_credentials", return_value=("ck", "cs")),
			patch(
				"onerc_payments.gateways.mpesa_daraja.requests.get",
				return_value=FakeResponse({"access_token": "T", "expires_in": 100}),
			),
			patch.object(frappe, "cache", return_value=cache),
		):
			self.gateway()._get_token()
		self.assertEqual(list(cache.lifetimes.values()), [60])

	def test_the_cache_key_changes_with_environment_shortcode_and_key(self):
		keys = {
			self.gateway()._token_cache_key("ck"),
			self.gateway(environment="Production")._token_cache_key("ck"),
			self.gateway(shortcode="600000")._token_cache_key("ck"),
			self.gateway()._token_cache_key("another"),
		}
		self.assertEqual(len(keys), 4)
		for key in keys:
			self.assertTrue(key.startswith(TOKEN_CACHE_PREFIX))
			self.assertNotIn("ck", key.removeprefix(TOKEN_CACHE_PREFIX))

	def test_saving_the_settings_drops_every_cached_token(self):
		key = f"{TOKEN_CACHE_PREFIX}:test"
		frappe.cache().set_value(key, "stale-token")
		self.addCleanup(clear_token_cache)

		frappe.get_single(SETTINGS).save(ignore_permissions=True)

		self.assertIsNone(frappe.cache().get_value(key))

	def _post_with(self, *replies):
		tokens = []

		def token(self_, refresh=False):
			tokens.append(refresh)
			return "fresh" if refresh else "cached"

		with (
			patch.object(MpesaDarajaGateway, "_get_token", autospec=True, side_effect=token),
			patch("onerc_payments.gateways.mpesa_daraja.requests.post", side_effect=list(replies)) as post,
		):
			response = self.gateway()._post("/x", {"a": 1})
		return response, post, tokens

	def test_a_refused_token_is_refreshed_and_the_request_sent_once_more(self):
		for refusal in (
			FakeResponse({"errorCode": "404.001.03", "errorMessage": "Invalid Access Token"}, 404),
			FakeResponse(
				{"errorCode": "401.002.01", "errorMessage": "Error Occurred - Invalid Access Token"}, 400
			),
			FakeResponse({}, 401),
		):
			with self.subTest(refusal=refusal.text):
				response, post, tokens = self._post_with(refusal, FakeResponse({"ResponseCode": "0"}))
				self.assertEqual(response.json(), {"ResponseCode": "0"})
				self.assertEqual(post.call_count, 2)
				self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer fresh")
				self.assertEqual(tokens, [False, True])

	def test_any_other_error_is_not_sent_again(self):
		response, post, _ = self._post_with(
			FakeResponse({"errorCode": "400.002.02", "errorMessage": "Bad Request - Invalid Amount"}, 400)
		)
		self.assertEqual(post.call_count, 1)
		self.assertEqual(response.status_code, 400)


# ── THE REVIEW LIST ────────────────────────────────────────────────────────


class TestReviewActions(PaymentTestCase):
	def test_the_same_reason_is_not_added_twice(self):
		txn = self.transaction()
		pay._flag_review(txn.name, "Check this one")
		pay._flag_review(txn.name, "Check this one")
		self.assertEqual(self.value(txn.name, "review_reason").count("Check this one"), 1)

	def test_closing_a_review_needs_a_note_and_records_who(self):
		txn = self.transaction()
		pay._flag_review(txn.name, "Check this one")

		with self.assertRaises(frappe.ValidationError):
			pay.close_review(txn.name, "  ")

		self.assertEqual(pay.close_review(json.dumps([txn.name]), "On the statement, fine")["closed"], 1)
		self.assertFalse(self.value(txn.name, "needs_review"))
		self.assertEqual(self.value(txn.name, "reviewed_by"), "Administrator")
		self.assertEqual(self.value(txn.name, "review_note"), "On the statement, fine")

	def test_only_someone_who_can_edit_payments_can_close_a_review(self):
		txn = self.transaction()
		pay._flag_review(txn.name, "Check this one")
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			pay.close_review(txn.name, "note")

	def test_a_person_can_confirm_a_failed_payment_with_its_receipt(self):
		txn = self.transaction(status="Failed")
		pay._flag_review(txn.name, "Payer says they paid")

		self.assertTrue(pay.review_options(txn.name)["can_confirm_paid"])
		with patch.object(pay, "_notify_source_app") as notify:
			reply = pay.confirm_paid_after_review(txn.name, " slk4h8n2q6 ", "Statement line 14")

		self.assertEqual(reply, {"status": "Completed", "receipt": RECEIPT})
		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertFalse(self.value(txn.name, "needs_review"))
		notify.assert_called_once()

	def test_confirming_refuses_a_used_receipt_a_paid_document_or_a_paid_payment(self):
		self.transaction(status="Completed", receipt=RECEIPT)
		txn = self.transaction(status="Failed")
		with self.assertRaises(frappe.ValidationError):
			pay.confirm_paid_after_review(txn.name, RECEIPT, "note")  # receipt already used

		with self.assertRaises(frappe.ValidationError):
			pay.confirm_paid_after_review(txn.name, "TKB0000002", "note")  # document already paid

		done = self.transaction(status="Completed", receipt="TKB0000003", source=("User", "Administrator"))
		with self.assertRaises(frappe.ValidationError):
			pay.confirm_paid_after_review(done.name, "TKB0000004", "note")  # already Completed


class TestRefundsAndDeploys(PaymentTestCase):
	def test_a_reviewed_payment_can_be_marked_refunded(self):
		txn = self.transaction(status="Completed", receipt=RECEIPT)
		pay._flag_review(txn.name, "Paid twice")
		self.assertTrue(pay.review_options(txn.name)["can_mark_refunded"])

		with self.assertRaises(frappe.ValidationError):
			pay.mark_refunded(txn.name, "")
		self.assertEqual(pay.mark_refunded(txn.name, "Reversal TKB0000010")["status"], "Refunded")

		self.assertEqual(self.value(txn.name, "status"), "Refunded")
		self.assertFalse(self.value(txn.name, "needs_review"))
		self.assertEqual(self.value(txn.name, "review_note"), "Reversal TKB0000010")

	def test_only_a_completed_payment_can_be_refunded(self):
		txn = self.transaction(status="Failed")
		with self.assertRaises(frappe.ValidationError):
			pay.mark_refunded(txn.name, "note")

	def test_a_setting_not_migrated_yet_reads_as_its_default(self):
		"""New code can run for minutes before migrate adds its fields."""
		self.assertEqual(pay._settings_value("a_field_not_migrated_yet", "fallback"), "fallback")
		self.assertEqual(pay._settings_value("environment"), "Sandbox")

	def test_a_flag_that_cannot_be_written_does_not_stop_the_payment(self):
		txn = self.transaction()
		with patch.object(frappe.db, "set_value", side_effect=RuntimeError("Unknown column")):
			pay._flag_review(txn.name, "x")  # does not raise

	def test_a_locked_reload_sees_the_row_as_committed_now(self):
		txn = self.transaction()
		frappe.db.set_value(TRANSACTION, txn.name, "status", "Completed", update_modified=False)

		pay._reload_locked(txn)

		self.assertEqual(txn.status, "Completed")
		self.assertFalse(txn.flags.for_update)


class TestReconciliationReport(PaymentTestCase):
	def test_the_report_counts_what_needs_looking_at(self):
		from onerc_payments.onerc_payments.report.payment_reconciliation.payment_reconciliation import (
			execute,
		)

		source_app = f"test_app_{frappe.generate_hash(length=6)}"
		self.transaction(status="Completed", source_app=source_app)  # paid, no receipt
		self.transaction(status="Completed", receipt="TKB0000005", source_app=source_app, amount=40)
		failed = self.transaction(status="Failed", source_app=source_app)
		pay._flag_review(failed.name, "x")

		columns, rows = execute({"source_app": source_app})

		self.assertTrue(columns)
		self.assertEqual(len(rows), 1)
		row = rows[0]
		self.assertEqual(row.requests, 3)
		self.assertEqual(row.completed, 2)
		self.assertEqual(row.completed_amount, 50)
		self.assertEqual(row.failed, 1)
		self.assertEqual(row.missing_receipt, 1)
		self.assertEqual(row.needs_review, 1)
