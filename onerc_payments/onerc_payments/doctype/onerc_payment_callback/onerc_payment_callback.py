# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

from frappe.model.document import Document


class OneRCPaymentCallback(Document):
	"""Every message a gateway posts to us, saved exactly as received before it is
	processed, so nothing a gateway tells us is ever lost to a crash or a bug.
	Written only by onerc_payments.api.v1.payment; read-only on the form."""

	pass
