# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Backfill the generic gateway-detail link from the old ``mpesa_payment`` link.

The M-Pesa-specific detail link on OneRC Payment Transaction was replaced by a
generic (gateway_detail_doctype, gateway_detail) dynamic link. Existing rows that
pointed at an Mpesa Payment via the old column are repointed at the same record
through the new link so nothing loses its detail record.
"""

import frappe


def execute():
	# The old column lingers as an orphan after the field was removed from the
	# doctype; if it's already gone there is nothing to backfill.
	if "mpesa_payment" not in frappe.db.get_table_columns("OneRC Payment Transaction"):
		return

	rows = frappe.db.sql(
		"""
		SELECT name, mpesa_payment
		FROM `tabOneRC Payment Transaction`
		WHERE mpesa_payment IS NOT NULL AND mpesa_payment != ''
		""",
		as_dict=True,
	)
	for row in rows:
		frappe.db.set_value(
			"OneRC Payment Transaction",
			row.name,
			{"gateway_detail_doctype": "Mpesa Payment", "gateway_detail": row.mpesa_payment},
			update_modified=False,
		)
