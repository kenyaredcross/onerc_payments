# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""``before_request`` hooks. They run on EVERY request to the site, so this module imports
nothing but frappe and its functions never raise.

Jenga posts its card payment notification with HTTP Basic credentials we chose (JengaHQ
portal, Settings > IPN; Jenga requires Basic auth there). Frappe reads any two-part
``Authorization`` header as its own API-key login, and refuses one that is not a Frappe API
key with 401 before the endpoint is reached - so, left alone, every genuine notification
would be refused. On the notification's own address only, the header is set aside before
Frappe looks at it; the endpoint checks it against the Jenga Accounts itself
(``JengaGateway.authenticated_account``). The request then runs as Guest, which is what that
endpoint expects. Every other address keeps Frappe's authentication exactly as it is.
"""

import frappe

JENGA_NOTIFICATION = "onerc_payments.api.v1.card.jenga_notification"
_JENGA_NOTIFICATION_PATHS = frozenset(
	f"/api/{version}method/{JENGA_NOTIFICATION}" for version in ("", "v1/", "v2/")
)


def set_jenga_credentials_aside():
	try:
		request = getattr(frappe.local, "request", None)
		if request is None or (request.path or "").rstrip("/") not in _JENGA_NOTIFICATION_PATHS:
			return
		frappe.local.flags.jenga_authorization = request.environ.pop("HTTP_AUTHORIZATION", None)
	except Exception:
		# Never take the site down for this; at worst the notification gets Frappe's 401.
		pass
