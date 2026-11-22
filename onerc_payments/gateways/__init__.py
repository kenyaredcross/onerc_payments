# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

import importlib

import frappe


def get_gateway(name=None, allow_inactive=False):
	"""
	Load and return an instance of a gateway driver.

	`name` selects a specific gateway; omitted, the one set as active in
	OneRC Payment Settings is used. The argument exists because a site may
	have several gateways live at once and let the payer choose between them —
	an organisation that takes both M-Pesa and bank transfer offers both, and
	the single `active_gateway` setting can only answer for one of them. It
	stays the default so every existing caller is unaffected.

	A named gateway must actually be active to start a payment. Anything else would
	let a caller revive a gateway an administrator switched off, which is the one
	thing switching it off is supposed to prevent.

	``allow_inactive`` is for a payment that was already started: switching a gateway
	off (a till closed after an appeal) stops new payments, but the money already on
	its way must still be resolved - its callback, its status, its receipt.

	Usage:
	    gateway = get_gateway()
	    result = gateway.initiate(transaction)
	"""
	settings = frappe.get_single("OneRC Payment Settings")
	chosen = (name or "").strip() or settings.active_gateway

	if not chosen:
		frappe.throw("No payment gateway is configured. Go to Payment Settings and select an active gateway.")

	if not frappe.db.exists("OneRC Payment Gateway", chosen):
		frappe.throw(f"Payment Gateway '{chosen}' does not exist.")

	if not allow_inactive and not frappe.db.get_value("OneRC Payment Gateway", chosen, "is_active"):
		frappe.throw(f"Payment Gateway '{chosen}' is not active.")

	gateway_doc = frappe.get_doc("OneRC Payment Gateway", chosen)
	return driver_class_of(gateway_doc)(settings, gateway_doc)


def driver_class_of(gateway_doc):
	"""The driver class a gateway record names."""
	if not gateway_doc.driver_class:
		frappe.throw(f"Payment Gateway '{gateway_doc.name}' has no driver class configured.")

	try:
		module_path, class_name = gateway_doc.driver_class.rsplit(".", 1)
		module = importlib.import_module(module_path)
		return getattr(module, class_name)
	except Exception as e:
		frappe.throw(f"Could not load gateway driver: {e}")


def gateway_rows(*fields):
	"""Every gateway record with ``fields``, ``account`` included when it exists yet: new code
	can run for a few minutes before ``migrate`` adds the column (it then reads as None)."""
	wanted = ["name", *fields]
	has_account = frappe.get_meta("OneRC Payment Gateway").has_field("account")
	rows = frappe.get_all(
		"OneRC Payment Gateway", fields=[*wanted, "account"] if has_account else wanted, order_by="name asc"
	)
	for row in rows:
		row.setdefault("account", None)
	return rows


def callback_kind_of(gateway_name):
	"""Which callback endpoint a gateway's messages arrive at ("Payment" when unsure).

	A transaction written before gateways were recorded on it has no gateway; it was
	collected by the active gateway of the time, which reported at the shared endpoint.
	"""
	if not gateway_name:
		return "Payment"
	driver_class = frappe.db.get_value("OneRC Payment Gateway", gateway_name, "driver_class")
	if not driver_class:
		return "Payment"
	try:
		module_path, class_name = driver_class.rsplit(".", 1)
		return getattr(importlib.import_module(module_path), class_name).callback_kind
	except Exception:
		return "Payment"
