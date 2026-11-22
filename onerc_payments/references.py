# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""The account reference a payer sees: on the M-Pesa prompt, in their SMS, on our statement.

Safaricom shows one piece of per-payment text, AccountReference, and allows 12 letters and
digits. We used to send the transaction name (PAY-2026-00042, 14 characters with hyphens),
which Safaricom cuts to 12, so a hundred payments in a row shared "PAY-2026-000" on the
statement. A reference here is a short prefix naming the app plus a running number, e.g.
PREQ000042 - unique, readable and always within the limit.

Where the prefix comes from, first match wins:
1. a row for the app in OneRC Payment Settings > Prefix per App (the site decides),
2. ``onerc_payments_reference_prefix`` in the app's own hooks.py (a new app brings its own),
3. OneRC Payment Settings > Default Reference Prefix (e.g. KRCS),
4. PAY.
The description (TransactionDesc, 13 characters) follows the same order.
"""

import re

import frappe
from frappe.model.naming import getseries

FALLBACK_PREFIX = "PAY"
FALLBACK_DESCRIPTION = "Payment"
PREFIX_MAX = 5
NUMBER_DIGITS = 6
REFERENCE_MAX = 12
DESCRIPTION_MAX = 13

_NOT_ALNUM = re.compile(r"[^A-Z0-9]")
_NOT_DESCRIPTION = re.compile(r"[^A-Za-z0-9 ]")


def clean_prefix(value, label="Prefix"):
	"""Upper-cased letters and digits, at most PREFIX_MAX. Blank stays blank."""
	raw = (value or "").strip().upper()
	if not raw:
		return None
	if _NOT_ALNUM.search(raw) or len(raw) > PREFIX_MAX:
		frappe.throw(
			f"{label} {value!r} must be 1 to {PREFIX_MAX} letters or numbers, with no spaces or "
			"symbols. The payer sees it as part of the account number."
		)
	return raw


def clean_description(value):
	"""Letters, digits and spaces only, cut to DESCRIPTION_MAX. Blank stays blank."""
	text = " ".join(_NOT_DESCRIPTION.sub(" ", value or "").split())
	return text[:DESCRIPTION_MAX].strip() or None


def _from_hooks(hook, source_app):
	"""A value the source app declares in its own hooks.py, if it is an installed app."""
	if not source_app or source_app not in frappe.get_installed_apps():
		return None
	try:
		values = frappe.get_hooks(hook, app_name=source_app)
	except Exception:
		return None
	if isinstance(values, list | tuple):
		values = values[-1] if values else None
	return values or None


def reference_settings(source_app, settings=None):
	"""(prefix, description) for payments requested by ``source_app``."""
	settings = settings or frappe.get_single("OneRC Payment Settings")
	row = next(
		(r for r in (settings.get("reference_prefixes") or []) if (r.source_app or "").strip() == source_app),
		None,
	)

	prefix = None
	for candidate in (
		row.prefix if row else None,
		_from_hooks("onerc_payments_reference_prefix", source_app),
		settings.get("default_reference_prefix"),
	):
		try:
			prefix = clean_prefix(candidate)
		except frappe.ValidationError:
			# A bad prefix declared by an app must not stop a payment; fall through.
			frappe.clear_messages()
			prefix = None
		if prefix:
			break

	description = None
	for candidate in (
		row.description if row else None,
		_from_hooks("onerc_payments_reference_description", source_app),
	):
		description = clean_description(candidate)
		if description:
			break

	return prefix or FALLBACK_PREFIX, description or FALLBACK_DESCRIPTION


def next_reference(source_app, settings=None):
	"""Allocate the next account reference for ``source_app``: (reference, description).

	The running number comes from Frappe's own series table, which locks the row, so two
	payments started at the same moment can never get the same number. Each prefix counts on
	its own; the series key is namespaced so it cannot collide with a doctype's naming series.
	"""
	prefix, description = reference_settings(source_app, settings)
	number = getseries(f"onerc_payments.reference.{prefix}", NUMBER_DIGITS)
	reference = f"{prefix}{number}"
	if len(reference) > REFERENCE_MAX:
		# Only reachable past 9,999,999 payments on a 5-character prefix. Keep the prefix and
		# the lowest digits rather than refuse a payment; payments are matched by the
		# gateway's own IDs, never by this reference.
		reference = prefix + number[-(REFERENCE_MAX - len(prefix)) :]
	return reference, description
