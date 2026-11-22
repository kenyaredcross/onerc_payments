# Copyright (c) 2026, OneRC and contributors
# For license information, please see license.txt

"""Gateways with several accounts: one KCB till per appeal, one card merchant per country.

Each account is a record on its driver's own account doctype (KCB Buni Account, Jenga
Account), holding that account's credentials. Saving it keeps a OneRC Payment Gateway
record of the same name in step, so everything else - routing a payment to it, the
transaction's ``gateway``, switching it on or off, the reconciliation report - works
exactly as for the original gateways. A new account starts switched off; switching it
on runs the driver's own checks (``validate_account``).
"""

import hashlib
from urllib.parse import urlsplit

import frappe

GATEWAY = "OneRC Payment Gateway"
TRANSACTION = "OneRC Payment Transaction"


def require_https(value, label, *, base_only=False):
	"""A public https address with no credentials, query or fragment."""
	url = urlsplit((value or "").strip())
	host = (url.hostname or "").lower()
	if (
		url.scheme != "https"
		or not host
		or url.username
		or url.password
		or url.query
		or url.fragment
		or (base_only and url.path not in ("", "/"))
	):
		frappe.throw(
			f"{label} must be a public https address without a password or query, e.g. https://example.org"
		)
	if host == "localhost" or host.endswith(".localhost") or host.startswith("127.") or host == "0.0.0.0":
		frappe.throw(f"{label} must be reachable from the internet, not {host}.")
	return (value or "").strip().rstrip("/")


def public_base(account, label):
	"""Where the gateway reaches this site: the account's own address, else the site's.

	Never taken from the request's Host header: a request could forge it and have the
	gateway post our payment results to another server.
	"""
	base = (account.get("callback_base_url") or "").strip() or (
		frappe.utils.get_url(allow_header_override=False) or ""
	)
	return require_https(base, f"{label}: Public Site Address", base_only=True)


def sync_gateway_record(account, driver_class, default_label):
	"""Create or update the gateway record this account stands behind. A new one is off."""
	values = {
		"label": (account.get("label") or "").strip() or default_label,
		"driver_class": driver_class,
		"supports_inbound": 1,
		"supports_outbound": 0,
		"account_doctype": account.doctype,
		"account": account.name,
	}
	if frappe.db.exists(GATEWAY, account.name):
		gateway = frappe.get_doc(GATEWAY, account.name)
		gateway.update(values)
		gateway.flags.from_account = True
		gateway.save(ignore_permissions=True)
	else:
		gateway = frappe.get_doc({"doctype": GATEWAY, "gateway_name": account.name, "is_active": 0, **values})
		gateway.flags.from_account = True
		gateway.insert(ignore_permissions=True)
	return gateway


def refuse_name_clash(account):
	"""An account cannot take the name of a gateway that is not its own (e.g. "Manual")."""
	if not account.is_new():
		return
	owner = frappe.db.get_value(GATEWAY, account.name, ["account_doctype", "account"], as_dict=True)
	if owner and (owner.account_doctype, owner.account) != (account.doctype, account.name):
		frappe.throw(f"A payment gateway called {account.name} already exists. Choose another name.")


def refuse_delete_with_payments(account):
	"""An account that has taken payments stays, so its payments keep their gateway."""
	if frappe.db.exists(TRANSACTION, {"gateway": account.name}):
		frappe.throw(
			f"{account.name} has payments recorded against it, so it cannot be deleted. "
			"Switch its gateway off instead."
		)
	if frappe.db.exists(GATEWAY, account.name):
		frappe.delete_doc(GATEWAY, account.name, ignore_permissions=True, force=True)


def revalidate_if_active(account):
	"""Run the driver's checks on a saved account whose gateway is switched on."""
	if account.is_new() or not frappe.db.get_value(GATEWAY, account.name, "is_active"):
		return
	from onerc_payments.gateways import get_gateway

	driver = get_gateway(account.name, allow_inactive=True)
	driver._account = account
	driver.validate_account()


def upsert_detail(doctype, transaction_name, values):
	"""Insert or update a per-gateway detail record keyed by its payment transaction.

	None values are dropped, so a later stage never blanks what an earlier one stored.
	"""
	values = {k: v for k, v in values.items() if v is not None}
	existing = frappe.db.get_value(doctype, {"payment_transaction": transaction_name}, "name")
	if existing:
		doc = frappe.get_doc(doctype, existing)
		doc.update(values)
		doc.save(ignore_permissions=True)
	else:
		doc = frappe.get_doc({"doctype": doctype, "payment_transaction": transaction_name, **values})
		doc.insert(ignore_permissions=True)
	return doc.name


def sign_in_request(url, **kwargs):
	"""POST a provider sign-in. Returns (HTTP status, JSON body, failure) and never raises.

	Frappe logs a traceback with every frame's variables, masking only variables *named*
	like a secret. A failure raised from inside ``requests`` would carry its arguments - the
	consumer secret in ``json``, the key in ``headers``, the ``auth`` pair - into Error Log.
	So a failure comes back as text, and the caller raises its own error outside any
	``except`` block, from a frame that holds nothing worse than masked names.
	"""
	import requests

	status, body, failure = 0, {}, None
	try:
		response = requests.post(url, **kwargs)
		status = getattr(response, "status_code", 0) or 0
		try:
			parsed = response.json()
		except Exception:
			parsed = None
		body = parsed if isinstance(parsed, dict) else {}
	except Exception as e:
		failure = f"{type(e).__name__}: {str(e)[:200]}"
	return status, body, failure


def token_cache_key(prefix, *parts):
	scope = "|".join(str(p or "") for p in parts)
	return f"{prefix}:{hashlib.sha256(scope.encode()).hexdigest()[:16]}"
