# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Where KCB Buni posts the result of an M-Pesa prompt sent to one of our tills.

KCB allows a callbackUrl of at most 50 characters, so this is reached through the alias
``/api/method/kcb`` (hooks.py: override_whitelisted_methods). It carries no account in
its address: the callback is matched by the MerchantRequestID KCB gave the prompt, and
the payment it belongs to knows its till. See ``gateways/kcb_buni.py`` for what makes a
KCB callback count.

Like every callback, the message is saved and committed before it is processed, and KCB
is always told it was accepted.
"""

import frappe

from onerc_payments.api.v1 import payment as pay

KIND = "KCB payment"


@frappe.whitelist(allow_guest=True, methods=["POST"])
def callback(**kwargs):
	raw, data = pay._read_body()
	log = pay._open_log(KIND, raw, None, pay._reference_or_none(None, data, KIND), False)
	pay._run_callback(KIND, data, None, log)
	return pay.ACCEPTED
