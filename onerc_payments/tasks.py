# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

import time

import frappe
from frappe.utils import add_to_date, now_datetime

# A gateway that resolves payments by itself answers within minutes. One still Pending
# after a day will not be answered by asking again - the hourly job used to re-query
# every such payment, every hour, for ever (152 of them in production by 2026-11). It is
# put on the review list instead, once, and no longer queried.
STOP_POLLING_AFTER_HOURS = 24

# A "paid" callback the gateway could not confirm yet is asked about again this often,
# for this long, before it goes on the review list.
CALLBACK_RETRY_FOR_HOURS = 24

# A callback whose processing crashed is tried this many times in all (the first
# included) before it goes on the review list. Most such crashes are passing ones - a
# lock held by the status poll, a database restart; a bug fails every time.
CALLBACK_MAX_ATTEMPTS = 3

# A callback still "Received" this long after it arrived was never finished - the worker
# was stopped mid-way (a deploy, a timeout) - and is treated like one that crashed.
CALLBACK_STALLED_AFTER_MINUTES = 10

# A callback can arrive before the request that started its payment has saved it - a slow
# reply from Safaricom, a slow app. One that matched nothing is looked for again this long.
UNMATCHED_RETRY_MINUTES = 30

# Each retry may wait up to 10 s on Safaricom. During an outage hundreds can queue up, and
# the job must finish well inside its queue's timeout; whatever is left waits 5 minutes.
RETRY_TIME_BUDGET_SECONDS = 150


def _automatic_gateways():
	"""Active gateways whose driver resolves payments by itself (not by a person)."""
	from onerc_payments.gateways import get_gateway

	names = []
	for name in frappe.get_all("OneRC Payment Gateway", filters={"is_active": 1}, pluck="name"):
		try:
			if get_gateway(name).resolves_automatically:
				names.append(name)
		except Exception:
			continue
	return names


def poll_pending_transactions():
	"""
	Runs every hour. Asks the gateway about each Pending payment from the last day,
	and resolves any that have completed or failed.

	Pending payments older than that, on gateways that resolve by themselves, are
	flagged for review instead. Manual (bank/cash) payments are left alone: they stay
	Pending until a person confirms them, and that can take days.
	"""
	from onerc_payments.api.v1.payment import refresh_status

	cutoff = add_to_date(now_datetime(), hours=-STOP_POLLING_AFTER_HOURS)
	pending = frappe.get_all(
		"OneRC Payment Transaction",
		filters={"status": "Pending", "creation": [">=", cutoff]},
		fields=["name"],
	)
	for row in pending:
		try:
			refresh_status(frappe.get_doc("OneRC Payment Transaction", row.name))
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			frappe.logger().error(f"onerc_payments poll error for {row.name}: {e}")

	try:
		flag_stale_pending(cutoff)
		frappe.db.commit()
	except Exception as e:
		# Say, the job ran on new code before ``migrate`` added the review fields.
		frappe.db.rollback()
		frappe.logger().error(f"onerc_payments: flagging stale payments failed: {e}")


def flag_stale_pending(cutoff=None):
	"""Put payments with no final answer after a day on the review list, once each."""
	from onerc_payments.api.v1.payment import _flag_review

	gateways = _automatic_gateways()
	if not gateways:
		return

	cutoff = cutoff or add_to_date(now_datetime(), hours=-STOP_POLLING_AFTER_HOURS)
	stale = frappe.get_all(
		"OneRC Payment Transaction",
		filters={
			"status": ["in", ["Pending", "Initiated"]],
			"gateway": ["in", gateways],
			"creation": ["<", cutoff],
			"needs_review": 0,
			# Closed once already means a person has looked; do not reopen it every hour.
			"reviewed_by": ["is", "not set"],
		},
		pluck="name",
	)
	for name in stale:
		_flag_review(
			name,
			f"No final answer from the gateway within {STOP_POLLING_AFTER_HOURS} hours, so it is no "
			"longer checked automatically. Look for it on the statement: confirm it as paid if it is "
			"there, otherwise close the review.",
		)
	frappe.db.commit()


def retry_unconfirmed_callbacks():
	"""Runs every 5 minutes. Processes saved callbacks again, most urgent first:

	1. "paid" callbacks the gateway could not confirm yet (Safaricom unreachable, or still
	   processing) - after a day without an answer, they and their payment go on the
	   review list;
	2. callbacks whose processing crashed or never finished;
	3. callbacks that matched no payment, for half an hour.

	Stops when its time budget is spent; the rest wait for the next run.
	"""
	deadline = time.monotonic() + RETRY_TIME_BUDGET_SECONDS
	_retry_awaiting_confirmation(deadline)
	retry_failed_callbacks(deadline)
	retry_unmatched_callbacks(deadline)


def _out_of_time(deadline):
	return deadline is not None and time.monotonic() >= deadline


