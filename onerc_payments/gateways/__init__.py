# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

import importlib
import frappe


def get_gateway():
	"""
	Load and return an instance of the active gateway driver.

	Usage:
	    gateway = get_gateway()
	    result = gateway.initiate(transaction)
	"""
	settings = frappe.get_single("OneRC Payment Settings")

	if not settings.active_gateway:
		frappe.throw(
			"No payment gateway is configured. "
			"Go to Payment Settings and select an active gateway."
		)

	gateway_doc = frappe.get_doc("OneRC Payment Gateway", settings.active_gateway)

	if not gateway_doc.driver_class:
		frappe.throw(
			f"Payment Gateway '{settings.active_gateway}' "
			f"has no driver class configured."
		)

	try:
		module_path, class_name = gateway_doc.driver_class.rsplit(".", 1)
		module = importlib.import_module(module_path)
		driver_class = getattr(module, class_name)
	except Exception as e:
		frappe.throw(f"Could not load gateway driver: {e}")

	return driver_class(settings)