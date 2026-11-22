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
from onerc_payments.gateways.kcb_buni import DRIVER, clear_token_cache


class KCBBuniAccount(Document):
	def validate(self):
		self.account_name = (self.account_name or "").strip()
		refuse_name_clash(self)
		self.till_number = (self.till_number or "").strip()
		from onerc_payments.gateways.kcb_buni import TILL_MAX_DIGITS

		if not (
			self.till_number.isascii()
			and self.till_number.isdigit()
			and len(self.till_number) <= TILL_MAX_DIGITS
		):
			frappe.throw(f"The till number must be 1 to {TILL_MAX_DIGITS} digits.")
		if (self.callback_base_url or "").strip():
			self.callback_base_url = require_https(
				self.callback_base_url, "Public Site Address", base_only=True
			)
		for field in ("production_token_url", "production_stk_url"):
			if (self.get(field) or "").strip():
				self.set(field, require_https(self.get(field), self.meta.get_label(field)))
		revalidate_if_active(self)

	def on_update(self):
		gateway = sync_gateway_record(self, DRIVER, "M-Pesa (KCB till)")
		values = {"gateway": gateway.name}
		try:
			from onerc_payments.gateways import get_gateway

			values["callback_url"] = get_gateway(self.name, allow_inactive=True).callback_url()
		except frappe.ValidationError:
			frappe.clear_messages()
			values["callback_url"] = None
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
		return {"message": "KCB accepted the credentials. No payment was started."}
