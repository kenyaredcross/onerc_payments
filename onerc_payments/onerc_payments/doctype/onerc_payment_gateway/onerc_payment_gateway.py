# Copyright (c) 2026, Kelvin Njenga and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class OneRCPaymentGateway(Document):
	def validate(self):
		# A gateway that is one account of its driver (a KCB till, a card merchant) is
		# switched on only once it is set up in a way that can work: credentials present,
		# a callback address the provider accepts. Switched on with a gap, the first payment
		# would fail in front of a payer - or worse, succeed with nowhere to report.
		if self.is_active and self.account:
			from onerc_payments.gateways import get_gateway

			driver = get_gateway(self.name, allow_inactive=True)
			driver.validate_account()
