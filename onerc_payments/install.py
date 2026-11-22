# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Refuse to install on a site where another app already owns our settings' name.

onerc_fundraising has a single doctype "ONERC Payment Settings". MariaDB compares names
case-insensitively, so to the database it IS "OneRC Payment Settings": installing this app
there would take that doctype over and wipe the other app's settings and credentials.
"""

import frappe

SETTINGS_DOCTYPE = "OneRC Payment Settings"
MODULE = "Onerc Payments"


def before_install():
	check_settings_clash(SETTINGS_DOCTYPE)


def check_settings_clash(doctype=SETTINGS_DOCTYPE):
	"""Throw if ``doctype`` (compared without regard to case) belongs to another app, or has
	single values stored with no doctype of ours behind them.

	Throws rather than returning False: Frappe stops an install on False without a word.
	Compared with LOWER() on both sides, so the answer does not depend on the column's
	collation (MariaDB's is case-insensitive, Postgres's is not).
	"""
	others = frappe.db.sql(
		"""select name, module from `tabDocType`
		where lower(name) = lower(%(doctype)s) and lower(coalesce(module, '')) != lower(%(module)s)""",
		{"doctype": doctype, "module": MODULE},
		as_dict=True,
	)
	if others:
		other = others[0]
		frappe.throw(
			f"This site already has a doctype named {other.name!r} (module {other.module or 'unknown'}). "
			f"The database treats that name and {doctype!r} as the same, so installing onerc_payments "
			"would take it over and wipe that app's settings and credentials. Rename the other app's "
			"settings doctype first, then install onerc_payments.",
			title="Payment settings name already taken",
		)

	ours = frappe.db.sql(
		"""select count(*) from `tabDocType`
		where lower(name) = lower(%(doctype)s) and lower(module) = lower(%(module)s)""",
		{"doctype": doctype, "module": MODULE},
	)[0][0]
	if ours:
		return

	stored = frappe.db.sql(
		"select distinct doctype from `tabSingles` where lower(doctype) = lower(%(doctype)s)",
		{"doctype": doctype},
	)
	if stored:
		frappe.throw(
			f"This site already has settings stored under {stored[0][0]!r}, which the database treats "
			f"as the same name as {doctype!r}. Installing onerc_payments would overwrite them. Rename "
			"the other app's settings doctype (and move its values) first, then install onerc_payments.",
			title="Payment settings name already taken",
		)
