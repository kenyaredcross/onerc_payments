# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""KCB Buni tills: what is sent, what a callback may settle, and what goes to a person."""

import io
import json
from contextlib import redirect_stdout
from unittest.mock import patch

import frappe
import requests

from onerc_payments import sandbox_check
from onerc_payments.api.v1 import kcb
from onerc_payments.api.v1 import payment as pay
from onerc_payments.gateways import get_gateway
from onerc_payments.gateways.base import MANUAL_CHECK
from onerc_payments.tests.account_utils import (
	BASE,
	CALLBACK_LOG,
	CARD_ACCOUNT,
	KCB_ACCOUNT,
	TILL,
	TRANSACTION,
	AccountTestCase,
	kcb_callback,
	new_receipt,
)
from onerc_payments.tests.utils import GATEWAY, PHONE, FakeResponse, stk_callback


class TestKcbAccount(AccountTestCase):
	def test_saving_an_account_makes_its_gateway_switched_off(self):
		self.make_kcb(activate=False)

		gateway = frappe.get_doc("OneRC Payment Gateway", KCB_ACCOUNT)
		self.assertFalse(gateway.is_active)
		self.assertEqual(gateway.driver_class, "onerc_payments.gateways.kcb_buni.KcbBuniGateway")
		self.assertEqual((gateway.account_doctype, gateway.account), ("KCB Buni Account", KCB_ACCOUNT))
		self.assertEqual(
			frappe.db.get_value("KCB Buni Account", KCB_ACCOUNT, "callback_url"), f"{BASE}/api/method/kcb"
		)

	def test_a_switched_off_till_takes_no_new_payment(self):
		self.make_kcb(activate=False)

		with self.assertRaises(frappe.ValidationError):
			self.start_kcb()

	def test_it_cannot_be_switched_on_without_credentials(self):
		self.make_kcb(activate=False, consumer_secret=None)

		with self.assertRaises(frappe.ValidationError):
			self.activate(KCB_ACCOUNT)

	def test_a_callback_address_over_50_characters_is_refused(self):
		account = self.make_kcb()
		account.callback_base_url = "https://payments.nairobi-county-branch.redcross.or.ke"

		with self.assertRaises(frappe.ValidationError) as caught:
			account.save(ignore_permissions=True)
		self.assertIn("50", str(caught.exception))

	def test_a_failed_sign_in_leaves_no_secret_in_a_logged_traceback(self):
		self.make_kcb(activate=False)
		driver = get_gateway(KCB_ACCOUNT, allow_inactive=True)

		with patch(
			"onerc_payments.gateways.kcb_buni.requests.post",
			side_effect=requests.exceptions.ConnectionError("no route to host"),
		):
			try:
				driver.test_connection()
			except frappe.ValidationError:
				text = frappe.get_traceback(with_context=True)
		frappe.clear_messages()

		self.assertIn("no route to host", text)
		self.assertNotIn("test-secret", text)
		self.assertNotIn("test-key", text)

	def test_a_till_too_long_for_the_invoice_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_kcb(activate=False, till_number="12345678")

	def test_an_account_cannot_take_an_existing_gateways_name(self):
		with self.assertRaises(frappe.ValidationError):
			self.make_account(
				"KCB Buni Account",
				{"account_name": "Manual", "till_number": TILL, "environment": "Sandbox"},
				activate=False,
			)

	def test_an_account_with_payments_cannot_be_deleted(self):
		self.make_kcb()
		self.start_kcb()

		with self.assertRaises(frappe.ValidationError):
			frappe.delete_doc("KCB Buni Account", KCB_ACCOUNT, ignore_permissions=True)


