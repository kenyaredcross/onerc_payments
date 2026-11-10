# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

import frappe
from onerc_payments.api.v1.payment import check_payment_status


def poll_pending_transactions():
	"""
	Runs every hour. Checks all Pending transactions
	that are older than 5 minutes with the gateway.
	Resolves any that have completed or failed.
	"""
	pending = frappe.get_all(
		"OneRC Payment Transaction",
		filters={"status": "Pending"},
		fields=["name"],
	)
	for row in pending:
		try:
			check_payment_status(row.name)
		except Exception as e:
			frappe.logger().error(f"onerc_payments poll error for {row.name}: {e}")

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

		last = frappe.db.get_value("Mpesa Payment", {"payment_transaction": row.name}, "status_query_requested_on")
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
