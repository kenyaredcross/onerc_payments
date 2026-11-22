# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Give the two new payment-check settings their defaults on sites that already exist.

A Single doctype's field default only applies when the document is first created, so
on an existing site the new fields would read as blank: confirmation would behave as
"Watch only" (fine) but recovering payments marked Failed would be off. Only fills a
value that has never been set, so a choice someone already made is kept.
"""

import frappe

SETTINGS = "OneRC Payment Settings"
DEFAULTS = {
	"callback_confirmation": "Watch only",
	"recover_failed_payments": 1,
}


def execute():
	for field, value in DEFAULTS.items():
		already = frappe.db.sql(
			"select 1 from `tabSingles` where doctype = %s and field = %s",
			(SETTINGS, field),
		)
		if not already:
			frappe.db.set_single_value(SETTINGS, field, value)
