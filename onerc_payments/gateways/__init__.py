# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

import importlib
import frappe


def get_gateway(name=None):
	"""
	Load and return an instance of a gateway driver.

	`name` selects a specific gateway; omitted, the one set as active in
	OneRC Payment Settings is used. The argument exists because a site may
	have several gateways live at once and let the payer choose between them —
	an organisation that takes both M-Pesa and bank transfer offers both, and
	the single `active_gateway` setting can only answer for one of them. It
	stays the default so every existing caller is unaffected.

	A named gateway must actually be active. Anything else would let a caller
	revive a gateway an administrator switched off, which is the one thing
	switching it off is supposed to prevent.

	Usage:
	    gateway = get_gateway()
	    result = gateway.initiate(transaction)
	"""
	settings = frappe.get_single("OneRC Payment Settings")
	chosen = (name or "").strip() or settings.active_gateway

	if not chosen:
		frappe.throw(
			"No payment gateway is configured. "
			"Go to Payment Settings and select an active gateway."
		)

	if not frappe.db.exists("OneRC Payment Gateway", chosen):
		frappe.throw(f"Payment Gateway '{chosen}' does not exist.")

	if not frappe.db.get_value("OneRC Payment Gateway", chosen, "is_active"):
		frappe.throw(f"Payment Gateway '{chosen}' is not active.")

	gateway_doc = frappe.get_doc("OneRC Payment Gateway", chosen)

	if not gateway_doc.driver_class:
		frappe.throw(
			f"Payment Gateway '{chosen}' "
			f"has no driver class configured."
		)

	try:
		module_path, class_name = gateway_doc.driver_class.rsplit(".", 1)
		module = importlib.import_module(module_path)
		driver_class = getattr(module, class_name)
	except Exception as e:
		frappe.throw(f"Could not load gateway driver: {e}")

	return driver_class(settings)