def _retry_awaiting_confirmation(deadline):
	from onerc_payments.api.v1.payment import CALLBACK_LOG, _flag_review

	cutoff = add_to_date(now_datetime(), hours=-CALLBACK_RETRY_FOR_HOURS)
	waiting = frappe.get_all(
		CALLBACK_LOG,
		filters={"status": "Awaiting confirmation"},
		fields=["name", "received_on", "payment_transaction"],
		order_by="received_on asc",
		limit=100,
	)
	for row in waiting:
		if _out_of_time(deadline):
			return
		if row.received_on and row.received_on < cutoff:
			frappe.db.set_value(
				CALLBACK_LOG,
				row.name,
				{"status": "Needs review", "note": "The gateway never confirmed this callback."},
				update_modified=False,
			)
			if row.payment_transaction:
				_flag_review(
					row.payment_transaction,
					f"A callback said this was paid, but the gateway could not confirm it within "
					f"{CALLBACK_RETRY_FOR_HOURS} hours (callback {row.name}).",
				)
			frappe.db.commit()
			continue
		_reprocess(row.name)


def retry_failed_callbacks(deadline=None):
	"""Callbacks whose processing crashed or never finished: try again, then hand them to
	a person."""
	from onerc_payments.api.v1.payment import CALLBACK_LOG, _flag_review

	fields = ["name", "attempts", "payment_transaction"]
	failed = frappe.get_all(
		CALLBACK_LOG, filters={"status": "Error"}, fields=fields, order_by="received_on asc", limit=100
	)
	failed += frappe.get_all(
		CALLBACK_LOG,
		filters={
			"status": "Received",
			"received_on": ["<", add_to_date(now_datetime(), minutes=-CALLBACK_STALLED_AFTER_MINUTES)],
		},
		fields=fields,
		order_by="received_on asc",
		limit=100,
	)
	for row in failed:
		if _out_of_time(deadline):
			return
		if (row.attempts or 0) < CALLBACK_MAX_ATTEMPTS:
			_reprocess(row.name)
			continue
		frappe.db.set_value(
			CALLBACK_LOG,
			row.name,
			{
				"status": "Needs review",
				"note": f"Processing failed {row.attempts} times; the last error is kept below.",
			},
			update_modified=False,
		)
		if row.payment_transaction:
			_flag_review(
				row.payment_transaction,
				f"A message from the gateway about this payment could not be processed "
				f"(callback {row.name}). Open it to see the error, then use Process again.",
			)
		frappe.db.commit()


def retry_unmatched_callbacks(deadline=None):
	"""Payment callbacks that matched nothing, looked for again for half an hour."""
	from onerc_payments.api.v1.payment import CALLBACK_LOG

	recent = frappe.get_all(
		CALLBACK_LOG,
		filters={
			"status": "Unmatched",
			"kind": "Payment",
			"received_on": [">=", add_to_date(now_datetime(), minutes=-UNMATCHED_RETRY_MINUTES)],
		},
		pluck="name",
		order_by="received_on asc",
		limit=100,
	)
	for name in recent:
		if _out_of_time(deadline):
			return
		_reprocess(name)


def _reprocess(name):
	from onerc_payments.api.v1.payment import reprocess_saved_callback

	try:
		reprocess_saved_callback(name)
	except Exception as e:
		frappe.db.rollback()
		frappe.logger().error(f"onerc_payments: retrying callback {name} failed: {e}")
	finally:
		frappe.clear_messages()


# A lost callback is usually noticed only when somebody opens the receipt, so the
# lookup is asked for here, in the background. Recent enough that Safaricom still
# holds the transaction; old enough that the callback has had its chance; and not
# more than once every few hours per payment, so a lookup that cannot succeed
# (wrong initiator, transaction not found) does not hammer Daraja for a week.
RECEIPT_LOOKUP_GRACE_MINUTES = 15
RECEIPT_LOOKUP_WINDOW_DAYS = 7
RECEIPT_LOOKUP_RETRY_HOURS = 6


def request_missing_receipts():
	"""Runs every hour. Asks the gateway for the receipt of each completed
	payment that is still without one, where the gateway is set up to look it up."""
	from frappe.utils import add_to_date, get_datetime, now_datetime

	from onerc_payments.gateways import get_gateway

	now = now_datetime()
	rows = frappe.get_all(
		"OneRC Payment Transaction",
		filters={
			"status": "Completed",
			"direction": "Inbound",
			"gateway_receipt": ["is", "not set"],
			"modified": ["<", add_to_date(now, minutes=-RECEIPT_LOOKUP_GRACE_MINUTES)],
			"creation": [">", add_to_date(now, days=-RECEIPT_LOOKUP_WINDOW_DAYS)],
		},
		fields=["name", "gateway"],
	)

	drivers = {}
	retry_before = add_to_date(now, hours=-RECEIPT_LOOKUP_RETRY_HOURS)

	for row in rows:
		if row.gateway not in drivers:
			try:
				driver = get_gateway(row.gateway)
				drivers[row.gateway] = driver if driver.can_request_receipt() else None
			except Exception:
				drivers[row.gateway] = None
		driver = drivers[row.gateway]
		if not driver:
			continue

		last = frappe.db.get_value(
			"Mpesa Payment", {"payment_transaction": row.name}, "status_query_requested_on"
		)
		if last and get_datetime(last) > retry_before:
			continue

		try:
			driver.request_receipt(frappe.get_doc("OneRC Payment Transaction", row.name))
			frappe.db.commit()
		except Exception as e:
			frappe.db.rollback()
			frappe.logger().error(f"onerc_payments: receipt lookup for {row.name} failed: {e}")
		finally:
			frappe.clear_messages()