class TestKcbPrompt(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_kcb()

	def test_what_kcb_is_sent_fits_its_limits(self):
		result, sent = self.start_kcb(amount=1500.5)

		body = sent[0]["json"]
		txn = self.doc(result["transaction_id"])
		self.assertEqual(result["status"], "Pending")
		self.assertEqual(body["callbackUrl"], f"{BASE}/api/method/kcb")
		self.assertLessEqual(len(body["callbackUrl"]), 50)
		self.assertEqual(body["invoiceNumber"], f"{TILL}-{txn.account_reference}")
		self.assertLessEqual(len(body["invoiceNumber"]), 20)
		self.assertLessEqual(len(body["transactionDescription"]), 13)
		self.assertEqual(body["amount"], "1501")
		self.assertEqual(body["phoneNumber"], PHONE)
		self.assertIs(body["sharedShortCode"], True)
		self.assertEqual(sent[0]["headers"]["Authorization"], "Bearer kcb-token")

	def test_the_request_ids_stay_off_the_transaction(self):
		result, _ = self.start_kcb()

		ids = self.ids(result["transaction_id"])
		self.assertTrue(ids.merchant_request_id)
		txn = self.doc(result["transaction_id"])
		self.assertEqual(txn.gateway_reference, ids.invoice_number)
		self.assertEqual(result["gateway_reference"], ids.invoice_number)
		visible = json.dumps(txn.as_dict(), default=str)
		self.assertNotIn(ids.merchant_request_id, visible)
		self.assertNotIn(ids.checkout_request_id, visible)
		self.assertNotIn(ids.merchant_request_id, json.dumps(result, default=str))

	def test_a_double_click_gets_the_first_prompt_back(self):
		first, sent_first = self.start_kcb()
		second, sent_second = self.start_kcb()

		self.assertEqual(second["transaction_id"], first["transaction_id"])
		self.assertEqual(len(sent_first) + len(sent_second), 1)

	def test_a_checkout_id_of_false_is_accepted(self):
		reply = self.kcb_reply()
		reply["response"]["CheckoutRequestID"] = False
		result, _ = self.start_kcb(reply=reply)

		self.assertEqual(result["status"], "Pending")
		self.assertIsNone(self.ids(result["transaction_id"]).checkout_request_id)

	def test_a_refusal_is_a_plain_failure(self):
		reply = {"header": {"statusCode": "1", "statusDescription": "Invalid till"}, "response": {}}
		result, _ = self.start_kcb(reply=reply, status=400)

		self.assertEqual(result["status"], "Failed")
		self.assertFalse(frappe.db.get_value(TRANSACTION, result["transaction_id"], "initiation_unconfirmed"))

	def test_a_lost_reply_is_marked_unconfirmed(self):
		result, _ = self.start_kcb(error=requests.exceptions.ReadTimeout("read timed out"))

		self.assertEqual(result["status"], "Failed")
		self.assertTrue(frappe.db.get_value(TRANSACTION, result["transaction_id"], "initiation_unconfirmed"))

	def test_a_rejected_token_is_retried_once_with_a_new_one(self):
		calls = []
		reply = self.kcb_reply()

		def post(url, **kw):
			if url.endswith("/token"):
				return FakeResponse({"access_token": f"t{len(calls)}", "expires_in": 3599})
			calls.append(kw["headers"]["Authorization"])
			if len(calls) == 1:
				return FakeResponse({"fault": {"code": "900901", "description": "Invalid Credentials"}}, 401)
			return FakeResponse(reply)

		with patch("onerc_payments.gateways.kcb_buni.requests.post", side_effect=post):
			result = self.initiate(KCB_ACCOUNT, 10, "KES", "0712345678")
		self.references.append(reply["response"]["MerchantRequestID"])

		self.assertEqual(result["status"], "Pending")
		self.assertEqual(calls, ["Bearer t0", "Bearer t1"])

	def test_a_prompt_whose_record_cannot_be_written_is_not_left_pending(self):
		from onerc_payments.gateways.kcb_buni import KcbBuniGateway

		with patch.object(KcbBuniGateway, "record_initiation_details", side_effect=RuntimeError("disk full")):
			result, sent = self.start_kcb()

		self.assertEqual(len(sent), 1)
		self.assertEqual(result["status"], "Failed")
		# The prompt may be on the phone: a callback for it must still reach a person.
		self.assertTrue(frappe.db.get_value(TRANSACTION, result["transaction_id"], "initiation_unconfirmed"))

	def test_only_shillings_are_taken(self):
		with self.assertRaises(frappe.ValidationError):
			self.initiate(KCB_ACCOUNT, 10, "USD", "0712345678")

	def test_the_status_poll_never_calls_kcb(self):
		result, _ = self.start_kcb()

		with (
			patch("onerc_payments.gateways.kcb_buni.requests.post") as post,
			patch("onerc_payments.gateways.kcb_buni.requests.get") as get,
		):
			reply = pay.check_payment_status(result["transaction_id"])
			pay.refresh_status(self.doc(result["transaction_id"]), force=True)

		self.assertEqual(reply["status"], "Pending")
		self.assertFalse(post.called or get.called)


class TestKcbCallback(AccountTestCase):
	def setUp(self):
		super().setUp()
		self.make_kcb()
		self.result, _ = self.start_kcb()
		self.name = self.result["transaction_id"]
		self.request = self.ids(self.name)

	def paid(self, **kwargs):
		values = {"amount": 10, "receipt": new_receipt(), **kwargs}
		merchant = values.pop("merchant", self.request.merchant_request_id)
		checkout = values.pop("checkout", self.request.checkout_request_id)
		return kcb_callback(merchant, checkout, **values)

	def test_a_genuine_callback_completes_the_payment_once(self):
		payload = self.paid()
		receipt = payload["Body"]["stkCallback"]["CallbackMetadata"]["Item"][1]["Value"]

		with patch.object(pay, "_notify_source_app") as notify:
			self.assertEqual(self.fire_kcb(payload), pay.ACCEPTED)
			self.fire_kcb(payload)  # KCB retries

		txn = self.doc(self.name)
		self.assertEqual(txn.status, "Completed")
		self.assertEqual(txn.gateway_receipt, receipt)
		self.assertFalse(txn.needs_review)
		self.assertEqual(notify.call_count, 1)
		log = self.last_log("KCB payment")
		self.assertEqual(log.status, "Processed")
		self.assertEqual(log.gateway, KCB_ACCOUNT)
		self.assertEqual(log.payment_transaction, self.name)
		detail = self.ids(self.name)
		self.assertEqual(detail.mpesa_receipt_number, receipt)

	def test_a_callback_for_a_switched_off_till_still_completes(self):
		self.activate(KCB_ACCOUNT, on=False)

		self.fire_kcb(self.paid())

		self.assertEqual(self.doc(self.name).status, "Completed")

	def test_a_failure_fails_the_payment(self):
		self.fire_kcb(self.paid(result_code=1032))

		self.assertEqual(self.doc(self.name).status, "Failed")

	def test_what_does_not_add_up_goes_to_a_person(self):
		cases = {
			"amount": self.paid(amount=1),
			"phone": self.paid(phone="254700000001"),
			"checkout": self.paid(checkout="ws_CO_SOMETHING_ELSE"),
			"receipt": self.paid(receipt=""),
		}
		for case, payload in cases.items():
			with self.subTest(case), patch.object(pay, "_notify_source_app") as notify:
				self.fire_kcb(payload)

				txn = self.doc(self.name)
				self.assertEqual(txn.status, "Pending")
				self.assertTrue(txn.needs_review)
				self.assertFalse(txn.gateway_receipt)
				self.assertFalse(notify.called)
				log = self.last_log("KCB payment")
				self.assertEqual(log.status, "Needs review")
				self.assertEqual(log.gateway_confirmation, MANUAL_CHECK)

	def test_a_masked_phone_does_not_count_against_a_callback(self):
		self.fire_kcb(self.paid(phone="2547*****678"))

		self.assertEqual(self.doc(self.name).status, "Completed")

	def test_a_masked_phone_must_fit_the_phone_prompted(self):
		self.fire_kcb(self.paid(phone="2547*****999"))

		txn = self.doc(self.name)
		self.assertEqual(txn.status, "Pending")
		self.assertTrue(txn.needs_review)

	def test_many_unmatched_callbacks_do_not_hold_up_a_payment_that_cannot_be_guessed(self):
		# A random-looking request ID and a CheckoutRequestID: junk posted to the callback
		# (anyone can) must not stop it completing.
		self.unmatched.return_value = 25

		self.fire_kcb(self.paid())

		self.assertEqual(self.doc(self.name).status, "Completed")

	def test_an_account_set_to_check_every_payment_never_completes_on_a_callback(self):
		frappe.db.set_value("KCB Buni Account", KCB_ACCOUNT, "complete_on_callback", 0)

		self.fire_kcb(self.paid())

		txn = self.doc(self.name)
		self.assertEqual(txn.status, "Pending")
		self.assertTrue(txn.needs_review)

	def test_staff_can_confirm_it_with_the_m_pesa_code(self):
		frappe.db.set_value("KCB Buni Account", KCB_ACCOUNT, "complete_on_callback", 0)
		self.fire_kcb(self.paid())

		with patch.object(pay, "_notify_source_app") as notify:
			pay.confirm_paid_after_review(self.name, "tkb1a2c3d4", "Checked on the till statement")

		txn = self.doc(self.name)
		self.assertEqual((txn.status, txn.gateway_receipt), ("Completed", "TKB1A2C3D4"))
		self.assertEqual(notify.call_count, 1)
		self.assertEqual(
			frappe.db.get_value("KCB Buni Payment", {"payment_transaction": self.name}, "receipt_source"),
			"Entered by Staff",
		)

	def test_a_late_paid_callback_for_a_failed_payment_goes_to_a_person(self):
		frappe.db.set_value(TRANSACTION, self.name, "status", "Failed")

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire_kcb(self.paid())

		txn = self.doc(self.name)
		self.assertEqual(txn.status, "Failed")
		self.assertTrue(txn.needs_review)
		self.assertFalse(notify.called)

	def test_a_callback_without_the_request_id_matches_nothing(self):
		self.fire_kcb(self.paid(merchant="KCBMR-GUESSED"))

		txn = self.doc(self.name)
		self.assertEqual(txn.status, "Pending")
		self.assertFalse(txn.needs_review)
		self.assertEqual(self.last_log("KCB payment").status, "Unmatched")

	def test_the_public_invoice_number_is_not_enough(self):
		self.fire_kcb(self.paid(merchant=self.request.invoice_number))

		self.assertEqual(self.doc(self.name).status, "Pending")


class TestKcbGuessing(AccountTestCase):
	"""KCB's older request IDs were sequential, and came with CheckoutRequestID false."""

	def setUp(self):
		super().setUp()
		self.make_kcb()
		reply = self.kcb_reply()
		reply["response"]["MerchantRequestID"] = (
			f"17684-{frappe.generate_hash(length=8).translate(str.maketrans('abcdef', '123456'))}-1"
		)
		reply["response"]["CheckoutRequestID"] = False
		self.result, _ = self.start_kcb(reply=reply)
		self.name = self.result["transaction_id"]
		self.merchant = reply["response"]["MerchantRequestID"]

	def test_a_guessable_payment_completes_while_all_is_quiet(self):
		self.fire_kcb(kcb_callback(self.merchant, "ws_CO_ANY", amount=10))

		self.assertEqual(self.doc(self.name).status, "Completed")

	def test_many_unmatched_callbacks_stop_a_guessable_payment_completing_by_itself(self):
		self.unmatched.return_value = 25

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire_kcb(kcb_callback(self.merchant, "ws_CO_ANY", amount=10))

		txn = self.doc(self.name)
		self.assertEqual(txn.status, "Pending")
		self.assertIn("guessing", txn.review_reason)
		self.assertFalse(notify.called)


class TestKcbLostReply(AccountTestCase):
	def test_a_callback_fitting_a_prompt_whose_reply_was_lost_goes_to_a_person(self):
		self.make_kcb()
		result, _ = self.start_kcb(error=requests.exceptions.ReadTimeout("read timed out"))
		name = result["transaction_id"]

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire_kcb(kcb_callback("KCBMR-NEVER-SEEN", "ws_CO_NEVER_SEEN", amount=10))

		txn = self.doc(name)
		self.assertEqual(txn.status, "Failed")
		self.assertTrue(txn.needs_review)
		self.assertEqual(txn.gateway_reference, None)
		self.assertFalse(notify.called)
		self.assertEqual(self.last_log("KCB payment").status, "Unmatched")

	def test_more_such_messages_add_nothing_to_the_review(self):
		self.make_kcb()
		result, _ = self.start_kcb(error=requests.exceptions.ReadTimeout("read timed out"))
		name = result["transaction_id"]

		for i in range(3):
			self.fire_kcb(kcb_callback(f"KCBMR-NEVER-SEEN-{i}", f"ws_CO_NEVER_SEEN_{i}", amount=10))
		frappe.db.set_value(TRANSACTION, name, "needs_review", 0)
		self.fire_kcb(kcb_callback("KCBMR-NEVER-SEEN-9", "ws_CO_NEVER_SEEN_9", amount=10))

		txn = self.doc(name)
		self.assertEqual(txn.review_reason.count("got no reply"), 1)
		self.assertFalse(txn.needs_review)  # closed by a person, and stays closed


class TestOddMessages(AccountTestCase):
	def test_any_shape_of_message_is_kept_and_changes_nothing(self):
		self.make_kcb()
		result, _ = self.start_kcb()
		before = frappe.db.count("Error Log")

		kept = []
		self.addCleanup(self._drop_logs, kept)
		for body in ({"Body": "text"}, {"Body": {"stkCallback": "text"}}, {"Body": ["x"]}, {}):
			with self.subTest(body=body):
				self.fire_kcb(body)
				log = self.last_log("KCB payment")
				kept.append(log.name)
				self.assertIn(log.status, ("Ignored", "Unmatched"))

		self.assertEqual(self.doc(result["transaction_id"]).status, "Pending")
		self.assertEqual(frappe.db.count("Error Log"), before)

	@staticmethod
	def _drop_logs(names):
		# These name no payment, so the usual cleanup (by payment or gateway) cannot find them.
		if names:
			frappe.db.delete(CALLBACK_LOG, {"name": ["in", names]})
			frappe.db.commit()


class TestEndpointsKeepToTheirOwn(AccountTestCase):
	"""Every M-Pesa-shaped message looks alike, so each endpoint settles only its own."""

	def setUp(self):
		super().setUp()
		self.make_kcb()

	def test_the_daraja_endpoint_cannot_settle_a_kcb_payment(self):
		result, _ = self.start_kcb()
		ids = self.ids(result["transaction_id"])

		# Even naming the KCB payment's own (public) reference.
		for reference in (ids.checkout_request_id, ids.invoice_number):
			self.fire(stk_callback(reference, receipt=new_receipt()))
			self.fire({"reference": reference, "status": "Completed"})

		self.assertEqual(self.doc(result["transaction_id"]).status, "Pending")
		log = self.log_for(ids.invoice_number)
		self.assertEqual(log.status, "Unmatched")

	def test_the_kcb_endpoint_cannot_settle_a_daraja_payment(self):
		txn = self.transaction()
		payload = stk_callback(txn.gateway_reference, receipt=new_receipt())

		self.fire_kcb(payload)

		self.assertEqual(self.value(txn.name, "status"), "Pending")
		self.assertEqual(self.last_log("KCB payment").status, "Unmatched")

	def test_a_kcb_callback_never_adopts_a_daraja_payment_whose_reply_was_lost(self):
		txn = self.transaction(checkout="", status="Failed", initiation_unconfirmed=1)

		self.fire_kcb(kcb_callback("KCBMR-X", "ws_CO_X", amount=10))

		self.assertEqual(self.value(txn.name, "status"), "Failed")
		self.assertFalse(self.value(txn.name, "needs_review"))
		self.assertFalse(self.value(txn.name, "gateway_reference"))

	def test_the_daraja_gateway_still_works_beside_a_till(self):
		txn = self.transaction()

		self.fire(stk_callback(txn.gateway_reference, receipt=new_receipt()), gateway_name=GATEWAY)

		self.assertEqual(self.value(txn.name, "status"), "Completed")

	def test_the_short_alias_reaches_the_kcb_endpoint(self):
		self.assertEqual(frappe.override_whitelisted_method("kcb"), "onerc_payments.api.v1.kcb.callback")
		self.assertLessEqual(len(f"{BASE}/api/method/kcb"), 50)
		self.assertTrue(kcb.callback)


class TestKcbDriver(AccountTestCase):
	def test_the_guessing_alarm_reads_the_callback_log(self):
		self.unmatched.stop()
		from onerc_payments.gateways import kcb_buni

		self.assertIsInstance(kcb_buni._recent_unmatched_callbacks(), int)
		self.unmatched.start()

	def test_confirm_says_why_a_person_must_look(self):
		self.make_kcb()
		result, _ = self.start_kcb()
		driver = get_gateway(KCB_ACCOUNT)
		ids = self.ids(result["transaction_id"])

		answer = driver.confirm_callback(
			self.doc(result["transaction_id"]),
			{},
			kcb_callback(ids.merchant_request_id, ids.checkout_request_id, amount=99),
		)

		self.assertEqual(answer, MANUAL_CHECK)
		self.assertIn("not 10", driver.manual_check_reason)


LIVE_ACCOUNT = {
	"environment": "Production",
	"production_token_url": "https://buni.example.org/token",
	"production_stk_url": "https://buni.example.org/mm/api/request/1.0.0/stkpush",
}


class TestLiveCheck(AccountTestCase):
	"""sandbox_check's live steps move real money, so each guard is tested."""

	def quietly(self, step, *args, **kwargs):
		sent = []

		def post(url, **kw):
			if url.endswith("/token"):
				return FakeResponse({"access_token": "kcb-token", "expires_in": 3599})
			sent.append({"url": url, **kw})
			return FakeResponse(self.kcb_reply())

		with (
			patch("onerc_payments.gateways.kcb_buni.requests.post", side_effect=post),
			redirect_stdout(io.StringIO()),
		):
			step(*args, **kwargs)
		return sent

	def live_prompt(self, confirm=sandbox_check.LIVE_CONFIRMATION):
		return self.quietly(sandbox_check.live_kcb_prompt, KCB_ACCOUNT, "0712345678", confirm=confirm)

	def refused(self, step, *args, **kwargs):
		with self.assertRaises(frappe.ValidationError):
			self.quietly(step, *args, **kwargs)
		frappe.clear_messages()

	def test_a_live_prompt_needs_the_confirmation_phrase(self):
		self.make_kcb(**LIVE_ACCOUNT)

		for confirm in (None, "", "yes", sandbox_check.LIVE_CONFIRMATION.lower()):
			with self.subTest(confirm=confirm):
				self.refused(sandbox_check.live_kcb_prompt, KCB_ACCOUNT, "0712345678", confirm=confirm)

		self.assertFalse(frappe.db.count(TRANSACTION, {"gateway": KCB_ACCOUNT}))

	def test_a_live_prompt_asks_for_kes_1_at_the_live_address(self):
		self.make_kcb(**LIVE_ACCOUNT)

		sent = self.live_prompt()

		self.assertEqual(len(sent), 1)
		self.assertEqual(sent[0]["url"], LIVE_ACCOUNT["production_stk_url"])
		self.assertEqual(sent[0]["json"]["amount"], "1")
		txn = frappe.get_all(TRANSACTION, filters={"gateway": KCB_ACCOUNT}, fields=["amount", "currency"])
		self.assertEqual([(t.amount, t.currency) for t in txn], [(1, "KES")])

	def test_at_most_five_live_prompts_an_account_a_day(self):
		self.make_kcb(**LIVE_ACCOUNT)

		for _ in range(5):
			self.live_prompt()
			# Answered (as each will be on the phone), so the next is a new prompt, not a reuse.
			frappe.db.set_value(
				TRANSACTION, {"gateway": KCB_ACCOUNT, "status": "Pending"}, "status", "Cancelled"
			)
		sent = []
		with self.assertRaises(frappe.ValidationError):
			sent = self.live_prompt()
		frappe.clear_messages()

		self.assertEqual(sent, [])
		self.assertEqual(frappe.db.count(TRANSACTION, {"gateway": KCB_ACCOUNT}), 5)

	def test_live_steps_refuse_a_sandbox_account(self):
		self.make_kcb()

		self.refused(sandbox_check.live_sign_in, KCB_ACCOUNT)
		self.refused(
			sandbox_check.live_kcb_prompt, KCB_ACCOUNT, "0712345678", confirm=sandbox_check.LIVE_CONFIRMATION
		)
		self.assertFalse(frappe.db.count(TRANSACTION, {"gateway": KCB_ACCOUNT}))

	def test_sandbox_steps_refuse_a_live_account(self):
		self.make_kcb(**LIVE_ACCOUNT)

		self.refused(sandbox_check.sign_in, KCB_ACCOUNT)
		self.refused(sandbox_check.kcb_prompt, KCB_ACCOUNT, "0712345678")
		self.assertFalse(frappe.db.count(TRANSACTION, {"gateway": KCB_ACCOUNT}))

	def test_live_steps_are_for_kcb_only(self):
		self.make_card(activate=False, environment="Production")

		with (
			patch(
				"onerc_payments.gateways.jenga.requests.post",
				side_effect=requests.exceptions.ConnectionError("no network in tests"),
			) as jenga_called,
			self.assertRaisesRegex(frappe.ValidationError, "for KCB Buni accounts only"),
		):
			self.quietly(sandbox_check.live_sign_in, CARD_ACCOUNT)
		frappe.clear_messages()
		self.assertFalse(jenga_called.called)
