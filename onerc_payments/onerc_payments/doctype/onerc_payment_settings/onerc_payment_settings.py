# Copyright (c) 2026, Kelvin Njenga and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from onerc_payments.references import clean_description, clean_prefix


class OneRCPaymentSettings(Document):
	def validate(self):
		self.default_reference_prefix = clean_prefix(
			self.default_reference_prefix, "Default Reference Prefix"
		)

		seen = set()
		for row in self.get("reference_prefixes") or []:
			row.source_app = (row.source_app or "").strip()
			row.prefix = clean_prefix(row.prefix, f"Row {row.idx}: Prefix")
			if not row.prefix:
				frappe.throw(
					f"Row {row.idx}: give {row.source_app or 'the app'} a prefix, or remove the row."
				)
			if row.source_app in seen:
				frappe.throw(f"{row.source_app} has two rows in Prefix per App. Keep one.")
			seen.add(row.source_app)
			row.description = clean_description(row.description)

	def on_update(self):
		# A saved key, secret, shortcode or environment takes effect on the very next
		# payment, rather than after the cached access token (up to an hour) runs out.
		from onerc_payments.gateways.mpesa_daraja import clear_token_cache

		clear_token_cache()
