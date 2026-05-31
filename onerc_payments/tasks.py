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