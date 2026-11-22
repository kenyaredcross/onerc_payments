# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from onerc_payments.gateways.accounts import (
	refuse_delete_with_payments,
	refuse_name_clash,
	require_https,
	revalidate_if_active,
	sync_gateway_record,
)
from onerc_payments.gateways.jenga import DRIVER, clear_token_cache


class JengaAccount(Document):
	def validate(self):
		self.account_name = (self.account_name or "").strip()
		refuse_name_clash(self)
		from onerc_payments.gateways.jenga import parse_currencies

		self.merchant_code = (self.merchant_code or "").strip()
		self.currencies = ", ".join(parse_currencies(self.currencies)) or "KES"
		self.country_code = (self.country_code or "KE").strip().upper()
		self.ipn_username = (self.ipn_username or "").strip() or None
		if self.ipn_username and frappe.db.exists(
			"Jenga Account", {"ipn_username": self.ipn_username, "name": ["!=", self.name]}
		):
			frappe.throw("Another Jenga Account uses this notification username. Each needs its own.")
		if (self.callback_base_url or "").strip():
			self.callback_base_url = require_https(
				self.callback_base_url, "Public Site Address", base_only=True
			)
		for field in ("auth_url", "checkout_url"):
			if (self.get(field) or "").strip():
				self.set(field, require_https(self.get(field), self.meta.get_label(field)))
		revalidate_if_active(self)

	def on_update(self):
		gateway = sync_gateway_record(self, DRIVER, "Card")
		values = {"gateway": gateway.name}
		try:
			from onerc_payments.gateways import get_gateway

			values["notification_url"] = get_gateway(self.name, allow_inactive=True).notification_url()
		except frappe.ValidationError:
			frappe.clear_messages()
			values["notification_url"] = None
		self.db_set(values, update_modified=False)
		# New credentials take effect on the very next payment.
		clear_token_cache()

	def on_trash(self):
		refuse_delete_with_payments(self)

	@frappe.whitelist()
	def test_connection(self):
		"""Sign in with these credentials. Nothing is charged and no one is prompted."""
		self.check_permission("write")
		from onerc_payments.gateways import get_gateway

		driver = get_gateway(self.name, allow_inactive=True)
		driver.test_connection()
		return {"message": "Jenga accepted the credentials. Nothing was charged."}
