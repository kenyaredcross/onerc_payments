# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""A "paid" callback is believed only once Safaricom confirms it, and every callback is
kept exactly as it arrived.

The callback URL is open to the internet and the CheckoutRequestID reaches the payer's
own browser, so anyone could post "paid" for their own payment. The source-IP check that
was meant to stop that trusted headers the sender sets. Now a "paid" callback is checked
by asking Safaricom (the STK query, which only answers for our shortcode); the IP is
recorded for information only.
"""

import json
from unittest.mock import patch

import frappe

from onerc_payments import tasks
from onerc_payments.api.v1 import payment as pay
from onerc_payments.gateways.base import CONFIRMED, CONTRADICTED, UNAVAILABLE
from onerc_payments.gateways.mpesa_daraja import MpesaDarajaGateway
from onerc_payments.tests.utils import REAL_CONFIRM_CALLBACK, RECEIPT, PaymentTestCase, stk_callback


class TestEveryCallbackIsKept(PaymentTestCase):
	def test_a_callback_is_saved_exactly_as_received_and_linked(self):
		txn = self.transaction()
		payload = stk_callback(txn.gateway_reference)

		self.fire(payload, headers={"X-Forwarded-For": "196.201.214.200"})

		log = self.log_for(txn.gateway_reference)
		self.assertEqual(json.loads(log.payload), payload)
		self.assertEqual(log.kind, "Payment")
		self.assertEqual(log.status, "Processed")
		self.assertEqual(log.payment_transaction, txn.name)
		self.assertEqual(log.gateway_confirmation, CONFIRMED)
		self.assertIn("196.201.214.200", log.headers)

	def test_a_callback_for_no_payment_of_ours_is_kept_as_unmatched(self):
		self.fire(stk_callback("ws_CO_NOBODY_KNOWS_THIS"))

		log = self.log_for("ws_CO_NOBODY_KNOWS_THIS")
		self.assertEqual(log.status, "Unmatched")
		self.assertFalse(log.payment_transaction)

	def test_a_message_with_no_reference_is_kept_and_ignored(self):
		frappe.local.request = frappe._dict(data=b'{"hello": "world"}', headers={})
		frappe.form_dict = frappe._dict()
		before = frappe.db.count("OneRC Payment Callback", {"status": "Ignored"})

		self.assertEqual(pay.payment_callback(gateway_name="Mpesa Daraja"), pay.ACCEPTED)

		self.assertEqual(frappe.db.count("OneRC Payment Callback", {"status": "Ignored"}), before + 1)
		frappe.db.delete("OneRC Payment Callback", {"payload": '{"hello": "world"}'})
		frappe.db.commit()

	def test_a_crash_keeps_the_message_and_its_error_and_it_can_be_processed_again(self):
		txn = self.transaction()
		with patch.object(MpesaDarajaGateway, "handle_callback", side_effect=RuntimeError("boom")):
			response = self.fire(stk_callback(txn.gateway_reference))

		# Safaricom is still told Accepted: the message is ours now, there is nothing to retry.
		self.assertEqual(response, pay.ACCEPTED)
		log = self.log_for(txn.gateway_reference)
		self.assertEqual(log.status, "Error")
		self.assertIn("boom", log.error)
		self.assertEqual(self.value(txn.name, "status"), "Pending")

		self.assertEqual(pay.reprocess_callback(log.name), {"status": "Processed"})
		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertEqual(self.value(txn.name, "gateway_receipt"), RECEIPT)

	def test_the_job_retries_a_crashed_callback_and_it_completes(self):
		txn = self.transaction()
		with patch.object(MpesaDarajaGateway, "handle_callback", side_effect=RuntimeError("lock wait")):
			self.fire(stk_callback(txn.gateway_reference))
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Error")

		tasks.retry_unconfirmed_callbacks()

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Processed")
		self.assertEqual(self.log_for(txn.gateway_reference).attempts, 2)

	def test_a_callback_that_keeps_crashing_goes_to_review_after_three_tries(self):
		txn = self.transaction()
		with patch.object(MpesaDarajaGateway, "handle_callback", side_effect=RuntimeError("a bug")):
			self.fire(stk_callback(txn.gateway_reference))
			for _ in range(tasks.CALLBACK_MAX_ATTEMPTS):
				tasks.retry_unconfirmed_callbacks()

		log = self.log_for(txn.gateway_reference)
		self.assertEqual(log.attempts, tasks.CALLBACK_MAX_ATTEMPTS)
		self.assertEqual(log.status, "Needs review")
		self.assertIn("a bug", log.error)
		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertEqual(self.value(txn.name, "status"), "Pending")

	def test_a_callback_left_half_done_is_picked_up(self):
		"""The worker was stopped between saving the message and finishing it."""
		txn = self.transaction()
		with patch.object(pay, "_run_callback"):
			self.fire(stk_callback(txn.gateway_reference))
		log = self.log_for(txn.gateway_reference)
		self.assertEqual(log.status, "Received")

		tasks.retry_unconfirmed_callbacks()
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Received")  # too soon

		frappe.db.set_value(
			"OneRC Payment Callback",
			log.name,
			"received_on",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-11),
		)
		tasks.retry_unconfirmed_callbacks()

		self.assertEqual(self.log_for(txn.gateway_reference).status, "Processed")
		self.assertEqual(self.value(txn.name, "status"), "Completed")

	def test_a_callback_that_beat_its_payment_is_matched_when_looked_for_again(self):
		"""The callback came before the request that started the payment had saved it."""
		self.fire(stk_callback("ws_CO_EARLY_BIRD_1"))
		self.assertEqual(self.log_for("ws_CO_EARLY_BIRD_1").status, "Unmatched")

		txn = self.transaction(checkout="ws_CO_EARLY_BIRD_1")
		tasks.retry_unconfirmed_callbacks()

		self.assertEqual(self.log_for("ws_CO_EARLY_BIRD_1").status, "Processed")
		self.assertEqual(self.value(txn.name, "status"), "Completed")

	def test_an_old_unmatched_callback_is_left_alone(self):
		self.fire(stk_callback("ws_CO_LONG_GONE_1"))
		log = self.log_for("ws_CO_LONG_GONE_1")
		frappe.db.set_value(
			"OneRC Payment Callback",
			log.name,
			"received_on",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), minutes=-31),
		)

		tasks.retry_unconfirmed_callbacks()

		self.assertEqual(self.log_for("ws_CO_LONG_GONE_1").attempts, 1)

	def test_the_retry_job_stops_when_its_time_is_spent(self):
		txn = self.transaction()
		with patch.object(MpesaDarajaGateway, "handle_callback", side_effect=RuntimeError("boom")):
			self.fire(stk_callback(txn.gateway_reference))

		with (
			patch.object(tasks, "RETRY_TIME_BUDGET_SECONDS", 0),
			patch.object(tasks, "_reprocess") as retried,
		):
			tasks.retry_unconfirmed_callbacks()

		retried.assert_not_called()

	def test_only_a_system_manager_can_process_a_callback_again(self):
		txn = self.transaction()
		self.fire(stk_callback(txn.gateway_reference))
		log = self.log_for(txn.gateway_reference)

		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			pay.reprocess_callback(log.name)


class TestConfirmedWithSafaricom(PaymentTestCase):
	def test_a_confirmed_callback_completes_and_tells_the_app_once(self):
		txn = self.transaction()

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertEqual(self.value(txn.name, "gateway_receipt"), RECEIPT)
		self.assertFalse(self.value(txn.name, "needs_review"))
		notify.assert_called_once()

	def test_safaricom_is_asked_about_the_callbacks_own_request(self):
		# The real confirm_callback, with only the STK query under it patched.
		txn = self.transaction()
		gateway = MpesaDarajaGateway(frappe._dict(environment="Sandbox", mpesa_shortcode="174379"))
		payload = stk_callback(txn.gateway_reference)

		for answer, expected in (
			({"status": "Completed"}, CONFIRMED),
			({"status": "Failed", "result_code": "1032"}, CONTRADICTED),
			({"status": "Pending"}, UNAVAILABLE),
			({"status": "Pending", "unreachable": True}, UNAVAILABLE),
		):
			with self.subTest(answer=answer):
				with patch.object(MpesaDarajaGateway, "check_status", return_value=answer) as query:
					self.assertEqual(REAL_CONFIRM_CALLBACK(gateway, txn, {}, payload), expected)
				probe = query.call_args.args[0]
				self.assertEqual(probe.gateway_reference, txn.gateway_reference)
				self.assertLessEqual(query.call_args.kwargs["timeout"], 10)

		with patch.object(MpesaDarajaGateway, "check_status", side_effect=Exception("no network")):
			self.assertEqual(REAL_CONFIRM_CALLBACK(gateway, txn, {}, payload), UNAVAILABLE)

	def test_a_confirmed_callback_counts_whatever_its_address(self):
		"""Production, and a callback from an address that is not Safaricom's: it used to
		be refused with a 403 (and its receipt lost). Safaricom confirms it, so it counts;
		the address is recorded."""
		self.settings(environment="Production")
		txn = self.transaction()
		frappe.local.request_ip = "203.0.113.9"

		response = self.fire(stk_callback(txn.gateway_reference), headers={"X-Forwarded-For": "203.0.113.9"})

		self.assertEqual(response, pay.ACCEPTED)
		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertFalse(self.log_for(txn.gateway_reference).source_trusted)
		frappe.local.request_ip = None

	def test_a_faked_safaricom_header_passes_the_ip_check_but_not_confirmation(self):
		"""The old gate: CF-Connecting-IP is set by whoever sends the request."""
		self.settings(environment="Production", callback_confirmation="Enforce")
		self.confirm.return_value = CONTRADICTED
		txn = self.transaction()
		frappe.local.request_ip = "203.0.113.9"

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference), headers={"CF-Connecting-IP": "196.201.214.200"})

		self.assertTrue(self.log_for(txn.gateway_reference).source_trusted)  # fooled, as before
		self.assertEqual(self.value(txn.name, "status"), "Pending")  # but nothing came of it
		notify.assert_not_called()
		frappe.local.request_ip = None


class TestEnforce(PaymentTestCase):
	def setUp(self):
		super().setUp()
		self.settings(callback_confirmation="Enforce")

	def test_a_paid_callback_safaricom_contradicts_completes_nothing(self):
		self.confirm.return_value = CONTRADICTED
		txn = self.transaction()

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Pending")
		self.assertIsNone(self.value(txn.name, "gateway_receipt"))
		self.assertTrue(self.value(txn.name, "needs_review"))
		notify.assert_not_called()
		# The unproven message did not write its receipt onto the detail record either.
		self.assertFalse(
			frappe.db.get_value("Mpesa Payment", {"payment_transaction": txn.name}, "mpesa_receipt_number")
		)
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Needs review")

	def test_safaricom_unreachable_waits_then_the_retry_completes_it(self):
		self.confirm.return_value = UNAVAILABLE
		txn = self.transaction()

		self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Pending")
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Awaiting confirmation")

		self.confirm.return_value = CONFIRMED
		with patch.object(pay, "_notify_source_app") as notify:
			tasks.retry_unconfirmed_callbacks()

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertEqual(self.value(txn.name, "gateway_receipt"), RECEIPT)
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Processed")
		notify.assert_called_once()

	def test_a_callback_safaricom_never_confirms_goes_to_review_after_a_day(self):
		self.confirm.return_value = UNAVAILABLE
		txn = self.transaction()
		self.fire(stk_callback(txn.gateway_reference))
		log = self.log_for(txn.gateway_reference)
		frappe.db.set_value(
			"OneRC Payment Callback",
			log.name,
			"received_on",
			frappe.utils.add_to_date(frappe.utils.now_datetime(), hours=-25),
		)

		tasks.retry_unconfirmed_callbacks()

		self.assertEqual(self.log_for(txn.gateway_reference).status, "Needs review")
		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertEqual(self.value(txn.name, "status"), "Pending")


class TestWatchOnly(PaymentTestCase):
	def test_watch_only_completes_as_before_and_flags_what_safaricom_did_not_confirm(self):
		self.confirm.return_value = CONTRADICTED
		txn = self.transaction()

		with patch.object(pay, "_notify_source_app") as notify:
			self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertIn("Watch only", self.value(txn.name, "review_reason"))
		notify.assert_called_once()

	def test_blank_setting_means_watch_only(self):
		self.settings(callback_confirmation=None)
		self.confirm.return_value = UNAVAILABLE
		txn = self.transaction()

		self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Completed")


class TestWatchOnlyKeepsTheOldGate(PaymentTestCase):
	"""In production, Watch only completes an unconfirmed "paid" only from Safaricom's own
	addresses - exactly what the old IP check allowed. So it is never weaker than before."""

	SAFARICOM = "196.201.214.200"
	ELSEWHERE = "203.0.113.9"

	def setUp(self):
		super().setUp()
		self.settings(environment="Production")
		self.addCleanup(setattr, frappe.local, "request_ip", None)

	def _from(self, ip, txn, payload=None):
		frappe.local.request_ip = ip
		return self.fire(payload or stk_callback(txn.gateway_reference))

	def test_from_elsewhere_and_contradicted_nothing_is_paid(self):
		self.confirm.return_value = CONTRADICTED
		txn = self.transaction()

		with patch.object(pay, "_notify_source_app") as notify:
			self._from(self.ELSEWHERE, txn)

		self.assertEqual(self.value(txn.name, "status"), "Pending")
		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Needs review")
		notify.assert_not_called()

	def test_from_elsewhere_and_unconfirmable_it_waits(self):
		self.confirm.return_value = UNAVAILABLE
		txn = self.transaction()

		self._from(self.ELSEWHERE, txn)

		self.assertEqual(self.value(txn.name, "status"), "Pending")
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Awaiting confirmation")

	def test_from_safaricom_and_unconfirmable_it_completes_as_before(self):
		self.confirm.return_value = UNAVAILABLE
		txn = self.transaction()

		self._from(self.SAFARICOM, txn)

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertTrue(self.value(txn.name, "needs_review"))

	def test_a_retry_judges_the_sender_as_it_was_on_arrival(self):
		"""The retry job has no request, so it must not judge the sender by its own (absent)
		address - nor forget that a message did come from Safaricom."""
		self.confirm.return_value = UNAVAILABLE
		from_elsewhere = self.transaction()
		from_safaricom = self.transaction()
		self._from(self.ELSEWHERE, from_elsewhere)
		with patch.object(MpesaDarajaGateway, "handle_callback", side_effect=RuntimeError("lock wait")):
			self._from(self.SAFARICOM, from_safaricom)
		self.assertEqual(self.log_for(from_safaricom.gateway_reference).status, "Error")

		frappe.local.request_ip = None
		frappe.local.request = None
		tasks.retry_unconfirmed_callbacks()

		self.assertEqual(self.value(from_elsewhere.name, "status"), "Pending")
		self.assertEqual(self.log_for(from_elsewhere.gateway_reference).status, "Awaiting confirmation")
		self.assertEqual(self.value(from_safaricom.name, "status"), "Completed")


class TestWhatACallbackCannotDo(PaymentTestCase):
	def test_a_failure_for_a_payment_already_paid_is_flagged_not_applied(self):
		txn = self.transaction(status="Completed", receipt=RECEIPT)

		self.fire(stk_callback(txn.gateway_reference, result_code=1032))

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertTrue(self.value(txn.name, "needs_review"))

	def test_a_different_amount_is_flagged(self):
		txn = self.transaction(amount=10)

		self.fire(stk_callback(txn.gateway_reference, amount=1))

		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertIn("asked for 10", self.value(txn.name, "review_reason"))

	def test_a_receipt_already_on_another_payment_is_not_stored_twice(self):
		self.transaction(status="Completed", receipt=RECEIPT)
		txn = self.transaction(status="Completed")

		self.fire(stk_callback(txn.gateway_reference))

		self.assertIsNone(self.value(txn.name, "gateway_receipt"))
		self.assertTrue(self.value(txn.name, "needs_review"))

	def test_a_message_that_is_not_an_stk_callback_fails_nothing(self):
		"""Anyone holding a payment's reference could post {"reference": ...} and fail it."""
		txn = self.transaction()

		self.fire({"reference": txn.gateway_reference}, gateway_name="Other")

		self.assertEqual(self.value(txn.name, "status"), "Pending")
		self.assertFalse(self.value(txn.name, "needs_review"))
		self.assertEqual(self.log_for(txn.gateway_reference).status, "Ignored")

	def test_a_callback_to_a_manual_payment_changes_and_flags_nothing(self):
		if not frappe.db.exists("OneRC Payment Gateway", "Manual"):
			self.skipTest("no Manual gateway on this site")
		was_active = frappe.db.get_value("OneRC Payment Gateway", "Manual", "is_active")
		frappe.db.set_value("OneRC Payment Gateway", "Manual", "is_active", 1)
		self.addCleanup(frappe.db.set_value, "OneRC Payment Gateway", "Manual", "is_active", was_active)
		paid = self.transaction(checkout="", gateway="Manual", status="Completed", receipt="BANK-1")
		paid.db_set("gateway_reference", paid.name, update_modified=False)
		self.references.append(paid.name)

		self.fire({"reference": paid.name}, gateway_name="Manual")

		self.assertEqual(self.value(paid.name, "status"), "Completed")
		self.assertFalse(self.value(paid.name, "needs_review"))
		self.assertEqual(self.log_for(paid.name).status, "Ignored")

	def test_an_oversized_message_is_kept_only_in_part(self):
		frappe.local.request = frappe._dict(data=json.dumps({"junk": "x" * 200_000}).encode(), headers={})
		frappe.form_dict = frappe._dict()

		pay.payment_callback(gateway_name="Mpesa Daraja")

		log = frappe.get_all(
			"OneRC Payment Callback", fields=["name", "payload"], order_by="creation desc", limit=1
		)[0]
		self.addCleanup(frappe.db.delete, "OneRC Payment Callback", {"name": log.name})
		self.assertLess(len(log.payload), pay.MAX_LOGGED_PAYLOAD + 100)
		self.assertIn("characters received", log.payload)

	def test_a_paid_callback_for_a_document_already_paid_is_flagged(self):
		source = ("OneRC Payment Settings", "OneRC Payment Settings")
		self.transaction(status="Completed", receipt="TKB0000001", source=source)
		txn = self.transaction(source=source)

		self.fire(stk_callback(txn.gateway_reference))

		self.assertEqual(self.value(txn.name, "status"), "Completed")
		self.assertTrue(self.value(txn.name, "needs_review"))
		self.assertIn("also paid by", self.value(txn.name, "review_reason"))
