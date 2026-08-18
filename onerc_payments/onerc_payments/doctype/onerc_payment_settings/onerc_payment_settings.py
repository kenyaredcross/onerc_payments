# Copyright (c) 2026, Kelvin Njenga and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from onerc_payments.gateways.mpesa_daraja import DARAJA_TOKEN_CACHE_KEY


class OneRCPaymentSettings(Document):
	def on_update(self):
		# A cached OAuth token is bound to whichever consumer key/secret minted it;
		# keep serving it across a credential change and every STK call keeps
		# failing against the new app until the old token happens to expire.
		frappe.cache().delete_value(DARAJA_TOKEN_CACHE_KEY)
