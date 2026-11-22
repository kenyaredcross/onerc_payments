# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Payments in, per day, app and gateway: what came in, what failed, what still needs
someone. The numbers to hold against the M-Pesa statement for the same days, and the
places to look first - payments with no receipt, stuck in Pending, or on the review list.
"""

import frappe
from frappe.utils import add_days, getdate, today


def execute(filters=None):
	filters = frappe._dict(filters or {})
	from_date = getdate(filters.from_date or add_days(today(), -30))
	to_date = getdate(filters.to_date or today())

	conditions = ["direction = 'Inbound'", "creation >= %(from_date)s", "creation < %(to_next)s"]
	values = {"from_date": from_date, "to_next": add_days(to_date, 1)}
	if filters.gateway:
		conditions.append("gateway = %(gateway)s")
		values["gateway"] = filters.gateway
	if filters.source_app:
		conditions.append("source_app = %(source_app)s")
		values["source_app"] = filters.source_app

	rows = frappe.db.sql(
		f"""
		select
			date(creation) as day,
			source_app,
			gateway,
			count(*) as requests,
			sum(status = 'Completed') as completed,
			sum(case when status = 'Completed' then amount else 0 end) as completed_amount,
			sum(status in ('Failed', 'Cancelled')) as failed,
			sum(status in ('Pending', 'Initiated')) as pending,
			sum(status = 'Completed' and ifnull(gateway_receipt, '') = '') as missing_receipt,
			sum(needs_review = 1) as needs_review
		from `tabOneRC Payment Transaction`
		where {" and ".join(conditions)}
		group by date(creation), source_app, gateway
		order by day desc, source_app, gateway
		""",
		values,
		as_dict=True,
	)
	return _columns(), rows


def _columns():
	return [
		{"fieldname": "day", "label": "Date", "fieldtype": "Date", "width": 110},
		{"fieldname": "source_app", "label": "App", "fieldtype": "Data", "width": 190},
		{
			"fieldname": "gateway",
			"label": "Gateway",
			"fieldtype": "Link",
			"options": "OneRC Payment Gateway",
			"width": 130,
		},
		{"fieldname": "requests", "label": "Requests", "fieldtype": "Int", "width": 90},
		{"fieldname": "completed", "label": "Paid", "fieldtype": "Int", "width": 80},
		{"fieldname": "completed_amount", "label": "Amount Paid", "fieldtype": "Currency", "width": 130},
		{"fieldname": "failed", "label": "Failed", "fieldtype": "Int", "width": 80},
		{"fieldname": "pending", "label": "Still Pending", "fieldtype": "Int", "width": 110},
		{"fieldname": "missing_receipt", "label": "Paid, No Receipt", "fieldtype": "Int", "width": 130},
		{"fieldname": "needs_review", "label": "Needs Review", "fieldtype": "Int", "width": 110},
	]
